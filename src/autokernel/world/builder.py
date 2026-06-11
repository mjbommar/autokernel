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


def effective_cflags(flags: GlobalFlags, override: PackageOverride | None) -> str:
    tokens = flags.cflags_for(effective_compiler(flags, override)).split()
    if override:
        tokens = [t for t in tokens if t not in set(override.strip_flags)]
        tokens.extend(override.add_flags)
    return " ".join(tokens)


def effective_build_options(
    flags: GlobalFlags, override: PackageOverride | None
) -> list[str]:
    merged = dict.fromkeys(
        [*flags.build_options, *(override.build_options if override else [])]
    )
    strip = set(override.strip_build_options) if override else set()
    return [o for o in merged if o not in strip]


def effective_ldflags(flags: GlobalFlags, override: PackageOverride | None) -> str:
    """Link-stage flags, honoring strip_flags: a remedy that strips the
    LTO token must reach the *link* too, and lld goes with it (it's
    only there for ThinLTO). Found live: strip-flags retries kept
    failing because DEB_LDFLAGS_APPEND still carried -flto=thin."""
    base = flags.ldflags_for(effective_compiler(flags, override))
    if not base or not override:
        return base
    tokens = base.split()
    stripped = set(override.strip_flags)
    if any(t.startswith("-flto") for t in stripped):
        tokens = [
            t for t in tokens if not t.startswith("-flto") and t != "-fuse-ld=lld"
        ]
    else:
        tokens = [t for t in tokens if t not in stripped]
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
) -> dict[str, str]:
    compiler = effective_compiler(flags, override)
    cflags = effective_cflags(flags, override)
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
    ldflags = effective_ldflags(flags, override)
    if ldflags:
        env["DEB_LDFLAGS_APPEND"] = ldflags
    if profiles:
        env["DEB_BUILD_PROFILES"] = " ".join(profiles)
    if ccache_dir:
        env["CCACHE_DIR"] = ccache_dir
    return env


def flags_hash(flags: GlobalFlags, override: PackageOverride | None) -> str:
    payload = {
        "cflags": effective_cflags(flags, override),
        "options": effective_build_options(flags, override),
        "profiles": effective_profiles(flags, override),
        "compiler": effective_compiler(flags, override),
        "patches": override.patches if override else [],
    }
    # Key only present when non-empty so gcc-world hashes (and their
    # cached build records) are unchanged by the clang plumbing.
    ldflags = effective_ldflags(flags, override)
    if ldflags:
        payload["ldflags"] = ldflags
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


# ── sbuildrc ────────────────────────────────────────────────────────────────

_CCACHE_MOUNT = "/srv/world-ccache"


def render_sbuildrc(
    *,
    env: dict[str, str],
    ccache_dir: Path | None,
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

    env_lines = ",\n".join(
        f"    '{k}' => '{v}'" for k, v in sorted(env_in_chroot.items())
    )
    return (
        "\n".join(
            [
                "$chroot_mode = 'unshare';",
                "$build_environment = {",
                env_lines,
                "};",
                # /usr/lib/ccache shims first; harmless when ccache is absent.
                "$path = '/usr/lib/ccache:/usr/local/sbin:/usr/local/bin:"
                "/usr/sbin:/usr/bin:/sbin:/bin';",
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


# ── audit ───────────────────────────────────────────────────────────────────


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
    if ctx.manifest.flags.compiler == "clang":
        # clang isn't a build-dep of anything; it must live in the
        # chroot image. lld: ThinLTO needs a plugin-capable linker.
        include = "ccache,clang,lld"
    argv = [
        "mmdebstrap",
        "--variant=buildd",
        "--mode=unshare",
        f"--include={include}",
        # sbuild's unshare bind mounts need pre-existing mountpoints.
        f'--customize-hook=mkdir -p "$1{_CCACHE_MOUNT}"',
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
    fhash = flags_hash(ctx.manifest.flags, override)
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
    )
    sbuildrc = udir / "sbuildrc"
    sbuildrc.write_text(
        render_sbuildrc(env=env, ccache_dir=ctx.ccache_dir),
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
        fhash = flags_hash(manifest.flags, manifest.override_for(unit.source))
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
) -> list[PackageBuildRecord]:
    """Build all pending units wave by wave. FTBFS records and
    continues; with ``triage=True`` a bounded LLM triage→retry pass
    runs over this run's failures afterwards. ``progress`` is an
    optional callback(record)."""
    import os

    manifest = apply_exceptions(manifest, world_dir)
    base = manifest.base
    ccache_dir = default_ccache_dir() if ccache else None
    tarball_tag = "-clang" if manifest.flags.compiler == "clang" else ""
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
            ctx, plan, records, model=triage_model, progress=triage_progress
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


def triage_and_retry(
    ctx: BuildContext,
    plan: WorldPlan,
    records: list[PackageBuildRecord],
    *,
    model: str | None = None,
    progress=None,
) -> list[PackageBuildRecord]:
    """One bounded triage→retry pass over this run's FTBFS records.

    Confirmed remedies (retry built green) persist to exceptions.json;
    deferred / still-failing packages stay FTBFS (stock fallback).
    Returns the updated record list.
    """
    from autokernel.world import triage as triage_mod

    model = model or triage_mod.DEFAULT_MODEL
    units = {u.source: u for u in plan.units}
    out = {r.source: r for r in records}
    for record in records:
        if record.outcome != BuildOutcome.FTBFS:
            continue
        unit = units.get(record.source)
        if unit is None:
            continue
        log_path = _latest_build_log(ctx.world_dir, unit)
        if log_path is None:
            continue
        override = ctx.manifest.override_for(record.source)
        eff = effective_cflags(ctx.manifest.flags, override)
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
        if remedy is None:
            continue  # deferred (or override_check rejected it)

        if remedy.use_stock:
            # Nothing to validate by building; persist the surrender.
            triage_mod.save_exception(ctx.world_dir, remedy)
            continue

        # Retry with the remedy active: remedy is prepended so
        # override_for() picks it over any earlier entry.
        patched = ctx.manifest.model_copy(
            update={"overrides": [remedy, *ctx.manifest.overrides]}
        )
        retry_ctx = replace(ctx, manifest=patched)
        new_record = build_unit(retry_ctx, unit)
        out[record.source] = new_record
        if progress is not None:
            progress(new_record, None)
        changes_something = any(
            [
                remedy.strip_flags,
                remedy.add_flags,
                remedy.build_options,
                remedy.strip_build_options,
                remedy.profiles,
                remedy.force_compiler,
            ]
        )
        if new_record.outcome == BuildOutcome.OK and changes_something:
            triage_mod.save_exception(ctx.world_dir, remedy)
    return list(out.values())
