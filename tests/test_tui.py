"""End-to-end TUI tests using Textual's Pilot.

Each test boots a real :class:`ReviewApp`, presses keys via Pilot, and
asserts on the resulting state / `app.result`. These run headless and
are stable in CI (Textual's test harness uses an in-memory driver).

Faster pure-logic tests live in test_tui_state.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autokernel.models import (
    ProposalSource,
    RemovalProposal,
    ReviewDecision,
    Reviewer,
    ReviewSet,
    ReviewedProposal,
    RiskLevel,
)
from autokernel.tui import ReviewApp


pytestmark = pytest.mark.asyncio


# ── shared fixtures / helpers ──────────────────────────────────────────────


def _proposal(config: str) -> RemovalProposal:
    return RemovalProposal(
        config=config,
        current_value="m",
        proposed_value="n",
        reason=f"reason for {config}",
        risk=RiskLevel.LOW,
        confidence=0.9,
        source=ProposalSource.LLM,
        evidence=[],
    )


def _deferred(*configs: str) -> ReviewSet:
    return ReviewSet(
        base_diff_path=Path("/tmp/p.json"),
        deferred=[
            ReviewedProposal(
                proposal=_proposal(c),
                decision=ReviewDecision.DEFER,
                reviewer=Reviewer.POLICY,
                rule="initial",
            )
            for c in configs
        ],
    )


# ── basic lifecycle ────────────────────────────────────────────────────────


async def test_app_mounts_and_quits_clean():
    app = ReviewApp(_deferred("CONFIG_A", "CONFIG_B"))
    async with app.run_test() as pilot:
        await pilot.press("q")
    assert app.result is None  # quit without save


async def test_save_returns_review_set_with_no_changes():
    rs_in = _deferred("CONFIG_A")
    app = ReviewApp(rs_in)
    async with app.run_test() as pilot:
        await pilot.press("w")
    assert app.result is not None
    assert len(app.result.deferred) == 1
    assert app.result.deferred[0].proposal.config == "CONFIG_A"


# ── decision keybindings ───────────────────────────────────────────────────


async def test_press_a_accepts_first_visible_proposal():
    app = ReviewApp(_deferred("CONFIG_A", "CONFIG_B"))
    async with app.run_test() as pilot:
        await pilot.press("a")
        await pilot.press("w")
    assert app.result is not None
    assert {r.proposal.config for r in app.result.accepted} == {"CONFIG_A"}
    assert {r.proposal.config for r in app.result.deferred} == {"CONFIG_B"}


async def test_press_r_rejects_first_visible():
    app = ReviewApp(_deferred("CONFIG_A", "CONFIG_B"))
    async with app.run_test() as pilot:
        await pilot.press("r")
        await pilot.press("w")
    assert app.result is not None
    assert {r.proposal.config for r in app.result.rejected} == {"CONFIG_A"}


async def test_navigation_then_decision_acts_on_navigated_item():
    app = ReviewApp(_deferred("CONFIG_A", "CONFIG_B", "CONFIG_C"))
    async with app.run_test() as pilot:
        await pilot.press("j")  # cursor → CONFIG_B
        await pilot.press("a")
        await pilot.press("w")
    assert app.result is not None
    assert {r.proposal.config for r in app.result.accepted} == {"CONFIG_B"}


async def test_decision_marks_reviewer_user_only_for_touched_items():
    app = ReviewApp(_deferred("CONFIG_A", "CONFIG_B"))
    async with app.run_test() as pilot:
        await pilot.press("a")  # CONFIG_A
        await pilot.press("w")
    rs = app.result
    assert rs is not None
    by_cfg = {
        **{r.proposal.config: r for r in rs.accepted},
        **{r.proposal.config: r for r in rs.deferred},
    }
    assert by_cfg["CONFIG_A"].reviewer == Reviewer.USER
    assert by_cfg["CONFIG_A"].rule == "interactive"
    # CONFIG_B was not touched in the TUI; its rule + reviewer stay as initial.
    assert by_cfg["CONFIG_B"].reviewer == Reviewer.POLICY
    assert by_cfg["CONFIG_B"].rule == "initial"


# ── filters ─────────────────────────────────────────────────────────────────


async def test_cycle_view_eventually_shows_accepted():
    """Default view is DEFERRED; pressing `f` cycles → ALL → ACCEPTED → REJECTED."""
    rs = ReviewSet(
        base_diff_path=Path("/tmp/p.json"),
        accepted=[
            ReviewedProposal(
                proposal=_proposal("CONFIG_A"),
                decision=ReviewDecision.ACCEPT,
                reviewer=Reviewer.POLICY,
                rule="bulk",
            )
        ],
        deferred=[
            ReviewedProposal(
                proposal=_proposal("CONFIG_D"),
                decision=ReviewDecision.DEFER,
                reviewer=Reviewer.POLICY,
                rule="init",
            )
        ],
    )
    app = ReviewApp(rs)
    async with app.run_test() as pilot:
        # Default view: DEFERRED → cursor lands on CONFIG_D
        current = app.state.current()
        assert current is not None
        assert current.proposal.config == "CONFIG_D"
        await pilot.press("f")  # → ALL
        await pilot.press("f")  # → ACCEPTED
        # Now CONFIG_A is the only visible item.
        current = app.state.current()
        assert current is not None
        assert current.proposal.config == "CONFIG_A"
        await pilot.press("q")


async def test_cycle_subsystem_filters_to_one_bucket():
    rs = _deferred("CONFIG_DRM_X", "CONFIG_USB_Y", "CONFIG_DRM_Z")
    app = ReviewApp(rs)
    async with app.run_test() as pilot:
        # Initial: subsystem filter None → all 3 visible
        assert len(app.state.visible()) == 3
        await pilot.press("s")  # → "gpu"
        assert app.state.subsystem_filter == "gpu"
        visible = [i.proposal.config for i in app.state.visible()]
        assert set(visible) == {"CONFIG_DRM_X", "CONFIG_DRM_Z"}
        await pilot.press("q")


# ── edge cases ─────────────────────────────────────────────────────────────


async def test_decision_on_empty_view_is_safe():
    """Pressing 'a' when there's nothing visible must not crash or wedge."""
    rs = ReviewSet(  # all accepted; default view DEFERRED is empty
        base_diff_path=Path("/tmp/p.json"),
        accepted=[
            ReviewedProposal(
                proposal=_proposal("CONFIG_A"),
                decision=ReviewDecision.ACCEPT,
                reviewer=Reviewer.POLICY,
                rule="bulk",
            )
        ],
    )
    app = ReviewApp(rs)
    async with app.run_test() as pilot:
        assert app.state.current() is None
        await pilot.press("a")  # no-op (no visible item to act on)
        await pilot.press("w")
    # Output preserves the input.
    assert app.result is not None
    assert len(app.result.accepted) == 1


async def test_quit_without_save_does_not_persist_changes():
    app = ReviewApp(_deferred("CONFIG_A"))
    async with app.run_test() as pilot:
        await pilot.press("a")  # would accept if saved
        await pilot.press("q")
    assert app.result is None
    # The input ReviewSet was never mutated externally; the App's
    # internal state is moot once it's exited.


# ── complete-coverage invariant ────────────────────────────────────────────


async def test_save_complete_coverage_invariant():
    """No proposal vanishes between input and output."""
    rs = _deferred("CONFIG_A", "CONFIG_B", "CONFIG_C", "CONFIG_D")
    app = ReviewApp(rs)
    async with app.run_test() as pilot:
        await pilot.press("a")  # CONFIG_A
        await pilot.press("j")
        await pilot.press("r")  # CONFIG_B
        # CONFIG_C and CONFIG_D stay deferred
        await pilot.press("w")

    out = app.result
    assert out is not None
    total = len(out.accepted) + len(out.rejected) + len(out.deferred)
    assert total == 4
