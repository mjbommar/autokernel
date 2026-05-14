"""Drive the kernel build pipeline.

Two phases:

* :func:`prepare` — drops ``final.config`` into a kernel source tree as
  ``<source>/.config`` and runs ``make olddefconfig`` to canonicalize it.
  Fast (~1s), idempotent, no compilation.

* :func:`build` — runs ``make -j N bindeb-pkg`` (Debian/Ubuntu .deb
  output) inside the prepared source tree. Slow (15-60 min), produces
  ``linux-image-*.deb`` and ``linux-headers-*.deb`` siblings of the source.

Both phases capture every subprocess invocation's stdout/stderr to dated
log files under ``<log_dir>/<step>.{out,err,argv,env}.log``. The ``.argv``
file records the literal argv list and CWD; ``.env`` records the
reproducibility-relevant environment variables. This makes a build
reproducible (or at least diagnosable) after the fact.

Reproducibility:

* ``KBUILD_BUILD_TIMESTAMP``, ``KBUILD_BUILD_USER``, ``KBUILD_BUILD_HOST``
  are pinned by default. The user can override via ``env_overrides``.
* ``SOURCE_DATE_EPOCH`` is set when given so debianize timestamps are
  deterministic.
* If ``ccache`` is on PATH and not disabled, ``CC`` and ``HOSTCC`` are
  wrapped: ``CC="ccache cc"``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from autokernel.audio import AUDIO_KEEP_MODULES


REPRO_TIMESTAMP_DEFAULT = "1970-01-01T00:00:00Z"
REPRO_USER_DEFAULT = "autokernel"
REPRO_HOST_DEFAULT = "autokernel"


@dataclass
class StepResult:
    name: str
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    exit_code: int
    duration_s: float
    stdout_path: Path
    stderr_path: Path

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class PrepareResult:
    source_dir: Path
    config_path: Path  # path to <source>/.config
    log_dir: Path
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)


@dataclass
class BuildResult:
    source_dir: Path
    log_dir: Path
    deb_paths: list[Path] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    target: str = "bindeb-pkg"  # what was built — affects success criterion
    bzimage_path: Path | None = None  # populated for kernel-only

    @property
    def ok(self) -> bool:
        if not all(s.ok for s in self.steps):
            return False
        # `kernel-only` target deliberately skips packaging — success
        # = bzImage actually got built. `auto`/`bindeb-pkg`/`rpm-pkg`/
        # `targz-pkg` need a packaged artifact in deb_paths to count.
        if self.target == "kernel-only":
            return self.bzimage_path is not None and self.bzimage_path.exists()
        return bool(self.deb_paths)


# ── helpers ─────────────────────────────────────────────────────────────────


def _new_log_dir(snapshot_dir: Path) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    p = snapshot_dir / "build" / ts
    p.mkdir(parents=True, exist_ok=True)
    return p


_VALID_COMPILERS = ("clang", "gcc", "llvm")


def _compiler_make_vars(compiler: str) -> list[str]:
    """Make-variable assignments to inject onto the `make` argv.

    The kernel's top-level Makefile reassigns CC unconditionally, so
    setting ``CC=clang`` only in the env doesn't take effect — gcc
    ends up doing the actual compile. We have to pass these as
    command-line make variables so Kbuild honors them.

    Returns a list like ``["CC=clang", "HOSTCC=clang"]`` to splice
    into the argv between ``make`` and the targets.
    """
    if compiler == "llvm":
        # LLVM=1 in env already works because kernel Makefile reads
        # the env LLVM variable. Adding it on argv is harmless and
        # explicit.
        return ["LLVM=1"]
    if compiler == "clang":
        return ["CC=clang", "HOSTCC=clang"]
    if compiler == "gcc":
        return ["CC=gcc", "HOSTCC=gcc"]
    return []


def _build_env(
    *,
    use_ccache: bool,
    env_overrides: dict[str, str] | None,
    compiler: str = "clang",
    lto: str = "none",
) -> dict[str, str]:
    """Compose make's environment.

    ``compiler``:

    * ``"clang"`` — set ``CC=clang HOSTCC=clang`` so make uses clang.
      Kernel-built modules use clang; LLD/binutils stay on GNU.
    * ``"llvm"`` — set ``LLVM=1`` (the kernel build system's flag for
      "use the entire LLVM toolchain": clang, lld, llvm-{ar,nm,objcopy,
      readelf}). Required for clang-LTO and clang-CFI.
    * ``"gcc"`` — set ``CC=gcc HOSTCC=gcc`` (explicit, in case the
      system default is something else).

    ``lto``:

    * ``"none"`` — no LTO (default; fastest builds).
    * ``"thin"`` — adds ``CONFIG_LTO_CLANG_THIN=y`` semantics by
      injecting ``KCFLAGS=-flto=thin`` (caller is also expected to
      enable the matching CONFIG via the propose path or directly).
    * ``"full"`` — same with ``-flto``.

    Note: this function only sets compiler/LTO env. The matching
    ``CONFIG_*`` knobs (CFI_CLANG, LTO_CLANG_*) flow through propose
    or are set directly in final.config.
    """
    if compiler not in _VALID_COMPILERS:
        raise ValueError(f"unknown compiler {compiler!r}; valid: {_VALID_COMPILERS}")

    env = os.environ.copy()
    env.setdefault("KBUILD_BUILD_TIMESTAMP", REPRO_TIMESTAMP_DEFAULT)
    env.setdefault("KBUILD_BUILD_USER", REPRO_USER_DEFAULT)
    env.setdefault("KBUILD_BUILD_HOST", REPRO_HOST_DEFAULT)

    # Compiler selection.
    if compiler == "llvm":
        env["LLVM"] = "1"
    elif compiler == "clang":
        env["CC"] = "clang"
        env["HOSTCC"] = "clang"
    elif compiler == "gcc":
        env["CC"] = "gcc"
        env["HOSTCC"] = "gcc"

    # LTO opt-in. Both flags are additive — the kernel's Kbuild
    # respects KCFLAGS/HOSTCFLAGS for in-tree builds.
    if lto in {"thin", "full"}:
        flag = "-flto=thin" if lto == "thin" else "-flto"
        existing = env.get("KCFLAGS", "")
        env["KCFLAGS"] = (existing + " " + flag).strip()

    if use_ccache and shutil.which("ccache"):
        # Wrap CC; let the kernel's Kbuild detect HOSTCC similarly.
        existing_cc = env.get("CC", "cc")
        if "ccache" not in existing_cc.split():
            env["CC"] = f"ccache {existing_cc}"
        existing_hostcc = env.get("HOSTCC", "cc")
        if "ccache" not in existing_hostcc.split():
            env["HOSTCC"] = f"ccache {existing_hostcc}"

    if env_overrides:
        env.update(env_overrides)
    return env


def _run_step(
    name: str,
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_dir: Path,
    timeout: float | None = None,
) -> StepResult:
    """Run a subprocess, persisting argv/env/stdout/stderr to log_dir."""
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / f"{name}.out.log"
    err_path = log_dir / f"{name}.err.log"
    argv_path = log_dir / f"{name}.argv.log"
    env_path = log_dir / f"{name}.env.log"

    argv_path.write_text(
        f"# cwd: {cwd}\n# timeout: {timeout}\n" + " ".join(repr(a) for a in argv) + "\n"
    )
    env_path.write_text(
        "\n".join(
            f"{k}={v}"
            for k, v in sorted(env.items())
            if k.startswith("KBUILD_")
            or k in {"CC", "HOSTCC", "ARCH", "CROSS_COMPILE", "SOURCE_DATE_EPOCH"}
        )
        + "\n"
    )

    started = datetime.now(UTC)
    try:
        with out_path.open("wb") as outf, err_path.open("wb") as errf:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                env=env,
                stdout=outf,
                stderr=errf,
                timeout=timeout,
                check=False,
            )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -1
        err_path.write_bytes(b"TIMEOUT\n")
    except FileNotFoundError as e:
        rc = -2
        err_path.write_text(f"command not found: {e}\n")

    duration = (datetime.now(UTC) - started).total_seconds()
    return StepResult(
        name=name,
        argv=argv,
        cwd=cwd,
        env=env,
        exit_code=rc,
        duration_s=duration,
        stdout_path=out_path,
        stderr_path=err_path,
    )


# ── prepare ─────────────────────────────────────────────────────────────────


def prepare(
    *,
    source_dir: Path,
    config_path: Path,
    snapshot_dir: Path,
    log_dir: Path | None = None,
    env_overrides: dict[str, str] | None = None,
    olddefconfig_timeout: float = 60.0,
    localmodconfig: bool = False,
    lsmod_path: Path | None = None,
    compiler: str = "clang",
    lto: str = "none",
) -> PrepareResult:
    """Drop ``config_path`` into ``<source_dir>/.config`` and run
    ``make olddefconfig`` to canonicalize.

    Idempotent: re-running with the same inputs produces the same .config.

    When ``localmodconfig=True`` is passed, additionally runs
    ``make LSMOD=<lsmod_path> localmodconfig`` after the initial
    olddefconfig — this disables every tristate module that isn't
    currently loaded on the host. Cuts module count from ~6000 → ~250
    on a stock Ubuntu kernel and reduces build time ~5-10×. Pass
    ``lsmod_path`` to point at the snapshot's lsmod file
    (``<snapshot_dir>/lsmod``); falls back to ``/proc/modules`` when
    ``None``.
    """
    source_dir = Path(source_dir).resolve()
    config_path = Path(config_path).resolve()
    if not source_dir.is_dir():
        raise FileNotFoundError(f"kernel source dir not found: {source_dir}")
    if not (source_dir / "Makefile").exists():
        raise FileNotFoundError(
            f"{source_dir} does not look like a kernel source tree (no Makefile)"
        )
    if not config_path.exists():
        raise FileNotFoundError(f"final.config not found: {config_path}")

    log_dir = log_dir or _new_log_dir(snapshot_dir)
    target = source_dir / ".config"
    shutil.copyfile(config_path, target)

    # Distro-baked-in certificate paths (e.g. CONFIG_SYSTEM_TRUSTED_KEYS=
    # "debian/canonical-certs.pem") only exist inside Ubuntu/Debian's own
    # kernel source tree. Any autokernel-driven build uses kernel.org or
    # apt-get-source or upstream — those paths don't exist and the build
    # will fail with `No rule to make target 'debian/canonical-certs.pem'`.
    # Auto-clear them in-place when the referenced files are absent.
    _strip_missing_distro_cert_paths(target, source_dir)
    # The kernel has two separate module-compression switches:
    # CONFIG_MODULE_COMPRESS selects support/type, while
    # CONFIG_MODULE_COMPRESS_ALL controls whether modules_install actually
    # writes .ko.gz/.ko.xz/.ko.zst files. Ubuntu configs can carry the former
    # without the latter, producing unexpectedly large custom packages.
    _enable_module_compress_all(target)

    env = _build_env(
        use_ccache=False,
        env_overrides=env_overrides,
        compiler=compiler,
        lto=lto,
    )
    compiler_vars = _compiler_make_vars(compiler)
    steps: list[StepResult] = []
    steps.append(
        _run_step(
            "olddefconfig",
            ["make", *compiler_vars, "olddefconfig"],
            cwd=source_dir,
            env=env,
            log_dir=log_dir,
            timeout=olddefconfig_timeout,
        )
    )

    if localmodconfig:
        # Use the snapshot's lsmod when given, else /proc/modules. The copy is
        # augmented with common late-loaded modules that are required by
        # firewall/container/libvirt workflows but may not be loaded at scan time.
        lsmod = str(
            _write_localmodconfig_lsmod(
                snapshot_dir=snapshot_dir,
                lsmod_path=lsmod_path,
            ).resolve()
        )
        lmc_env = dict(env)
        lmc_env["LSMOD"] = lsmod
        # Compose the make argv string: `make CC=clang HOSTCC=clang LSMOD=... localmodconfig`
        make_argv_str = "make " + " ".join(
            compiler_vars + [f"LSMOD={lsmod}", "localmodconfig"]
        )
        # `make localmodconfig` prompts for input on every "new" choice
        # — pipe blank stdin to accept Kconfig defaults uniformly.
        # _run_step doesn't take stdin so we run a small shell command.
        steps.append(
            _run_step(
                "localmodconfig",
                ["sh", "-c", f"yes '' | {make_argv_str}"],
                cwd=source_dir,
                env=lmc_env,
                log_dir=log_dir,
                timeout=olddefconfig_timeout
                * 4,  # localmodconfig is heavier than olddefconfig
            )
        )
        _enable_module_compress_all(target)
        # Re-canonicalize after the trim — localmodconfig doesn't run
        # olddefconfig itself.
        steps.append(
            _run_step(
                "olddefconfig-after-localmodconfig",
                ["make", *compiler_vars, "olddefconfig"],
                cwd=source_dir,
                env=env,
                log_dir=log_dir,
                timeout=olddefconfig_timeout,
            )
        )

    return PrepareResult(
        source_dir=source_dir,
        config_path=target,
        log_dir=log_dir,
        steps=steps,
    )


_DISTRO_CERT_KEYS: tuple[str, ...] = (
    "CONFIG_SYSTEM_TRUSTED_KEYS",
    "CONFIG_SYSTEM_REVOCATION_KEYS",
)


def _enable_module_compress_all(config_path: Path) -> None:
    """Make module compression affect installed module files.

    ``CONFIG_MODULE_COMPRESS=y`` only enables compressed-module support and
    selects an algorithm. Upstream ``modules_install`` emits compressed files
    only when ``CONFIG_MODULE_COMPRESS_ALL=y`` is present. Preserve configs
    that disable module compression entirely.
    """
    text = config_path.read_text()
    lines = text.splitlines(keepends=True)
    if not any(line.strip() == "CONFIG_MODULE_COMPRESS=y" for line in lines):
        return

    changed = False
    found = False
    out_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == "CONFIG_MODULE_COMPRESS_ALL=y":
            found = True
            out_lines.append(line)
            continue
        if stripped == "# CONFIG_MODULE_COMPRESS_ALL is not set" or stripped.startswith(
            "CONFIG_MODULE_COMPRESS_ALL="
        ):
            found = True
            indent = line[: len(line) - len(line.lstrip())]
            nl = "\n" if line.endswith("\n") else ""
            out_lines.append(f"{indent}CONFIG_MODULE_COMPRESS_ALL=y{nl}")
            changed = True
            continue
        out_lines.append(line)

    if not found:
        if out_lines and not out_lines[-1].endswith("\n"):
            out_lines[-1] += "\n"
        out_lines.append("CONFIG_MODULE_COMPRESS_ALL=y\n")
        changed = True

    if changed:
        config_path.write_text("".join(out_lines))


_LOCALMODCONFIG_LATE_LOAD_MODULES: tuple[str, ...] = (
    # libvirt, Docker/Podman, VPNs, Kubernetes, and nft/iptables frontends can
    # request these only when rules are applied after boot. A pure lsmod-driven
    # localmodconfig pass otherwise trims them and leaves firewall/NAT setup
    # failing on the first real boot.
    "br_netfilter",
    "ip6t_REJECT",
    "ip6table_filter",
    "ipt_REJECT",
    "iptable_filter",
    "nf_reject_ipv4",
    "nf_reject_ipv6",
    "nft_reject",
    "nft_reject_bridge",
    "nft_reject_inet",
    "nft_reject_ipv4",
    "nft_reject_ipv6",
    "nft_reject_netdev",
)

_LOCALMODCONFIG_FIREWALL_SOFTWARE_FEATURES: frozenset[str] = frozenset(
    {"containers", "kubernetes", "virtualization", "firewall"}
)

_LOCALMODCONFIG_FIREWALL_RUNTIME_MODULES: frozenset[str] = frozenset(
    {
        "bridge",
        "ip_set",
        "nf_conntrack",
        "nf_nat",
        "nf_tables",
        "nft_compat",
        "overlay",
        "veth",
        "xt_MASQUERADE",
        "xt_addrtype",
        "xt_conntrack",
    }
)


def _write_localmodconfig_lsmod(
    *,
    snapshot_dir: Path,
    lsmod_path: Path | None,
) -> Path:
    source = Path(lsmod_path) if lsmod_path is not None else Path("/proc/modules")
    text = source.read_text(errors="replace")
    lines = text.splitlines()
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("Module "):
            continue
        seen.add(stripped.split()[0])

    out = snapshot_dir / "lsmod.localmodconfig"
    extra_modules = set(_snapshot_hardware_modules(snapshot_dir))
    if _needs_late_firewall_modules(snapshot_dir=snapshot_dir, loaded_modules=seen):
        extra_modules.update(_LOCALMODCONFIG_LATE_LOAD_MODULES)
    if _needs_late_audio_modules(snapshot_dir=snapshot_dir, loaded_modules=seen):
        extra_modules.update(AUDIO_KEEP_MODULES)
    with out.open("w") as fh:
        if lines:
            fh.write("\n".join(lines) + "\n")
        else:
            fh.write("Module Size Used by\n")
        for module in sorted(extra_modules):
            if module not in seen:
                fh.write(f"{module} 0 0\n")
    return out


def _snapshot_hardware_modules(snapshot_dir: Path) -> set[str]:
    modules: set[str] = set()

    for line in _read_snapshot_lines(snapshot_dir / "lspci_vmmnk"):
        if line.startswith("Module:"):
            module = line.split(":", 1)[1].strip()
            if module:
                modules.add(module)

    for line in _read_snapshot_lines(snapshot_dir / "sys_bound_drivers"):
        if "\t" in line:
            driver = line.rsplit("\t", 1)[1].strip()
            if driver:
                modules.add(driver)

    modules.update(_read_snapshot_lines(snapshot_dir / "initramfs_modules"))

    for line in _read_snapshot_lines(snapshot_dir / "module_firmware"):
        if "\t" in line:
            module = line.split("\t", 1)[0].strip()
            if module:
                modules.add(module)

    return modules


def _read_snapshot_lines(path: Path) -> set[str]:
    try:
        text = path.read_text(errors="replace")
    except FileNotFoundError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def _needs_late_firewall_modules(
    *,
    snapshot_dir: Path,
    loaded_modules: set[str],
) -> bool:
    if loaded_modules & _LOCALMODCONFIG_FIREWALL_RUNTIME_MODULES:
        return True

    software_features = snapshot_dir / "software_features"
    try:
        lines = software_features.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return False

    for line in lines:
        feature = line.split("\t", 1)[0].strip()
        if feature in _LOCALMODCONFIG_FIREWALL_SOFTWARE_FEATURES:
            return True
    return False


def _needs_late_audio_modules(
    *,
    snapshot_dir: Path,
    loaded_modules: set[str],
) -> bool:
    if any(_looks_like_audio_module(module) for module in loaded_modules):
        return True

    for line in _read_snapshot_lines(snapshot_dir / "lspci_vmmnk"):
        if line.startswith("Class:") and line.split(":", 1)[1].strip().startswith(
            ("0401", "0403")
        ):
            return True
        if line.startswith("Device:") and "audio" in line.lower():
            return True
        if line.startswith("Driver:") and _looks_like_audio_module(
            line.split(":", 1)[1].strip()
        ):
            return True

    asound_cards = "\n".join(_read_snapshot_lines(snapshot_dir / "asound_cards"))
    if asound_cards and "no soundcards" not in asound_cards.lower():
        return True

    if _read_snapshot_lines(snapshot_dir / "asound_pcm"):
        return True

    for line in _read_snapshot_lines(snapshot_dir / "dev_snd"):
        if any(token in line for token in ("controlC", "pcmC", "seq", "timer")):
            return True

    for line in _read_snapshot_lines(snapshot_dir / "software_features"):
        feature = line.split("\t", 1)[0].strip()
        if feature in {"audio", "desktop", "bluetooth"}:
            return True

    return False


def _looks_like_audio_module(module: str) -> bool:
    lower = module.lower().replace("-", "_")
    return lower.startswith(("snd", "soundwire")) or any(
        frag in lower for frag in ("_sof", "sof_", "hda", "sdw", "sdca")
    )


def _strip_missing_distro_cert_paths(config_path: Path, source_dir: Path) -> None:
    """Empty-out cert-path Kconfigs whose referenced .pem doesn't exist.

    Ubuntu's running config carries
    ``CONFIG_SYSTEM_TRUSTED_KEYS="debian/canonical-certs.pem"`` (and the
    matching revocation path). Those files only exist inside Ubuntu's own
    kernel source. Building from kernel.org or apt-get-source against this
    config dies with::

        make[3]: *** No rule to make target 'debian/canonical-certs.pem'

    For any path-to-pem this helper hits, if the .pem isn't on disk
    relative to ``source_dir``, replace it with the empty string. Leaves
    intact any path that DOES exist (so e.g. a user's own signing key
    survives), and leaves intact absolute paths that exist.
    """
    text = config_path.read_text()
    out_lines: list[str] = []
    changed = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if not any(stripped.startswith(f"{k}=") for k in _DISTRO_CERT_KEYS):
            out_lines.append(line)
            continue
        # Parse: CONFIG_X="path"
        eq = stripped.find("=")
        if eq < 0 or '"' not in stripped:
            out_lines.append(line)
            continue
        rhs = stripped[eq + 1 :].strip().rstrip("\n")
        if not (rhs.startswith('"') and rhs.endswith('"')):
            out_lines.append(line)
            continue
        path_str = rhs[1:-1]
        if not path_str:
            out_lines.append(line)
            continue
        candidate = Path(path_str)
        if not candidate.is_absolute():
            candidate = source_dir / candidate
        if candidate.exists():
            out_lines.append(line)
            continue
        # Replace with empty string; preserve key + indent + any trailing
        # newline.
        sym = stripped.split("=", 1)[0]
        indent = line[: len(line) - len(stripped)]
        nl = "\n" if line.endswith("\n") else ""
        out_lines.append(f'{indent}{sym}=""{nl}')
        changed = True
    if changed:
        config_path.write_text("".join(out_lines))


# ── build ───────────────────────────────────────────────────────────────────


def build(
    *,
    source_dir: Path,
    snapshot_dir: Path,
    jobs: int | None = None,
    use_ccache: bool = True,
    env_overrides: dict[str, str] | None = None,
    log_dir: Path | None = None,
    target: str = "bindeb-pkg",
    timeout: float | None = None,
    compiler: str = "clang",
    lto: str = "none",
) -> BuildResult:
    """Run ``make -j N <target>`` in the prepared source tree.

    Returns a :class:`BuildResult` with ``deb_paths`` populated for any
    ``linux-*.deb`` files produced as siblings of the source dir (where
    Debian/Ubuntu's ``bindeb-pkg`` puts them).
    """
    source_dir = Path(source_dir).resolve()
    if not (source_dir / ".config").exists():
        raise FileNotFoundError(f"{source_dir} has no .config — run prepare() first.")

    if jobs is None:
        jobs = os.cpu_count() or 4

    log_dir = log_dir or _new_log_dir(snapshot_dir)
    env = _build_env(
        use_ccache=use_ccache,
        env_overrides=env_overrides,
        compiler=compiler,
        lto=lto,
    )
    compiler_vars = _compiler_make_vars(compiler)

    # Special-case "kernel-only" — runs `make bzImage modules` (no
    # packaging). Used by `iterate --execute` where we just need to
    # know the kernel built and boots; we don't need a .deb. Saves
    # the install-time deps (debhelper-compat etc.) and is the
    # closest analog to what users do during interactive kernel work.
    if target == "kernel-only":
        argv = ["make", f"-j{jobs}", *compiler_vars, "bzImage", "modules"]
        step_name = "make-bzImage-modules"
    else:
        argv = ["make", f"-j{jobs}", *compiler_vars, target]
        step_name = f"make-{target}"

    step = _run_step(
        step_name,
        argv,
        cwd=source_dir,
        env=env,
        log_dir=log_dir,
        timeout=timeout,
    )

    # Output package layout differs per target. bindeb-pkg lands .debs in
    # the parent of source_dir; rpm-pkg lands .rpms in
    # ~/rpmbuild/RPMS/<arch>/; targz-pkg writes a tarball into the parent.
    deb_dir = source_dir.parent
    deb_paths: list[Path] = []
    deb_paths.extend(sorted(deb_dir.glob("linux-*.deb")))
    deb_paths.extend(sorted(deb_dir.glob("kernel-*.tar.gz")))
    deb_paths.extend(sorted(deb_dir.glob("linux-*.tar.gz")))
    deb_paths.extend(sorted(deb_dir.glob("linux-*.tar.zst")))

    rpm_root = Path.home() / "rpmbuild" / "RPMS"
    if rpm_root.is_dir():
        deb_paths.extend(sorted(rpm_root.glob("*/kernel-*.rpm")))

    # bzImage location for kernel-only success check.
    bzimage_path: Path | None = None
    for arch in ("x86", "arm64", "riscv", "powerpc"):
        candidate = source_dir / "arch" / arch / "boot" / "bzImage"
        if candidate.exists():
            bzimage_path = candidate
            break

    return BuildResult(
        source_dir=source_dir,
        log_dir=log_dir,
        deb_paths=deb_paths,
        steps=[step],
        target=target,
        bzimage_path=bzimage_path,
    )
