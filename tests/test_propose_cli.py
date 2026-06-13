"""CLI defaults for `autokernel propose`."""

from __future__ import annotations

import inspect

from autokernel.cli import propose


def test_propose_max_candidates_defaults_to_unlimited():
    sig = inspect.signature(propose)

    assert sig.parameters["max_candidates"].default == 0
