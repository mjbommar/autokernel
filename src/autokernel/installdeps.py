"""Distro-aware system-dependency installer for autokernel.

A user who just ran the autokernel installer wants one command to
install every package they need to build + boot-test + install a
custom kernel. ``preflight`` already tells them what's missing in
distro-specific terms (``sudo apt install -y flex bison …``); this
module turns that into a verb the user can run instead of copying.

Design is parallel to ``install.py``:

1. ``plan()`` composes a typed :class:`InstallDepsPlan` from
   ``(distro, target)``. Pure logic; no subprocess.
2. ``execute()`` shells out to the package manager via ``sudo`` (with
   the user's password prompt) and writes a log.
3. The ``--for`` target is one of:
   * ``build`` — kernel build deps (``DistroSpec.build_deps``)
   * ``boot-test`` — qemu (and optionally pip-install virtme-ng)
   * ``install`` — bootloader tools (usually already present)
   * ``all`` — union of the above

Recommended (non-required) packages are included by default but can
be skipped with ``--no-recommended``. Most useful: ``ccache``
(rebuild caching) and ``dwarves``/``pahole`` (CONFIG_DEBUG_INFO_BTF
pre-req on modern kernels).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from autokernel.distro import DistroInfo, DistroSpec, Family


class Target(str, Enum):
    BUILD = "build"
    BOOT_TEST = "boot-test"
    INSTALL = "install"
    ALL = "all"


# ── per-family extras (beyond DistroSpec.build_deps) ───────────────────────


# QEMU package names per family. The kernel-only boot-test path needs
# qemu-system-x86_64 to be on PATH; this maps to the right package.
_QEMU_PKG: dict[Family, list[str]] = {
    Family.DEBIAN: ["qemu-system-x86"],
    Family.FEDORA: ["qemu-system-x86"],
    Family.ARCH: ["qemu-base"],
    Family.SUSE: ["qemu-x86"],
    Family.GENTOO: ["app-emulation/qemu"],
    Family.ALPINE: ["qemu-system-x86_64"],
    Family.UNKNOWN: [],
    Family.NIXOS: [],
}


# Recommended-but-not-required packages, by family. ccache is universal;
# pahole comes from `dwarves` on most distros, but Arch's package is
# named pahole.
_RECOMMENDED_PKG: dict[Family, list[str]] = {
    Family.DEBIAN: ["ccache"],
    Family.FEDORA: ["ccache"],
    Family.ARCH: ["ccache"],
    Family.SUSE: ["ccache"],
    Family.GENTOO: ["dev-util/ccache"],
    Family.ALPINE: ["ccache"],
    Family.UNKNOWN: [],
    Family.NIXOS: [],
}


# Bootloader tooling required for `install --execute`. Usually already
# pulled in by the base system but listed for completeness so a
# stripped-down container has a way to install it.
_BOOTLOADER_PKG: dict[Family, list[str]] = {
    Family.DEBIAN: ["grub2-common"],
    Family.FEDORA: ["grub2-tools"],
    Family.ARCH: ["grub"],
    Family.SUSE: ["grub2"],
    Family.GENTOO: [],
    Family.ALPINE: ["grub-bios"],
    Family.UNKNOWN: [],
    Family.NIXOS: [],
}


# ── data ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InstallDepsPlan:
    family: Family
    target: Target
    install_cmd: list[str]
    """Argv prefix for the package install (``apt install -y`` etc.)."""

    requested: list[str]
    """All packages that should be installed for this target."""

    missing: list[str]
    """Subset of ``requested`` that probing said is *not* installed."""

    already_installed: list[str]
    """Subset of ``requested`` that probing said is already installed."""

    optional_python_pkgs: list[str] = field(default_factory=list)
    """``uv tool install`` candidates (e.g. virtme-ng); shown separately
    so the user can choose to skip them. We use ``uv tool install``
    rather than ``pip install --user`` because:

    * autokernel's install.sh already requires uv, so it's always
      available;
    * ``uv tool install`` puts the tool in an isolated environment
      under ``~/.local/share/uv/tools/`` rather than polluting the
      system Python;
    * subsequent invocations are just exec'ing the dropped shim, same
      cost as a system-package binary.
    """

    rejected_reason: str | None = None
    """When set, the plan is invalid; caller must NOT execute."""

    @property
    def is_valid(self) -> bool:
        return self.rejected_reason is None

    @property
    def needs_anything(self) -> bool:
        """True iff there are missing system packages or optional python
        packages that aren't yet installed."""
        return bool(self.missing) or bool(self.optional_python_pkgs)

    @property
    def full_argv(self) -> list[str]:
        """Complete sudo install command argv, ready to render or run."""
        if not self.missing:
            return []
        sudo = ["sudo"] if os.geteuid() != 0 else []
        return [*sudo, *self.install_cmd, *self.missing]


# ── plan builder ───────────────────────────────────────────────────────────


def _packages_for_target(spec: DistroSpec, target: Target, *, recommended: bool) -> list[str]:
    """Compose the package list for a given target, in stable order."""
    pkgs: list[str] = []
    seen: set[str] = set()

    def _extend(items: list[str]) -> None:
        for p in items:
            if p and p not in seen:
                seen.add(p)
                pkgs.append(p)

    if target in (Target.BUILD, Target.ALL):
        _extend(list(spec.build_deps))
        if recommended:
            _extend(_RECOMMENDED_PKG.get(spec.family, []))

    if target in (Target.BOOT_TEST, Target.ALL):
        _extend(_QEMU_PKG.get(spec.family, []))

    if target in (Target.INSTALL, Target.ALL):
        _extend(_BOOTLOADER_PKG.get(spec.family, []))

    return pkgs


def _query_installed(family: Family, packages: list[str]) -> tuple[list[str], list[str]]:
    """Return ``(missing, installed)`` partition of ``packages``.

    Conservative on probe failure: when the query tool is unavailable
    (``dpkg-query`` not on PATH, etc.), return everything as missing —
    the user will see the full install command. ``apt`` (and friends)
    are themselves no-op when a package is already installed, so this
    is safe.
    """
    if family == Family.DEBIAN:
        cmd = ["dpkg-query", "-W", "-f=${Package} ${Status}\n"]
    elif family in (Family.FEDORA, Family.SUSE):
        cmd = ["rpm", "-qa", "--queryformat=%{NAME}\n"]
    elif family == Family.ARCH:
        cmd = ["pacman", "-Qq"]
    elif family == Family.ALPINE:
        cmd = ["apk", "info"]
    elif family == Family.GENTOO:
        # Gentoo's `qlist -I` lists installed atoms but is from
        # portage-utils. Skip detection on Gentoo for now and return
        # everything as missing (emerge will handle dedup).
        return packages, []
    else:
        return packages, []

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return packages, []
    if result.returncode != 0:
        return packages, []

    out = result.stdout

    missing: list[str] = []
    installed: list[str] = []
    for p in packages:
        # Distro-specific match: dpkg-query gives "name install ok installed";
        # rpm/pacman/apk just give the bare package name on its own line.
        is_installed = False
        if family == Family.DEBIAN:
            for line in out.splitlines():
                if line.startswith(p + " ") and "install ok installed" in line:
                    is_installed = True
                    break
        else:
            installed_set = set(out.split())
            if p in installed_set:
                is_installed = True

        if is_installed:
            installed.append(p)
        else:
            missing.append(p)
    return missing, installed


def plan(
    *,
    distro: DistroInfo,
    spec: DistroSpec,
    target: Target,
    recommended: bool = True,
    include_virtme: bool = True,
) -> InstallDepsPlan:
    """Compose the install plan for the given target.

    ``include_virtme=True`` adds ``virtme-ng`` to ``optional_python_pkgs``
    when the target is BOOT_TEST or ALL — it's the preferred boot-test
    runtime and is install-via-pip rather than the system package
    manager (apt's ``virtme`` is a much older fork).
    """
    if spec.family == Family.UNKNOWN:
        return InstallDepsPlan(
            family=spec.family,
            target=target,
            install_cmd=[],
            requested=[],
            missing=[],
            already_installed=[],
            rejected_reason=(
                "distro family is UNKNOWN; can't pick a package manager. "
                "Install build-essential / flex / bison / libssl-dev / libelf-dev / "
                "libncurses-dev / dwarves / zstd / qemu-system-x86 by hand."
            ),
        )

    if not spec.install_cmd:
        return InstallDepsPlan(
            family=spec.family,
            target=target,
            install_cmd=[],
            requested=[],
            missing=[],
            already_installed=[],
            rejected_reason=f"no install command defined for family {spec.family.value!r}",
        )

    pkgs = _packages_for_target(spec, target, recommended=recommended)
    missing, installed = _query_installed(spec.family, pkgs)

    optional_python: list[str] = []
    if include_virtme and target in (Target.BOOT_TEST, Target.ALL):
        # Only suggest if it's not already importable.
        if not _virtme_installed():
            optional_python.append("virtme-ng")

    return InstallDepsPlan(
        family=spec.family,
        target=target,
        install_cmd=list(spec.install_cmd),
        requested=pkgs,
        missing=missing,
        already_installed=installed,
        optional_python_pkgs=optional_python,
    )


def _virtme_installed() -> bool:
    return bool(shutil.which("virtme-ng") or shutil.which("virtme-run"))


# ── execution ──────────────────────────────────────────────────────────────


@dataclass
class StepRun:
    name: str
    argv: list[str]
    exit_code: int
    duration_s: float
    log_path: Path | None


@dataclass
class InstallDepsResult:
    plan: InstallDepsPlan
    runs: list[StepRun] = field(default_factory=list)
    log_dir: Path | None = None

    @property
    def ok(self) -> bool:
        return all(r.exit_code == 0 for r in self.runs)


def execute(
    plan: InstallDepsPlan,
    *,
    log_dir: Path | None = None,
    install_virtme: bool = True,
) -> InstallDepsResult:
    """Run the install plan: shell out to the package manager (with
    ``sudo`` if not already root), then optionally pip-install
    ``virtme-ng``.

    Streams stdout/stderr to per-step log files when ``log_dir`` is
    given; otherwise the package-manager output goes to the parent's
    inherited stdout/stderr (so the user sees their sudo prompt).
    """
    if not plan.is_valid:
        raise RuntimeError(f"refusing to execute invalid plan: {plan.rejected_reason}")

    runs: list[StepRun] = []

    if plan.missing:
        argv = plan.full_argv
        rc, dur, log_path = _run_step(argv, "install_packages", log_dir)
        runs.append(StepRun(
            name="install_packages", argv=argv,
            exit_code=rc, duration_s=dur, log_path=log_path,
        ))

    if install_virtme and plan.optional_python_pkgs and (
        not plan.missing or any(r.exit_code == 0 for r in runs)
    ):
        for pkg in plan.optional_python_pkgs:
            # `uv tool install` matches autokernel's stack: uv is already
            # required by install.sh; the tool lands in an isolated env
            # under ~/.local/share/uv/tools/ with a shim on PATH.
            argv = ["uv", "tool", "install", pkg]
            rc, dur, log_path = _run_step(argv, f"uv_tool_{pkg}", log_dir)
            runs.append(StepRun(
                name=f"uv_tool_install_{pkg}", argv=argv,
                exit_code=rc, duration_s=dur, log_path=log_path,
            ))

    return InstallDepsResult(plan=plan, runs=runs, log_dir=log_dir)


def _run_step(
    argv: list[str], name: str, log_dir: Path | None
) -> tuple[int, float, Path | None]:
    started = datetime.now(UTC)
    log_path: Path | None = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{name}.log"

    try:
        if log_path is not None:
            with log_path.open("wb") as f:
                proc = subprocess.run(
                    argv, stdout=f, stderr=subprocess.STDOUT, check=False,
                )
        else:
            # Inherit stdio so the user sees the sudo password prompt.
            proc = subprocess.run(argv, check=False)
        rc = proc.returncode
    except FileNotFoundError:
        rc = 127

    duration = (datetime.now(UTC) - started).total_seconds()
    return rc, duration, log_path
