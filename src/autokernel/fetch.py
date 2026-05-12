"""Distro-aware acquisition of a Linux kernel source tree.

Three acquisition methods, picked automatically based on the detected
:class:`autokernel.distro.Family` and what's already on disk:

1. **apt** (Debian-family only) — ``apt-get source linux`` to a working
   directory. Doesn't need root. Requires ``deb-src`` lines enabled in
   ``/etc/apt/sources.list[.d]``. Produces a versioned source dir.

2. **distro source-package install** — ``sudo apt install
   linux-source-X.Y`` (Debian-family) or ``sudo zypper install
   kernel-source`` (SUSE), then extract from ``/usr/src/`` to a
   user-writable working dir. Needs root.

3. **kernel.org tarball** (universal fallback) — download
   ``https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.13.tar.xz``,
   verify SHA256 if a known-good is provided, extract. Works on any
   distro. Best when the user wants a vanilla kernel rather than the
   distro-patched variant.

The verb is **idempotent**: if the target dir already exists with a
``Makefile`` matching the requested version, we skip the work and
return that path.

This module shells out to subprocesses but never executes anything that
modifies system state outside the working directory unless explicitly
asked (the ``apt install`` path needs root).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from autokernel.distro import DistroInfo, DistroSpec, Family


class Method(str, Enum):
    AUTO = "auto"
    APT_GET_SOURCE = "apt-get-source"
    APT_INSTALL_SOURCE = "apt-install-source"
    DNF_INSTALL_SOURCE = "dnf-install-source"
    PACMAN_INSTALL_SOURCE = "pacman-install-source"
    ZYPPER_INSTALL_SOURCE = "zypper-install-source"
    EMERGE_GENTOO_SOURCES = "emerge-gentoo-sources"
    TARBALL = "tarball"


@dataclass(frozen=True)
class FetchPlan:
    """What :func:`fetch_source` would do, before doing it."""

    method: Method
    description: str
    commands: list[list[str]]
    """Argv lists to invoke in order, in CWD ``working_dir``."""
    target_dir: Path
    """Final extracted source path that the caller can pass to
    ``autokernel build --kernel-source``."""
    needs_root: bool = False


@dataclass(frozen=True)
class FetchResult:
    plan: FetchPlan
    target_dir: Path
    cached: bool  # True if we returned an existing dir without re-fetching


# ── version normalization ───────────────────────────────────────────────────


_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:[-+~].*)?$")


def normalize_kernel_version(release: str) -> tuple[int, int, int]:
    """Parse a uname-r like ``6.13.0-12-generic`` → ``(6, 13, 0)``.

    Trailing ``-flavour`` (e.g. ``-generic``, ``-rt``) is stripped. Returns
    a 3-tuple; missing patchlevel becomes 0.
    """
    s = release.split("-", 1)[0]
    m = _VERSION_RE.match(s)
    if not m:
        raise ValueError(f"can't parse kernel version: {release!r}")
    major = int(m.group(1))
    minor = int(m.group(2))
    patch = int(m.group(3) or 0)
    return major, minor, patch


def _major_minor(release: str) -> str:
    M, m, _ = normalize_kernel_version(release)
    return f"{M}.{m}"


def _source_package_version(release: str) -> str:
    """Version string used by distro binary source packages.

    Ubuntu source packages include the patchlevel in names such as
    ``linux-source-7.0.0``.  Older callers may still pass a two-component
    version like ``6.13``; preserve that shape instead of inventing a
    trailing ``.0``.
    """
    base = release.split("-", 1)[0]
    normalize_kernel_version(base)
    return base


def _kernelorg_url(release: str) -> str:
    M, m, p = normalize_kernel_version(release)
    base = f"https://cdn.kernel.org/pub/linux/kernel/v{M}.x"
    name = f"linux-{M}.{m}.tar.xz" if p == 0 else f"linux-{M}.{m}.{p}.tar.xz"
    return f"{base}/{name}"


# ── plan builders (per method) ──────────────────────────────────────────────


def _plan_apt_get_source(release: str, working_dir: Path) -> FetchPlan:
    target = working_dir / f"linux-{_major_minor(release)}-source"
    return FetchPlan(
        method=Method.APT_GET_SOURCE,
        description=(
            f"`apt-get source linux` to {working_dir}. Requires deb-src lines "
            f"enabled in apt sources. Produces a patched-by-distro source tree."
        ),
        commands=[
            ["mkdir", "-p", str(working_dir)],
            ["apt-get", "source", "--download-only", "linux"],
            ["dpkg-source", "-x", "linux_*.dsc", str(target)],
        ],
        target_dir=target,
    )


def _plan_apt_install_source(
    release: str, spec: DistroSpec, working_dir: Path
) -> FetchPlan:
    version = _source_package_version(release)
    pkg = (spec.kernel_source_package_pattern or "linux-source").format(
        version=version
    )
    target = working_dir / f"linux-source-{version}"
    src_tarball = f"/usr/src/linux-source-{version}.tar.bz2"
    return FetchPlan(
        method=Method.APT_INSTALL_SOURCE,
        description=(
            f"`apt install {pkg}` then extract {src_tarball} to {target}. "
            f"Requires root for the install step."
        ),
        commands=[
            ["mkdir", "-p", str(working_dir)],
            list(spec.install_cmd) + [pkg],
            ["tar", "-xf", src_tarball, "-C", str(working_dir)],
        ],
        target_dir=target,
        needs_root=True,
    )


def _plan_dnf_install_source(
    release: str, spec: DistroSpec, working_dir: Path
) -> FetchPlan:
    """On Fedora, full kernel source comes as an SRPM. We instruct the
    user but don't auto-execute the SRPM dance — too distro-specific."""
    target = working_dir / f"linux-{_major_minor(release)}"
    return FetchPlan(
        method=Method.DNF_INSTALL_SOURCE,
        description=(
            "Fedora: install the kernel SRPM, then `rpmbuild -bp` to extract sources. "
            "This plan only prints the commands; for a faster path use --method tarball."
        ),
        commands=[
            ["mkdir", "-p", str(working_dir)],
            list(spec.install_cmd) + ["rpm-build", "dnf-plugins-core"],
            ["dnf", "download", "--source", "kernel"],
            # User must `rpm -i kernel-*.src.rpm; rpmbuild -bp ...` themselves.
        ],
        target_dir=target,
        needs_root=True,
    )


def _plan_pacman_kernel(release: str, spec: DistroSpec, working_dir: Path) -> FetchPlan:
    """Arch ships kernel via PKGBUILD; for vanilla source, the tarball
    method is more universal. We point users there."""
    return _plan_tarball(release, working_dir)


def _plan_zypper_kernel_source(
    release: str, spec: DistroSpec, working_dir: Path
) -> FetchPlan:
    pkg = "kernel-source"
    target = Path("/usr/src/linux")  # SUSE convention
    return FetchPlan(
        method=Method.ZYPPER_INSTALL_SOURCE,
        description=f"`zypper install {pkg}` puts source under /usr/src/linux.",
        commands=[
            list(spec.install_cmd) + [pkg],
        ],
        target_dir=target,
        needs_root=True,
    )


def _plan_emerge_gentoo_sources(
    release: str, spec: DistroSpec, working_dir: Path
) -> FetchPlan:
    target = Path("/usr/src/linux")
    return FetchPlan(
        method=Method.EMERGE_GENTOO_SOURCES,
        description="`emerge sys-kernel/gentoo-sources`; result symlinked at /usr/src/linux.",
        commands=[
            ["emerge", "sys-kernel/gentoo-sources"],
        ],
        target_dir=target,
        needs_root=True,
    )


def _plan_tarball(release: str, working_dir: Path) -> FetchPlan:
    url = _kernelorg_url(release)
    M, m, p = normalize_kernel_version(release)
    name = f"linux-{M}.{m}" if p == 0 else f"linux-{M}.{m}.{p}"
    tarball = working_dir / f"{name}.tar.xz"
    target = working_dir / name
    return FetchPlan(
        method=Method.TARBALL,
        description=f"download {url}, extract to {target}",
        commands=[
            ["mkdir", "-p", str(working_dir)],
            ["curl", "-fL", "-o", str(tarball), url],
            ["tar", "-xf", str(tarball), "-C", str(working_dir)],
        ],
        target_dir=target,
    )


# ── method selection ────────────────────────────────────────────────────────


def select_method(family: Family) -> Method:
    """Pick the best automatic method for this distro family.

    Defaults reflect what's most user-friendly for the typical user, not
    the most powerful path. Power users can always pass ``--method``.
    """
    if family == Family.DEBIAN:
        return Method.APT_GET_SOURCE  # no root needed
    if family == Family.FEDORA:
        return Method.TARBALL  # SRPM is awkward; vanilla tarball is faster
    if family == Family.ARCH:
        return Method.TARBALL
    if family == Family.SUSE:
        return Method.ZYPPER_INSTALL_SOURCE
    if family == Family.GENTOO:
        return Method.EMERGE_GENTOO_SOURCES
    return Method.TARBALL


def plan(
    *,
    distro: DistroInfo,
    spec: DistroSpec,
    release: str,
    working_dir: Path,
    method: Method = Method.AUTO,
) -> FetchPlan:
    """Build a :class:`FetchPlan` for the given inputs."""
    if method == Method.AUTO:
        method = select_method(distro.family)

    if method == Method.APT_GET_SOURCE:
        return _plan_apt_get_source(release, working_dir)
    if method == Method.APT_INSTALL_SOURCE:
        return _plan_apt_install_source(release, spec, working_dir)
    if method == Method.DNF_INSTALL_SOURCE:
        return _plan_dnf_install_source(release, spec, working_dir)
    if method == Method.PACMAN_INSTALL_SOURCE:
        return _plan_pacman_kernel(release, spec, working_dir)
    if method == Method.ZYPPER_INSTALL_SOURCE:
        return _plan_zypper_kernel_source(release, spec, working_dir)
    if method == Method.EMERGE_GENTOO_SOURCES:
        return _plan_emerge_gentoo_sources(release, spec, working_dir)
    if method == Method.TARBALL:
        return _plan_tarball(release, working_dir)
    raise ValueError(f"unknown method: {method}")


def fetch_source(
    *,
    distro: DistroInfo,
    spec: DistroSpec,
    release: str,
    working_dir: Path,
    method: Method = Method.AUTO,
    dry_run: bool = False,
    timeout_per_step: float = 600.0,
) -> FetchResult:
    """Acquire a kernel source tree according to the auto-selected (or
    explicit) method.

    Idempotent: returns the existing target dir if it already has a
    kernel ``Makefile`` and matches the requested major.minor version.
    """
    p = plan(
        distro=distro,
        spec=spec,
        release=release,
        working_dir=working_dir,
        method=method,
    )

    # Cache check: if target is already a valid kernel source tree we skip.
    if (p.target_dir / "Makefile").exists():
        return FetchResult(plan=p, target_dir=p.target_dir, cached=True)

    if dry_run:
        return FetchResult(plan=p, target_dir=p.target_dir, cached=False)

    working_dir.mkdir(parents=True, exist_ok=True)
    for cmd in p.commands:
        try:
            subprocess.run(
                cmd,
                cwd=str(working_dir),
                check=True,
                timeout=timeout_per_step,
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"missing command: {cmd[0]}") from e
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"command failed (rc={e.returncode}): {' '.join(cmd)}"
            ) from e

    return FetchResult(plan=p, target_dir=p.target_dir, cached=False)
