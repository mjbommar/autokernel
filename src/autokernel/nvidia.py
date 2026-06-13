"""NVIDIA driver planning for custom-kernel installs.

Ubuntu's stock NVIDIA path normally installs prebuilt, signed modules for
specific distro kernel ABI names. A custom localversion cannot use those
packages, so an install that detects existing NVIDIA usage needs to add a
DKMS-backed driver package and build it against the target kernel release
before arming the bootloader.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from autokernel.distro import DistroInfo, Family
from autokernel.models import Snapshot


class NvidiaMode(str, Enum):
    AUTO = "auto"
    OPEN = "open"
    PROPRIETARY = "proprietary"
    OFF = "off"


@dataclass(frozen=True)
class NvidiaPackage:
    name: str
    version: str | None = None
    source: str = "dpkg"


@dataclass(frozen=True)
class NvidiaDriverPlan:
    kernel_release: str
    branch: str
    flavor: str
    package_name: str
    reason: str
    evidence: tuple[str, ...] = field(default_factory=tuple)


_NVIDIA_BRANCH_RE = re.compile(
    r"^(?:nvidia-driver|nvidia-dkms|nvidia-utils|nvidia-kernel-source)-"
    r"(?P<branch>\d+)(?:$|-)"
)
_LIBNVIDIA_BRANCH_RE = re.compile(r"^libnvidia-[\w-]+-(?P<branch>\d+)(?:$|-)")
_LINUX_NVIDIA_BRANCH_RE = re.compile(
    r"^linux-(?:modules|objects)-nvidia-(?P<branch>\d+)(?:-|$)"
)
_DEBIAN_NVIDIA_PATTERNS = [
    "nvidia-driver-*",
    "nvidia-dkms-*",
    "nvidia-kernel-source-*",
    "nvidia-utils-*",
    "libnvidia-compute-*",
    "linux-modules-nvidia-*",
    "linux-objects-nvidia-*",
]


def kernel_release_from_packages(package_paths: Iterable[Path]) -> str | None:
    """Best-effort extraction of the target kernel release from package names."""
    for path in package_paths:
        name = path.name
        if name.startswith("linux-image-") and "_dbg_" not in name:
            tail = name.removeprefix("linux-image-")
            if "_" in tail:
                return tail.split("_", 1)[0]
        if name.startswith("linux-headers-"):
            tail = name.removeprefix("linux-headers-")
            if "_" in tail:
                return tail.split("_", 1)[0]
    return None


def plan_nvidia_support(
    *,
    snapshot: Snapshot,
    distro: DistroInfo,
    package_paths: Iterable[Path],
    mode: NvidiaMode = NvidiaMode.AUTO,
    installed_packages: Iterable[NvidiaPackage] | None = None,
) -> NvidiaDriverPlan | None:
    """Return an install-time NVIDIA DKMS plan when the host needs one.

    ``mode=auto`` preserves the currently installed NVIDIA flavor when it can
    infer one. Users can force ``open`` or ``proprietary`` from the CLI.
    """
    if mode == NvidiaMode.OFF:
        return None
    if distro.family != Family.DEBIAN:
        return None

    kernel_release = kernel_release_from_packages(package_paths)
    if not kernel_release:
        return None

    base_evidence = _snapshot_nvidia_evidence(snapshot)
    if not base_evidence:
        return None

    packages = list(installed_packages) if installed_packages is not None else []
    if installed_packages is None:
        packages.extend(_snapshot_nvidia_packages(snapshot))
        packages.extend(_live_debian_nvidia_packages())

    evidence = _nvidia_evidence(snapshot, packages)
    if not evidence:
        return None

    branch = _select_branch(snapshot, packages)
    if branch is None:
        return None

    flavor = _select_flavor(mode=mode, packages=packages, branch=branch)
    suffix = "-open" if flavor == "open" else ""
    return NvidiaDriverPlan(
        kernel_release=kernel_release,
        branch=branch,
        flavor=flavor,
        package_name=f"nvidia-driver-{branch}{suffix}",
        reason=(
            "NVIDIA hardware/userspace detected; custom kernels need DKMS "
            f"NVIDIA modules built for {kernel_release}"
        ),
        evidence=tuple(sorted(evidence)),
    )


def _nvidia_evidence(snapshot: Snapshot, packages: Iterable[NvidiaPackage]) -> set[str]:
    evidence = _snapshot_nvidia_evidence(snapshot)

    names = {p.name for p in packages}
    if any(_is_nvidia_driver_intent(name) for name in names):
        evidence.add("nvidia-driver-packages")

    return evidence


def _snapshot_nvidia_evidence(snapshot: Snapshot) -> set[str]:
    evidence: set[str] = set()
    if _has_nvidia_display(snapshot):
        evidence.add("nvidia-pci-display")

    loaded = {m.name for m in snapshot.loaded_modules}
    if loaded & {"nvidia", "nvidia_drm", "nvidia_modeset", "nvidia_uvm"}:
        evidence.add("nvidia-modules-loaded")

    if any(d.name == "nvidia" for d in snapshot.dkms):
        evidence.add("nvidia-dkms-status")

    return evidence


def _has_nvidia_display(snapshot: Snapshot) -> bool:
    return any(
        p.vendor_id.lower() == "10de" and (p.class_id or "").startswith("03")
        for p in snapshot.pci
    )


def _snapshot_nvidia_packages(snapshot: Snapshot) -> list[NvidiaPackage]:
    packages: list[NvidiaPackage] = []
    for signal in snapshot.software_features:
        if signal.feature == "nvidia" or signal.name.startswith(
            ("nvidia-", "libnvidia-", "linux-modules-nvidia-", "linux-objects-nvidia-")
        ):
            packages.append(
                NvidiaPackage(
                    name=signal.name,
                    version=signal.detail,
                    source=signal.source,
                )
            )
    return packages


def _live_debian_nvidia_packages() -> list[NvidiaPackage]:
    try:
        result = subprocess.run(
            [
                "dpkg-query",
                "-W",
                "-f=${db:Status-Abbrev}\t${binary:Package}\t${Version}\n",
                *_DEBIAN_NVIDIA_PATTERNS,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    packages: list[NvidiaPackage] = []
    seen: set[str] = set()
    for line in getattr(result, "stdout", "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0].startswith("ii"):
            continue
        name = parts[1].strip()
        if not name or name in seen:
            continue
        seen.add(name)
        packages.append(NvidiaPackage(name=name, version=parts[2].strip()))
    return packages


def _is_nvidia_driver_intent(name: str) -> bool:
    return name.startswith(
        (
            "nvidia-driver-",
            "nvidia-dkms-",
            "nvidia-utils-",
            "libnvidia-compute-",
            "linux-modules-nvidia-",
        )
    )


def _select_branch(snapshot: Snapshot, packages: Iterable[NvidiaPackage]) -> str | None:
    names = [p.name for p in packages]
    for prefix in ("nvidia-driver-", "nvidia-dkms-", "nvidia-utils-"):
        for name in sorted(names):
            if not name.startswith(prefix):
                continue
            branch = _branch_from_package_name(name)
            if branch:
                return branch

    for name in sorted(names):
        branch = _branch_from_package_name(name)
        if branch:
            return branch

    for d in snapshot.dkms:
        if d.name == "nvidia":
            branch = d.version.split(".", 1)[0]
            if branch.isdigit():
                return branch
    return None


def _branch_from_package_name(name: str) -> str | None:
    for rx in (_NVIDIA_BRANCH_RE, _LIBNVIDIA_BRANCH_RE, _LINUX_NVIDIA_BRANCH_RE):
        m = rx.match(name)
        if m:
            return m.group("branch")
    return None


def _select_flavor(
    *, mode: NvidiaMode, packages: Iterable[NvidiaPackage], branch: str
) -> str:
    if mode == NvidiaMode.OPEN:
        return "open"
    if mode == NvidiaMode.PROPRIETARY:
        return "proprietary"

    names = {p.name for p in packages}
    if f"nvidia-driver-{branch}-open" in names or f"nvidia-dkms-{branch}-open" in names:
        return "open"
    if f"nvidia-driver-{branch}" in names or f"nvidia-dkms-{branch}" in names:
        return "proprietary"
    if any(_is_open_package(name) for name in names):
        return "open"
    return "proprietary"


def _is_open_package(name: str) -> bool:
    return (
        name.startswith("nvidia-driver-")
        and name.endswith("-open")
        or name.startswith("nvidia-dkms-")
        and name.endswith("-open")
        or name.startswith("linux-modules-nvidia-")
        and "-open-" in name
    )
