"""Deterministic GPU/CPU mismatch rules — pure logic, no LLM."""

from __future__ import annotations

from autokernel.agent import deterministic_proposals
from autokernel.models import ProposalSource, RiskLevel, Snapshot
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


# ── microarch tuning ─────────────────────────────────────────────────────


def test_intel_meteor_lake_microarch_proposal(intel_laptop: Snapshot):
    """Intel laptop fixture is Core Ultra 7 165U (family 6 model 170 →
    Meteor Lake). The kernel release is 6.13 which supports MMETEORLAKE
    (added in 6.7). Expect: disable GENERIC_CPU + enable MMETEORLAKE."""
    cands = _candidates(intel_laptop)
    # The fixture's running_config doesn't currently have CONFIG_GENERIC_CPU=y
    # (we'd have to add it). Inject one to exercise the disable path.
    cands_with_generic = [*cands, ("CONFIG_GENERIC_CPU", "y")]
    props = deterministic_proposals(intel_laptop, cands_with_generic)
    micro = [p for p in props if p.source == ProposalSource.MICROARCH]
    by_cfg = {p.config: p for p in micro}
    assert "CONFIG_GENERIC_CPU" in by_cfg
    assert by_cfg["CONFIG_GENERIC_CPU"].proposed_value == "n"
    assert "CONFIG_MMETEORLAKE" in by_cfg
    assert by_cfg["CONFIG_MMETEORLAKE"].proposed_value == "y"
    # Both should be load-bearing-style high confidence
    for p in micro:
        assert p.confidence >= 0.9
        assert p.risk == RiskLevel.LOW


def test_amd_zen3_microarch_proposal(amd_desktop: Snapshot):
    """AMD desktop fixture is Ryzen 5800X3D (family 25 model 33 → Zen 3)."""
    cands = [*_candidates(amd_desktop), ("CONFIG_GENERIC_CPU", "y")]
    props = deterministic_proposals(amd_desktop, cands)
    by_cfg = {p.config: p for p in props if p.source == ProposalSource.MICROARCH}
    assert "CONFIG_MZEN3" in by_cfg
    assert by_cfg["CONFIG_MZEN3"].proposed_value == "y"


def test_microarch_proposal_skipped_when_target_already_set(intel_laptop: Snapshot):
    """If the running config already has CONFIG_MMETEORLAKE=y, no enable
    proposal should be emitted (and the GENERIC_CPU disable also skipped
    because GENERIC_CPU isn't currently set)."""
    cands = [*_candidates(intel_laptop), ("CONFIG_MMETEORLAKE", "y")]
    props = deterministic_proposals(intel_laptop, cands)
    micro_targets = {
        p.config for p in props
        if p.source == ProposalSource.MICROARCH and p.proposed_value == "y"
    }
    assert "CONFIG_MMETEORLAKE" not in micro_targets


def test_microarch_proposal_skipped_for_unrecognized_cpu():
    """Snapshot with an unknown CPU should produce no microarch proposals."""
    from autokernel.models import (
        BootContext, CpuInfo, KernelInfo, Snapshot,
    )
    from datetime import UTC, datetime
    from pathlib import Path

    snap = Snapshot(
        collected_at=datetime.now(UTC),
        host="x",
        snapshot_dir=Path("/tmp/x"),
        kernel=KernelInfo(release="6.13.0", version="x", arch="x86_64"),
        cpu=CpuInfo(vendor_id="UnknownCorp", cpu_family=99, model=99),
        boot=BootContext(cmdline=""),
    )
    props = deterministic_proposals(snap, [("CONFIG_GENERIC_CPU", "y")])
    micro = [p for p in props if p.source == ProposalSource.MICROARCH]
    assert micro == []
