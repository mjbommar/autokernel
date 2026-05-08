"""Pure-logic tests for the TUI: filters, working state, evidence rendering.

No Textual app instances are spun up here — these are fast unit tests
of the data layer. Pilot-driven end-to-end tests live in test_tui.py.
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
from autokernel.tui.filters import DecisionView, SubsystemCycler
from autokernel.tui.state import WorkingState
from autokernel.tui.widgets import _render_evidence


# ── helpers ─────────────────────────────────────────────────────────────────


def _proposal(config: str, *, risk: RiskLevel = RiskLevel.LOW, conf: float = 0.9) -> RemovalProposal:
    return RemovalProposal(
        config=config,
        current_value="m",
        proposed_value="n",
        reason=f"reason for {config}",
        risk=risk,
        confidence=conf,
        source=ProposalSource.LLM,
        evidence=[],
    )


def _rp(config: str, decision: ReviewDecision, rule: str = "policy") -> ReviewedProposal:
    return ReviewedProposal(
        proposal=_proposal(config),
        decision=decision,
        reviewer=Reviewer.POLICY,
        rule=rule,
    )


def _build_review_set(
    accepted: list[str] = (),
    rejected: list[str] = (),
    deferred: list[str] = (),
    base_diff_path: Path = Path("/tmp/p.json"),
) -> ReviewSet:
    return ReviewSet(
        base_diff_path=base_diff_path,
        accepted=[_rp(c, ReviewDecision.ACCEPT) for c in accepted],
        rejected=[_rp(c, ReviewDecision.REJECT) for c in rejected],
        deferred=[_rp(c, ReviewDecision.DEFER) for c in deferred],
    )


# ── DecisionView ────────────────────────────────────────────────────────────


def test_decision_view_default_is_deferred():
    assert DecisionView.default() == DecisionView.DEFERRED


def test_decision_view_cycle_loops():
    seq = []
    v = DecisionView.default()
    for _ in range(5):
        seq.append(v)
        v = v.cycle()
    # After 4 cycles we should be back at the start.
    assert seq[0] == seq[4]


def test_decision_view_all_matches_everything():
    for d in ReviewDecision:
        assert DecisionView.ALL.matches(d)


def test_decision_view_specific_matches_only_self():
    assert DecisionView.DEFERRED.matches(ReviewDecision.DEFER)
    assert not DecisionView.DEFERRED.matches(ReviewDecision.ACCEPT)
    assert DecisionView.ACCEPTED.matches(ReviewDecision.ACCEPT)
    assert DecisionView.REJECTED.matches(ReviewDecision.REJECT)


# ── SubsystemCycler ────────────────────────────────────────────────────────


def test_subsystem_cycler_empty_list_always_returns_none():
    c = SubsystemCycler([])
    assert c.cycle(None) is None
    assert c.cycle("anything") is None


def test_subsystem_cycler_single_subsystem():
    c = SubsystemCycler(["gpu"])
    assert c.cycle(None) == "gpu"
    assert c.cycle("gpu") is None  # wraps to "all" (None)


def test_subsystem_cycler_walks_in_order_then_wraps():
    c = SubsystemCycler(["gpu", "fs", "crypto"])
    assert c.cycle(None) == "gpu"
    assert c.cycle("gpu") == "fs"
    assert c.cycle("fs") == "crypto"
    assert c.cycle("crypto") is None  # wrap


def test_subsystem_cycler_unknown_current_resets():
    c = SubsystemCycler(["gpu", "fs"])
    assert c.cycle("missing") is None


# ── WorkingState construction ──────────────────────────────────────────────


def test_from_review_set_preserves_decisions_and_rules():
    rs = ReviewSet(
        base_diff_path=Path("/x"),
        accepted=[_rp("CONFIG_A", ReviewDecision.ACCEPT, rule="bulk-low")],
        rejected=[_rp("CONFIG_B", ReviewDecision.REJECT, rule="reject-crypto")],
        deferred=[_rp("CONFIG_C", ReviewDecision.DEFER, rule="unmatched")],
    )
    state = WorkingState.from_review_set(rs)

    by_cfg = {item.proposal.config: item for item in state.items}
    assert by_cfg["CONFIG_A"].decision == ReviewDecision.ACCEPT
    assert by_cfg["CONFIG_A"].rule == "bulk-low"
    assert by_cfg["CONFIG_B"].decision == ReviewDecision.REJECT
    assert by_cfg["CONFIG_C"].decision == ReviewDecision.DEFER


def test_from_review_set_uses_default_rule_when_missing():
    """A ReviewedProposal with rule=None (which the Pydantic model allows)
    becomes 'policy' / 'deferred' depending on bucket."""
    rs = ReviewSet(
        base_diff_path=Path("/x"),
        accepted=[
            ReviewedProposal(
                proposal=_proposal("CONFIG_X"),
                decision=ReviewDecision.ACCEPT,
                reviewer=Reviewer.POLICY,
                rule=None,
            ),
        ],
    )
    state = WorkingState.from_review_set(rs)
    assert state.items[0].rule == "policy"


# ── visible() filter behavior ──────────────────────────────────────────────


def test_visible_default_view_shows_only_deferred():
    rs = _build_review_set(accepted=["A"], rejected=["B"], deferred=["C", "D"])
    state = WorkingState.from_review_set(rs)
    visible = [i.proposal.config for i in state.visible()]
    assert visible == ["C", "D"]


def test_visible_view_all_shows_everything():
    rs = _build_review_set(accepted=["A"], rejected=["B"], deferred=["C"])
    state = WorkingState.from_review_set(rs)
    state.decision_view = DecisionView.ALL
    visible = [i.proposal.config for i in state.visible()]
    assert set(visible) == {"A", "B", "C"}


def test_visible_subsystem_filter_intersects_with_decision_view():
    rs = _build_review_set(deferred=["CONFIG_DRM_FOO", "CONFIG_USB_BAR", "CONFIG_DRM_BAZ"])
    state = WorkingState.from_review_set(rs)
    state.subsystem_filter = "gpu"  # DRM_* classifies as gpu
    visible = [i.proposal.config for i in state.visible()]
    assert visible == ["CONFIG_DRM_FOO", "CONFIG_DRM_BAZ"]


# ── all_subsystems / cycler integration ────────────────────────────────────


def test_all_subsystems_preserves_first_appearance_order():
    rs = _build_review_set(
        deferred=["CONFIG_DRM_X", "CONFIG_USB_Y", "CONFIG_DRM_Z"]
    )
    state = WorkingState.from_review_set(rs)
    assert state.all_subsystems() == ["gpu", "usb"]


def test_cycle_subsystem_walks_through_buckets_then_resets():
    rs = _build_review_set(deferred=["CONFIG_DRM_X", "CONFIG_USB_Y"])
    state = WorkingState.from_review_set(rs)
    assert state.subsystem_filter is None
    state.cycle_subsystem(); assert state.subsystem_filter == "gpu"
    state.cycle_subsystem(); assert state.subsystem_filter == "usb"
    state.cycle_subsystem(); assert state.subsystem_filter is None


def test_cycle_subsystem_resets_cursor_to_zero():
    rs = _build_review_set(deferred=["A", "B", "C"])
    state = WorkingState.from_review_set(rs)
    state.cursor = 2
    state.cycle_subsystem()
    assert state.cursor == 0


# ── current() + cursor ─────────────────────────────────────────────────────


def test_current_returns_none_when_visible_empty():
    rs = _build_review_set(accepted=["A"])  # no deferred → default view empty
    state = WorkingState.from_review_set(rs)
    assert state.current() is None


def test_current_clamps_cursor_within_visible():
    rs = _build_review_set(deferred=["A", "B"])
    state = WorkingState.from_review_set(rs)
    state.cursor = 99
    item = state.current()
    assert item is not None
    # Clamped to last visible
    assert item.proposal.config == "B"
    assert state.cursor == 1


def test_move_cursor_clamps_to_visible_range():
    rs = _build_review_set(deferred=["A", "B", "C"])
    state = WorkingState.from_review_set(rs)
    state.move_cursor(+5)
    assert state.cursor == 2  # last index
    state.move_cursor(-99)
    assert state.cursor == 0


def test_cursor_first_and_last():
    rs = _build_review_set(deferred=["A", "B", "C"])
    state = WorkingState.from_review_set(rs)
    state.cursor_last(); assert state.cursor == 2
    state.cursor_first(); assert state.cursor == 0


def test_move_cursor_on_empty_visible_is_safe():
    rs = _build_review_set()
    state = WorkingState.from_review_set(rs)
    state.move_cursor(+5)  # should not raise
    assert state.cursor == 0


# ── set_current_decision ───────────────────────────────────────────────────


def test_set_current_decision_mutates_and_tags_interactive():
    rs = _build_review_set(deferred=["A", "B"])
    state = WorkingState.from_review_set(rs)
    item = state.set_current_decision(ReviewDecision.ACCEPT)
    assert item is not None
    assert item.decision == ReviewDecision.ACCEPT
    assert item.rule == "interactive"
    assert item.touched_by_user is True


def test_set_current_decision_returns_none_when_no_visible():
    rs = _build_review_set(accepted=["only-accepted"])
    state = WorkingState.from_review_set(rs)
    # Default view is DEFERRED, so visible is empty
    assert state.set_current_decision(ReviewDecision.ACCEPT) is None


# ── counts() ───────────────────────────────────────────────────────────────


def test_counts_independent_of_filters():
    rs = _build_review_set(accepted=["A", "B"], rejected=["C"], deferred=["D"])
    state = WorkingState.from_review_set(rs)
    state.subsystem_filter = "fs"  # nothing matches
    assert state.counts() == (2, 1, 1)


# ── to_review_set ──────────────────────────────────────────────────────────


def test_to_review_set_preserves_untouched_audit_trail():
    """Items the user didn't touch keep their input rule + Reviewer.POLICY."""
    rs = _build_review_set(accepted=["A"], deferred=["B"])
    state = WorkingState.from_review_set(rs)
    # User touches B (and only B)
    state.set_current_decision(ReviewDecision.REJECT)

    out = state.to_review_set()
    assert len(out.accepted) == 1 and out.accepted[0].proposal.config == "A"
    assert len(out.rejected) == 1 and out.rejected[0].proposal.config == "B"

    # A keeps Reviewer.POLICY (untouched)
    assert out.accepted[0].reviewer == Reviewer.POLICY
    # B was touched → Reviewer.USER
    assert out.rejected[0].reviewer == Reviewer.USER
    assert out.rejected[0].rule == "interactive"


def test_to_review_set_complete_coverage():
    """Every input item must land in exactly one output bucket."""
    rs = _build_review_set(
        accepted=["A1", "A2"],
        rejected=["R1"],
        deferred=["D1", "D2", "D3"],
    )
    state = WorkingState.from_review_set(rs)
    out = state.to_review_set()
    total = len(out.accepted) + len(out.rejected) + len(out.deferred)
    assert total == 6


# ── evidence rendering (pure) ──────────────────────────────────────────────


def test_render_evidence_includes_key_fields():
    rs = _build_review_set(deferred=["CONFIG_DRM_NOUVEAU"])
    state = WorkingState.from_review_set(rs)
    item = state.current()
    rendered = _render_evidence(item)
    assert "CONFIG_DRM_NOUVEAU" in rendered
    assert "reason for CONFIG_DRM_NOUVEAU" in rendered
    assert "low" in rendered
    assert "0.90" in rendered
    assert "gpu" in rendered  # subsystem
    # Decision shown via ReviewDecision.value ("defer", not "deferred").
    assert "defer" in rendered


def test_render_evidence_lists_evidence_block_when_present():
    p = RemovalProposal(
        config="CONFIG_X",
        current_value="m", proposed_value="n",
        reason="r", risk=RiskLevel.LOW, confidence=0.9,
        source=ProposalSource.LLM,
        evidence=["pci.vendor_id=8086", "lspci slot 00:02.0"],
    )
    rs = ReviewSet(
        base_diff_path=Path("/x"),
        deferred=[ReviewedProposal(
            proposal=p, decision=ReviewDecision.DEFER,
            reviewer=Reviewer.POLICY, rule="t",
        )],
    )
    state = WorkingState.from_review_set(rs)
    rendered = _render_evidence(state.current())
    assert "pci.vendor_id=8086" in rendered
    assert "lspci slot 00:02.0" in rendered
