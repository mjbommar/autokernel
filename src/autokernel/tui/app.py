"""``ReviewApp``: the Textual orchestrator.

This module is intentionally thin. It owns the :class:`WorkingState`,
composes the widgets, and translates keypresses into state mutations.
All formatting lives in :mod:`autokernel.tui.widgets`; all data
manipulation in :mod:`autokernel.tui.state`.

Exit paths:

* ``w`` (action_save) — sets :attr:`result` to a fresh ReviewSet built
  from the current state and exits the app.
* ``q`` (action_quit_no_save) — exits without writing; ``result`` stays
  ``None``. The CLI treats this as "user cancelled".
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header

from autokernel.models import ReviewDecision, ReviewSet
from autokernel.tui.state import WorkingState
from autokernel.tui.widgets import CountsBar, EvidencePanel, ProposalTable


class ReviewApp(App):
    """Interactive review TUI.

    Construct with the result of bulk-rule application; call ``run()``
    (or ``run_test()`` from tests) and read :attr:`result` afterward.
    """

    CSS_PATH = "review.tcss"

    BINDINGS = [
        Binding("a", "accept", "Accept", show=True),
        Binding("r", "reject", "Reject", show=True),
        Binding("d", "defer", "Defer", show=True),
        Binding("j,down", "next", "Next", show=False),
        Binding("k,up", "prev", "Prev", show=False),
        Binding("g", "first", "Top", show=False),
        Binding("G", "last", "Bottom", show=False),
        Binding("s", "cycle_subsystem", "Subsystem filter", show=True),
        Binding("f", "cycle_view", "View", show=True),
        Binding("w", "save", "Save & exit", show=True),
        Binding("q", "quit_no_save", "Quit (no save)", show=True),
    ]

    def __init__(
        self,
        review_set: ReviewSet,
        *,
        snapshot_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self._state = WorkingState.from_review_set(review_set)
        self._snapshot_dir = snapshot_dir
        self._result: ReviewSet | None = None

    # ── public API ─────────────────────────────────────────────────────────

    @property
    def result(self) -> ReviewSet | None:
        """Final ReviewSet after the user saved (``w``), or ``None`` if
        the user quit without saving (``q``)."""
        return self._result

    @property
    def state(self) -> WorkingState:
        """Test hook: lets tests inspect the working state directly."""
        return self._state

    # ── lifecycle ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        yield CountsBar(id="counts")
        with Horizontal(id="main"):
            yield ProposalTable(id="table")
            yield EvidencePanel(id="evidence")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "autokernel review"
        if self._snapshot_dir:
            self.sub_title = str(self._snapshot_dir)
        self._refresh()

    # ── single-point refresh ───────────────────────────────────────────────

    def _refresh(self) -> None:
        self.query_one(CountsBar).refresh_from(self._state)
        self.query_one(ProposalTable).refresh_from(self._state)
        self.query_one(EvidencePanel).refresh_from(self._state)

    # ── decision actions ──────────────────────────────────────────────────

    def action_accept(self) -> None:
        self._state.set_current_decision(ReviewDecision.ACCEPT)
        self._refresh()

    def action_reject(self) -> None:
        self._state.set_current_decision(ReviewDecision.REJECT)
        self._refresh()

    def action_defer(self) -> None:
        self._state.set_current_decision(ReviewDecision.DEFER)
        self._refresh()

    # ── navigation actions ────────────────────────────────────────────────

    def action_next(self) -> None:
        self._state.move_cursor(+1)
        self._refresh()

    def action_prev(self) -> None:
        self._state.move_cursor(-1)
        self._refresh()

    def action_first(self) -> None:
        self._state.cursor_first()
        self._refresh()

    def action_last(self) -> None:
        self._state.cursor_last()
        self._refresh()

    # ── filter actions ────────────────────────────────────────────────────

    def action_cycle_subsystem(self) -> None:
        self._state.cycle_subsystem()
        self._refresh()

    def action_cycle_view(self) -> None:
        self._state.cycle_decision_view()
        self._refresh()

    # ── DataTable cursor sync ─────────────────────────────────────────────

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Mouse-click or built-in arrow-key cursor moves bypass our
        action_next/prev. Sync our state so the evidence panel stays in
        sync with the table cursor."""
        self._state.cursor = event.cursor_row
        self.query_one(EvidencePanel).refresh_from(self._state)

    # ── exit paths ────────────────────────────────────────────────────────

    def action_save(self) -> None:
        self._result = self._state.to_review_set()
        self.exit()

    def action_quit_no_save(self) -> None:
        self._result = None
        self.exit()
