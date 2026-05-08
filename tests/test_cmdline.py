"""Tests for /proc/cmdline structured parsing."""

from __future__ import annotations

from autokernel.snapshot import _parse_cmdline


def test_parse_basic_keys_and_flags():
    params, blacklist = _parse_cmdline(
        "BOOT_IMAGE=/vmlinuz-6.13 root=UUID=abc ro quiet splash"
    )
    assert params["BOOT_IMAGE"] == "/vmlinuz-6.13"
    assert params["root"] == "UUID=abc"
    assert params["ro"] == ""
    assert params["quiet"] == ""
    assert params["splash"] == ""
    assert blacklist == []


def test_parse_module_blacklist_extracted():
    params, blacklist = _parse_cmdline(
        "root=/dev/sda1 module_blacklist=nouveau,radeon ro"
    )
    assert blacklist == ["nouveau", "radeon"]
    assert "module_blacklist" in params


def test_parse_blacklist_alias():
    """Some bootloaders pass `blacklist=` instead of `module_blacklist=`."""
    _, blacklist = _parse_cmdline("root=/ blacklist=foo,bar")
    assert blacklist == ["foo", "bar"]


def test_parse_cryptdevice_present():
    params, _ = _parse_cmdline(
        "root=/dev/mapper/cryptroot cryptdevice=UUID=xyz:cryptroot ro"
    )
    assert params["cryptdevice"] == "UUID=xyz:cryptroot"


def test_parse_empty():
    params, blacklist = _parse_cmdline("")
    assert params == {}
    assert blacklist == []


def test_intel_laptop_fixture_cmdline_parsed(intel_laptop):
    """The Intel laptop fixture's cmdline includes cryptdevice and a blacklist."""
    bc = intel_laptop.boot
    assert bc.cmdline_params["root"] == "UUID=11111111-2222-3333-4444-555555555555"
    assert bc.cmdline_params["rootfstype"] == "btrfs"
    assert "cryptdevice" in bc.cmdline_params
    assert bc.blacklisted_modules == ["nouveau"]


def test_amd_desktop_fixture_cmdline_no_blacklist(amd_desktop):
    bc = amd_desktop.boot
    assert bc.blacklisted_modules == []
    assert "cryptdevice" not in bc.cmdline_params
