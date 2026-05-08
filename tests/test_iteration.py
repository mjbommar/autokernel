"""Tests for autokernel.iteration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autokernel.iteration import (
    IterationRecord,
    auto_revert_set,
    has_converged,
    iteration_dir,
    load_history,
    save_record,
    summarize_history_for_prompt,
)
from autokernel.measurements import BuildMeasurements


def _record(
    n: int = 1,
    *,
    proposals: list[str] | None = None,
    bzimage_bytes: int = 16_000_000,
    boot_passed: bool = True,
    landed: int = 5,
    proposed: int = 5,
    regressed: bool = False,
    revert_reason: str | None = None,
) -> IterationRecord:
    return IterationRecord(
        iteration=n,
        ctx_summary={"workload": "desktop", "threat": "balanced",
                     "modules": "distro", "aggression": "balanced"},
        proposals=proposals or ["CONFIG_X", "CONFIG_Y"],
        measurements=BuildMeasurements(
            bzimage_bytes=bzimage_bytes,
            boot_test_passed=boot_passed,
            proposed_count=proposed,
            actually_landed_count=landed,
        ),
        regressed=regressed,
        revert_reason=revert_reason,
    )


# ── round-trip ───────────────────────────────────────────────────────────


def test_record_round_trip(tmp_path):
    r = _record(2, proposals=["CONFIG_FOO", "CONFIG_BAR"])
    path = save_record(tmp_path, r)
    assert path.exists()
    history = load_history(tmp_path)
    assert len(history) == 1
    assert history[0].iteration == 2
    assert history[0].proposals == ["CONFIG_FOO", "CONFIG_BAR"]


def test_load_history_orders_by_iteration(tmp_path):
    save_record(tmp_path, _record(1, proposals=["A"]))
    save_record(tmp_path, _record(3, proposals=["C"]))
    save_record(tmp_path, _record(2, proposals=["B"]))
    history = load_history(tmp_path)
    assert [r.iteration for r in history] == [1, 2, 3]


def test_load_history_empty(tmp_path):
    assert load_history(tmp_path) == []


def test_iteration_dir_naming(tmp_path):
    d = iteration_dir(tmp_path, 7)
    assert d.name == "i007"


# ── summarize ────────────────────────────────────────────────────────────


def test_summarize_empty_history():
    assert summarize_history_for_prompt([]) == ""


def test_summarize_renders_recent_iterations():
    history = [
        _record(1, bzimage_bytes=18_000_000, landed=12, proposed=18),
        _record(2, bzimage_bytes=16_800_000, landed=11, proposed=14),
        _record(3, bzimage_bytes=16_500_000, landed=5,  proposed=9),
    ]
    text = summarize_history_for_prompt(history)
    assert "i=1" in text
    assert "i=2" in text
    assert "i=3" in text
    assert "18.0MB" in text or "17.2MB" in text or "MB" in text  # some rendering of bz
    assert "boot PASS" in text


def test_summarize_includes_revert_rules():
    history = [
        _record(1, proposals=["CONFIG_OK"], boot_passed=True),
        _record(
            2,
            proposals=["CONFIG_BTRFS_FS"],
            boot_passed=False,
            regressed=True,
            revert_reason="VFS panic — rootfs driver",
        ),
    ]
    text = summarize_history_for_prompt(history)
    assert "REVERTED" in text
    assert "CONFIG_BTRFS_FS" in text
    # Rules section
    assert "do NOT re-propose" in text
    assert "VFS panic" in text


def test_summarize_baseline_always_included_when_history_long():
    """The baseline (first iteration) should appear even when budget_recent
    only includes recent ones."""
    history = [
        _record(1, bzimage_bytes=18_000_000),
        _record(2, bzimage_bytes=17_500_000),
        _record(3, bzimage_bytes=17_000_000),
        _record(4, bzimage_bytes=16_500_000),
        _record(5, bzimage_bytes=16_000_000),
    ]
    text = summarize_history_for_prompt(history, budget_recent=2, include_baseline=True)
    assert "i=1" in text  # baseline
    assert "i=4" in text or "i=5" in text  # recent
    assert "(baseline)" in text


# ── auto-revert ──────────────────────────────────────────────────────────


def test_auto_revert_set_collects_failed_proposals():
    history = [
        _record(1, proposals=["A"], boot_passed=True),
        _record(2, proposals=["B", "C"], boot_passed=False, regressed=True),
        _record(3, proposals=["D"], boot_passed=True),
        _record(4, proposals=["E"], boot_passed=False, regressed=True),
    ]
    s = auto_revert_set(history)
    assert s == {"B", "C", "E"}


def test_auto_revert_set_empty_when_no_regressions():
    history = [
        _record(1, proposals=["A"], boot_passed=True),
        _record(2, proposals=["B"], boot_passed=True),
    ]
    assert auto_revert_set(history) == set()


# ── convergence ──────────────────────────────────────────────────────────


def test_has_converged_when_size_stable():
    """Last `window` step-deltas all within 1% = converged.

    window=2 requires 3 records. We need both the i=1→i=2 step AND
    the i=2→i=3 step to be small.
    """
    history = [
        _record(1, bzimage_bytes=16_510_000),
        _record(2, bzimage_bytes=16_500_000),  # 0.06% drop from i=1
        _record(3, bzimage_bytes=16_490_000),  # 0.06% drop from i=2
    ]
    assert has_converged(history, window=2, size_delta_pct=1.0)


def test_has_not_converged_when_one_step_too_big():
    """If ANY step delta in the window exceeds the threshold,
    not converged."""
    history = [
        _record(1, bzimage_bytes=18_000_000),
        _record(2, bzimage_bytes=16_500_000),  # 8% drop — not within 1%
        _record(3, bzimage_bytes=16_490_000),
    ]
    assert not has_converged(history, window=2, size_delta_pct=1.0)
    # But with window=1 it converges (just the last delta matters):
    assert has_converged(history, window=1, size_delta_pct=1.0)


def test_has_not_converged_when_size_still_changing():
    history = [
        _record(1, bzimage_bytes=18_000_000),
        _record(2, bzimage_bytes=16_500_000),
        _record(3, bzimage_bytes=15_800_000),  # >1% drop from i=2
    ]
    assert not has_converged(history, window=2, size_delta_pct=1.0)


def test_has_not_converged_when_too_few_iterations():
    history = [_record(1, bzimage_bytes=18_000_000)]
    assert not has_converged(history)


def test_has_not_converged_with_missing_measurements():
    history = [
        _record(1, bzimage_bytes=18_000_000),
        IterationRecord(
            iteration=2, ctx_summary={}, proposals=[],
            measurements=BuildMeasurements(bzimage_bytes=None),
        ),
    ]
    assert not has_converged(history)
