"""Iteration history + history-summarizer for the closed loop.

The :func:`autokernel iterate` orchestrator runs propose → check →
apply → build → boot-test → measure for N rounds. Each round produces
an :class:`IterationRecord` summarizing what was attempted, what
landed, and what the build measured.

Two consumers:

1. :func:`summarize_history_for_prompt` — feeds a compact text block
   to the propose agents so they reason about prior rounds: "I-1
   proposed CONFIG_X=y; build PASSED; bzImage shrunk 18.2 → 16.5 MB.
   I-2 proposed CONFIG_BTRFS_FS=n; build FAILED at VFS panic; revert."

2. :func:`auto_revert_set` — when an iteration regressed (boot failed,
   or size grew), produce the set of CONFIG_* symbols to mark
   do-not-propose for the next round.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from autokernel.measurements import BuildMeasurements


@dataclass(frozen=True)
class IterationRecord:
    """One round of the closed loop.

    ``ctx_summary`` is a flat dict of the OptimizationContext axes.
    ``proposals`` is the list of CONFIG_ symbols proposed this round
    (just symbol names; full proposals are in proposal.json).
    ``measurements`` is the post-build snapshot of size/time/boot.
    ``regressed`` is set by the orchestrator when this iteration was
    auto-reverted.
    """

    iteration: int
    ctx_summary: dict[str, str]  # {"workload": ..., "threat": ..., ...}
    proposals: list[str]  # CONFIG_NAMEs proposed
    measurements: BuildMeasurements
    regressed: bool = False
    revert_reason: str | None = None
    note: str | None = None  # free-form

    def to_json_dict(self) -> dict:
        return {
            "iteration": self.iteration,
            "ctx_summary": self.ctx_summary,
            "proposals": list(self.proposals),
            "measurements": asdict(self.measurements),
            "regressed": self.regressed,
            "revert_reason": self.revert_reason,
            "note": self.note,
        }

    @classmethod
    def from_json_dict(cls, d: dict) -> IterationRecord:
        m = BuildMeasurements(**d["measurements"])
        return cls(
            iteration=d["iteration"],
            ctx_summary=dict(d["ctx_summary"]),
            proposals=list(d["proposals"]),
            measurements=m,
            regressed=d.get("regressed", False),
            revert_reason=d.get("revert_reason"),
            note=d.get("note"),
        )


# ── persistence ───────────────────────────────────────────────────────────


def iteration_dir(snapshot_dir: Path, n: int) -> Path:
    return snapshot_dir / "iterations" / f"i{n:03d}"


def save_record(snapshot_dir: Path, record: IterationRecord) -> Path:
    """Write the record to <snap>/iterations/i<NNN>/record.json."""
    d = iteration_dir(snapshot_dir, record.iteration)
    d.mkdir(parents=True, exist_ok=True)
    out = d / "record.json"
    out.write_text(json.dumps(record.to_json_dict(), indent=2))
    return out


def load_history(snapshot_dir: Path) -> list[IterationRecord]:
    """Load all iteration records from <snap>/iterations/, ordered."""
    root = snapshot_dir / "iterations"
    if not root.is_dir():
        return []
    out: list[IterationRecord] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        record_path = d / "record.json"
        if not record_path.exists():
            continue
        try:
            data = json.loads(record_path.read_text())
            out.append(IterationRecord.from_json_dict(data))
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return out


# ── prompt summarization ──────────────────────────────────────────────────


def summarize_history_for_prompt(
    history: list[IterationRecord],
    *,
    budget_recent: int = 3,
    include_baseline: bool = True,
) -> str:
    """Compact, prompt-ready summary of iteration history.

    Layout:
        # iteration history (last 3 + baseline):
        #   i=1 (baseline): proposed=18, landed=12, bzImage=18.2MB, boot PASS
        #   i=2:            proposed=14, landed=11, bzImage=16.8MB, boot PASS
        #   i=3 REVERTED:   proposed=9,  landed=5,  boot FAIL (vfs-panic) —
        #                   reverted CONFIG_BTRFS_FS=n
        #
        # rules from past iterations:
        #   - do not propose CONFIG_BTRFS_FS=n (rootfs is btrfs!)

    Returns ``""`` when history is empty.
    """
    if not history:
        return ""

    out: list[str] = []
    out.append(f"# iteration history (last {budget_recent} of {len(history)}):")

    # Pick which records to render.
    rendered: list[IterationRecord] = []
    if include_baseline and len(history) >= 1:
        baseline = history[0]
        rendered.append(baseline)
    recent_window = history[-budget_recent:]
    for r in recent_window:
        if r not in rendered:
            rendered.append(r)

    for r in rendered:
        m = r.measurements
        bz = f"{m.bzimage_bytes / (1024 * 1024):.1f}MB" if m.bzimage_bytes else "?"
        boot = "PASS" if m.boot_test_passed else (
            f"FAIL ({m.boot_failure_mode or '?'})" if m.boot_test_passed is False
            else "skipped"
        )
        landed_frac = (
            f"{m.actually_landed_count}/{m.proposed_count}"
            if m.proposed_count is not None
            else "?"
        )
        flag = " REVERTED" if r.regressed else ""
        baseline_tag = " (baseline)" if r is history[0] else ""
        out.append(
            f"#   i={r.iteration}{flag}{baseline_tag}: "
            f"landed={landed_frac}, bzImage={bz}, boot {boot}"
        )

    # Rules harvested from regressions: any reverted iteration's proposals
    # become "do not repeat".
    rules = []
    for r in history:
        if r.regressed:
            rules.append(
                f"#   - i={r.iteration} regressed; do NOT re-propose: "
                f"{', '.join(r.proposals[:5])}"
                + (" …" if len(r.proposals) > 5 else "")
                + (f"  (reason: {r.revert_reason})" if r.revert_reason else "")
            )
    if rules:
        out.append("#")
        out.append("# rules from past iterations:")
        out.extend(rules)

    return "\n".join(out)


# ── auto-revert ───────────────────────────────────────────────────────────


def auto_revert_set(history: list[IterationRecord]) -> set[str]:
    """Symbols the next iteration should NOT propose (because past
    iterations were regressed against them)."""
    out: set[str] = set()
    for r in history:
        if r.regressed:
            out.update(r.proposals)
    return out


# ── convergence ───────────────────────────────────────────────────────────


def has_converged(
    history: list[IterationRecord],
    *,
    window: int = 2,
    size_delta_pct: float = 1.0,
) -> bool:
    """Heuristic: ``window`` *consecutive* iteration-to-iteration size
    deltas all within ``size_delta_pct`` of each other = converged.

    With ``window=2``: requires the last 3 iterations' sizes to step by
    less than the threshold each step. (The first iteration is the
    baseline; the next ``window`` step deltas determine convergence.)
    """
    if len(history) < window + 1:
        return False
    sizes = [r.measurements.bzimage_bytes for r in history[-(window + 1):]]
    if any(s is None for s in sizes):
        return False
    for prev, cur in zip(sizes, sizes[1:]):
        if prev == 0:
            return False
        if abs(cur - prev) / prev > size_delta_pct / 100.0:
            return False
    return True
