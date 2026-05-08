"""Merge a Kconfig fragment into a base ``.config`` to produce a final config.

Mirrors the semantics of the kernel's
``scripts/kconfig/merge_config.sh -m``:

* Lines from the base ``.config`` are preserved verbatim unless the fragment
  redefines them.
* For each symbol the fragment sets, the fragment **wins**: the base's
  assignment is replaced. (``-m`` is "merge mode" — collisions are silent
  overrides, no prompt.)
* Lines unique to the base or to the fragment both end up in the output.
* Comment-form disables (``# CONFIG_FOO is not set``) are first-class
  assignments to ``n`` and participate in conflict resolution exactly like
  ``CONFIG_FOO=y``.

The merge is **idempotent and order-stable**: the output preserves the base
config's line order, with the fragment's overrides applied in place. Symbols
unique to the fragment are appended at the end under a section header so a
subsequent ``make olddefconfig`` will canonicalize the result.

Why we don't shell out to ``merge_config.sh``: it requires a kernel source
tree to be present (the script lives there). For ``autokernel apply`` we
want to produce a final ``.config`` from purely the snapshot's
``running_config`` plus our kfrag, with no kernel source dependency. The
final config can then be dropped into a kernel source tree and refined
with ``make olddefconfig``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from autokernel.kfrag import parse_kfrag


_NOT_SET_RE = re.compile(r"^#\s*(CONFIG_[A-Z0-9_]+)\s+is not set\s*$")
_SET_RE = re.compile(r"^(CONFIG_[A-Z0-9_]+)=(.*)$")


@dataclass
class MergeReport:
    """Summary of what the merge changed."""

    overrides: list[tuple[str, str, str]] = field(default_factory=list)
    """``(symbol, base_value, frag_value)`` for each symbol where base and
    fragment disagree."""

    no_ops: list[str] = field(default_factory=list)
    """Symbols where the fragment's value matches the base — no change."""

    fragment_only: list[str] = field(default_factory=list)
    """Symbols introduced by the fragment that weren't in the base."""

    base_only_count: int = 0
    """How many base symbols were preserved unchanged."""


def _normalize_value(line: str) -> tuple[str, str] | None:
    """Map any meaningful config line to ``(symbol, value)``.

    ``# CONFIG_FOO is not set`` → ``("CONFIG_FOO", "n")``
    ``CONFIG_FOO=y``           → ``("CONFIG_FOO", "y")``
    everything else            → ``None``
    """
    s = line.rstrip("\n")
    m = _NOT_SET_RE.match(s)
    if m:
        return m.group(1), "n"
    m = _SET_RE.match(s)
    if m:
        return m.group(1), m.group(2).strip().strip('"')
    return None


def _format_assignment(symbol: str, value: str) -> str:
    if value == "n":
        return f"# {symbol} is not set"
    if value in ("y", "m") or value.isdigit():
        return f"{symbol}={value}"
    if value.startswith("0x"):
        return f"{symbol}={value}"
    # Treat anything else as a string value; quote it as kernel does.
    return f'{symbol}="{value}"'


def merge_kfrag(
    base_config_path: Path,
    kfrag_path: Path,
) -> tuple[str, MergeReport]:
    """Merge ``kfrag_path`` into ``base_config_path``.

    Returns the merged config text and a ``MergeReport``.
    """
    base_text = Path(base_config_path).read_text()
    parsed = parse_kfrag(Path(kfrag_path))

    # Collect fragment assignments: symbol → value
    frag_values: dict[str, str] = {}
    for sym in parsed.disables:
        frag_values[sym] = "n"
    for sym, val in parsed.assignments.items():
        # Reject the kfrag's own header lines and meta from leaking — they
        # don't have CONFIG_ prefix so they wouldn't get here anyway.
        frag_values[sym] = val

    report = MergeReport()
    out_lines: list[str] = []
    seen_in_base: set[str] = set()

    for raw_line in base_text.splitlines():
        nv = _normalize_value(raw_line)
        if nv is None:
            # comments and blank lines pass through
            out_lines.append(raw_line)
            continue
        sym, base_val = nv
        if sym in frag_values:
            frag_val = frag_values[sym]
            if frag_val == base_val:
                report.no_ops.append(sym)
                out_lines.append(raw_line)
            else:
                report.overrides.append((sym, base_val, frag_val))
                out_lines.append(_format_assignment(sym, frag_val))
            seen_in_base.add(sym)
        else:
            report.base_only_count += 1
            out_lines.append(raw_line)

    # Append fragment-only symbols at the end. ``make olddefconfig`` will
    # promote them to their proper place if their dependencies are met.
    appended_syms = sorted(s for s in frag_values if s not in seen_in_base)
    if appended_syms:
        report.fragment_only = appended_syms
        out_lines.append("")
        out_lines.append("# ── autokernel: appended from kfrag ─────────────────")
        for sym in appended_syms:
            out_lines.append(_format_assignment(sym, frag_values[sym]))

    return "\n".join(out_lines) + "\n", report


# ── validation ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ValidationFinding:
    symbol: str
    expected_in: str
    actual_value: str  # "n", "missing", or actual value
    reason: str


def _config_text_to_dict(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        nv = _normalize_value(line)
        if nv is not None:
            out[nv[0]] = nv[1]
    return out


def validate_load_bearing(
    merged_config_text: str,
    load_bearing_symbols: dict[str, str],
    *,
    base_config_text: str | None = None,
) -> list[ValidationFinding]:
    """Flag load-bearing symbols that the merge **downgraded** from a working
    state.

    Definition of "downgrade":

        base value ∈ {y, m}     AND     merged value ∉ {y, m}

    If a load-bearing symbol was never set in the base config, we don't flag
    it — the merge didn't break anything that was working. (Many symbols
    enter the load-bearing set defensively as candidate guesses for
    unresolved modules; not all of them ever appear in a particular
    kernel's running config.)

    When ``base_config_text`` is omitted, falls back to the legacy semantic
    of "any load-bearing symbol must be =y or =m in the merged output" —
    suitable for tests with synthetic configs that always set everything
    they care about.

    Returns an empty list on success, otherwise one ``ValidationFinding``
    per downgrade.
    """
    merged = _config_text_to_dict(merged_config_text)
    base = _config_text_to_dict(base_config_text) if base_config_text else None

    findings: list[ValidationFinding] = []
    for sym, reason in load_bearing_symbols.items():
        merged_val = merged.get(sym, "missing")
        if merged_val in ("y", "m"):
            continue  # still set — fine

        if base is not None:
            base_val = base.get(sym, "missing")
            if base_val not in ("y", "m"):
                # Was already off / missing in the base — merge didn't break it.
                continue

        findings.append(
            ValidationFinding(
                symbol=sym,
                expected_in="y|m",
                actual_value=merged_val,
                reason=reason,
            )
        )
    return findings
