"""Interactive review TUI for autokernel.

Public API is intentionally narrow: the CLI calls :func:`run_review`,
which encapsulates "open the TUI, return the saved ReviewSet (or None
if the user cancelled)".

The TUI itself is built from focused submodules:

* :mod:`autokernel.tui.state` — working state (pure data + logic)
* :mod:`autokernel.tui.filters` — filter cyclers (pure)
* :mod:`autokernel.tui.widgets` — Textual widgets, one job each
* :mod:`autokernel.tui.app` — orchestrator

Tests for the pure modules live in ``tests/test_tui_state.py``;
end-to-end tests using ``App.run_test()`` live in ``tests/test_tui.py``.
"""

from __future__ import annotations

from pathlib import Path

from autokernel.models import ReviewSet
from autokernel.tui.app import ReviewApp


def run_review(
    review_set: ReviewSet,
    *,
    snapshot_dir: Path | None = None,
) -> ReviewSet | None:
    """Open the interactive review TUI on ``review_set``.

    Returns the user-edited :class:`ReviewSet` if they pressed ``w``
    (save), or ``None`` if they quit without saving. The caller (the
    CLI) is responsible for persisting the returned ReviewSet.
    """
    app = ReviewApp(review_set, snapshot_dir=snapshot_dir)
    app.run()
    return app.result


__all__ = ["ReviewApp", "run_review"]
