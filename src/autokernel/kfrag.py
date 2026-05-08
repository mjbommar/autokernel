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
    lines.append(f"# rejected:  {len(review_set.rejected)} (kept at current value)")
    lines.append(f"# deferred:  {len(review_set.deferred)} (no decision; current value preserved)")
    lines.append("#")
    lines.append("# Apply with:  scripts/kconfig/merge_config.sh -m .config <this-file>")
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

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    return header


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
    disables: list[str]   # symbols set to =n
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
