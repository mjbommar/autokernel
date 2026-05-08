"""Reusable widgets for the review TUI.

Each widget knows how to render one piece of the state and exposes a
single ``refresh_from(state)`` method so the App can drive updates
without the widgets reaching back into App-internal data.

Splitting widgets from the App means:

* the rendering logic for proposals lives in one place (no string
  formatting in the App),
* widgets can be unit-tested by mounting them in tiny single-widget
  test apps,
* swapping a widget (e.g. moving from a DataTable to a Tree) doesn't
  ripple into the orchestrator.
"""

from __future__ import annotations

from textual.widgets import DataTable, Static

from autokernel.models import ReviewDecision, RiskLevel
from autokernel.tui.state import Item, WorkingState


# ── small helpers (rendering primitives) ───────────────────────────────────


_DECISION_GLYPH = {
    ReviewDecision.ACCEPT: "[green]✓[/green]",
    ReviewDecision.REJECT: "[red]✗[/red]",
    ReviewDecision.DEFER: "[yellow]·[/yellow]",
}


_RISK_COLOR = {
    RiskLevel.LOW: "green",
    RiskLevel.MEDIUM: "yellow",
    RiskLevel.HIGH: "red",
}


def render_decision(decision: ReviewDecision) -> str:
    return _DECISION_GLYPH[decision]


def render_risk(risk: RiskLevel) -> str:
    return f"[{_RISK_COLOR[risk]}]{risk.value}[/{_RISK_COLOR[risk]}]"


# ── widgets ────────────────────────────────────────────────────────────────


class CountsBar(Static):
    """Single-line bar showing accept / reject / defer totals + active filters."""

    DEFAULT_ID = "counts"

    def refresh_from(self, state: WorkingState) -> None:
        a, r, d = state.counts()
        view = f"  view: {state.decision_view.value}"
        sub = f"  subsystem: {state.subsystem_filter}" if state.subsystem_filter else ""
        self.update(
            f"[green]✓ {a} accepted[/green]   "
            f"[red]✗ {r} rejected[/red]   "
            f"[yellow]· {d} deferred[/yellow]"
            f"{view}{sub}"
        )


class ProposalTable(DataTable):
    """Tabular view of the visible proposals.

    Columns: status glyph, symbol, subsystem, value transition, risk,
    confidence, source. Maintains insertion order — the App owns the
    cursor position and pushes it via :meth:`refresh_from`.
    """

    DEFAULT_ID = "table"

    _COLUMNS = [
        (" ", "status", 1),
        ("symbol", "symbol", None),
        ("subsystem", "subsystem", None),
        ("from→to", "vals", None),
        ("risk", "risk", None),
        ("conf", "conf", None),
        ("src", "src", None),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cursor_type = "row"

    def on_mount(self) -> None:
        for label, key, width in self._COLUMNS:
            if width is not None:
                self.add_column(label, width=width, key=key)
            else:
                self.add_column(label, key=key)

    def refresh_from(self, state: WorkingState) -> None:
        self.clear()
        for item in state.visible():
            p = item.proposal
            self.add_row(
                render_decision(item.decision),
                p.config,
                item.subsystem,
                f"{p.current_value}→{p.proposed_value}",
                render_risk(p.risk),
                f"{p.confidence:.2f}",
                p.source.value[:3],
            )
        if self.row_count > 0 and state.cursor < self.row_count:
            try:
                self.move_cursor(row=state.cursor)
            except Exception as exc:
                # Textual sometimes raises during mount/clear races; benign.
                self.log.debug("cursor restore skipped during table refresh: %r", exc)


class EvidencePanel(Static):
    """Detail pane for one item.

    Renders the proposal's reason, risk/confidence/source, current
    decision, and any structured evidence strings. Idempotent: calling
    :meth:`refresh_from` with the same state produces the same markup.
    """

    DEFAULT_ID = "evidence"

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("markup", True)
        super().__init__(*args, **kwargs)

    def refresh_from(self, state: WorkingState) -> None:
        item = state.current()
        if item is None:
            self.update("[dim]no proposals match the current filter[/dim]")
            return
        self.update(_render_evidence(item))


def _render_evidence(item: Item) -> str:
    """Build the evidence-panel markup for one item. Pure function so
    we can test the layout without instantiating the widget."""
    p = item.proposal
    glyph = render_decision(item.decision)
    risk = render_risk(p.risk)
    block: list[str] = [
        f"{glyph} [bold]{p.config}[/bold]   [dim]{item.subsystem}[/dim]",
        "",
        f"[bold]from[/bold] {p.current_value}  →  [bold]to[/bold] {p.proposed_value}",
        f"[bold]risk[/bold] {risk}   "
        f"[bold]confidence[/bold] {p.confidence:.2f}   "
        f"[bold]source[/bold] {p.source.value}",
        f"[bold]current decision[/bold] {item.decision.value}   "
        f"[dim]rule: {item.rule}[/dim]",
        "",
        "[bold]reason[/bold]",
        p.reason,
    ]
    if p.evidence:
        block.append("")
        block.append("[bold]evidence[/bold]")
        for e in p.evidence:
            block.append(f"  · {e}")
    return "\n".join(block)
