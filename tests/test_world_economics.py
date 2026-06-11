"""Tests for the economics ledger (Phase 5)."""

from __future__ import annotations

import json
from pathlib import Path

from autokernel.world import economics as econ


def test_cost_of_rate_card():
    # sonnet: $3/$15 per Mtok
    assert econ.cost_of("anthropic:claude-sonnet-4-6", 1_000_000, 0) == 3.0
    assert econ.cost_of("sonnet", 0, 1_000_000) == 15.0
    assert econ.cost_of("opus", 1_000_000, 1_000_000) == 30.0
    # unknown model → sonnet assumption
    assert econ.cost_of("mystery", 1_000_000, 0) == 3.0


def test_record_and_tally(tmp_path: Path):
    econ.record_usage(
        tmp_path, agent="triage", label="acl", model="sonnet",
        input_tokens=10_000, output_tokens=2_000,
    )  # fmt: skip
    econ.record_usage(
        tmp_path, agent="dim-flags", label="batch", model="sonnet",
        input_tokens=40_000, output_tokens=3_000,
    )  # fmt: skip
    # a build record contributes CPU-hours
    rec = tmp_path / "builds" / "acl" / "1.0" / "record.json"
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps({"duration_s": 3600.0, "outcome": "ok"}))

    led = econ.tally(tmp_path)
    assert led.build_count == 1
    assert led.build_cpu_hours == 1.0
    assert led.llm_calls == 2
    assert led.llm_input_tokens == 50_000
    assert led.llm_output_tokens == 5_000
    # 50k in @ $3/M + 5k out @ $15/M = 0.15 + 0.075 = 0.225
    assert abs(led.llm_cost_usd - 0.225) < 1e-6
    assert set(led.by_agent) == {"triage", "dim-flags"}
    assert "CPU-hours" in led.render() and "LLM cost" in led.render()


def test_tally_empty_world(tmp_path: Path):
    led = econ.tally(tmp_path)
    assert led.build_count == 0 and led.llm_calls == 0


def test_tally_skips_malformed_cost_lines(tmp_path: Path):
    econ.record_usage(
        tmp_path, agent="triage", label="acl", model="sonnet",
        input_tokens=1000, output_tokens=100,
    )  # fmt: skip
    # a corrupt line in costs.jsonl must not crash the tally
    with econ.costs_path(tmp_path).open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")
    led = econ.tally(tmp_path)
    assert led.llm_calls == 1  # only the good line counted
