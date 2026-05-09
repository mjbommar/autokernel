"""Kconfig fragment (.kfrag) writer + reader.

A kfrag is the artifact the future ``build`` verb consumes. It is a plain
.config-format file containing only the symbols whose values *change* —
disables (``# CONFIG_FOO is not set``) and demotions (``CONFIG_FOO=m``).

The format is identical to what the kernel's
``scripts/kconfig/merge_config.sh`` accepts as a fragment, so a kfrag can
be applied with the kernel's own merge logic::

    cd <kernel-source>
    scripts/kconfig/merge_config.sh -m .config /path/to/auto.kfrag
    make olddefconfig

We also write a comment header with provenance: snapshot directory,
autonomy level, model + service tier (if any), reviewer, ISO timestamp,
counts. The header is ignored by ``merge_config.sh`` and by ``make
olddefconfig``; it's there for humans and for ``autokernel`` to detect
"is this our kfrag?".

A reverse parser is provided for round-trip tests and for the future
build stage to load a kfrag without re-running review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from autokernel.models import (
    ProposalSource,
    ReviewDecision,
    ReviewedProposal,
    ReviewSet,
)


_HEADER_PREFIX = "# autokernel-kfrag"


@dataclass(frozen=True)
class KfragHeader:
    """Provenance metadata embedded in the kfrag's comment block."""

    schema_version: int
    snapshot_dir: str
    autonomy: str
    model: str | None
    service_tier: str | None
    timestamp: str
    n_disable: int
    n_demote: int
    n_enable: int = 0
    n_other: int = 0  # choices/toggles/tunables (v0.13 multi-dim)
    """Count of ENABLE proposals (proposed_value == 'y'). These come from
    deterministic tuning rules — most commonly CPU microarch — and emit
    ``CONFIG_FOO=y`` lines into the fragment."""


def write_kfrag(
    out_path: Path,
    review_set: ReviewSet,
    *,
    snapshot_dir: Path,
    autonomy: str,
    model: str | None = None,
    service_tier: str | None = None,
) -> KfragHeader:
    """Emit a kfrag from a ``ReviewSet``.

    Only ``ACCEPTED`` proposals make it into the fragment. Rejected and
    deferred items are intentionally omitted — the build stage interprets
    "absent from the kfrag" as "leave at current value".

    Returns the header so callers can summarize.
    """
    accepted = [r for r in review_set.accepted if r.decision == ReviewDecision.ACCEPT]
    disables = [r for r in accepted if r.proposal.proposed_value == "n"]
    demotions = [r for r in accepted if r.proposal.proposed_value == "m"]
    enables = [r for r in accepted if r.proposal.proposed_value == "y"]
    # Anything else — string ("foo"), int (250), choice option (HZ_1000) —
    # comes from the v0.13 multi-dimensional path and gets emitted as a
    # raw assignment.
    others = [r for r in accepted if r.proposal.proposed_value not in {"y", "n", "m"}]

    header = KfragHeader(
        schema_version=1,
        snapshot_dir=str(snapshot_dir.resolve()),
        autonomy=autonomy,
        model=model,
        service_tier=service_tier,
        timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        n_disable=len(disables),
        n_demote=len(demotions),
        n_enable=len(enables),
        n_other=len(others),
    )

    lines: list[str] = []
    lines.append(f"{_HEADER_PREFIX} schema={header.schema_version}")
    lines.append(f"# generated: {header.timestamp}")
    lines.append(f"# snapshot:  {header.snapshot_dir}")
    lines.append(f"# autonomy:  {header.autonomy}")
    if model:
        lines.append(f"# model:     {model}")
    if service_tier:
        lines.append(f"# tier:      {service_tier}")
    lines.append(f"# enables:   {header.n_enable}")
    lines.append(f"# disables:  {header.n_disable}")
    lines.append(f"# demotions: {header.n_demote}")
    lines.append(f"# other:     {header.n_other} (choices/toggles/tunables)")
    lines.append(f"# rejected:  {len(review_set.rejected)} (kept at current value)")
    lines.append(
        f"# deferred:  {len(review_set.deferred)} (no decision; current value preserved)"
    )
    lines.append("#")
    lines.append(
        "# Apply with:  scripts/kconfig/merge_config.sh -m .config <this-file>"
    )
    lines.append("#              make olddefconfig")
    lines.append("")

    if enables:
        lines.append("# ── enables (tuning) ─────────────────────────────────")
        for r in enables:
            note = _comment_for(r)
            lines.append(f"# {r.proposal.config}: {note}")
            lines.append(f"{r.proposal.config}=y")
            lines.append("")

    if disables:
        lines.append("# ── disables ─────────────────────────────────────────")
        for r in disables:
            note = _comment_for(r)
            lines.append(f"# {r.proposal.config}: {note}")
            lines.append(f"# {r.proposal.config} is not set")
            lines.append("")

    if demotions:
        lines.append("# ── demotions (built-in → module) ────────────────────")
        for r in demotions:
            note = _comment_for(r)
            lines.append(f"# {r.proposal.config}: {note}")
            lines.append(f"{r.proposal.config}=m")
            lines.append("")

    if others:
        lines.append("# ── choices / toggles / tunables ─────────────────────")
        for r in others:
            note = _comment_for(r)
            lines.append(f"# {r.proposal.config}: {note}")
            previous_choice = _previous_choice_symbol(r)
            if previous_choice is not None:
                lines.append(f"# {previous_choice} is not set")
            lines.append(
                _format_assignment(r.proposal.config, r.proposal.proposed_value)
            )
            lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    return header


def _format_assignment(symbol: str, value: str) -> str:
    """Render a Kconfig assignment line for ``proposed_value``.

    Handles three cases that the multi-dim path produces:

    * Choice option (``"PREEMPT_VOLUNTARY"``) → ``CONFIG_PREEMPT_VOLUNTARY=y``.
      The writer also unsets the previously-selected sibling when the proposal
      records one.
    * Numeric (``"250"``, ``"0x10"``) → ``CONFIG_HZ=250``
    * String — anything that contains non-numeric characters and isn't
      a CONFIG_-shaped option → ``CONFIG_LOCALVERSION="-custom"``
    """
    v = value.strip()
    # Choice option — looks like a CONFIG name (uppercase + digits + _).
    # Even when our caller passed just the suffix (e.g. "HZ_1000"), we
    # render that as a separate symbol assignment. The trick: the
    # ``symbol`` of the proposal IS the choice-OPTION's CONFIG_*, not
    # the choice container's. Agents construct the proposal that way.
    # So if value is the bare option name and symbol matches, we just
    # set =y. Otherwise treat as raw RHS.
    sym_short = symbol[len("CONFIG_") :] if symbol.startswith("CONFIG_") else symbol
    if v == sym_short:
        return f"{symbol}=y"
    # Numeric (int or hex)?
    if v.lstrip("-").isdigit() or (
        v.startswith("0x") and all(c in "0123456789abcdefABCDEF" for c in v[2:])
    ):
        return f"{symbol}={v}"
    # Already-quoted string?
    if v.startswith('"') and v.endswith('"'):
        return f"{symbol}={v}"
    # y/n/m already handled by caller; anything else is a string —
    # quote it.
    return f'{symbol}="{v}"'


def _previous_choice_symbol(r: ReviewedProposal) -> str | None:
    """Return the previously-selected CONFIG for a choice proposal.

    Choice proposals store ``current_value`` as the old bare option name
    (for example ``PREEMPT_VOLUNTARY``) and ``config`` as the newly selected
    option (for example ``CONFIG_PREEMPT``). Emitting an explicit unset for the
    old option keeps the merged config coherent before ``olddefconfig`` has a
    chance to reconcile the choice.
    """
    p = r.proposal
    if p.source != ProposalSource.CHOICE:
        return None

    current = p.current_value.strip()
    proposed = p.proposed_value.strip()
    if not current or current in {"?", "y", "n", "m"} or current == proposed:
        return None

    if current.startswith("CONFIG_"):
        old_symbol = current
        current_short = current.removeprefix("CONFIG_")
    else:
        current_short = current
        old_symbol = f"CONFIG_{current}"

    if not re.fullmatch(r"[A-Z0-9_]+", current_short):
        return None
    if old_symbol == p.config:
        return None
    return old_symbol


def _comment_for(r: ReviewedProposal) -> str:
    src = r.proposal.source.value
    rule = r.rule or ""
    return (
        f"[{src}, conf={r.proposal.confidence:.2f}, risk={r.proposal.risk.value}, "
        f"rule={rule}] {r.proposal.reason}"
    )


# ── reverse parser (for tests + future build stage) ─────────────────────────


_NOT_SET_RE = re.compile(r"^#\s*(CONFIG_[A-Z0-9_]+)\s+is not set\s*$")
_SET_RE = re.compile(r"^(CONFIG_[A-Z0-9_]+)=(.*)$")


@dataclass(frozen=True)
class ParsedKfrag:
    header_lines: list[str]
    disables: list[str]  # symbols set to =n
    assignments: dict[str, str]  # symbol → value (m, y, "string", numeric…)


def parse_kfrag(path: Path | str) -> ParsedKfrag:
    """Parse a kfrag back into its semantic content.

    Header lines (starting with ``# `` and not the "is not set" form) are
    preserved verbatim — useful for asserting provenance round-trips.
    """
    p = Path(path)
    text = p.read_text()

    header_lines: list[str] = []
    disables: list[str] = []
    assignments: dict[str, str] = {}

    for line in text.splitlines():
        s = line.rstrip()
        if not s:
            continue
        # disables look like comments but are actionable
        m = _NOT_SET_RE.match(s)
        if m:
            disables.append(m.group(1))
            continue
        if s.startswith("#"):
            header_lines.append(s)
            continue
        m = _SET_RE.match(s)
        if m:
            sym, val = m.group(1), m.group(2).strip().strip('"')
            assignments[sym] = val

    return ParsedKfrag(
        header_lines=header_lines,
        disables=disables,
        assignments=assignments,
    )
