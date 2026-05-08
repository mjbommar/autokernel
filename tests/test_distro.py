"""Tests for distro detection from synthetic /etc/os-release fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from autokernel.distro import (
    DistroInfo,
    Family,
    detect,
    parse_os_release,
    spec_for,
)

FIXTURES = Path(__file__).parent / "fixtures" / "os_release"


def _load(name: str) -> DistroInfo:
    return parse_os_release((FIXTURES / name).read_text())


# ── family classification ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fixture,expected_id,expected_family",
    [
        ("ubuntu_24_04", "ubuntu", Family.DEBIAN),
        ("debian_12", "debian", Family.DEBIAN),
        ("fedora_41", "fedora", Family.FEDORA),
        ("rhel_9", "rhel", Family.FEDORA),
        ("arch", "arch", Family.ARCH),
        ("manjaro", "manjaro", Family.ARCH),
        ("gentoo", "gentoo", Family.GENTOO),
        ("alpine", "alpine", Family.ALPINE),
        ("opensuse_tumbleweed", "opensuse-tumbleweed", Family.SUSE),
        ("nixos", "nixos", Family.NIXOS),
    ],
)
def test_family_classification(fixture: str, expected_id: str, expected_family: Family):
    info = _load(fixture)
    assert info.id == expected_id
    assert info.family == expected_family


def test_ubuntu_id_like_resolved():
    info = _load("ubuntu_24_04")
    assert info.id_like == ["debian"]


def test_manjaro_id_like_resolves_to_arch():
    info = _load("manjaro")
    assert info.id_like == ["arch"]
    assert info.family == Family.ARCH


def test_unknown_id_falls_back_to_unknown_family():
    info = parse_os_release('ID=mysterydistro\n')
    assert info.id == "mysterydistro"
    assert info.family == Family.UNKNOWN


def test_unknown_id_with_known_id_like_inherits_family():
    """A made-up Ubuntu derivative with ID_LIKE=ubuntu should still be Debian-family."""
    info = parse_os_release('ID=mydistro\nID_LIKE="ubuntu debian"\n')
    assert info.family == Family.DEBIAN


# ── parsing edge cases ──────────────────────────────────────────────────────


def test_quoted_values_unquoted():
    info = parse_os_release('ID="ubuntu"\nVERSION_CODENAME="noble"\n')
    assert info.id == "ubuntu"
    assert info.version_codename == "noble"


def test_unquoted_values_preserved():
    info = parse_os_release("ID=debian\nVERSION_ID=12\n")
    assert info.id == "debian"
    assert info.version_id == "12"


def test_comments_ignored():
    info = parse_os_release("# header\nID=arch\n# trailing\n")
    assert info.id == "arch"


def test_blank_lines_tolerated():
    info = parse_os_release("\n\nID=arch\n\n")
    assert info.id == "arch"


def test_empty_input_is_unknown():
    info = parse_os_release("")
    assert info.id == "unknown"
    assert info.family == Family.UNKNOWN


# ── spec dispatch ───────────────────────────────────────────────────────────


def test_debian_spec_uses_apt():
    spec = spec_for(_load("ubuntu_24_04"))
    assert spec.package_manager == "apt"
    assert spec.install_cmd[0] == "apt"
    assert spec.apt_get_source_supported is True
    assert "build-essential" in spec.build_deps
    assert spec.build_target_default == "bindeb-pkg"
    assert spec.kernel_source_package_pattern == "linux-source-{version}"


def test_fedora_spec_uses_dnf():
    spec = spec_for(_load("fedora_41"))
    assert spec.package_manager == "dnf"
    assert "openssl-devel" in spec.build_deps
    assert spec.build_target_default == "rpm-pkg"
    # Fedora uses initramfs-<release>.img, not initrd.img-<release>
    assert "initramfs-" in spec.initramfs_image_path_pattern


def test_arch_spec_uses_pacman():
    spec = spec_for(_load("arch"))
    assert spec.package_manager == "pacman"
    assert "base-devel" in spec.build_deps
    assert spec.kernel_source_package_pattern is None  # no native source pkg


def test_gentoo_spec_uses_emerge():
    spec = spec_for(_load("gentoo"))
    assert spec.package_manager == "emerge"
    assert spec.source_install_cmd is not None
    assert "gentoo-sources" in " ".join(spec.source_install_cmd)
    assert spec.kernel_config_path_pattern == "/usr/src/linux/.config"


def test_suse_spec():
    spec = spec_for(_load("opensuse_tumbleweed"))
    assert spec.package_manager == "zypper"
    assert spec.build_target_default == "rpm-pkg"


def test_nixos_spec_has_unusual_paths():
    """NixOS doesn't put kernel config in /boot/config-<release>."""
    spec = spec_for(_load("nixos"))
    assert "booted-system" in spec.kernel_config_path_pattern


def test_unknown_spec_has_safe_fallbacks():
    info = parse_os_release("")
    spec = spec_for(info)
    assert spec.family == Family.UNKNOWN
    # Targets a tarball — least invasive fallback
    assert spec.build_target_default == "targz-pkg"


# ── detect() against synthetic search paths ─────────────────────────────────


def test_detect_uses_first_existing(tmp_path: Path):
    p1 = tmp_path / "etc-os-release"
    p2 = tmp_path / "usr-os-release"
    p2.write_text("ID=fedora\n")
    info = detect(search_paths=[p1, p2])
    assert info.id == "fedora"


def test_detect_falls_back_to_unknown_when_nothing_found(tmp_path: Path):
    info = detect(search_paths=[tmp_path / "nope1", tmp_path / "nope2"])
    assert info.family == Family.UNKNOWN
