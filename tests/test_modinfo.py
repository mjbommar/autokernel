"""Tests for autokernel.modinfo — pure unit tests against fixture bytes."""

from __future__ import annotations

from pathlib import Path

import pytest

from autokernel.modinfo import (
    ModuleInfo,
    _parse_modinfo_filename_line,
    parse_builtin_modinfo,
)


def test_parse_builtin_record_basic(tmp_path: Path):
    """Two modules, one with .file= (good), one without."""
    blob = (
        b"intel_uncore.license=GPL\0"
        b"intel_uncore.file=arch/x86/events/intel/intel-uncore\0"
        b"intel_uncore.description=Support for Intel uncore\0"
        b"workqueue.parmtype=power_efficient:bool\0"
    )
    p = tmp_path / "modules.builtin.modinfo"
    p.write_bytes(blob)

    out = parse_builtin_modinfo(p)
    assert "intel_uncore" in out
    assert out["intel_uncore"].source_path == "arch/x86/events/intel/intel-uncore"
    assert out["intel_uncore"].is_builtin is True
    assert "GPL" in out["intel_uncore"].extras["license"]

    # workqueue has no .file=, should still be returned
    assert out["workqueue"].source_path is None


def test_parse_builtin_handles_dashes(tmp_path: Path):
    blob = b"hyperv-keyboard.file=drivers/input/serio/hyperv-keyboard\0"
    p = tmp_path / "x.modinfo"
    p.write_bytes(blob)
    out = parse_builtin_modinfo(p)
    assert out["hyperv-keyboard"].source_path == "drivers/input/serio/hyperv-keyboard"


def test_parse_builtin_missing_file_returns_empty(tmp_path: Path):
    out = parse_builtin_modinfo(tmp_path / "does_not_exist")
    assert out == {}


def test_parse_loadable_filename_line_strips_zst():
    info = _parse_modinfo_filename_line(
        "i915", "/lib/modules/6.13.0/kernel/drivers/gpu/drm/i915/i915.ko.zst"
    )
    assert info is not None
    assert info.source_path == "drivers/gpu/drm/i915/i915"
    assert info.is_builtin is False


def test_parse_loadable_filename_line_strips_plain_ko():
    info = _parse_modinfo_filename_line(
        "iwlwifi",
        "/lib/modules/6.13.0/kernel/drivers/net/wireless/intel/iwlwifi/iwlwifi.ko",
    )
    assert info is not None
    assert info.source_path == "drivers/net/wireless/intel/iwlwifi/iwlwifi"


def test_parse_loadable_filename_line_handles_builtin_marker():
    info = _parse_modinfo_filename_line("ext4", "(builtin)")
    assert info is not None
    assert info.source_path is None
    assert info.is_builtin is True


def test_parse_loadable_filename_line_blank_returns_none():
    assert _parse_modinfo_filename_line("foo", "") is None
    assert _parse_modinfo_filename_line("foo", "   ") is None
