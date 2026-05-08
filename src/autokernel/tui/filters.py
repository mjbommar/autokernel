"""Pure-logic filter cyclers used by the review TUI.

These don't depend on Textual at all — they're tiny state machines that
the App mutates in response to keybindings. Splitting them out keeps the
App thin and makes the cycling behavior unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from autokernel.models import ReviewDecision


class DecisionView(str, Enum):
    """Which decisions are visible in the proposal table.

    Cycles via :meth:`cycle` in the order DEFERRED → ALL → ACCEPTED →
    REJECTED → DEFERRED. Defaults to DEFERRED on app start because that's
    the bucket the user came here to work through.

    Note that the View labels and ReviewDecision values use different
    vocabularies (``deferred`` vs ``defer``). The mapping is in
    :data:`_VIEW_TO_DECISION`; :meth:`matches` is the single intended
    consumer.
    """

    DEFERRED = "deferred"
    ALL = "all"
    ACCEPTED = "accepted"
    REJECTED = "rejected"

    @classmethod
    def default(cls) -> "DecisionView":
        return cls.DEFERRED

    def cycle(self) -> "DecisionView":
        order = [
            DecisionView.DEFERRED,
            DecisionView.ALL,
            DecisionView.ACCEPTED,
            DecisionView.REJECTED,
        ]
        return order[(order.index(self) + 1) % len(order)]

    def matches(self, decision: ReviewDecision) -> bool:
        if self == DecisionView.ALL:
            return True
        return _VIEW_TO_DECISION[self] == decision


_VIEW_TO_DECISION: dict[DecisionView, ReviewDecision] = {
    DecisionView.DEFERRED: ReviewDecision.DEFER,
    DecisionView.ACCEPTED: ReviewDecision.ACCEPT,
    DecisionView.REJECTED: ReviewDecision.REJECT,
}


@dataclass
class SubsystemCycler:
    """Cycles a current selection through ``None → s[0] → s[1] → … → None``.

    ``None`` means "no subsystem filter; show all". The cycler is built
    from the ordered list of subsystems present in the working state, so
    if a state contains no `crypto` proposals, `crypto` is never offered
    as a filter — the cycle only steps through buckets the user can
    actually drill into.
    """

    subsystems: list[str]

    def cycle(self, current: str | None) -> str | None:
        if not self.subsystems:
            return None
        if current is None:
            return self.subsystems[0]
        try:
            idx = self.subsystems.index(current)
        except ValueError:
            return None  # filter pointed at a vanished subsystem; reset
        if idx + 1 >= len(self.subsystems):
            return None  # wrap back to "all"
        return self.subsystems[idx + 1]
