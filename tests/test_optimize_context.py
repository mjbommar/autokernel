"""Tests for autokernel.optimize_context."""

from __future__ import annotations

import pytest

from autokernel.optimize_context import (
    Aggression,
    ModuleStrategy,
    OptimizationContext,
    PRESETS,
    Preset,
    ThreatModel,
    context_from_flags,
    from_preset,
)
from autokernel.workload import WorkloadProfile


# ── enum values ──────────────────────────────────────────────────────────


def test_threat_enum_has_three_levels():
    assert {t.value for t in ThreatModel} == {"permissive", "balanced", "paranoid"}


def test_module_strategy_enum():
    assert {m.value for m in ModuleStrategy} == {"distro", "monolithic", "modular"}


def test_aggression_enum():
    assert {a.value for a in Aggression} == {"conservative", "balanced", "aggressive"}


def test_aggression_confidence_floor_monotonic():
    """Aggressive accepts lower-confidence proposals than conservative."""
    assert Aggression.CONSERVATIVE.confidence_floor > Aggression.BALANCED.confidence_floor
    assert Aggression.BALANCED.confidence_floor > Aggression.AGGRESSIVE.confidence_floor


# ── OptimizationContext ──────────────────────────────────────────────────


def test_context_defaults():
    ctx = OptimizationContext(workload=WorkloadProfile.DESKTOP)
    assert ctx.threat == ThreatModel.BALANCED
    assert ctx.modules == ModuleStrategy.DISTRO
    assert ctx.aggression == Aggression.BALANCED


def test_context_render_for_prompt_includes_all_four_axes():
    ctx = OptimizationContext(
        workload=WorkloadProfile.SERVER,
        threat=ThreatModel.PARANOID,
        modules=ModuleStrategy.MONOLITHIC,
        aggression=Aggression.AGGRESSIVE,
    )
    text = ctx.render_for_prompt()
    assert "server" in text
    assert "paranoid" in text
    assert "monolithic" in text
    assert "aggressive" in text
    # Floor stated numerically so the LLM can apply it.
    assert "0.40" in text


def test_context_immutable():
    """OptimizationContext is frozen — mutation should error."""
    ctx = OptimizationContext(workload=WorkloadProfile.DESKTOP)
    with pytest.raises(Exception):
        ctx.threat = ThreatModel.PARANOID  # type: ignore[misc]


# ── presets ──────────────────────────────────────────────────────────────


def test_preset_table_includes_named_combinations():
    """The README documents these names — they must exist."""
    expected = {
        "desktop", "gaming-desktop", "paranoid-desktop",
        "laptop", "paranoid-laptop",
        "server", "hardened-server",
        "cloud-vm", "realtime", "embedded",
        "lean-static", "lean-module", "hyperoptimize",
    }
    assert expected.issubset(set(PRESETS.keys()))


def test_from_preset_returns_context():
    ctx = from_preset("hardened-server")
    assert ctx.workload == WorkloadProfile.SERVER
    assert ctx.threat == ThreatModel.PARANOID
    assert ctx.modules == ModuleStrategy.MONOLITHIC


def test_from_preset_raises_on_unknown():
    with pytest.raises(KeyError):
        from_preset("nonexistent-preset")


def test_gaming_desktop_is_permissive_monolithic_aggressive():
    ctx = from_preset("gaming-desktop")
    assert ctx.workload == WorkloadProfile.DESKTOP
    assert ctx.threat == ThreatModel.PERMISSIVE
    assert ctx.modules == ModuleStrategy.MONOLITHIC
    assert ctx.aggression == Aggression.AGGRESSIVE


def test_paranoid_laptop_is_paranoid_distro_balanced():
    ctx = from_preset("paranoid-laptop")
    assert ctx.workload == WorkloadProfile.LAPTOP
    assert ctx.threat == ThreatModel.PARANOID
    assert ctx.modules == ModuleStrategy.DISTRO


# ── context_from_flags ───────────────────────────────────────────────────


def test_context_from_flags_uses_preset_when_no_overrides():
    ctx = context_from_flags(
        preset="hardened-server",
        workload=None, threat=None, modules=None, aggression=None,
    )
    assert ctx.workload == WorkloadProfile.SERVER
    assert ctx.threat == ThreatModel.PARANOID


def test_context_from_flags_per_axis_overrides_preset():
    """`--preset=hardened-server --threat=balanced` overrides just threat."""
    ctx = context_from_flags(
        preset="hardened-server",
        workload=None, threat="balanced", modules=None, aggression=None,
    )
    assert ctx.workload == WorkloadProfile.SERVER  # from preset
    assert ctx.threat == ThreatModel.BALANCED  # overridden
    assert ctx.modules == ModuleStrategy.MONOLITHIC  # from preset


def test_context_from_flags_uses_detected_workload_as_fallback():
    ctx = context_from_flags(
        preset=None, workload=None, threat=None, modules=None, aggression=None,
        detected_workload=WorkloadProfile.LAPTOP,
    )
    assert ctx.workload == WorkloadProfile.LAPTOP


def test_context_from_flags_explicit_workload_beats_detected():
    ctx = context_from_flags(
        preset=None, workload="server", threat=None, modules=None, aggression=None,
        detected_workload=WorkloadProfile.LAPTOP,
    )
    assert ctx.workload == WorkloadProfile.SERVER


def test_context_from_flags_unknown_preset_raises():
    with pytest.raises(KeyError):
        context_from_flags(
            preset="bogus", workload=None, threat=None,
            modules=None, aggression=None,
        )


def test_context_from_flags_unknown_axis_value_raises():
    with pytest.raises(ValueError):
        context_from_flags(
            preset=None, workload=None, threat="hyperparanoid",
            modules=None, aggression=None,
            detected_workload=WorkloadProfile.DESKTOP,
        )


def test_full_per_axis_flags_no_preset():
    """All four flags + no preset = direct construction."""
    ctx = context_from_flags(
        preset=None,
        workload="vm-guest", threat="permissive",
        modules="monolithic", aggression="aggressive",
    )
    assert ctx.workload == WorkloadProfile.VM_GUEST
    assert ctx.threat == ThreatModel.PERMISSIVE
    assert ctx.modules == ModuleStrategy.MONOLITHIC
    assert ctx.aggression == Aggression.AGGRESSIVE
