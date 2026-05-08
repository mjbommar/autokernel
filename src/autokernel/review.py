"""Bulk decision engine for ``autokernel review``.

Composable rules turn a ``ConfigDiff.needs_review`` list into a
``ReviewSet`` (accepted / rejected / deferred). The engine is pure:
deterministic given the same input + rule sequence; trivially testable.

Rules are applied **in order**; the FIRST rule that matches a proposal
sets its decision and the proposal is removed from the pool. Anything
left after all rules run goes to ``deferred`` (the safe default).

Example::

    rules = [
        AcceptRule(risk={LOW}, source={DETERMINISTIC}, label="accept-deterministic-low"),
        RejectRule(subsystems={"crypto", "security"}, label="reject-crypto-security"),
        AcceptRule(risk={LOW, MEDIUM}, label="accept-rest-low-medium"),
    ]
    review_set = apply_rules(needs_review, rules, base_diff_path=...)

This file does not invoke any LLM. The ``review`` verb may *itself* call
out to Claude for hard cases, but that's a separate orchestrator wrapping
this deterministic engine.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from autokernel.models import (
    ProposalSource,
    RemovalProposal,
    ReviewDecision,
    ReviewedProposal,
    Reviewer,
    ReviewSet,
    RiskLevel,
)
from autokernel.subsystem import classify


# ── rule types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _RuleBase:
    """Common matcher fields. ``None`` means "don't filter on this axis".

    Globs match against the symbol name; subsystems match against the
    classifier's bucket label.
    """

    label: str
    risk: frozenset[RiskLevel] | None = None
    source: frozenset[ProposalSource] | None = None
    subsystems: frozenset[str] | None = None
    name_globs: tuple[str, ...] = ()  # e.g. ("CONFIG_DRM_*", "CONFIG_FB_*")
    min_confidence: float | None = None
    max_confidence: float | None = None

    def matches(self, p: RemovalProposal) -> bool:
        if self.risk is not None and p.risk not in self.risk:
            return False
        if self.source is not None and p.source not in self.source:
            return False
        if self.subsystems is not None and classify(p.config) not in self.subsystems:
            return False
        if self.name_globs and not any(fnmatch.fnmatchcase(p.config, g) for g in self.name_globs):
            return False
        if self.min_confidence is not None and p.confidence < self.min_confidence:
            return False
        if self.max_confidence is not None and p.confidence > self.max_confidence:
            return False
        return True


@dataclass(frozen=True)
class AcceptRule(_RuleBase):
    """Bulk-accept matching proposals."""


@dataclass(frozen=True)
class RejectRule(_RuleBase):
    """Bulk-reject matching proposals (keep the current value)."""


@dataclass(frozen=True)
class DeferRule(_RuleBase):
    """Explicitly leave matching proposals undecided."""


# ── application ─────────────────────────────────────────────────────────────


def apply_rules(
    proposals: list[RemovalProposal],
    rules: list[AcceptRule | RejectRule | DeferRule],
    *,
    base_diff_path: Path,
    reviewer: Reviewer = Reviewer.POLICY,
) -> ReviewSet:
    """Run ``proposals`` through ``rules`` in order; return a complete
    ``ReviewSet``.

    Anything not matched by any rule lands in ``deferred`` with rule label
    ``'unmatched'`` and reviewer left unchanged.
    """
    out_accepted: list[ReviewedProposal] = []
    out_rejected: list[ReviewedProposal] = []
    out_deferred: list[ReviewedProposal] = []

    for p in proposals:
        chosen_rule: _RuleBase | None = None
        chosen_decision: ReviewDecision | None = None

        for r in rules:
            if r.matches(p):
                chosen_rule = r
                if isinstance(r, AcceptRule):
                    chosen_decision = ReviewDecision.ACCEPT
                elif isinstance(r, RejectRule):
                    chosen_decision = ReviewDecision.REJECT
                else:
                    chosen_decision = ReviewDecision.DEFER
                break

        if chosen_decision is None:
            out_deferred.append(
                ReviewedProposal(
                    proposal=p,
                    decision=ReviewDecision.DEFER,
                    reviewer=reviewer,
                    rule="unmatched",
                )
            )
            continue

        rp = ReviewedProposal(
            proposal=p,
            decision=chosen_decision,
            reviewer=reviewer,
            rule=chosen_rule.label if chosen_rule else None,
        )
        if chosen_decision == ReviewDecision.ACCEPT:
            out_accepted.append(rp)
        elif chosen_decision == ReviewDecision.REJECT:
            out_rejected.append(rp)
        else:
            out_deferred.append(rp)

    return ReviewSet(
        base_diff_path=base_diff_path,
        accepted=out_accepted,
        rejected=out_rejected,
        deferred=out_deferred,
    )


# ── high-level preset rule sets ─────────────────────────────────────────────


def preset_accept_recommended() -> list[AcceptRule | RejectRule | DeferRule]:
    """Accept everything not explicitly high-risk.

    Equivalent to ``--accept-recommended``. The build stage will still
    honor the load-bearing blocklist (which acted before review), so this
    is safe — but the user is delegating the medium-confidence calls to
    the LLM/deterministic rules.
    """
    return [
        AcceptRule(
            label="accept-recommended-not-high-risk",
            risk=frozenset({RiskLevel.LOW, RiskLevel.MEDIUM}),
        ),
        DeferRule(label="defer-high-risk", risk=frozenset({RiskLevel.HIGH})),
    ]


def preset_accept_low_risk() -> list[AcceptRule | RejectRule | DeferRule]:
    """Accept only ``risk=LOW`` proposals; defer the rest."""
    return [
        AcceptRule(label="accept-low-risk", risk=frozenset({RiskLevel.LOW})),
    ]


def preset_accept_deterministic() -> list[AcceptRule | RejectRule | DeferRule]:
    """Accept only deterministic proposals; defer LLM proposals.

    Useful as a confidence-bearing baseline before stepping through LLM
    suggestions interactively.
    """
    return [
        AcceptRule(
            label="accept-deterministic",
            source=frozenset({ProposalSource.DETERMINISTIC}),
        ),
    ]


def reject_subsystems_rule(subsystems: list[str]) -> RejectRule:
    """Veto every proposal whose symbol classifies into one of ``subsystems``.

    Common usage::

        reject_subsystems_rule(["crypto", "security", "kasan"])

    keeps all crypto/security/kasan-related symbols at their current value,
    on the principle that the user should opt in to trimming those.
    """
    return RejectRule(
        label=f"reject-subsystems:{','.join(sorted(subsystems))}",
        subsystems=frozenset(subsystems),
    )


def reject_pattern_rule(patterns: list[str]) -> RejectRule:
    """Veto symbols matching any of the glob patterns."""
    return RejectRule(
        label=f"reject-pattern:{','.join(patterns)}",
        name_globs=tuple(patterns),
    )
