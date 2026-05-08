"""Detect the running Linux distribution and provide per-family adapters.

Per the systemd ``os-release(5)`` spec, every modern Linux distribution
ships ``/etc/os-release`` (or ``/usr/lib/os-release``) with shell-style
``KEY=VALUE`` pairs. The fields that matter for autokernel:

* ``ID`` — primary distro id (e.g. ``ubuntu``, ``debian``, ``fedora``, ``arch``).
* ``ID_LIKE`` — derivative chain (e.g. Ubuntu's ``ID_LIKE=debian`` lets us
  treat it as Debian-family for package tooling).
* ``VERSION_ID``, ``VERSION_CODENAME``, ``PRETTY_NAME`` — informational.

We classify each distro into a :class:`Family` so verbs that need
distro-specific behavior (``fetch-source``, ``build``, future ``install``)
can dispatch on the family rather than the exact id. Each family has a
:class:`DistroSpec` with the per-family knowledge: package manager, kernel
source acquisition recipe, build-deps package list, kernel-config /
initramfs path patterns.

The families are deliberately broad. A more specific dispatch (Ubuntu LTS
vs interim, RHEL vs CentOS Stream) can layer on top by inspecting
``DistroInfo.id`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Family(str, Enum):
    DEBIAN = "debian"   # Debian, Ubuntu, Mint, Pop!_OS, KDE neon, …
    FEDORA = "fedora"   # Fedora, RHEL, CentOS Stream, Rocky, AlmaLinux, Oracle Linux
    ARCH = "arch"       # Arch, Manjaro, EndeavourOS, …
    SUSE = "suse"       # openSUSE Leap/Tumbleweed, SLES
    GENTOO = "gentoo"   # Gentoo
    ALPINE = "alpine"   # Alpine
    NIXOS = "nixos"     # NixOS (special case — autokernel mostly doesn't apply)
    UNKNOWN = "unknown"


# Map of canonical ID values (or ID_LIKE tokens) → Family.
_ID_TO_FAMILY: dict[str, Family] = {
    # Debian family
    "debian": Family.DEBIAN,
    "ubuntu": Family.DEBIAN,
    "linuxmint": Family.DEBIAN,
    "pop": Family.DEBIAN,
    "neon": Family.DEBIAN,
    "elementary": Family.DEBIAN,
    "kali": Family.DEBIAN,
    "raspbian": Family.DEBIAN,
    "mx": Family.DEBIAN,
    "deepin": Family.DEBIAN,
    # Fedora / RHEL family
    "fedora": Family.FEDORA,
    "rhel": Family.FEDORA,
    "centos": Family.FEDORA,
    "rocky": Family.FEDORA,
    "almalinux": Family.FEDORA,
    "ol": Family.FEDORA,            # Oracle Linux
    "amzn": Family.FEDORA,          # Amazon Linux
    # Arch family
    "arch": Family.ARCH,
    "manjaro": Family.ARCH,
    "endeavouros": Family.ARCH,
    "garuda": Family.ARCH,
    "artix": Family.ARCH,
    # SUSE family
    "opensuse": Family.SUSE,
    "opensuse-leap": Family.SUSE,
    "opensuse-tumbleweed": Family.SUSE,
    "sles": Family.SUSE,
    "sled": Family.SUSE,
    # Other
    "gentoo": Family.GENTOO,
    "alpine": Family.ALPINE,
    "nixos": Family.NIXOS,
}


# ── DistroInfo ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DistroInfo:
    """Parsed ``/etc/os-release`` fields plus the family classification."""

    id: str
    id_like: list[str]
    family: Family
    version_id: str | None = None
    version_codename: str | None = None
    pretty_name: str | None = None
    raw: dict[str, str] = field(default_factory=dict)


def _parse_os_release_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        # Strip surrounding quotes only if balanced.
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _classify(id_: str, id_like_tokens: list[str]) -> Family:
    if id_ in _ID_TO_FAMILY:
        return _ID_TO_FAMILY[id_]
    for tok in id_like_tokens:
        if tok in _ID_TO_FAMILY:
            return _ID_TO_FAMILY[tok]
    return Family.UNKNOWN


def parse_os_release(text: str) -> DistroInfo:
    """Parse ``os-release`` content into a :class:`DistroInfo`."""
    raw = _parse_os_release_text(text)
    id_ = raw.get("ID", "unknown").strip().lower()
    id_like_raw = raw.get("ID_LIKE", "").strip().lower()
    id_like = [t for t in id_like_raw.split() if t]
    family = _classify(id_, id_like)
    return DistroInfo(
        id=id_,
        id_like=id_like,
        family=family,
        version_id=raw.get("VERSION_ID"),
        version_codename=raw.get("VERSION_CODENAME"),
        pretty_name=raw.get("PRETTY_NAME"),
        raw=raw,
    )


def detect(*, search_paths: list[Path] | None = None) -> DistroInfo:
    """Read the live ``os-release`` from the standard locations.

    Tries ``/etc/os-release`` then ``/usr/lib/os-release`` per the spec.
    Falls back to a synthetic ``UNKNOWN`` if neither is present.
    """
    paths = search_paths or [
        Path("/etc/os-release"),
        Path("/usr/lib/os-release"),
    ]
    for p in paths:
        try:
            return parse_os_release(p.read_text())
        except (FileNotFoundError, PermissionError):
            continue
    return DistroInfo(id="unknown", id_like=[], family=Family.UNKNOWN)


# ── DistroSpec: per-family knowledge ────────────────────────────────────────


@dataclass(frozen=True)
class DistroSpec:
    """Per-family defaults for autokernel verbs.

    Fields are deliberately kept small. When a verb needs more specificity
    (e.g. RHEL 8 vs Fedora 40), it can branch on ``DistroInfo.id`` after
    consulting the spec for the common case.
    """

    family: Family
    package_manager: str
    """The user-facing package manager command (``apt``, ``dnf``, ``pacman``,
    ``zypper``, ``emerge``, ``apk``)."""

    install_cmd: tuple[str, ...]
    """Argv prefix to install packages, e.g. ``('apt', 'install', '-y')``.
    Caller appends package names. For families that need root, the caller
    is responsible for prepending ``sudo`` if appropriate."""

    source_install_cmd: tuple[str, ...] | None
    """Argv prefix to install the kernel source package on this distro,
    e.g. ``('apt', 'install', '-y', 'linux-source-VERSION')``. ``None``
    if the distro does not ship a kernel-source package and we need to
    fall back to a tarball from kernel.org."""

    apt_get_source_supported: bool = False
    """True if `apt-get source linux` is available (Debian-family only)."""

    build_deps: tuple[str, ...] = ()
    """Package names required for ``make bindeb-pkg`` / equivalent.
    Names match this distro's package repo, not a normalized abstraction."""

    build_target_default: str = "bindeb-pkg"
    """Default ``make`` target for kernel package output. Ubuntu/Debian
    use ``bindeb-pkg``, Fedora/RHEL use ``rpm-pkg``, Arch/Gentoo prefer
    ``targz-pkg`` (no native packaging target)."""

    kernel_config_path_pattern: str = "/boot/config-{release}"
    """sprintf-style; ``{release}`` substitutes ``uname -r``. Path is the
    booted kernel's config (the source for ``Snapshot.running_config_path``)."""

    initramfs_image_path_pattern: str = "/boot/initrd.img-{release}"
    """sprintf-style; ``{release}`` substitutes ``uname -r``. Path to the
    initramfs image."""

    kernel_source_package_pattern: str | None = None
    """The package name that contains kernel sources. Templated on
    ``{version}`` (e.g. ``linux-source-6.13``). ``None`` for distros that
    don't ship one."""


_SPECS: dict[Family, DistroSpec] = {
    Family.DEBIAN: DistroSpec(
        family=Family.DEBIAN,
        package_manager="apt",
        install_cmd=("apt", "install", "-y"),
        source_install_cmd=("apt", "install", "-y"),
        apt_get_source_supported=True,
        build_deps=(
            "build-essential", "flex", "bison", "bc", "libssl-dev",
            # libdw-dev provides <dwarf.h> for kernel >= 6.19's gendwarfksyms;
            # libelf-dev is the long-standing requirement for the rest of the build.
            "libelf-dev", "libdw-dev", "libncurses-dev", "dwarves", "zstd", "kmod",
            "cpio", "rsync",
            # debhelper + libdw-dev:native are required by `make bindeb-pkg` to
            # build the .deb packages (dpkg-checkbuilddeps enforces them).
            "debhelper",
            # v0.15: clang/lld/llvm — required for the default compiler;
            # also required for CFI_CLANG, LTO_CLANG_*, and KCSAN.
            "clang", "lld", "llvm",
        ),
        build_target_default="bindeb-pkg",
        kernel_config_path_pattern="/boot/config-{release}",
        initramfs_image_path_pattern="/boot/initrd.img-{release}",
        kernel_source_package_pattern="linux-source-{version}",
    ),
    Family.FEDORA: DistroSpec(
        family=Family.FEDORA,
        package_manager="dnf",
        install_cmd=("dnf", "install", "-y"),
        source_install_cmd=None,  # Fedora ships kernel-source via SRPM, not a binary pkg
        build_deps=(
            "gcc", "make", "flex", "bison", "bc", "openssl-devel",
            # elfutils-devel provides <dwarf.h> (gendwarfksyms on 6.19+);
            # elfutils-libelf-devel is the older libelf header set.
            "elfutils-libelf-devel", "elfutils-devel", "ncurses-devel",
            "dwarves", "zstd", "kmod", "cpio", "rsync", "perl",
            # v0.15: clang as default compiler.
            "clang", "lld", "llvm",
        ),
        build_target_default="rpm-pkg",
        kernel_config_path_pattern="/boot/config-{release}",
        # On Fedora the initramfs is named differently:
        initramfs_image_path_pattern="/boot/initramfs-{release}.img",
        kernel_source_package_pattern=None,
    ),
    Family.ARCH: DistroSpec(
        family=Family.ARCH,
        package_manager="pacman",
        install_cmd=("pacman", "-S", "--noconfirm"),
        source_install_cmd=None,  # No bundled kernel-source pkg — use kernel.org
        build_deps=(
            "base-devel", "flex", "bison", "bc", "openssl",
            # libelf has libelf.h; libdw has <dwarf.h> for gendwarfksyms (6.19+).
            "libelf", "libdw", "ncurses", "pahole", "zstd", "kmod", "cpio", "rsync",
            # v0.15: clang/lld/llvm.
            "clang", "lld", "llvm",
        ),
        build_target_default="tarzst-pkg",  # closest to a "loose tarball" target
        kernel_config_path_pattern="/boot/config-{release}",
        initramfs_image_path_pattern="/boot/initramfs-linux.img",
        kernel_source_package_pattern=None,
    ),
    Family.SUSE: DistroSpec(
        family=Family.SUSE,
        package_manager="zypper",
        install_cmd=("zypper", "install", "-y"),
        source_install_cmd=("zypper", "install", "-y"),
        build_deps=(
            "gcc", "make", "flex", "bison", "bc", "libopenssl-devel",
            # libdw-devel adds <dwarf.h> for gendwarfksyms (6.19+).
            "libelf-devel", "libdw-devel", "ncurses-devel", "dwarves", "zstd",
        ),
        build_target_default="rpm-pkg",
        kernel_config_path_pattern="/boot/config-{release}",
        initramfs_image_path_pattern="/boot/initrd-{release}",
        kernel_source_package_pattern="kernel-source",
    ),
    Family.GENTOO: DistroSpec(
        family=Family.GENTOO,
        package_manager="emerge",
        install_cmd=("emerge",),
        source_install_cmd=("emerge", "sys-kernel/gentoo-sources"),
        build_deps=(
            "sys-devel/gcc", "sys-devel/make", "sys-devel/flex",
            "sys-devel/bison", "sys-devel/bc", "dev-libs/openssl",
            # dev-libs/elfutils provides <dwarf.h> for gendwarfksyms (6.19+).
            "virtual/libelf", "dev-libs/elfutils", "sys-libs/ncurses",
            "dev-util/dwarves",
        ),
        build_target_default="targz-pkg",
        kernel_config_path_pattern="/usr/src/linux/.config",
        initramfs_image_path_pattern="/boot/initramfs-{release}.img",
        kernel_source_package_pattern=None,
    ),
    Family.ALPINE: DistroSpec(
        family=Family.ALPINE,
        package_manager="apk",
        install_cmd=("apk", "add"),
        source_install_cmd=None,
        build_deps=(
            "build-base", "flex", "bison", "bc", "openssl-dev",
            # elfutils-dev on Alpine bundles libdw + libelf headers — covers
            # <dwarf.h> for gendwarfksyms (6.19+) without a separate package.
            "elfutils-dev", "ncurses-dev", "zstd",
        ),
        build_target_default="targz-pkg",
        kernel_config_path_pattern="/boot/config-{release}",
        initramfs_image_path_pattern="/boot/initramfs-{release}",
        kernel_source_package_pattern=None,
    ),
    Family.NIXOS: DistroSpec(
        family=Family.NIXOS,
        package_manager="nix-env",
        install_cmd=("nix-env", "-i"),
        source_install_cmd=None,
        # On NixOS, kernel building is fundamentally different (managed by
        # configuration.nix). autokernel can still propose configs but
        # building+installing is out of scope for this verb set.
        build_deps=(),
        build_target_default="targz-pkg",
        kernel_config_path_pattern="/run/booted-system/kernel-modules/lib/modules/{release}/build/.config",
        initramfs_image_path_pattern="/run/booted-system/initrd",
        kernel_source_package_pattern=None,
    ),
    Family.UNKNOWN: DistroSpec(
        family=Family.UNKNOWN,
        package_manager="<unknown>",
        install_cmd=(),
        source_install_cmd=None,
        build_deps=(),
        build_target_default="targz-pkg",
        kernel_config_path_pattern="/boot/config-{release}",
        initramfs_image_path_pattern="/boot/initrd-{release}",
        kernel_source_package_pattern=None,
    ),
}


def spec_for(info: DistroInfo) -> DistroSpec:
    """Return the :class:`DistroSpec` matching this distro's family."""
    return _SPECS[info.family]
