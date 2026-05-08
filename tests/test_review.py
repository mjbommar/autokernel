"""Tests for the bulk decision engine."""

from __future__ import annotations

from pathlib import Path


from autokernel.models import (
    ProposalSource,
    RemovalProposal,
    Reviewer,
    RiskLevel,
)
from autokernel.review import (
    AcceptRule,
    DeferRule,
    RejectRule,
    apply_rules,
    preset_accept_deterministic,
    preset_accept_low_risk,
    preset_accept_recommended,
    reject_pattern_rule,
    reject_subsystems_rule,
)


def _p(
    config: str = "CONFIG_FOO",
    *,
    risk: RiskLevel = RiskLevel.LOW,
    conf: float = 0.9,
    source: ProposalSource = ProposalSource.LLM,
) -> RemovalProposal:
    return RemovalProposal(
        config=config,
        current_value="m",
        proposed_value="n",
        reason="test",
        risk=risk,
        confidence=conf,
        source=source,
        evidence=[],
    )


_BASE = Path("/tmp/fake-snap/proposal.json")


# ── single-rule basics ──────────────────────────────────────────────────────


def test_first_matching_rule_wins():
    p = _p(risk=RiskLevel.LOW)
    rules = [
        AcceptRule(label="accept-low", risk=frozenset({RiskLevel.LOW})),
        RejectRule(label="reject-low", risk=frozenset({RiskLevel.LOW})),
    ]
    rs = apply_rules([p], rules, base_diff_path=_BASE)
    assert len(rs.accepted) == 1
    assert rs.accepted[0].rule == "accept-low"
    assert not rs.rejected
    assert not rs.deferred


def test_unmatched_proposals_deferred():
    p = _p(risk=RiskLevel.HIGH)
    rules = [AcceptRule(label="only-low", risk=frozenset({RiskLevel.LOW}))]
    rs = apply_rules([p], rules, base_diff_path=_BASE)
    assert not rs.accepted
    assert len(rs.deferred) == 1
    assert rs.deferred[0].rule == "unmatched"


def test_explicit_defer_rule():
    p = _p(risk=RiskLevel.HIGH)
    rules = [DeferRule(label="defer-high", risk=frozenset({RiskLevel.HIGH}))]
    rs = apply_rules([p], rules, base_diff_path=_BASE)
    assert len(rs.deferred) == 1
    assert rs.deferred[0].rule == "defer-high"


def test_subsystem_filter_uses_classifier():
    drm = _p(config="CONFIG_DRM_NOUVEAU")
    crypto = _p(config="CONFIG_CRYPTO_AES")
    rules = [
        RejectRule(label="keep-crypto", subsystems=frozenset({"crypto"})),
        AcceptRule(label="accept-rest"),
    ]
    rs = apply_rules([drm, crypto], rules, base_diff_path=_BASE)
    assert {r.proposal.config for r in rs.accepted} == {"CONFIG_DRM_NOUVEAU"}
    assert {r.proposal.config for r in rs.rejected} == {"CONFIG_CRYPTO_AES"}


def test_name_glob_filter():
    a = _p(config="CONFIG_DRM_NOUVEAU")
    b = _p(config="CONFIG_USB_HID")
    rules = [
        RejectRule(label="keep-drm", name_globs=("CONFIG_DRM_*",)),
        AcceptRule(label="accept-rest"),
    ]
    rs = apply_rules([a, b], rules, base_diff_path=_BASE)
    assert rs.rejected[0].proposal.config == "CONFIG_DRM_NOUVEAU"
    assert rs.accepted[0].proposal.config == "CONFIG_USB_HID"


def test_confidence_filter():
    high = _p(conf=0.95)
    low = _p(conf=0.5)
    rules = [AcceptRule(label="high-only", min_confidence=0.9)]
    rs = apply_rules([high, low], rules, base_diff_path=_BASE)
    assert len(rs.accepted) == 1
    assert len(rs.deferred) == 1


# ── presets ─────────────────────────────────────────────────────────────────


def test_preset_accept_recommended_keeps_high_risk_in_review():
    proposals = [
        _p(config="CONFIG_A", risk=RiskLevel.LOW),
        _p(config="CONFIG_B", risk=RiskLevel.MEDIUM),
        _p(config="CONFIG_C", risk=RiskLevel.HIGH),
    ]
    rs = apply_rules(proposals, preset_accept_recommended(), base_diff_path=_BASE)
    assert {r.proposal.config for r in rs.accepted} == {"CONFIG_A", "CONFIG_B"}
    assert {r.proposal.config for r in rs.deferred} == {"CONFIG_C"}


def test_preset_accept_low_risk_only():
    proposals = [
        _p(config="CONFIG_LOW", risk=RiskLevel.LOW),
        _p(config="CONFIG_MED", risk=RiskLevel.MEDIUM),
    ]
    rs = apply_rules(proposals, preset_accept_low_risk(), base_diff_path=_BASE)
    assert {r.proposal.config for r in rs.accepted} == {"CONFIG_LOW"}
    assert {r.proposal.config for r in rs.deferred} == {"CONFIG_MED"}


def test_preset_accept_deterministic_only():
    proposals = [
        _p(config="CONFIG_D", source=ProposalSource.DETERMINISTIC),
        _p(config="CONFIG_L", source=ProposalSource.LLM),
    ]
    rs = apply_rules(proposals, preset_accept_deterministic(), base_diff_path=_BASE)
    assert rs.accepted[0].proposal.config == "CONFIG_D"
    assert rs.deferred[0].proposal.config == "CONFIG_L"


def test_layered_rules_compose():
    """Veto crypto, accept everything else low-risk, leave high-risk for review."""
    proposals = [
        _p(config="CONFIG_CRYPTO_AES", risk=RiskLevel.LOW),
        _p(config="CONFIG_DRM_NOUVEAU", risk=RiskLevel.LOW),
        _p(config="CONFIG_DEBUG_KERNEL", risk=RiskLevel.HIGH),
    ]
    rules = [
        reject_subsystems_rule(["crypto"]),
        *preset_accept_recommended(),
    ]
    rs = apply_rules(proposals, rules, base_diff_path=_BASE)
    assert {r.proposal.config for r in rs.rejected} == {"CONFIG_CRYPTO_AES"}
    assert {r.proposal.config for r in rs.accepted} == {"CONFIG_DRM_NOUVEAU"}
    assert {r.proposal.config for r in rs.deferred} == {"CONFIG_DEBUG_KERNEL"}


def test_reviewer_recorded():
    p = _p()
    rs = apply_rules(
        [p],
        [AcceptRule(label="x")],
        base_diff_path=_BASE,
        reviewer=Reviewer.CLAUDE,
    )
    assert rs.accepted[0].reviewer == Reviewer.CLAUDE


def test_reject_pattern_helper():
    proposals = [
        _p(config="CONFIG_DRM_FOO"),
        _p(config="CONFIG_DRM_BAR"),
        _p(config="CONFIG_USB_BAZ"),
    ]
    rules = [reject_pattern_rule(["CONFIG_DRM_*"]), AcceptRule(label="rest")]
    rs = apply_rules(proposals, rules, base_diff_path=_BASE)
    assert {r.proposal.config for r in rs.rejected} == {
        "CONFIG_DRM_FOO",
        "CONFIG_DRM_BAR",
    }
    assert {r.proposal.config for r in rs.accepted} == {"CONFIG_USB_BAZ"}


def test_complete_coverage_invariant():
    """Every input proposal lands in exactly one bucket."""
    proposals = [_p(config=f"CONFIG_X{i}") for i in range(20)]
    rs = apply_rules(proposals, preset_accept_recommended(), base_diff_path=_BASE)
    counts = len(rs.accepted) + len(rs.rejected) + len(rs.deferred)
    assert counts == len(proposals)
