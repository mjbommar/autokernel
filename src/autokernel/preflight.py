"""Pre-flight checks: catch system-level problems before a verb does work.

Building a Linux kernel reliably requires a non-trivial set of tools,
libraries, and free resources. Without preflight, a user can wait 30
minutes only to have ``make`` fail because ``flex`` is missing or because
``dwarves`` doesn't include ``pahole``. Preflight runs in seconds and
emits actionable hints in the user's own distro language.

Design:

* A **check** is a callable that takes :class:`CheckContext` and returns
  exactly one :class:`CheckResult`. No side effects.
* Each check declares its **tags** — labels for which verb(s) need it.
  ``run_checks(tags={"build"})`` runs only the build-relevant subset.
* Severities are PASS, WARN, FAIL, SKIP. ``run_checks`` returns a
  :class:`CheckRun` with summary counts so the CLI can decide exit codes.

Distro awareness:

When a check needs to recommend a remediation it consults the
:class:`autokernel.distro.DistroSpec` to phrase the fix in the local
package manager's terms — ``apt install`` on Debian, ``dnf install`` on
Fedora, etc. Checks that depend on resources only some distros provide
(``dwarves`` is named ``pahole`` on Arch core, etc.) handle the variance.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from autokernel.distro import DistroInfo, DistroSpec, Family, detect, spec_for
from autokernel.models import Snapshot


# ── data model ──────────────────────────────────────────────────────────────


class Severity(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    name: str
    severity: Severity
    message: str
    fix_hint: str | None = None
    details: dict | None = None


@dataclass
class CheckContext:
    """Inputs available to every check.

    ``snapshot`` is optional: checks that don't need it shouldn't read it
    so the same check runs identically before scan and after.
    """

    distro: DistroInfo
    spec: DistroSpec
    snapshot: Snapshot | None = None


@dataclass
class CheckRun:
    results: list[CheckResult] = field(default_factory=list)

    def by_severity(self, severity: Severity) -> list[CheckResult]:
        return [r for r in self.results if r.severity == severity]

    @property
    def has_failures(self) -> bool:
        return any(r.severity == Severity.FAIL for r in self.results)

    @property
    def has_warnings(self) -> bool:
        return any(r.severity == Severity.WARN for r in self.results)


# ── individual check implementations ────────────────────────────────────────


# Each check returns a CheckResult and declares its tags via the registry below.
# Tags drive the --for switch on the CLI.

CheckFn = Callable[[CheckContext], CheckResult]


def _which(name: str) -> str | None:
    return shutil.which(name)


def _install_hint(spec: DistroSpec, packages: list[str]) -> str | None:
    """Format an install command for the user's distro, or None when the
    spec doesn't have one (e.g. UNKNOWN family)."""
    if not spec.install_cmd or not packages:
        return None
    sudo = "sudo " if os.geteuid() != 0 and spec.family != Family.UNKNOWN else ""
    return f"{sudo}{' '.join(spec.install_cmd)} {' '.join(packages)}"


def check_distro_recognized(ctx: CheckContext) -> CheckResult:
    """Identify the distro family we're running on."""
    if ctx.distro.family == Family.UNKNOWN:
        return CheckResult(
            name="distro_recognized",
            severity=Severity.WARN,
            message=f"distro not recognized (id={ctx.distro.id!r}); using generic defaults",
            fix_hint=(
                "Most verbs still work but distro-specific helpers (fetch-source, "
                "build-deps install) won't. File an issue with /etc/os-release contents."
            ),
        )
    return CheckResult(
        name="distro_recognized",
        severity=Severity.PASS,
        message=f"{ctx.distro.pretty_name or ctx.distro.id} (family: {ctx.distro.family.value})",
    )


def check_python_version(ctx: CheckContext) -> CheckResult:
    import sys
    v = sys.version_info
    if (v.major, v.minor) < (3, 12):
        return CheckResult(
            name="python_version",
            severity=Severity.FAIL,
            message=f"python {v.major}.{v.minor}; autokernel needs >= 3.12",
            fix_hint="install python 3.12+ or use `uv run --python 3.12`",
        )
    return CheckResult(
        name="python_version",
        severity=Severity.PASS,
        message=f"python {v.major}.{v.minor}.{v.micro}",
    )


def check_free_disk_space(ctx: CheckContext, path: str = "/tmp", *, warn_gb: float = 20, fail_gb: float = 5) -> CheckResult:
    """Need ~30GB to build a full kernel comfortably; /tmp is the typical
    scratch location."""
    try:
        st = os.statvfs(path)
    except OSError as e:
        return CheckResult(
            name="free_disk_space",
            severity=Severity.WARN,
            message=f"can't stat {path}: {e}",
        )
    free_gb = (st.f_bavail * st.f_frsize) / (1024**3)
    if free_gb < fail_gb:
        return CheckResult(
            name="free_disk_space",
            severity=Severity.FAIL,
            message=f"{path}: {free_gb:.1f} GB free (need >= {fail_gb} GB)",
            fix_hint=f"free disk space, or build to a different path (--build-dir)",
            details={"path": path, "free_gb": free_gb},
        )
    if free_gb < warn_gb:
        return CheckResult(
            name="free_disk_space",
            severity=Severity.WARN,
            message=f"{path}: {free_gb:.1f} GB free (a full kernel build can use ~25 GB)",
            fix_hint=f"clear ~{warn_gb - free_gb:.0f} GB if you plan to build",
        )
    return CheckResult(
        name="free_disk_space",
        severity=Severity.PASS,
        message=f"{path}: {free_gb:.1f} GB free",
    )


def check_free_ram(ctx: CheckContext, *, warn_gb: float = 4, fail_gb: float = 2) -> CheckResult:
    """Modern kernel builds with LTO can use 4 GB+ during link. <2 GB on a
    real build will OOM."""
    try:
        with open("/proc/meminfo") as f:
            mem_kb = next(int(line.split()[1]) for line in f if line.startswith("MemTotal:"))
    except (FileNotFoundError, StopIteration):
        return CheckResult(
            name="free_ram",
            severity=Severity.SKIP,
            message="/proc/meminfo unavailable",
        )
    total_gb = mem_kb / (1024**2)
    if total_gb < fail_gb:
        return CheckResult(
            name="free_ram",
            severity=Severity.FAIL,
            message=f"{total_gb:.1f} GB RAM; minimum recommended {fail_gb}",
            fix_hint="add swap with `sudo dd if=/dev/zero of=/swapfile bs=1M count=8192 && sudo mkswap /swapfile && sudo swapon /swapfile`",
        )
    if total_gb < warn_gb:
        return CheckResult(
            name="free_ram",
            severity=Severity.WARN,
            message=f"{total_gb:.1f} GB RAM (build may swap; consider --jobs 1)",
        )
    return CheckResult(
        name="free_ram",
        severity=Severity.PASS,
        message=f"{total_gb:.1f} GB RAM",
    )


def check_cpu_cores(ctx: CheckContext) -> CheckResult:
    n = os.cpu_count() or 0
    return CheckResult(
        name="cpu_cores",
        severity=Severity.PASS if n > 0 else Severity.WARN,
        message=f"{n} CPU(s) — `make -j{n}` is the default",
    )


# ── build tool / library checks ─────────────────────────────────────────────


# Required tools — missing any is a FAIL for the build verb.
_REQUIRED_BUILD_TOOLS = ["gcc", "make", "ld", "flex", "bison", "bc", "perl", "awk", "tar"]
_RECOMMENDED_TOOLS = ["ccache", "pahole"]


def check_build_tools(ctx: CheckContext) -> CheckResult:
    """All hard-required build executables on PATH."""
    missing = [t for t in _REQUIRED_BUILD_TOOLS if _which(t) is None]
    if not missing:
        return CheckResult(
            name="build_tools",
            severity=Severity.PASS,
            message=f"all required tools on PATH ({len(_REQUIRED_BUILD_TOOLS)})",
        )

    # Map missing executable → distro package name(s).
    pkg_map_debian = {
        "gcc": "build-essential", "make": "build-essential", "ld": "binutils",
        "flex": "flex", "bison": "bison", "bc": "bc", "perl": "perl",
        "awk": "gawk", "tar": "tar",
    }
    pkg_map_fedora = {
        "gcc": "gcc", "make": "make", "ld": "binutils",
        "flex": "flex", "bison": "bison", "bc": "bc", "perl": "perl",
        "awk": "gawk", "tar": "tar",
    }
    pkg_map_arch = {
        "gcc": "base-devel", "make": "base-devel", "ld": "base-devel",
        "flex": "flex", "bison": "bison", "bc": "bc", "perl": "perl",
        "awk": "gawk", "tar": "tar",
    }
    fam_map = {
        Family.DEBIAN: pkg_map_debian,
        Family.FEDORA: pkg_map_fedora,
        Family.ARCH: pkg_map_arch,
    }
    pkg_map = fam_map.get(ctx.spec.family, pkg_map_debian)
    pkgs = sorted({pkg_map.get(t, t) for t in missing})
    return CheckResult(
        name="build_tools",
        severity=Severity.FAIL,
        message=f"missing required build tools: {', '.join(missing)}",
        fix_hint=_install_hint(ctx.spec, pkgs),
        details={"missing": missing},
    )


def check_recommended_tools(ctx: CheckContext) -> CheckResult:
    """Tools that improve the build but aren't strictly required."""
    missing = [t for t in _RECOMMENDED_TOOLS if _which(t) is None]
    if not missing:
        return CheckResult(
            name="recommended_tools",
            severity=Severity.PASS,
            message="ccache + pahole on PATH",
        )
    pkg_for = {
        Family.DEBIAN: {"ccache": "ccache", "pahole": "dwarves"},
        Family.FEDORA: {"ccache": "ccache", "pahole": "dwarves"},
        Family.ARCH:   {"ccache": "ccache", "pahole": "pahole"},
        Family.SUSE:   {"ccache": "ccache", "pahole": "dwarves"},
    }.get(ctx.spec.family, {"ccache": "ccache", "pahole": "dwarves"})
    pkgs = sorted({pkg_for.get(t, t) for t in missing})
    msg = f"recommended tools missing: {', '.join(missing)}"
    if "pahole" in missing:
        msg += " (pahole: needed for CONFIG_DEBUG_INFO_BTF on modern kernels)"
    return CheckResult(
        name="recommended_tools",
        severity=Severity.WARN,
        message=msg,
        fix_hint=_install_hint(ctx.spec, pkgs),
    )


def check_kernel_dev_libs(ctx: CheckContext) -> CheckResult:
    """Headers we can't easily probe via `which`: openssl, elfutils, ncurses."""
    fam = ctx.spec.family
    pkgs = {
        Family.DEBIAN: ["libssl-dev", "libelf-dev", "libncurses-dev"],
        Family.FEDORA: ["openssl-devel", "elfutils-libelf-devel", "ncurses-devel"],
        Family.ARCH:   ["openssl", "libelf", "ncurses"],
        Family.SUSE:   ["libopenssl-devel", "libelf-devel", "ncurses-devel"],
    }.get(fam)
    if pkgs is None:
        return CheckResult(
            name="kernel_dev_libs",
            severity=Severity.SKIP,
            message=f"library check not implemented for family {fam.value}",
        )

    missing = _query_packages_missing(fam, pkgs)
    if not missing:
        return CheckResult(
            name="kernel_dev_libs",
            severity=Severity.PASS,
            message=f"all dev libs installed ({len(pkgs)})",
        )
    return CheckResult(
        name="kernel_dev_libs",
        severity=Severity.FAIL,
        message=f"missing dev libs: {', '.join(missing)}",
        fix_hint=_install_hint(ctx.spec, missing),
        details={"missing": missing},
    )


def _query_packages_missing(family: Family, pkgs: list[str]) -> list[str]:
    """Probe each package via the family's query command. Conservative:
    if the query itself fails (no root, missing tool), assume installed
    so we don't throw spurious FAILs at users who actually have the libs.
    """
    if family == Family.DEBIAN:
        cmd = ["dpkg-query", "-W", "-f=${Package} ${Status}\n"]
    elif family == Family.FEDORA:
        cmd = ["rpm", "-qa", "--queryformat=%{NAME}\n"]
    elif family == Family.ARCH:
        cmd = ["pacman", "-Qq"]
    elif family == Family.SUSE:
        cmd = ["rpm", "-qa", "--queryformat=%{NAME}\n"]
    else:
        return []

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    out = result.stdout.lower()
    missing: list[str] = []
    for p in pkgs:
        # dpkg-query lines look like "libssl-dev install ok installed"; grep the
        # package name + "install ok installed".
        if family == Family.DEBIAN:
            if not any(
                p == ln.split(" ", 1)[0] and "install ok installed" in ln
                for ln in out.splitlines()
            ):
                missing.append(p)
        else:
            if p.lower() not in out.split():
                # also try plain substring (rpm output has version suffixes)
                if p.lower() not in out:
                    missing.append(p)
    return missing


# ── system config / runtime ─────────────────────────────────────────────────


def check_dmesg_readable(ctx: CheckContext) -> CheckResult:
    """Ubuntu sets ``kernel.dmesg_restrict=1`` by default, so unprivileged
    users can't read dmesg. ``scan`` falls back to ``journalctl -k``, but
    we surface this so the user knows."""
    try:
        with open("/proc/sys/kernel/dmesg_restrict") as f:
            restricted = f.read().strip() == "1"
    except OSError:
        return CheckResult(
            name="dmesg_readable",
            severity=Severity.SKIP,
            message="/proc/sys/kernel/dmesg_restrict unavailable",
        )
    if not restricted or os.geteuid() == 0:
        return CheckResult(
            name="dmesg_readable",
            severity=Severity.PASS,
            message="dmesg readable",
        )
    return CheckResult(
        name="dmesg_readable",
        severity=Severity.WARN,
        message="kernel.dmesg_restrict=1 — `scan` falls back to journalctl -k",
        fix_hint="run scan as root for richer firmware detection, or accept the journal fallback",
    )


def check_secure_boot(ctx: CheckContext) -> CheckResult:
    """Custom kernels need a signed boot path (or sb disabled)."""
    if not Path("/sys/firmware/efi").exists():
        return CheckResult(name="secure_boot", severity=Severity.SKIP, message="not an EFI boot")
    mokutil = _which("mokutil")
    if mokutil is None:
        return CheckResult(
            name="secure_boot",
            severity=Severity.WARN,
            message="EFI boot but mokutil not installed — can't determine SB state",
            fix_hint=_install_hint(ctx.spec, ["mokutil"]),
        )
    try:
        out = subprocess.run(
            [mokutil, "--sb-state"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except subprocess.TimeoutExpired:
        return CheckResult(name="secure_boot", severity=Severity.SKIP, message="mokutil timed out")
    if "SecureBoot enabled" in out:
        return CheckResult(
            name="secure_boot",
            severity=Severity.WARN,
            message="Secure Boot ENABLED — custom kernel needs MOK enrollment or sb=disabled",
            fix_hint="see Arch wiki for sbctl / shim+MOK; otherwise disable Secure Boot in firmware",
        )
    if "SecureBoot disabled" in out:
        return CheckResult(name="secure_boot", severity=Severity.PASS, message="Secure Boot disabled")
    return CheckResult(name="secure_boot", severity=Severity.SKIP, message=out.strip()[:120])


def check_root_or_sudo(ctx: CheckContext) -> CheckResult:
    """Some verbs (install, source-package install) need root. We don't
    require it for the *current* run but warn that some operations will."""
    if os.geteuid() == 0:
        return CheckResult(name="root_or_sudo", severity=Severity.PASS, message="running as root")
    if _which("sudo"):
        return CheckResult(
            name="root_or_sudo",
            severity=Severity.PASS,
            message="not root; sudo available for operations that need it",
        )
    return CheckResult(
        name="root_or_sudo",
        severity=Severity.WARN,
        message="not root and no sudo on PATH",
        fix_hint="install / install-source operations will fail without root or sudo",
    )


# ── install-specific checks ─────────────────────────────────────────────────


def check_bootloader_supported(ctx: CheckContext) -> CheckResult:
    """`autokernel install` v1 supports GRUB2 only. Detect what's actually
    on this host and pass/warn/fail accordingly."""
    from autokernel.bootloader import BootloaderKind, detect

    bl = detect()
    if bl.kind == BootloaderKind.GRUB2:
        return CheckResult(
            name="bootloader_supported",
            severity=Severity.PASS,
            message=f"GRUB2 detected ({bl.detected_via})",
        )
    if bl.kind == BootloaderKind.UNKNOWN:
        return CheckResult(
            name="bootloader_supported",
            severity=Severity.FAIL,
            message="no known bootloader detected",
            fix_hint=(
                "ensure /boot is mounted and GRUB is installed; otherwise install "
                "the kernel package manually and use the bootloader's own one-shot "
                "mechanism"
            ),
        )
    return CheckResult(
        name="bootloader_supported",
        severity=Severity.FAIL,
        message=f"bootloader {bl.kind.value!r} not yet supported by `autokernel install`",
        fix_hint=(
            f"v1 supports GRUB2 only. For {bl.kind.value}, install the .deb/.rpm "
            f"manually and set boot one-shot via the bootloader's own tool "
            f"(e.g. `bootctl set-oneshot` for systemd-boot)"
        ),
    )


def check_boot_writable(ctx: CheckContext) -> CheckResult:
    """The install verb needs to write to /boot (kernel + initramfs land
    there) and rebuild the bootloader config. As a non-root user this
    will always show as not-writable; the FAIL severity is correct only
    when running install."""
    boot = Path("/boot")
    if not boot.exists():
        return CheckResult(
            name="boot_writable",
            severity=Severity.FAIL,
            message="/boot does not exist",
            fix_hint="mount the boot partition or check `/etc/fstab`",
        )
    if os.geteuid() == 0 and os.access(boot, os.W_OK):
        return CheckResult(
            name="boot_writable",
            severity=Severity.PASS,
            message="/boot writable as root",
        )
    if os.geteuid() == 0:
        return CheckResult(
            name="boot_writable",
            severity=Severity.FAIL,
            message="/boot is not writable even as root (read-only mount?)",
            fix_hint="`mount -o remount,rw /boot`",
        )
    return CheckResult(
        name="boot_writable",
        severity=Severity.WARN,
        message="not root; can't probe /boot writability",
        fix_hint="install steps that touch /boot need sudo or root",
    )


def check_fallback_kernel_present(ctx: CheckContext) -> CheckResult:
    """Probation needs a fallback kernel to boot if the new one fails.

    We count vmlinuz files in /boot. The currently-running kernel (uname -r)
    is one of them; we want at LEAST one *other*. With only one kernel, a
    bad install would leave the user unable to boot.
    """
    boot = Path("/boot")
    if not boot.exists():
        return CheckResult(
            name="fallback_kernel_present",
            severity=Severity.SKIP,
            message="/boot not present",
        )
    try:
        running = os.uname().release
    except OSError:
        running = ""

    vmlinuz = sorted(boot.glob("vmlinuz-*")) + sorted(boot.glob("vmlinuz"))
    other = [p for p in vmlinuz if running not in p.name]

    if not vmlinuz:
        return CheckResult(
            name="fallback_kernel_present",
            severity=Severity.WARN,
            message="no vmlinuz-* files in /boot",
            fix_hint="install at least one distro-provided kernel as a fallback",
        )
    if not other:
        return CheckResult(
            name="fallback_kernel_present",
            severity=Severity.WARN,
            message=f"only one kernel installed ({vmlinuz[0].name}); no fallback",
            fix_hint=(
                "if the new kernel fails to boot you'll have no recovery path. "
                "install a distro kernel as fallback before proceeding"
            ),
        )
    return CheckResult(
        name="fallback_kernel_present",
        severity=Severity.PASS,
        message=f"{len(vmlinuz)} kernel(s) in /boot ({len(other)} fallback(s))",
    )


def check_grub_tools(ctx: CheckContext) -> CheckResult:
    """`grub-reboot` (Debian) or `grub2-reboot` (Fedora) must be on PATH."""
    deb = _which("grub-reboot")
    fed = _which("grub2-reboot")
    if deb or fed:
        which = "grub-reboot" if deb else "grub2-reboot"
        return CheckResult(
            name="grub_tools",
            severity=Severity.PASS,
            message=f"{which} available",
        )
    return CheckResult(
        name="grub_tools",
        severity=Severity.FAIL,
        message="neither grub-reboot nor grub2-reboot on PATH",
        fix_hint=_install_hint(ctx.spec, ["grub2-common"] if ctx.spec.family == Family.DEBIAN else ["grub2-tools"]),
    )


# ── snapshot-aware checks ───────────────────────────────────────────────────


def check_snapshot_running_config(ctx: CheckContext) -> CheckResult:
    if ctx.snapshot is None:
        return CheckResult(name="snapshot_running_config", severity=Severity.SKIP, message="no snapshot")
    if ctx.snapshot.running_config_path and ctx.snapshot.running_config_path.exists():
        return CheckResult(
            name="snapshot_running_config",
            severity=Severity.PASS,
            message=f"running .config: {ctx.snapshot.running_config_path}",
        )
    return CheckResult(
        name="snapshot_running_config",
        severity=Severity.FAIL,
        message="snapshot has no running_config",
        fix_hint=(
            "rerun `autokernel scan` on a host with /proc/config.gz or "
            "/boot/config-$(uname -r) readable"
        ),
    )


def check_snapshot_modinfo(ctx: CheckContext) -> CheckResult:
    if ctx.snapshot is None:
        return CheckResult(name="snapshot_modinfo", severity=Severity.SKIP, message="no snapshot")
    p = ctx.snapshot.modules_builtin_modinfo_path
    if p and p.exists():
        return CheckResult(name="snapshot_modinfo", severity=Severity.PASS, message=f"modules.builtin.modinfo: {p}")
    return CheckResult(
        name="snapshot_modinfo",
        severity=Severity.WARN,
        message="modules.builtin.modinfo not found — built-in module → CONFIG mapping will be weaker",
    )


def check_snapshot_dkms_clean(ctx: CheckContext) -> CheckResult:
    if ctx.snapshot is None:
        return CheckResult(name="snapshot_dkms_clean", severity=Severity.SKIP, message="no snapshot")
    if not ctx.snapshot.dkms:
        return CheckResult(name="snapshot_dkms_clean", severity=Severity.PASS, message="no DKMS modules")
    names = ", ".join(d.name for d in ctx.snapshot.dkms)
    return CheckResult(
        name="snapshot_dkms_clean",
        severity=Severity.WARN,
        message=f"DKMS modules present: {names} (must rebuild against the new kernel)",
        fix_hint="verify each rebuilds before --execute on autokernel build",
    )


# ── registry + dispatch ─────────────────────────────────────────────────────


@dataclass
class _Registered:
    fn: CheckFn
    tags: frozenset[str]


# Tag conventions:
#   "always"  — run for every verb
#   "scan"    — relevant before/during scan
#   "propose" — relevant before/during propose
#   "build"   — relevant before/during build
#   "install" — relevant before install (future)
#   "snapshot" — needs a Snapshot in the context
_REGISTRY: list[tuple[str, _Registered]] = [
    ("distro_recognized", _Registered(check_distro_recognized, frozenset({"always"}))),
    ("python_version",    _Registered(check_python_version,    frozenset({"always"}))),
    ("cpu_cores",         _Registered(check_cpu_cores,         frozenset({"always", "build"}))),
    ("free_ram",          _Registered(check_free_ram,          frozenset({"always", "build"}))),
    ("free_disk_space",   _Registered(check_free_disk_space,   frozenset({"build"}))),
    ("dmesg_readable",    _Registered(check_dmesg_readable,    frozenset({"scan"}))),
    ("secure_boot",       _Registered(check_secure_boot,       frozenset({"build", "install"}))),
    ("root_or_sudo",      _Registered(check_root_or_sudo,      frozenset({"install"}))),
    ("build_tools",       _Registered(check_build_tools,       frozenset({"build"}))),
    ("recommended_tools", _Registered(check_recommended_tools, frozenset({"build"}))),
    ("kernel_dev_libs",   _Registered(check_kernel_dev_libs,   frozenset({"build"}))),
    ("snapshot_running_config", _Registered(check_snapshot_running_config, frozenset({"propose", "apply", "snapshot"}))),
    ("snapshot_modinfo", _Registered(check_snapshot_modinfo, frozenset({"propose", "snapshot"}))),
    ("snapshot_dkms_clean", _Registered(check_snapshot_dkms_clean, frozenset({"build", "install", "snapshot"}))),
    # install-specific
    ("bootloader_supported", _Registered(check_bootloader_supported, frozenset({"install"}))),
    ("boot_writable", _Registered(check_boot_writable, frozenset({"install"}))),
    ("fallback_kernel_present", _Registered(check_fallback_kernel_present, frozenset({"install"}))),
    ("grub_tools", _Registered(check_grub_tools, frozenset({"install"}))),
]


def run_checks(
    *,
    tags: set[str] | None = None,
    snapshot: Snapshot | None = None,
    distro: DistroInfo | None = None,
) -> CheckRun:
    """Run the subset of registered checks matching ``tags``.

    ``tags`` is OR-matched against each check's tag set. ``None`` means
    "run everything except snapshot-only checks unless a snapshot is given".
    A check with the ``snapshot`` tag is always skipped when ``snapshot``
    is None; the SKIP result is included so the consumer sees what was
    omitted.
    """
    info = distro or detect()
    spec = spec_for(info)
    ctx = CheckContext(distro=info, spec=spec, snapshot=snapshot)

    out = CheckRun()
    for _name, reg in _REGISTRY:
        if tags is not None and reg.tags.isdisjoint(tags) and "always" not in reg.tags:
            continue
        if "snapshot" in reg.tags and snapshot is None:
            out.results.append(CheckResult(
                name=_name, severity=Severity.SKIP,
                message="needs snapshot (pass SNAPSHOT_DIR to preflight)",
            ))
            continue
        out.results.append(reg.fn(ctx))
    return out
