"""Economics ledger — what did the fork cost to create and maintain?

The thesis (docs/CLANG_PGO_EXPERIMENT.md Phase 5): an agent-maintained
fork costs ~$X of LLM judgment + Y CPU-hours to create, and pennies +
minutes per upstream-bump delta. This module measures both:

* **CPU-hours** from the build records' ``duration_s`` (the compute).
* **LLM cost** from ``costs.jsonl`` — one line per real (non-cached)
  agent call, recording input/output tokens; cost via a rate card.

``record_usage`` is called by the triage + dimension agents only when a
call actually hits the model (cache hits cost nothing), so the ledger
reflects real spend.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# USD per 1M tokens (input, output), from the research's rate card.
# Keyed by a substring of the model id.
_RATES: dict[str, tuple[float, float]] = {
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}


def _rate_for(model: str) -> tuple[float, float]:
    for key, rate in _RATES.items():
        if key in model:
            return rate
    return _RATES["sonnet"]  # unknown → assume sonnet (conservative-ish)


def cost_of(model: str, input_tokens: int, output_tokens: int) -> float:
    rin, rout = _rate_for(model)
    return input_tokens / 1e6 * rin + output_tokens / 1e6 * rout


def costs_path(world_dir: Path) -> Path:
    return world_dir / "costs.jsonl"


def record_usage(
    world_dir: Path,
    *,
    agent: str,
    label: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Append one real LLM call to the ledger. Best-effort: never raises
    into the agent path."""
    try:
        line = json.dumps(
            {
                "at": datetime.now(UTC).isoformat(),
                "agent": agent,
                "label": label,
                "model": model,
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "cost_usd": round(cost_of(model, input_tokens, output_tokens), 6),
            }
        )
        path = costs_path(world_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except (OSError, TypeError, ValueError):
        pass  # nosec B110 — telemetry must never break a build


# ── ledger ──────────────────────────────────────────────────────────────────


@dataclass
class Ledger:
    build_cpu_hours: float = 0.0
    build_count: int = 0
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_usd: float = 0.0
    by_agent: dict[str, float] = field(default_factory=dict)

    def render(self) -> str:
        lines = [
            f"builds:    {self.build_count} packages, "
            f"{self.build_cpu_hours:.2f} CPU-hours",
            f"LLM:       {self.llm_calls} calls, "
            f"{self.llm_input_tokens:,} in / {self.llm_output_tokens:,} out tokens",
            f"LLM cost:  ${self.llm_cost_usd:.4f}"
            + (
                "  ("
                + ", ".join(f"{a} ${c:.4f}" for a, c in sorted(self.by_agent.items()))
                + ")"
                if self.by_agent
                else ""
            ),
        ]
        return "\n".join(lines)


def tally(world_dir: Path) -> Ledger:
    """Compute the ledger from a world dir's build records + costs.jsonl."""
    led = Ledger()
    builds = world_dir / "builds"
    if builds.is_dir():
        for record in builds.glob("*/*/record.json"):
            try:
                d = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            led.build_count += 1
            led.build_cpu_hours += float(d.get("duration_s", 0.0)) / 3600.0
    costs = costs_path(world_dir)
    if costs.is_file():
        for line in costs.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            led.llm_calls += 1
            led.llm_input_tokens += int(e.get("input_tokens", 0))
            led.llm_output_tokens += int(e.get("output_tokens", 0))
            c = float(e.get("cost_usd", 0.0))
            led.llm_cost_usd += c
            led.by_agent[e.get("agent", "?")] = (
                led.by_agent.get(e.get("agent", "?"), 0.0) + c
            )
    led.build_cpu_hours = round(led.build_cpu_hours, 3)
    led.llm_cost_usd = round(led.llm_cost_usd, 6)
    return led
