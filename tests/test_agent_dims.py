"""Tests for the v0.13 multi-dimensional agents.

The actual LLM calls are mocked via pydantic-ai's
:class:`pydantic_ai.models.test.TestModel` — it returns deterministic
schema-conforming output without ever talking to a real provider.

We verify the *contract*:

* propose_choices: emits only proposals where the LLM picks something
  DIFFERENT from the current selection;
* propose_toggles: only emits proposals where value flips;
* propose_tunables: same;
* The eligibility filter for toggles cuts the ~4400-toggle universe
  down to the curated allowlist + workload-recipe symbols.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from autokernel.agent_dims import (
    TOGGLE_ALLOWLIST,
    TUNABLE_ALLOWLIST,
    _eligible_toggles,
    _eligible_tunables,
    _ChoiceBatch,
    _ChoiceDecision,
    _ToggleBatch,
    _ToggleDecision,
    _TunableBatch,
    _TunableDecision,
    propose_choices,
    propose_toggles,
    propose_tunables,
)
from autokernel.kconfig_walk import (
    BoolToggle,
    ChoiceGroup,
    ChoiceOption,
    KconfigSurface,
    NumericTunable,
    SymbolType,
)
from autokernel.models import (
    BootContext,
    CpuInfo,
    KernelInfo,
    ProposalSource,
    Snapshot,
)
from autokernel.workload import WorkloadProfile


def _bare_snap() -> Snapshot:
    return Snapshot(
        collected_at=datetime.now(timezone.utc),
        host="test",
        snapshot_dir=Path("/tmp/snap"),
        kernel=KernelInfo(release="6.19.0", version="#1 SMP", arch="x86_64"),
        cpu=CpuInfo(vendor_id="GenuineIntel", cpu_family=6, model=170, cores=8),
        boot=BootContext(cmdline=""),
    )


def _surface_with_choices_and_toggles() -> KconfigSurface:
    preempt = ChoiceGroup(
        name=None,
        prompt="Preemption Model",
        help=None,
        options=[
            ChoiceOption("PREEMPT_NONE", "No Forced Preemption", None, is_current=False),
            ChoiceOption("PREEMPT_VOLUNTARY", "Voluntary", None, is_current=True),
            ChoiceOption("PREEMPT", "Preemptible", None, is_current=False),
        ],
        location="kernel/Kconfig.preempt:18",
    )
    hz = ChoiceGroup(
        name=None,
        prompt="Timer frequency",
        help=None,
        options=[
            ChoiceOption("HZ_100", "100 HZ", None, is_current=False),
            ChoiceOption("HZ_250", "250 HZ", None, is_current=True),
            ChoiceOption("HZ_1000", "1000 HZ", None, is_current=False),
        ],
        location="kernel/Kconfig.hz:7",
    )
    thp = BoolToggle(
        name="TRANSPARENT_HUGEPAGE",
        prompt="Transparent Hugepage Support",
        help="Coalesce 4K pages into 2M ones.",
        current_value="y",
        location="mm/Kconfig:1",
        direct_dep_str="",
    )
    bpf = BoolToggle(
        name="BPF_JIT_ALWAYS_ON",
        prompt="Always JIT BPF",
        help=None,
        current_value="n",
        location="kernel/bpf/Kconfig:1",
        direct_dep_str="",
    )
    nr_cpus = NumericTunable(
        name="NR_CPUS",
        type=SymbolType.INT,
        prompt="Maximum number of CPUs",
        help=">64 incurs off-stack cpumask.",
        current_value="8192",
        ranges=[("2", "8192")],
        location="kernel/Kconfig.smp:1",
    )
    return KconfigSurface(
        arch="x86_64",
        source_dir=Path("/tmp/src"),
        choices=[preempt, hz],
        toggles=[thp, bpf],
        tunables=[nr_cpus],
    )


# ── propose_choices ──────────────────────────────────────────────────────


def test_propose_choices_emits_proposal_when_selection_changes(monkeypatch):
    surface = _surface_with_choices_and_toggles()
    snap = _bare_snap()
    workload = WorkloadProfile.DESKTOP

    # Mock the agent: return PREEMPT (change!) for preempt, HZ_1000 for hz.
    def _fake_agent(model, service_tier):
        class _Agent:
            def run_sync(self, prompt: str):
                class _R:
                    output = _ChoiceBatch(decisions=[
                        _ChoiceDecision(choice="Preemption Model", selected_option="PREEMPT", reason="desktop wants low-latency", confidence=0.9),
                        _ChoiceDecision(choice="Timer frequency", selected_option="HZ_1000", reason="desktop default", confidence=0.85),
                    ])
                return _R()
        return _Agent()

    monkeypatch.setattr("autokernel.agent_dims._build_choice_agent", _fake_agent)
    out = propose_choices(snap, surface, workload)
    assert len(out) == 2
    syms = {p.config for p in out}
    assert syms == {"CONFIG_PREEMPT", "CONFIG_HZ_1000"}
    assert all(p.source == ProposalSource.CHOICE for p in out)
    # proposed_value is the bare option name (so the kfrag writer sets =y)
    preempt_p = next(p for p in out if p.config == "CONFIG_PREEMPT")
    assert preempt_p.proposed_value == "PREEMPT"


def test_propose_choices_skips_unchanged_selections(monkeypatch):
    surface = _surface_with_choices_and_toggles()
    snap = _bare_snap()

    # Agent picks the CURRENT option for both — no proposals expected.
    def _fake_agent(model, service_tier):
        class _Agent:
            def run_sync(self, prompt: str):
                class _R:
                    output = _ChoiceBatch(decisions=[
                        _ChoiceDecision(choice="Preemption Model", selected_option="PREEMPT_VOLUNTARY", reason="ok as-is", confidence=0.8),
                        _ChoiceDecision(choice="Timer frequency", selected_option="HZ_250", reason="ok", confidence=0.75),
                    ])
                return _R()
        return _Agent()

    monkeypatch.setattr("autokernel.agent_dims._build_choice_agent", _fake_agent)
    out = propose_choices(snap, surface, WorkloadProfile.DESKTOP)
    assert out == []


def test_propose_choices_skips_hallucinated_options(monkeypatch):
    surface = _surface_with_choices_and_toggles()
    snap = _bare_snap()

    def _fake_agent(model, service_tier):
        class _Agent:
            def run_sync(self, prompt: str):
                class _R:
                    output = _ChoiceBatch(decisions=[
                        _ChoiceDecision(choice="Preemption Model", selected_option="PREEMPT_HALLUCINATED", reason="bogus", confidence=0.5),
                    ])
                return _R()
        return _Agent()

    monkeypatch.setattr("autokernel.agent_dims._build_choice_agent", _fake_agent)
    out = propose_choices(snap, surface, WorkloadProfile.DESKTOP)
    assert out == []


def test_propose_choices_returns_empty_when_no_choices():
    surface = KconfigSurface(arch="x86_64", source_dir=Path("/tmp"), choices=[])
    snap = _bare_snap()
    out = propose_choices(snap, surface, WorkloadProfile.DESKTOP)
    assert out == []


# ── propose_toggles ──────────────────────────────────────────────────────


def test_propose_toggles_emits_only_when_value_flips(monkeypatch):
    surface = _surface_with_choices_and_toggles()
    snap = _bare_snap()

    def _fake_agent(model, service_tier):
        class _Agent:
            def run_sync(self, prompt: str):
                class _R:
                    output = _ToggleBatch(decisions=[
                        # THP keep at y — no change
                        _ToggleDecision(symbol="TRANSPARENT_HUGEPAGE", value="y", reason="madvise default", risk="low", confidence=0.8),
                        # BPF_JIT_ALWAYS_ON: flip n → y
                        _ToggleDecision(symbol="BPF_JIT_ALWAYS_ON", value="y", reason="hardening", risk="low", confidence=0.95),
                    ])
                return _R()
        return _Agent()

    monkeypatch.setattr("autokernel.agent_dims._build_toggle_agent", _fake_agent)
    out = propose_toggles(snap, surface, WorkloadProfile.DESKTOP)
    assert len(out) == 1
    assert out[0].config == "CONFIG_BPF_JIT_ALWAYS_ON"
    assert out[0].proposed_value == "y"
    assert out[0].source == ProposalSource.TOGGLE


def test_propose_toggles_eligibility_filter_uses_allowlist():
    surface = KconfigSurface(
        arch="x86_64",
        source_dir=Path("/tmp"),
        toggles=[
            BoolToggle("BPF_JIT_ALWAYS_ON", "JIT", None, "n", "x", ""),  # in allowlist
            BoolToggle("OBSCURE_INTERNAL_FLAG", "X", None, "y", "y", ""),  # not
        ],
    )
    eligible = _eligible_toggles(surface, WorkloadProfile.DESKTOP)
    assert any(t.name == "BPF_JIT_ALWAYS_ON" for t in eligible)
    assert all(t.name != "OBSCURE_INTERNAL_FLAG" for t in eligible)


def test_propose_toggles_eligibility_includes_workload_recipe_symbols():
    """Symbols in the workload recipe but not in TOGGLE_ALLOWLIST should
    still be eligible — the allowlist is OR'd with recipe symbols."""
    # ACPI_DOCK is in laptop recipe but check it's also in allowlist.
    surface = KconfigSurface(
        arch="x86_64",
        source_dir=Path("/tmp"),
        toggles=[
            BoolToggle("ACPI_DOCK", "Dock", None, "n", "x", ""),
        ],
    )
    eligible = _eligible_toggles(surface, WorkloadProfile.LAPTOP)
    assert any(t.name == "ACPI_DOCK" for t in eligible)


# ── propose_tunables ─────────────────────────────────────────────────────


def test_propose_tunables_emits_when_value_changes(monkeypatch):
    surface = _surface_with_choices_and_toggles()
    snap = _bare_snap()

    def _fake_agent(model, service_tier):
        class _Agent:
            def run_sync(self, prompt: str):
                class _R:
                    output = _TunableBatch(decisions=[
                        # NR_CPUS 8192 → 32 (8-core desktop, plenty of headroom)
                        _TunableDecision(symbol="NR_CPUS", value="32", reason="8 cores; 32 = 4× headroom", confidence=0.9),
                    ])
                return _R()
        return _Agent()

    monkeypatch.setattr("autokernel.agent_dims._build_tunable_agent", _fake_agent)
    out = propose_tunables(snap, surface, WorkloadProfile.DESKTOP)
    assert len(out) == 1
    assert out[0].config == "CONFIG_NR_CPUS"
    assert out[0].proposed_value == "32"
    assert out[0].source == ProposalSource.TUNABLE


def test_propose_tunables_skips_unchanged(monkeypatch):
    surface = _surface_with_choices_and_toggles()
    snap = _bare_snap()

    def _fake_agent(model, service_tier):
        class _Agent:
            def run_sync(self, prompt: str):
                class _R:
                    output = _TunableBatch(decisions=[
                        _TunableDecision(symbol="NR_CPUS", value="8192", reason="leave default", confidence=0.6),
                    ])
                return _R()
        return _Agent()

    monkeypatch.setattr("autokernel.agent_dims._build_tunable_agent", _fake_agent)
    out = propose_tunables(snap, surface, WorkloadProfile.DESKTOP)
    assert out == []


def test_propose_tunables_eligibility_filter():
    surface = KconfigSurface(
        arch="x86_64",
        source_dir=Path("/tmp"),
        tunables=[
            NumericTunable("NR_CPUS", SymbolType.INT, "Max CPUs", None, "8192", [("2", "8192")], "x"),
            NumericTunable("OBSCURE_INT", SymbolType.INT, "Obscure", None, "0", [], "x"),
        ],
    )
    eligible = _eligible_tunables(surface)
    assert any(t.name == "NR_CPUS" for t in eligible)
    assert all(t.name != "OBSCURE_INT" for t in eligible)


# ── caching ──────────────────────────────────────────────────────────────


def test_propose_choices_uses_cache_dir(tmp_path, monkeypatch):
    surface = _surface_with_choices_and_toggles()
    snap = _bare_snap()
    cache_dir = tmp_path / "batches"

    call_count = {"n": 0}

    def _fake_agent(model, service_tier):
        class _Agent:
            def run_sync(self, prompt):
                call_count["n"] += 1
                class _R:
                    output = _ChoiceBatch(decisions=[
                        _ChoiceDecision(choice="Preemption Model", selected_option="PREEMPT", reason="x", confidence=0.9),
                    ])
                return _R()
        return _Agent()

    monkeypatch.setattr("autokernel.agent_dims._build_choice_agent", _fake_agent)
    propose_choices(snap, surface, WorkloadProfile.DESKTOP, cache_dir=cache_dir)
    assert call_count["n"] == 1
    # Re-run — cached, no second call.
    propose_choices(snap, surface, WorkloadProfile.DESKTOP, cache_dir=cache_dir)
    assert call_count["n"] == 1
    # Cache should have at least one file.
    cached_files = list(cache_dir.glob("choice-*.json"))
    assert len(cached_files) >= 1
