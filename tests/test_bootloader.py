"""Tests for bootloader detection.

Filesystem layouts are constructed under ``tmp_path``; ``detect_with_root``
re-roots the standard probe table so tests don't depend on the host's
real ``/boot``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autokernel.bootloader import (
    Bootloader,
    BootloaderKind,
    detect,
    detect_with_root,
)


def _mkboot(root: Path, *files: str) -> None:
    for rel in files:
        p = root / rel.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# fixture\n")


# ── detection ──────────────────────────────────────────────────────────────


def test_detect_debian_grub2(tmp_path: Path):
    _mkboot(tmp_path, "/boot/grub/grub.cfg")
    bl = detect_with_root(tmp_path)
    assert bl.kind == BootloaderKind.GRUB2
    assert bl.grub_tool_prefix == ""
    assert "grub" in str(bl.config_dir)


def test_detect_fedora_grub2_takes_precedence(tmp_path: Path):
    """When BOTH /boot/grub/grub.cfg AND /boot/grub2/grub.cfg exist
    (rare but possible), the more-specific Fedora layout wins."""
    _mkboot(tmp_path, "/boot/grub/grub.cfg", "/boot/grub2/grub.cfg")
    bl = detect_with_root(tmp_path)
    assert bl.kind == BootloaderKind.GRUB2
    assert bl.grub_tool_prefix == "grub2-"


def test_detect_systemd_boot_in_boot(tmp_path: Path):
    _mkboot(tmp_path, "/boot/loader/loader.conf")
    bl = detect_with_root(tmp_path)
    assert bl.kind == BootloaderKind.SYSTEMD_BOOT


def test_detect_systemd_boot_in_efi(tmp_path: Path):
    _mkboot(tmp_path, "/efi/loader/loader.conf")
    bl = detect_with_root(tmp_path)
    assert bl.kind == BootloaderKind.SYSTEMD_BOOT


def test_detect_refind(tmp_path: Path):
    _mkboot(tmp_path, "/boot/EFI/refind/refind.conf")
    bl = detect_with_root(tmp_path)
    assert bl.kind == BootloaderKind.REFIND


def test_detect_grub_legacy_only_when_no_grub2(tmp_path: Path):
    _mkboot(tmp_path, "/boot/grub/menu.lst")  # but NOT grub.cfg
    bl = detect_with_root(tmp_path)
    assert bl.kind == BootloaderKind.GRUB_LEGACY


def test_detect_unknown_when_nothing_matches(tmp_path: Path):
    bl = detect_with_root(tmp_path)
    assert bl.kind == BootloaderKind.UNKNOWN
    assert "no known" in bl.detected_via


# ── argv recipes ──────────────────────────────────────────────────────────


def test_grub2_debian_regenerate_uses_update_grub_or_mkconfig(monkeypatch):
    bl = Bootloader(
        kind=BootloaderKind.GRUB2,
        detected_via="x",
        config_dir=Path("/boot/grub"),
        grub_tool_prefix="",
    )
    # When update-grub is on PATH, prefer it (Debian convention)
    monkeypatch.setattr("autokernel.bootloader.shutil.which", lambda c: f"/usr/sbin/{c}")
    assert bl.regenerate_cmd() == ["update-grub"]

    # When update-grub is NOT on PATH, fall back to grub-mkconfig
    monkeypatch.setattr("autokernel.bootloader.shutil.which", lambda c: None)
    assert bl.regenerate_cmd() == ["grub-mkconfig", "-o", "/boot/grub/grub.cfg"]


def test_grub2_fedora_regenerate_uses_grub2_mkconfig():
    bl = Bootloader(
        kind=BootloaderKind.GRUB2,
        detected_via="x",
        config_dir=Path("/boot/grub2"),
        grub_tool_prefix="grub2-",
    )
    assert bl.regenerate_cmd() == ["grub2-mkconfig", "-o", "/boot/grub2/grub.cfg"]


def test_grub2_set_default_uses_correct_tool_prefix():
    deb = Bootloader(BootloaderKind.GRUB2, "x", grub_tool_prefix="")
    fed = Bootloader(BootloaderKind.GRUB2, "x", grub_tool_prefix="grub2-")
    assert deb.set_default_argv("Linux 6.13") == ["grub-set-default", "Linux 6.13"]
    assert fed.set_default_argv("Linux 6.13") == ["grub2-set-default", "Linux 6.13"]


def test_grub2_one_shot_reboot():
    deb = Bootloader(BootloaderKind.GRUB2, "x", grub_tool_prefix="")
    fed = Bootloader(BootloaderKind.GRUB2, "x", grub_tool_prefix="grub2-")
    assert deb.one_shot_argv("Linux 6.13") == ["grub-reboot", "Linux 6.13"]
    assert fed.one_shot_argv("Linux 6.13") == ["grub2-reboot", "Linux 6.13"]


def test_unsupported_bootloader_returns_none():
    bl = Bootloader(BootloaderKind.SYSTEMD_BOOT, "x")
    assert bl.regenerate_cmd() is None
    assert bl.set_default_argv("foo") is None
    assert bl.one_shot_argv("foo") is None
    assert not bl.is_supported


def test_grub2_is_supported():
    bl = Bootloader(BootloaderKind.GRUB2, "x")
    assert bl.is_supported


def test_unknown_bootloader_is_not_supported():
    bl = Bootloader(BootloaderKind.UNKNOWN, "x")
    assert not bl.is_supported
