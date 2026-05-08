"""Working state for the review TUI.

A :class:`WorkingState` is a mutable view over a :class:`ReviewSet` that
the App edits in response to keybindings. It owns:

* the per-proposal current decision + the rule label that produced it,
* the active filters (subsystem + decision view),
* the cursor position into the *filtered* view.

It exposes methods to:

* enumerate the visible items (after filters),
* mutate the current item's decision,
* cycle filters,
* build a fresh :class:`ReviewSet` for the caller to persist.

This module is **pure data + logic**. No Textual imports. Tests cover
behavior end-to-end without ever rendering a UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from autokernel.models import (
    RemovalProposal,
    ReviewDecision,
    Reviewer,
    ReviewSet,
    ReviewedProposal,
)
from autokernel.subsystem import classify
from autokernel.tui.filters import DecisionView, SubsystemCycler


_INTERACTIVE_RULE = "interactive"


@dataclass
class Item:
    """One row in the working state: a proposal + the decision currently
    attached to it + the rule label that produced that decision.

    ``rule == 'interactive'`` indicates the user touched it in the TUI
    (and hence the reviewer should be recorded as :class:`Reviewer.USER`).
    Any other rule is preserved verbatim from the input ReviewSet so
    the audit trail isn't lost when the TUI saves.
    """

    proposal: RemovalProposal
    decision: ReviewDecision
    rule: str

    @property
    def subsystem(self) -> str:
        return classify(self.proposal.config)

    @property
    def touched_by_user(self) -> bool:
        return self.rule == _INTERACTIVE_RULE


@dataclass
class WorkingState:
    """Mutable working state over a :class:`ReviewSet`."""

    items: list[Item]
    base_diff_path: Path
    subsystem_filter: str | None = None
    decision_view: DecisionView = field(default_factory=DecisionView.default)
    cursor: int = 0
    """Index into the *filtered* list, not :attr:`items`. Reset to 0 on
    any filter change so we never point past the end of the filtered view."""

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def from_review_set(cls, review_set: ReviewSet) -> "WorkingState":
        items: list[Item] = []
        for rp in review_set.accepted:
            items.append(Item(rp.proposal, ReviewDecision.ACCEPT, rp.rule or "policy"))
        for rp in review_set.rejected:
            items.append(Item(rp.proposal, ReviewDecision.REJECT, rp.rule or "policy"))
        for rp in review_set.deferred:
            items.append(Item(rp.proposal, ReviewDecision.DEFER, rp.rule or "deferred"))
        return cls(items=items, base_diff_path=review_set.base_diff_path)

    # ── visible-view derivations ───────────────────────────────────────────

    def visible(self) -> list[Item]:
        """Items matching all active filters, in insertion order."""
        return [
            item
            for item in self.items
            if self.decision_view.matches(item.decision)
            and (self.subsystem_filter is None or item.subsystem == self.subsystem_filter)
        ]

    def all_subsystems(self) -> list[str]:
        """Subsystems represented in :attr:`items`, ordered by first appearance."""
        seen: list[str] = []
        for item in self.items:
            if item.subsystem not in seen:
                seen.append(item.subsystem)
        return seen

    def current(self) -> Item | None:
        v = self.visible()
        if not v:
            return None
        if self.cursor >= len(v):
            self.cursor = max(0, len(v) - 1)
        return v[self.cursor]

    def counts(self) -> tuple[int, int, int]:
        """Total accepted / rejected / deferred across all items
        (independent of filters)."""
        a = sum(1 for i in self.items if i.decision == ReviewDecision.ACCEPT)
        r = sum(1 for i in self.items if i.decision == ReviewDecision.REJECT)
        d = sum(1 for i in self.items if i.decision == ReviewDecision.DEFER)
        return a, r, d

    # ── mutators ───────────────────────────────────────────────────────────

    def set_current_decision(self, decision: ReviewDecision) -> Item | None:
        """Set the current visible item's decision (and tag rule='interactive').
        Returns the touched item, or ``None`` if the filter is empty."""
        item = self.current()
        if item is None:
            return None
        item.decision = decision
        item.rule = _INTERACTIVE_RULE
        return item

    def move_cursor(self, delta: int) -> None:
        """Move cursor by ``delta``, clamped to the visible range."""
        n = len(self.visible())
        if n == 0:
            self.cursor = 0
            return
        self.cursor = max(0, min(self.cursor + delta, n - 1))

    def cursor_first(self) -> None:
        self.cursor = 0

    def cursor_last(self) -> None:
        n = len(self.visible())
        self.cursor = max(0, n - 1)

    def cycle_subsystem(self) -> None:
        cycler = SubsystemCycler(self.all_subsystems())
        self.subsystem_filter = cycler.cycle(self.subsystem_filter)
        self.cursor = 0

    def cycle_decision_view(self) -> None:
        self.decision_view = self.decision_view.cycle()
        self.cursor = 0

    # ── output ─────────────────────────────────────────────────────────────

    def to_review_set(self) -> ReviewSet:
        """Build a fresh :class:`ReviewSet` reflecting the current decisions.

        Items the user touched in the TUI get :class:`Reviewer.USER`;
        items that retain their input rule keep :class:`Reviewer.POLICY`
        — preserving the original audit trail.
        """
        accepted: list[ReviewedProposal] = []
        rejected: list[ReviewedProposal] = []
        deferred: list[ReviewedProposal] = []

        for item in self.items:
            reviewer = Reviewer.USER if item.touched_by_user else Reviewer.POLICY
            rp = ReviewedProposal(
                proposal=item.proposal,
                decision=item.decision,
                reviewer=reviewer,
                rule=item.rule,
            )
            if item.decision == ReviewDecision.ACCEPT:
                accepted.append(rp)
            elif item.decision == ReviewDecision.REJECT:
                rejected.append(rp)
            else:
                deferred.append(rp)

        return ReviewSet(
            base_diff_path=self.base_diff_path,
            accepted=accepted,
            rejected=rejected,
            deferred=deferred,
        )
