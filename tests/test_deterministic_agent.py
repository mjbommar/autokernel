"""Deterministic GPU/CPU mismatch rules — pure logic, no LLM."""

from __future__ import annotations

from autokernel.agent import deterministic_proposals
from autokernel.models import ProposalSource, Snapshot
from autokernel.resolve import _running_config_symbols


def _candidates(snap: Snapshot) -> list[tuple[str, str]]:
    """All =y/=m symbols from the snapshot's running config."""
    syms = _running_config_symbols(snap.running_config_path)
    return [(s, v) for s, v in syms.items() if v in ("y", "m")]


def test_amd_box_strips_intel_only_symbols(amd_desktop: Snapshot):
    props = deterministic_proposals(amd_desktop, _candidates(amd_desktop))
    targeted = {p.config for p in props}
    assert "CONFIG_INTEL_IDLE" in targeted
    assert "CONFIG_X86_INTEL_PSTATE" in targeted
    for p in props:
        if p.config == "CONFIG_INTEL_IDLE":
            assert p.source == ProposalSource.DETERMINISTIC
            assert p.confidence >= 0.95
            assert p.proposed_value == "n"


def test_intel_box_strips_amd_only_symbols(intel_laptop: Snapshot):
    props = deterministic_proposals(intel_laptop, _candidates(intel_laptop))
    targeted = {p.config for p in props}
    assert "CONFIG_X86_AMD_PSTATE" in targeted
    assert "CONFIG_X86_AMD_FREQ_SENSITIVITY" in targeted
    assert "CONFIG_X86_AMD_PLATFORM_DEVICE" in targeted
    for p in props:
        assert p.confidence >= 0.95


def test_amd_box_with_nvidia_keeps_nvidia(amd_desktop: Snapshot):
    """Box has an NVIDIA dGPU — NOUVEAU/NVIDIA proposals must NOT appear."""
    props = deterministic_proposals(amd_desktop, _candidates(amd_desktop))
    targeted = {p.config for p in props}
    assert "CONFIG_DRM_NOUVEAU" not in targeted
    assert all("NVIDIA" not in p.config for p in props)


def test_intel_only_box_strips_nvidia_and_amd_gpu(intel_laptop: Snapshot):
    """Box is Intel iGPU only — NVIDIA and AMD GPU stacks should be proposed off."""
    props = deterministic_proposals(intel_laptop, _candidates(intel_laptop))
    targeted = {p.config for p in props}
    # NOUVEAU is in the running config; must be proposed for removal.
    assert "CONFIG_DRM_NOUVEAU" in targeted
    # AMD GPU stack must be proposed for removal.
    assert "CONFIG_DRM_AMDGPU" in targeted
    assert "CONFIG_DRM_RADEON" in targeted
    # Intel i915 must NOT be proposed off (iGPU is present at PCI 00:02.0).
    assert "CONFIG_DRM_I915" not in targeted


def test_intel_box_does_not_remove_microcode_intel(intel_laptop: Snapshot):
    props = deterministic_proposals(intel_laptop, _candidates(intel_laptop))
    targeted = {p.config for p in props}
    assert "CONFIG_MICROCODE_INTEL" not in targeted


def test_amd_box_does_not_remove_microcode_amd(amd_desktop: Snapshot):
    props = deterministic_proposals(amd_desktop, _candidates(amd_desktop))
    targeted = {p.config for p in props}
    assert "CONFIG_MICROCODE_AMD" not in targeted


def test_proposals_never_propose_keeping_current_value(intel_laptop: Snapshot):
    """Sanity: each proposal must change the value, not restate it."""
    props = deterministic_proposals(intel_laptop, _candidates(intel_laptop))
    for p in props:
        assert p.proposed_value != p.current_value
