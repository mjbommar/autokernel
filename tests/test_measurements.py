"""Tests for autokernel.measurements."""

from __future__ import annotations

import json
from pathlib import Path


from autokernel.measurements import (
    BuildMeasurements,
    count_modules_in_config,
    diff_proposed_vs_actual,
    measure,
    measure_bzimage,
    measure_modules,
    measure_vmlinux,
    parse_compile_seconds_from_log,
    read_boot_test_record,
)


def _make_fake_source(
    tmp_path: Path, *, with_bzimage: bool = True, with_vmlinux: bool = True
) -> Path:
    src = tmp_path / "linux"
    src.mkdir()
    if with_bzimage:
        bzimage_dir = src / "arch" / "x86" / "boot"
        bzimage_dir.mkdir(parents=True)
        (bzimage_dir / "bzImage").write_bytes(b"X" * 16_000_000)  # 16MB
    if with_vmlinux:
        (src / "vmlinux").write_bytes(b"V" * 400_000_000)  # 400MB
    return src


# ── individual measurements ──────────────────────────────────────────────


def test_measure_bzimage_returns_size(tmp_path):
    src = _make_fake_source(tmp_path)
    assert measure_bzimage(src) == 16_000_000


def test_measure_bzimage_returns_none_when_missing(tmp_path):
    src = _make_fake_source(tmp_path, with_bzimage=False)
    assert measure_bzimage(src) is None


def test_measure_vmlinux(tmp_path):
    src = _make_fake_source(tmp_path)
    assert measure_vmlinux(src) == 400_000_000


def test_measure_modules_walks_tree(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text("")
    drv = src / "drivers/fake"
    drv.mkdir(parents=True)
    (drv / "fake.ko").write_bytes(b"K" * 1024)
    (drv / "other.ko").write_bytes(b"M" * 2048)
    count, total = measure_modules(src)
    assert count == 2
    assert total == 1024 + 2048


def test_count_modules_in_config_matches_m_lines(tmp_path):
    cfg = tmp_path / ".config"
    cfg.write_text(
        "CONFIG_X=y\n"
        "CONFIG_FOO=m\n"
        "# CONFIG_DISABLED is not set\n"
        "CONFIG_BAR=m\n"
        'CONFIG_LOCALVERSION=""\n'
    )
    assert count_modules_in_config(cfg) == 2


def test_parse_compile_seconds_from_log():
    log = (
        "│ olddefconfig            │  0 │     1.5 │ ... │\n"
        "│ make-bindeb-pkg         │  0 │   234.7 │ ... │\n"
    )
    seconds = parse_compile_seconds_from_log(log)
    assert seconds == 234.7


def test_parse_compile_seconds_returns_none_for_no_match():
    log = "nothing relevant here\n"
    assert parse_compile_seconds_from_log(log) is None


# ── diff proposed vs actual ──────────────────────────────────────────────


def test_diff_all_proposals_landed():
    proposed = "CONFIG_X=y\nCONFIG_Y=n\n"
    actual = "CONFIG_X=y\n# CONFIG_Y is not set\n"
    n, landed, dropped = diff_proposed_vs_actual(proposed, actual)
    assert n == 2
    assert landed == 2
    assert dropped == []


def test_diff_some_proposals_stripped():
    proposed = "CONFIG_X=y\nCONFIG_STRIPPED=y\n"
    actual = "CONFIG_X=y\n# CONFIG_STRIPPED is not set\n"
    n, landed, dropped = diff_proposed_vs_actual(proposed, actual)
    assert n == 2
    assert landed == 1
    assert dropped == ["CONFIG_STRIPPED"]


def test_diff_string_quoting_normalized():
    """`CONFIG_X="foo"` proposed should match actual `CONFIG_X="foo"`."""
    proposed = 'CONFIG_X="foo"\n'
    actual = 'CONFIG_X="foo"\n'
    _, landed, _ = diff_proposed_vs_actual(proposed, actual)
    assert landed == 1


# ── boot-test record reading ─────────────────────────────────────────────


def test_read_boot_test_record_parses_json(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    record = {"schema": 1, "verdict_ok": True, "duration_seconds": 0.3}
    (snap / "boot-test.json").write_text(json.dumps(record))
    assert read_boot_test_record(snap) == record


def test_read_boot_test_record_returns_none_when_missing(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    assert read_boot_test_record(snap) is None


# ── full measure() composition ───────────────────────────────────────────


def test_measure_full_composition(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "boot-test.json").write_text(
        json.dumps(
            {
                "verdict_ok": True,
                "duration_seconds": 0.42,
            }
        )
    )
    src = _make_fake_source(tmp_path)
    proposed = "CONFIG_X=y\nCONFIG_DROPPED=y\n"
    actual = "CONFIG_X=y\n# CONFIG_DROPPED is not set\n"
    log = "│ make-bindeb-pkg │  0 │   180.0 │ ... │\n"

    m = measure(
        snapshot_dir=snap,
        source_dir=src,
        proposed_config_text=proposed,
        actual_config_text=actual,
        build_log=log,
    )
    assert m.bzimage_bytes == 16_000_000
    assert m.vmlinux_bytes == 400_000_000
    assert m.compile_seconds == 180.0
    assert m.boot_test_passed is True
    assert m.boot_test_seconds == 0.42
    assert m.proposed_count == 2
    assert m.actually_landed_count == 1
    assert m.olddefconfig_dropped == ["CONFIG_DROPPED"]


def test_measure_partial_composition_when_inputs_missing(tmp_path):
    """measure() shouldn't fail when most inputs are absent; just
    returns Nones for unmeasurable fields."""
    snap = tmp_path / "snap"
    snap.mkdir()
    m = measure(snapshot_dir=snap)
    assert m.bzimage_bytes is None
    assert m.boot_test_passed is None
    assert m.proposed_count is None
    assert m.boot_test_passed_or_skipped is True  # null = treated as ok


def test_boot_test_passed_or_skipped_logic():
    assert BuildMeasurements(boot_test_passed=True).boot_test_passed_or_skipped
    assert BuildMeasurements(boot_test_passed=None).boot_test_passed_or_skipped
    assert not BuildMeasurements(boot_test_passed=False).boot_test_passed_or_skipped
