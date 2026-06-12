"""World builder: fetch → +ak version → sbuild unshare → audit → record.

Productizes the W0 spike (scripts/world-spike.sh) with the learnings
baked in:

* preflight accepts Ubuntu's userns restriction when the sbuild/
  mmdebstrap AppArmor profiles grant ``userns,``;
* sbuild is silent off-tty — the ``.build`` log's ``Status:`` line is
  the source of truth;
* blhc is recorded informationally; the hard audit gate is "our flags
  reached a compiler" (or the package compiles nothing);
* flags land via ``$build_environment`` in a generated sbuildrc, so
  sbuild's environment filter can't strip them.

Resumability: every attempt persists a PackageBuildRecord keyed on
(source, archive version, flags hash) under
``<world_dir>/builds/<source>/<version>/``. Re-running skips records
that are ok with an unchanged key — kill/restart pays only for new
work. FTBFS records and continues (the W3 triage agent takes over
from there); the wave moves on.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from autokernel.world import repo as repo_mod
from autokernel.world.models import (
    AuditVerdict,
    BuildOutcome,
    FlagsAudit,
    GlobalFlags,
    PackageBuildRecord,
    PackageOverride,
    SourceUnit,
    WorldManifest,
    WorldPlan,
)

_DCH_ENV = {
    "DEBEMAIL": "world@autokernel.local",
    "DEBFULLNAME": "autokernel world",
}
LOCAL_SUFFIX = "+ak"


def _run(
    argv: list[str],
    *,
    log_path: Path | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> int:
    """Run argv, append stdout+stderr to log_path, return exit code."""
    import os

    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as f:
            f.write(f"\n$ {' '.join(argv)}\n".encode())
            f.flush()
            proc = subprocess.run(
                argv,
                stdout=f,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=full_env,
                timeout=timeout,
                check=False,
            )
    else:
        proc = subprocess.run(
            argv,
            capture_output=True,
            cwd=cwd,
            env=full_env,
            timeout=timeout,
            check=False,
        )
    return proc.returncode


# ── flags / env composition ─────────────────────────────────────────────────


def effective_compiler(flags: GlobalFlags, override: PackageOverride | None) -> str:
    return (override.force_compiler if override else None) or flags.compiler


# PGO profiles are bind-mounted into the chroot here (the host path under
# /nas4 isn't visible inside sbuild's unshare namespace); -fprofile-use
# references this in-chroot path. Staged to a subuid-traversable /var/tmp
# dir like the ccache (stage_profiles), bind-mounted by render_sbuildrc.
_PROFILES_MOUNT = "/srv/world-profiles"


def pgo_extra(
    flags: GlobalFlags,
    *,
    source: str = "",
    profiles_dir: Path | None = None,
) -> tuple[str, str, str]:
    """The PGO axis → (cflags_extra, ldflags_extra, profile_digest).

    ``instrument`` appends ``-fprofile-generate`` to compile+link; the
    ``.profraw`` is written at *run* time by the workload harness, not
    during the build. ``use`` appends a per-package
    ``-fprofile-use=<_PROFILES_MOUNT>/<src>.profdata`` *iff* that profile
    exists on the host (read here only to compute the digest) — a graceful
    no-op otherwise, so a partially-collected hot set still builds. The
    returned digest (profile content hash, or the literal ``"instrument"``)
    joins ``flags_hash`` so a re-collected profile invalidates exactly that
    package and nothing else (Phase 2 resume integrity)."""
    if flags.pgo == "instrument":
        return "-fprofile-generate", "-fprofile-generate", "instrument"
    if flags.pgo == "use" and source and profiles_dir is not None:
        prof = profiles_dir / f"{source}.profdata"
        if prof.exists():
            digest = hashlib.sha256(prof.read_bytes()).hexdigest()[:16]
            inchroot = f"{_PROFILES_MOUNT}/{source}.profdata"
            cf = (
                f"-fprofile-use={inchroot} "
                "-Wno-profile-instr-unprofiled "
                "-Wno-profile-instr-out-of-date"
            )
            return cf, f"-fprofile-use={inchroot}", digest
    return "", "", ""


def effective_cflags(
    flags: GlobalFlags,
    override: PackageOverride | None,
    *,
    source: str = "",
    profiles_dir: Path | None = None,
) -> str:
    tokens = flags.cflags_for(effective_compiler(flags, override)).split()
    if override:
        tokens = [t for t in tokens if t not in set(override.strip_flags)]
        tokens.extend(override.add_flags)
    # PGO is a clang-only axis here (profiles are llvm-profdata); a
    # force_compiler=gcc package gets none.
    if effective_compiler(flags, override) == "clang":
        pgo_cf = pgo_extra(flags, source=source, profiles_dir=profiles_dir)[0]
        if pgo_cf:
            tokens.extend(pgo_cf.split())
    return " ".join(tokens)


def effective_build_options(
    flags: GlobalFlags, override: PackageOverride | None
) -> list[str]:
    merged = dict.fromkeys(
        [*flags.build_options, *(override.build_options if override else [])]
    )
    strip = set(override.strip_build_options) if override else set()
    return [o for o in merged if o not in strip]


def effective_ldflags(
    flags: GlobalFlags,
    override: PackageOverride | None,
    *,
    source: str = "",
    profiles_dir: Path | None = None,
) -> str:
    """Link-stage flags, honoring strip_flags: a remedy that strips the
    LTO token must reach the *link* too. The default lld goes with it
    (it's only there for ThinLTO) — but an *explicit* linker choice
    (linker=bfd/gold, the symver remedy) is kept. Found live: strip-flags
    retries kept failing because DEB_LDFLAGS_APPEND still carried
    -flto=thin."""
    base = flags.ldflags_for(effective_compiler(flags, override))
    tokens = base.split()
    if base and override:
        stripped = set(override.strip_flags)
        # The implicit lld is dropped with LTO; an explicit linker is not.
        implicit_lld = not flags.linker
        if any(t.startswith("-flto") for t in stripped):
            tokens = [
                t
                for t in tokens
                if not t.startswith("-flto")
                and not (implicit_lld and t == "-fuse-ld=lld")
            ]
        else:
            tokens = [t for t in tokens if t not in stripped]
    # PGO link flag (clang only) — appended after any LTO strip so the
    # instrument/use runtime is linked even when a package opted out of LTO.
    if effective_compiler(flags, override) == "clang":
        pgo_lf = pgo_extra(flags, source=source, profiles_dir=profiles_dir)[1]
        if pgo_lf:
            tokens.extend(pgo_lf.split())
    return " ".join(tokens)


def effective_profiles(
    flags: GlobalFlags, override: PackageOverride | None
) -> list[str]:
    """Build profiles, with strip_build_options applied here too:
    debhelper honors nodoc/nocheck from DEB_BUILD_OPTIONS *or*
    DEB_BUILD_PROFILES, so a strip must clear both (found live: bash
    kept failing with the option stripped but the profile present)."""
    merged = {*flags.build_profiles, *(override.profiles if override else [])}
    strip = set(override.strip_build_options) if override else set()
    return sorted(merged - strip)


def build_environment(
    flags: GlobalFlags,
    override: PackageOverride | None,
    *,
    jobs: int,
    ccache_dir: str | None,
    source: str = "",
    profiles_dir: Path | None = None,
) -> dict[str, str]:
    compiler = effective_compiler(flags, override)
    cflags = effective_cflags(flags, override, source=source, profiles_dir=profiles_dir)
    options = [*effective_build_options(flags, override), f"parallel={jobs}"]
    profiles = effective_profiles(flags, override)
    env = {
        "DEB_CFLAGS_APPEND": cflags,
        "DEB_CXXFLAGS_APPEND": cflags,
        "DEB_BUILD_OPTIONS": " ".join(options),
    }
    # gcc worlds get no CC/CXX/LDFLAGS keys at all: /usr/bin/cc is
    # already gcc, and key-set stability keeps the gcc baseline's
    # flags_hashes (and cached builds) valid.
    if compiler == "clang":
        env["CC"] = "clang"
        env["CXX"] = "clang++"
        # Drop the distro's gcc-flavored LTO defaults (-flto=auto,
        # -ffat-lto-objects) from dpkg-buildflags output; our ThinLTO
        # flags are appended instead. Best-effort: rules that set their
        # own MAINT_OPTIONS shadow this.
        env["DEB_BUILD_MAINT_OPTIONS"] = "optimize=-lto"
    elif flags.compiler == "clang":  # clang world, this package forced to gcc
        env["CC"] = "gcc"
        env["CXX"] = "g++"
    ldflags = effective_ldflags(
        flags, override, source=source, profiles_dir=profiles_dir
    )
    if ldflags:
        env["DEB_LDFLAGS_APPEND"] = ldflags
    if profiles:
        env["DEB_BUILD_PROFILES"] = " ".join(profiles)
    if ccache_dir:
        env["CCACHE_DIR"] = ccache_dir
    return env


def flags_hash(
    flags: GlobalFlags,
    override: PackageOverride | None,
    *,
    source: str = "",
    profiles_dir: Path | None = None,
) -> str:
    payload: dict[str, object] = {
        "cflags": effective_cflags(
            flags, override, source=source, profiles_dir=profiles_dir
        ),
        "options": effective_build_options(flags, override),
        "profiles": effective_profiles(flags, override),
        "compiler": effective_compiler(flags, override),
        "patches": override.patches if override else [],
    }
    # Key only present when non-empty/true so gcc-world hashes (and their
    # cached build records) are unchanged by the clang plumbing.
    ldflags = effective_ldflags(
        flags, override, source=source, profiles_dir=profiles_dir
    )
    if ldflags:
        payload["ldflags"] = ldflags
    if flags.masquerade and effective_compiler(flags, override) == "clang":
        payload["masquerade"] = True
    # The profile digest (or "instrument") invalidates exactly the PGO
    # builds; absent for pgo=off so existing world hashes are unchanged.
    pgo_digest = pgo_extra(flags, source=source, profiles_dir=profiles_dir)[2]
    if pgo_digest:
        payload["pgo"] = pgo_digest
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


# ── sbuildrc ────────────────────────────────────────────────────────────────

_CCACHE_MOUNT = "/srv/world-ccache"
# Compiler masquerade: gcc/cc names symlinked to clang so build systems
# that hardcode gcc still produce clang objects (the 100%-clang lever,
# docs/experiment/DIARY.md §0.5). Lives in the chroot, prepended to PATH.
_MASQ_DIR = "/usr/local/lib/ak-masq"
_MASQ_CC = ("gcc", "cc", "x86_64-linux-gnu-gcc")
_MASQ_CXX = ("g++", "c++", "x86_64-linux-gnu-g++")


def masquerade_hook() -> str:
    """mmdebstrap customize-hook that creates the gcc→clang masquerade in
    the chroot. Points at the ccache clang shim when present so masqueraded
    builds still cache."""
    cc_links = " && ".join(
        f'ln -sf /usr/bin/clang "$1{_MASQ_DIR}/{n}"' for n in _MASQ_CC
    )
    cxx_links = " && ".join(
        f'ln -sf /usr/bin/clang++ "$1{_MASQ_DIR}/{n}"' for n in _MASQ_CXX
    )
    return f'mkdir -p "$1{_MASQ_DIR}" && {cc_links} && {cxx_links}'


def stage_profiles(world_dir: Path) -> Path | None:
    """Copy the world's PGO profiles into a subuid-traversable /var/tmp dir
    so they can be bind-mounted into sbuild's unshare namespace at
    _PROFILES_MOUNT (the /nas4 world dir generally isn't traversable by the
    subuid range — same constraint as the ccache). Returns the stage dir, or
    None if there are no profiles to mount."""
    import os

    src = world_dir / "profiles"
    profs = sorted(src.glob("*.profdata")) if src.exists() else []
    if not profs:
        return None
    stage = Path(f"/var/tmp/autokernel-world-profiles-{os.getuid()}")  # noqa: S108
    stage.mkdir(parents=True, exist_ok=True)
    stage.chmod(0o777)
    for p in profs:
        dst = stage / p.name
        shutil.copyfile(p, dst)
        dst.chmod(0o644)
    return stage


def render_sbuildrc(
    *,
    env: dict[str, str],
    ccache_dir: Path | None,
    masquerade: bool = False,
    profiles_stage: Path | None = None,
) -> str:
    """Generated SBUILD_CONFIG.

    Bind-mount sources must be traversable by the subuid range —
    sbuild's unshare mode maps ns-root to the *subuid* start, not the
    invoking uid, so anything under a 0700 $HOME is invisible inside
    the namespace. That's why the ccache lives under /var/tmp (see
    default_ccache_dir) and why the repo is NOT bind-mounted at all:
    own-output packages are delivered per-build via --extra-package
    instead.
    """
    env_in_chroot = dict(env)
    mounts: list[str] = []
    if ccache_dir is not None:
        env_in_chroot["CCACHE_DIR"] = _CCACHE_MOUNT
        mounts.append(
            f"{{ directory => '{ccache_dir}', mountpoint => '{_CCACHE_MOUNT}' }}"
        )
    if profiles_stage is not None:
        mounts.append(
            f"{{ directory => '{profiles_stage}', mountpoint => '{_PROFILES_MOUNT}' }}"
        )

    env_lines = ",\n".join(
        f"    '{k}' => '{v}'" for k, v in sorted(env_in_chroot.items())
    )
    # masquerade dir goes first so gcc/cc names resolve to clang before
    # the real gcc in /usr/bin.
    path_dirs = (
        "/usr/lib/ccache:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    if masquerade:
        path_dirs = f"{_MASQ_DIR}:{path_dirs}"
    return (
        "\n".join(
            [
                "$chroot_mode = 'unshare';",
                "$build_environment = {",
                env_lines,
                "};",
                f"$path = '{path_dirs}';",
                f"$unshare_bind_mounts = [{', '.join(mounts)}];",
                "$run_lintian = 0;",
                "$run_autopkgtest = 0;",
                "$run_piuparts = 0;",
                "1;",
            ]
        )
        + "\n"
    )


def default_ccache_dir() -> Path:
    """Shared ccache under /var/tmp, world-writable.

    The cache must be (a) traversable and (b) writable by the subuid
    range that sbuild's namespace maps to, which rules out $HOME on
    hosts with 0700 home dirs. 0777 under /var/tmp trades cache
    integrity for that: fine on a single-user workstation, use
    --no-ccache on shared hosts.
    """
    import os

    path = Path(f"/var/tmp/autokernel-world-ccache-{os.getuid()}")  # noqa: S108
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o777)
    return path


def extra_package_args(repo_dir: Path) -> list[str]:
    """--extra-package args for every .deb already published — sbuild
    serves them to the chroot via its internal repo, so later waves
    build against our own output (no bind mount, no trusted=yes)."""
    return [f"--extra-package={deb}" for deb in sorted(repo_dir.glob("*.deb"))]


def apply_patches(unpacked: Path, patches: list[str]) -> tuple[bool, str]:
    """Apply override patches to an unpacked source tree, format-aware.

    3.0 (quilt) packages get the patch added to debian/patches/series
    (dpkg-source applies the series at build time). **Native** (3.0
    native / 1.0) packages have no quilt layer — the tree *is* the
    source — so the patch is applied directly to the tree (found live:
    apt is native, so a series entry was silently ignored, the agent's
    correct patch never reached the build). Each patch is dry-run-checked
    first. Returns (ok, problem)."""
    fmt_file = unpacked / "debian" / "source" / "format"
    is_quilt = (
        fmt_file.exists() and "quilt" in fmt_file.read_text(encoding="utf-8").lower()
    )
    for src in patches:
        if not Path(src).is_file():
            return False, f"patch not found: {src}"
        check = subprocess.run(
            ["patch", "-p1", "--dry-run", "-i", str(src)],
            cwd=unpacked,
            capture_output=True,
            text=True,
            check=False,
        )
        if check.returncode != 0:
            return (
                False,
                f"{Path(src).name} does not apply: {check.stdout.strip()[:200]}",
            )

    if is_quilt:
        patches_dir = unpacked / "debian" / "patches"
        patches_dir.mkdir(parents=True, exist_ok=True)
        series = patches_dir / "series"
        existing = (
            series.read_text(encoding="utf-8").splitlines() if series.exists() else []
        )
        added: list[str] = []
        for src in patches:
            name = f"autokernel-{Path(src).name}"
            if not name.endswith((".patch", ".diff")):
                name += ".patch"
            shutil.copy2(src, patches_dir / name)
            added.append(name)
        new_series = [*existing, *(n for n in added if n not in existing)]
        series.write_text("\n".join(new_series) + "\n", encoding="utf-8")
        return True, f"quilt: added {len(added)} patch(es) to series"

    # native / 1.0 — apply directly to the tree (no quilt layer)
    for src in patches:
        rc = subprocess.run(
            ["patch", "-p1", "-i", str(src)],
            cwd=unpacked,
            capture_output=True,
            text=True,
            check=False,
        )
        if rc.returncode != 0:
            return False, f"native patch apply failed: {rc.stdout.strip()[:200]}"
    return True, f"native: applied {len(patches)} patch(es) to tree"


# ── audit ───────────────────────────────────────────────────────────────────

# Compile invocations: a compiler driver name followed (anywhere) by a
# `-c ` compile flag. Anchored to word boundaries so "clang" doesn't
# match inside paths.
_CLANG_COMPILE = re.compile(r"(?:^|[\s/])(?:clang\+\+|clang)\b[^\n]*?\s-c\s", re.M)
# Any gcc-family driver compiling — used when the masquerade is OFF.
_GCC_COMPILE = re.compile(
    r"(?:^|[\s/])(?:x86_64-linux-gnu-)?(?:gcc|g\+\+|cc|c\+\+)\b[^\n]*?\s-c\s", re.M
)
# gcc that BYPASSES the masquerade: an absolute path not under ak-masq,
# or a version-suffixed driver (gcc-15) the masquerade doesn't shadow.
# Bare `gcc`/`cc` under masquerade resolve via PATH to clang, so they do
# NOT count here (Phase 1 finding: bzip2's debian/rules forces CC=gcc,
# but masqueraded gcc→clang built it — the log says "gcc -c" yet clang ran).
_GCC_BYPASS = re.compile(
    r"(?:/(?:usr|lib|bin)\S*?/(?:x86_64-linux-gnu-)?(?:gcc|g\+\+)"
    r"|(?:^|\s)(?:x86_64-linux-gnu-)?(?:gcc|g\+\+)-\d+)"
    r"\b[^\n]*?\s-c\s",
    re.M,
)


def compiler_identity_audit(
    log_text: str, want: str, *, masquerade: bool = False
) -> tuple[bool, str]:
    """Did the requested compiler actually compile the *package*? (Phase 0
    §0.5 / 1.9.) Returns (ok, detail).

    A package is "clang" if clang did the **majority** of compiles. A
    handful of gcc invocations are tolerated because some packages
    legitimately use gcc for throwaway build-time helpers (libselinux's
    `gcc -aux-info` to extract SWIG prototypes, etc.) while compiling the
    shipped objects with clang. The violation is when gcc did the bulk —
    e.g. bzip2's debian/rules forces CC=gcc and clang ran nothing.

    With the masquerade ON, bare gcc/cc names are PATH-redirected to clang
    and show as "gcc" in the log, so the majority test would miscount;
    there, only absolute-path/version-suffixed gcc (a real bypass) counts.
    A build that compiled *nothing* (data/script package) passes vacuously."""
    clang_n = len(_CLANG_COMPILE.findall(log_text))
    if want != "clang":
        return True, f"clang={clang_n} gcc={len(_GCC_COMPILE.findall(log_text))}"
    if masquerade:
        gcc_n = len(_GCC_BYPASS.findall(log_text))
        if gcc_n > 0:
            return False, f"masquerade-bypassing gcc compiled {gcc_n} (clang {clang_n})"
        return True, f"clang={clang_n} gcc=0 (masq)"
    gcc_n = len(_GCC_COMPILE.findall(log_text))
    # Majority rule: gcc only a violation if it out-compiled clang.
    if gcc_n > clang_n:
        return False, f"gcc compiled the majority: gcc={gcc_n} clang={clang_n}"
    return True, f"clang={clang_n} gcc={gcc_n}"


def audit_build_log(
    log_text: str,
    expected_tokens: list[str],
    *,
    blhc_output: str,
    blhc_rc: int,
) -> FlagsAudit:
    """Pure audit logic (docs/WORLD.md W0 semantics). Hard gate: every
    expected token appears somewhere in a compile line, unless the
    package compiles nothing at all."""
    findings = [
        line
        for line in blhc_output.splitlines()
        if line.startswith(("CFLAGS missing", "LDFLAGS missing", "CPPFLAGS missing"))
        or line.startswith("NONVERBOSE BUILD")
    ]
    kinds = sorted({f.split(":")[0] for f in findings})

    if blhc_rc != 0 and "No compiler commands" in blhc_output:
        return FlagsAudit(
            verdict=AuditVerdict.NO_COMPILER,
            expected=expected_tokens,
            blhc_finding_count=0,
        )

    missing = [t for t in expected_tokens if t not in log_text]
    return FlagsAudit(
        verdict=AuditVerdict.OK if not missing else AuditVerdict.MISSING_FLAGS,
        expected=expected_tokens,
        missing=missing,
        blhc_finding_count=len(findings),
        blhc_summary=kinds,
    )


# ── per-unit build ──────────────────────────────────────────────────────────


@dataclass
class BuildContext:
    """Everything shared across units in one `world build` run."""

    manifest: WorldManifest
    world_dir: Path
    chroot_tarball: Path
    apt_dir: Path
    repo_dir: Path
    gnupg_dir: Path
    ccache_dir: Path | None
    jobs: int
    publish_lock: threading.Lock


def _safe_version(version: str) -> str:
    return version.replace(":", "%3a")


def unit_dir(world_dir: Path, unit: SourceUnit) -> Path:
    return world_dir / "builds" / unit.source / _safe_version(unit.version)


def load_record(world_dir: Path, unit: SourceUnit) -> PackageBuildRecord | None:
    path = unit_dir(world_dir, unit) / "record.json"
    if not path.exists():
        return None
    try:
        return PackageBuildRecord.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (ValueError, OSError):
        return None


def needs_build(world_dir: Path, unit: SourceUnit, fhash: str) -> bool:
    if unit.use_stock:
        return False
    record = load_record(world_dir, unit)
    return not (record and record.ok and record.flags_hash == fhash)


def _save_record(world_dir: Path, unit: SourceUnit, record: PackageBuildRecord) -> None:
    path = unit_dir(world_dir, unit) / "record.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _apt_opts(apt_dir: Path) -> list[str]:
    import os

    return [
        "-o", f"Dir::Etc::SourceList={apt_dir}/sources.list",
        "-o", f"Dir::Etc::SourceParts={apt_dir}/sources.list.d",
        "-o", f"Dir::State={apt_dir}/state",
        "-o", f"Dir::Cache={apt_dir}/cache",
        "-o", "Dir::State::Status=/var/lib/dpkg/status",
        "-o", f"APT::Sandbox::User={os.environ.get('USER', 'root')}",
        "-o", "Acquire::Languages=none",
    ]  # fmt: skip


def setup_apt_state(ctx: BuildContext, log: Path) -> None:
    """deb-src lines + apt update in a private state dir (host apt
    config untouched — W0 pattern)."""
    base = ctx.manifest.base
    for sub in ("state/lists/partial", "cache/archives/partial", "sources.list.d"):
        (ctx.apt_dir / sub).mkdir(parents=True, exist_ok=True)
    lines = [
        f"deb-src {base.mirror} {dist} {' '.join(base.components)}"
        for dist in (base.suite, f"{base.suite}-updates", f"{base.suite}-security")
    ]
    (ctx.apt_dir / "sources.list").write_text("\n".join(lines) + "\n", encoding="utf-8")
    rc = _run(["apt-get", *_apt_opts(ctx.apt_dir), "update"], log_path=log)
    if rc != 0:
        raise RuntimeError(f"apt-get update failed (see {log})")


def ensure_chroot_tarball(ctx: BuildContext, log: Path) -> None:
    if ctx.chroot_tarball.exists():
        return
    base = ctx.manifest.base
    ctx.chroot_tarball.parent.mkdir(parents=True, exist_ok=True)
    comps = " ".join(base.components)
    include = "ccache"
    extra_hooks: list[str] = []
    if ctx.manifest.flags.compiler == "clang":
        # clang isn't a build-dep of anything; it must live in the chroot.
        # lld+llvm: ThinLTO needs a plugin-capable linker; llvm provides
        # LLVMgold.so so linker=bfd works (the symver remedy, Phase 0).
        # libclang-rt-dev carries libclang_rt.profile, linked by
        # -fprofile-generate (Phase 2 PGO instrument); harmless otherwise.
        include = "ccache,clang,lld,llvm,libclang-rt-dev"
    if ctx.manifest.flags.masquerade:
        extra_hooks.append(f"--customize-hook={masquerade_hook()}")
    argv = [
        "mmdebstrap",
        "--variant=buildd",
        "--mode=unshare",
        f"--include={include}",
        # sbuild's unshare bind mounts need pre-existing mountpoints.
        f'--customize-hook=mkdir -p "$1{_CCACHE_MOUNT}"',
        *extra_hooks,
        base.suite,
        str(ctx.chroot_tarball),
        f"deb {base.mirror} {base.suite} {comps}",
        f"deb {base.mirror} {base.suite}-updates {comps}",
        f"deb {base.mirror} {base.suite}-security {comps}",
    ]
    rc = _run(argv, log_path=log)
    if rc != 0:
        raise RuntimeError(f"buildd chroot creation failed (see {log})")


def build_unit(ctx: BuildContext, unit: SourceUnit) -> PackageBuildRecord:
    """Fetch, bump, build, audit, publish, record — one source unit."""
    started = datetime.now(UTC)
    override = ctx.manifest.override_for(unit.source)
    fhash = flags_hash(
        ctx.manifest.flags,
        override,
        source=unit.source,
        profiles_dir=ctx.world_dir / "profiles",
    )
    udir = unit_dir(ctx.world_dir, unit)
    log = udir / "build.log"

    def finish(
        outcome: BuildOutcome,
        *,
        local_version: str | None = None,
        audit: FlagsAudit | None = None,
        debs: list[Path] | None = None,
        note: str | None = None,
    ) -> PackageBuildRecord:
        record = PackageBuildRecord(
            source=unit.source,
            archive_version=unit.version,
            local_version=local_version,
            flags_hash=fhash,
            outcome=outcome,
            wave=unit.wave,
            duration_s=(datetime.now(UTC) - started).total_seconds(),
            audit=audit,
            debs=[d.name for d in debs or []],
            log_path=str(log),
            finished_at=datetime.now(UTC),
            note=note,
        )
        _save_record(ctx.world_dir, unit, record)
        return record

    # 1. fetch source (exact archive version, fallback to candidate)
    src_dir = udir / "src"
    if src_dir.exists():
        # Preserve prior attempts' sbuild logs before wiping the tree —
        # triage evidence must survive retries.
        for old in src_dir.glob("*.build"):
            if not old.is_symlink():
                shutil.move(str(old), udir / old.name)
        shutil.rmtree(src_dir)
    src_dir.mkdir(parents=True)
    fetch_argv = [
        "apt-get",
        *_apt_opts(ctx.apt_dir),
        "source",
        "--only-source",
        f"{unit.source}={unit.version}",
    ]
    if _run(fetch_argv, log_path=log, cwd=src_dir) != 0:
        fetch_argv[-1] = unit.source
        if _run(fetch_argv, log_path=log, cwd=src_dir) != 0:
            return finish(BuildOutcome.FETCH_FAILED, note="apt-get source failed")
    unpacked = next((p for p in src_dir.iterdir() if p.is_dir()), None)
    if unpacked is None:
        return finish(BuildOutcome.FETCH_FAILED, note="no unpacked source dir")

    # 1b. apply override patches (agentic-patch remedy, Phase 4) — into
    # debian/patches/ + series so dpkg-source -b ships them and the
    # chroot build applies them.
    if override and override.patches:
        applied, problem = apply_patches(unpacked, override.patches)
        if not applied:
            return finish(BuildOutcome.FTBFS, note=f"patch apply failed: {problem}")

    # 2. +ak suffix
    rc = _run(
        ["dch", "--local", LOCAL_SUFFIX, f"Rebuild by autokernel world ({fhash})."],
        log_path=log,
        cwd=unpacked,
        env=_DCH_ENV,
    )
    if rc != 0:
        return finish(BuildOutcome.FTBFS, note="dch failed")
    # For native-format packages dch renames the source dir to match
    # the new version (foo-1.2 → foo-1.2+ak1); re-resolve the path.
    unpacked = next((p for p in src_dir.iterdir() if p.is_dir()), None)
    if unpacked is None:
        return finish(BuildOutcome.FTBFS, note="source dir vanished after dch")
    if _run(["dpkg-source", "-b", str(unpacked)], log_path=log, cwd=src_dir) != 0:
        return finish(BuildOutcome.FTBFS, note="dpkg-source -b failed")
    dsc = next(iter(src_dir.glob(f"*{LOCAL_SUFFIX}1.dsc")), None)
    if dsc is None:
        return finish(BuildOutcome.FTBFS, note="no +ak1 .dsc produced")
    local_version = dsc.stem.split("_", 1)[1]

    # 3. sbuild
    env = build_environment(
        ctx.manifest.flags,
        override,
        jobs=ctx.jobs,
        ccache_dir=_CCACHE_MOUNT if ctx.ccache_dir else None,
        source=unit.source,
        profiles_dir=ctx.world_dir / "profiles",
    )
    sbuildrc = udir / "sbuildrc"
    # Masquerade only when this package's effective compiler is clang —
    # a force_compiler=gcc remedy must see a real gcc on PATH.
    masq = (
        ctx.manifest.flags.masquerade
        and effective_compiler(ctx.manifest.flags, override) == "clang"
    )
    # PGO use: stage + bind-mount the profiles so -fprofile-use=<mount>/... resolves.
    profiles_stage = (
        stage_profiles(ctx.world_dir) if ctx.manifest.flags.pgo == "use" else None
    )
    sbuildrc.write_text(
        render_sbuildrc(
            env=env,
            ccache_dir=ctx.ccache_dir,
            masquerade=masq,
            profiles_stage=profiles_stage,
        ),
        encoding="utf-8",
    )
    base = ctx.manifest.base
    with ctx.publish_lock:
        extra_pkgs = extra_package_args(ctx.repo_dir)
    rc = _run(
        [
            "sbuild",
            f"--dist={base.suite}",
            f"--chroot={ctx.chroot_tarball}",
            "--chroot-mode=unshare",
            "--no-source",
            *extra_pkgs,
            str(dsc),
        ],
        log_path=log,
        cwd=src_dir,
        env={"SBUILD_CONFIG": str(sbuildrc)},
    )
    build_logs = sorted(
        (p for p in src_dir.glob("*.build") if not p.is_symlink()),
        key=lambda p: p.stat().st_mtime,
    )
    build_log_text = (
        build_logs[-1].read_text(encoding="utf-8", errors="replace")
        if build_logs
        else ""
    )
    succeeded = rc == 0 and "Status: successful" in build_log_text
    if not succeeded:
        return finish(
            BuildOutcome.FTBFS,
            local_version=local_version,
            note=f"sbuild rc={rc} (see {build_logs[-1] if build_logs else log})",
        )

    # 4. audit
    blhc = subprocess.run(
        ["blhc", "--all", str(build_logs[-1])],
        capture_output=True,
        text=True,
        check=False,
    )
    audit = audit_build_log(
        build_log_text,
        effective_cflags(ctx.manifest.flags, override).split(),
        blhc_output=blhc.stdout,
        blhc_rc=blhc.returncode,
    )
    if audit.verdict == AuditVerdict.MISSING_FLAGS:
        return finish(
            BuildOutcome.FTBFS,
            local_version=local_version,
            audit=audit,
            note=f"flag audit failed: missing {audit.missing}",
        )

    # 4b. compiler-identity audit (the 100%-clang gate, Phase 0 §0.5):
    # a clang world that silently built objects with gcc is a violation
    # unless force_compiler=gcc was explicitly declared.
    want = effective_compiler(ctx.manifest.flags, override)
    if want == "clang":
        cc_ok, cc_detail = compiler_identity_audit(
            build_log_text, "clang", masquerade=masq
        )
        if not cc_ok:
            return finish(
                BuildOutcome.FTBFS,
                local_version=local_version,
                audit=audit,
                note=f"compiler-identity audit failed: {cc_detail}",
            )

    # 5. publish (serialized: the repo index is shared state)
    debs = sorted(src_dir.glob("*.deb"))
    with ctx.publish_lock:
        repo_mod.publish(
            ctx.repo_dir,
            debs,
            gnupg_dir=ctx.gnupg_dir,
            arch=_host_arch(),
        )
    return finish(BuildOutcome.OK, local_version=local_version, audit=audit, debs=debs)


def _host_arch() -> str:
    return subprocess.run(
        ["dpkg", "--print-architecture"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# ── orchestration ───────────────────────────────────────────────────────────


def apply_exceptions(manifest: WorldManifest, world_dir: Path) -> WorldManifest:
    """Merge the confirmed exceptions table into the manifest. Exceptions
    are prepended so override_for() prefers them; preset gates address
    disjoint (use_stock) sources, so precedence is moot there."""
    from autokernel.world import triage as triage_mod

    exceptions = triage_mod.load_exceptions(world_dir)
    if not exceptions:
        return manifest
    return manifest.model_copy(update={"overrides": [*exceptions, *manifest.overrides]})


def pending_units(
    manifest: WorldManifest, plan: WorldPlan, world_dir: Path
) -> tuple[list[SourceUnit], list[SourceUnit], list[SourceUnit]]:
    """(to_build, skipped_done, stock) after resume filtering."""
    to_build: list[SourceUnit] = []
    done: list[SourceUnit] = []
    stock: list[SourceUnit] = []
    for unit in plan.units:
        if unit.use_stock:
            stock.append(unit)
            continue
        fhash = flags_hash(
            manifest.flags,
            manifest.override_for(unit.source),
            source=unit.source,
            profiles_dir=world_dir / "profiles",
        )
        if needs_build(world_dir, unit, fhash):
            to_build.append(unit)
        else:
            done.append(unit)
    return to_build, done, stock


def build_world(
    manifest: WorldManifest,
    plan: WorldPlan,
    world_dir: Path,
    *,
    parallel: int = 1,
    jobs: int = 0,
    ccache: bool = True,
    only: list[str] | None = None,
    limit: int = 0,
    progress=None,
    triage: bool = False,
    triage_model: str | None = None,
    triage_progress=None,
    agentic_backend: str | None = None,
) -> list[PackageBuildRecord]:
    """Build all pending units wave by wave. FTBFS records and
    continues; with ``triage=True`` a bounded LLM triage→retry pass
    runs over this run's failures afterwards (escalating deferred
    failures to ``agentic_backend`` if set). ``progress`` is an optional
    callback(record)."""
    import os

    manifest = apply_exceptions(manifest, world_dir)
    base = manifest.base
    ccache_dir = default_ccache_dir() if ccache else None
    # Tag encodes the chroot's toolchain config so a different config
    # regenerates rather than reusing a stale chroot (e.g. the masquerade
    # + llvm additions for the 100%-clang world).
    if manifest.flags.compiler == "clang":
        # v3: the clang chroot now also includes libclang-rt-dev (the
        # PGO profile runtime); bump so -clang2 tarballs regenerate.
        tarball_tag = "-clang3-masq" if manifest.flags.masquerade else "-clang3"
    else:
        tarball_tag = ""
    ctx = BuildContext(
        manifest=manifest,
        world_dir=world_dir,
        chroot_tarball=Path.home()
        / ".cache"
        / "sbuild"
        / f"{base.suite}-{_host_arch()}-world{tarball_tag}.tar.zst",
        apt_dir=world_dir / "apt",
        repo_dir=world_dir / "repo",
        gnupg_dir=world_dir / "gnupg",
        ccache_dir=ccache_dir,
        jobs=jobs or max(1, (os.cpu_count() or 4) // max(1, parallel)),
        publish_lock=threading.Lock(),
    )
    ctx.repo_dir.mkdir(parents=True, exist_ok=True)
    repo_mod.ensure_key(ctx.gnupg_dir, ctx.repo_dir / repo_mod.KEYRING_NAME)
    setup_apt_state(ctx, world_dir / "apt-update.log")
    ensure_chroot_tarball(ctx, world_dir / "mmdebstrap-buildd.log")

    to_build, _done, _stock = pending_units(manifest, plan, world_dir)
    if only:
        wanted = set(only)
        to_build = [u for u in to_build if u.source in wanted]
    if limit:
        to_build = to_build[:limit]

    records: list[PackageBuildRecord] = []
    by_wave: dict[int, list[SourceUnit]] = {}
    for unit in to_build:
        by_wave.setdefault(unit.wave, []).append(unit)
    for wave in sorted(by_wave):
        units = by_wave[wave]
        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            for record in pool.map(lambda u: build_unit(ctx, u), units):
                records.append(record)
                if progress is not None:
                    progress(record)
    if triage and any(r.outcome == BuildOutcome.FTBFS for r in records):
        records = triage_and_retry(
            ctx,
            plan,
            records,
            model=triage_model,
            progress=triage_progress,
            agentic_backend=agentic_backend,
        )
    return records


# ── triage + retry (W3) ─────────────────────────────────────────────────────


def _latest_build_log(world_dir: Path, unit: SourceUnit) -> Path | None:
    udir = unit_dir(world_dir, unit)
    logs = sorted(
        (
            p
            for pattern in ("src/*.build", "*.build")
            for p in udir.glob(pattern)
            if not p.is_symlink()
        ),
        key=lambda p: p.stat().st_mtime,
    )
    if logs:
        return logs[-1]
    fallback = udir / "build.log"
    return fallback if fallback.exists() else None


def agentic_patch_remedy(
    ctx: BuildContext,
    unit: SourceUnit,
    record: PackageBuildRecord,
    *,
    backend: str,
    model: str | None = None,
    timeout_s: int = 600,
) -> PackageOverride | None:
    """Tier-3 escalation (Phase 4): a headless coding agent generates a
    source patch for an FTBFS no flag remedy could fix. Returns a
    patches= override (validated by the caller's rebuild) or None."""
    from autokernel.world import agent_patch
    from autokernel.world.models import OverrideSource

    scratch = unit_dir(ctx.world_dir, unit) / "agentic"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    fetch = [
        "apt-get",
        *_apt_opts(ctx.apt_dir),
        "source",
        "--only-source",
        f"{unit.source}={unit.version}",
    ]
    if _run(fetch, cwd=scratch) != 0:
        return None
    tree = next((p for p in scratch.iterdir() if p.is_dir()), None)
    if tree is None:
        return None

    log_path = _latest_build_log(ctx.world_dir, unit)
    log_tail = ""
    if log_path is not None:
        from autokernel.world import triage as triage_mod

        log_tail = triage_mod.extract_log_tail(
            log_path.read_text(encoding="utf-8", errors="replace")
        )
    override = ctx.manifest.override_for(unit.source)
    prompt = agent_patch.build_fix_prompt(
        source=unit.source,
        version=unit.version,
        flags_desc=effective_cflags(ctx.manifest.flags, override),
        log_tail=log_tail,
    )
    res = agent_patch.run_coding_agent(
        backend, tree, prompt, model=model, timeout_s=timeout_s
    )
    if not res.ok:
        return None
    patches_dir = ctx.world_dir / "patches"
    patch_path = agent_patch.save_patch(res.patch, patches_dir, unit.source)
    (patches_dir / f"{unit.source}.transcript.json").write_text(
        res.transcript, encoding="utf-8"
    )
    return PackageOverride(
        source_pkg=unit.source,
        patches=[str(patch_path)],
        reason=f"agentic patch ({backend}): {res.summary[:120]}",
        provenance=OverrideSource.LLM_TRIAGE,
    )


def _remedy_changes_something(remedy: PackageOverride) -> bool:
    return any(
        [
            remedy.strip_flags,
            remedy.add_flags,
            remedy.build_options,
            remedy.strip_build_options,
            remedy.profiles,
            remedy.force_compiler,
            remedy.patches,
            remedy.use_stock,
        ]
    )


def triage_and_retry(
    ctx: BuildContext,
    plan: WorldPlan,
    records: list[PackageBuildRecord],
    *,
    model: str | None = None,
    progress=None,
    agentic_backend: str | None = None,
    max_rounds: int = 3,
) -> list[PackageBuildRecord]:
    """Multi-round triage→retry over this run's FTBFS records.

    Each round re-triages the *latest* failure of every still-failing
    package and **accumulates** remedies (so a compound case like bash —
    strip-nodoc round 1, then force-gcc round 2 after the identity audit
    fires — converges instead of oscillating). Confirmed remedies persist
    to exceptions.json. With ``agentic_backend`` set, a deferred failure
    escalates to tier-3 (a coding agent patch). Stops early when a round
    makes no progress (no new success and no new remedy). Returns the
    updated record list.
    """
    from autokernel.world import triage as triage_mod

    model = model or triage_mod.DEFAULT_MODEL
    units = {u.source: u for u in plan.units}
    out = {r.source: r for r in records}
    # accumulated remedy per package across rounds
    acc: dict[str, PackageOverride] = {}

    for _round in range(max_rounds):
        failing = [
            r
            for r in out.values()
            if r.outcome == BuildOutcome.FTBFS and r.source in units
        ]
        if not failing:
            break
        progressed = False
        for record in failing:
            unit = units[record.source]
            log_path = _latest_build_log(ctx.world_dir, unit)
            if log_path is None:
                continue
            base_override = ctx.manifest.override_for(record.source)
            eff = effective_cflags(
                ctx.manifest.flags, acc.get(record.source) or base_override
            )
            verdict, problems = triage_mod.triage_record(
                record,
                log_text=log_path.read_text(encoding="utf-8", errors="replace"),
                flags=ctx.manifest.flags,
                effective_cflags=eff,
                world_dir=ctx.world_dir,
                model=model,
            )
            if progress is not None:
                progress(verdict, problems)
            remedy = verdict.remedy
            if remedy is None and agentic_backend:
                remedy = agentic_patch_remedy(
                    ctx, unit, record, backend=agentic_backend, model=model
                )
                if progress is not None and remedy is not None:
                    progress(verdict, ["agentic-patch generated"])
            if remedy is None:
                continue  # deferred this round

            # accumulate with any prior-round remedy for this package
            prior = acc.get(record.source)
            merged = triage_mod.merge_overrides(prior, remedy) if prior else remedy
            if prior is not None and merged == prior:
                continue  # no new information → don't re-burn a build
            acc[record.source] = merged

            if merged.use_stock:
                triage_mod.save_exception(ctx.world_dir, merged)
                progressed = True
                continue

            patched = ctx.manifest.model_copy(
                update={"overrides": [merged, *ctx.manifest.overrides]}
            )
            new_record = build_unit(replace(ctx, manifest=patched), unit)
            out[record.source] = new_record
            if progress is not None:
                progress(new_record, None)
            progressed = True  # a remedy was applied + rebuilt
            if new_record.outcome == BuildOutcome.OK and _remedy_changes_something(
                merged
            ):
                triage_mod.save_exception(ctx.world_dir, merged)
        if not progressed:
            break
    return list(out.values())
