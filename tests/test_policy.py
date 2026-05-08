"""Policy + load-bearing blocklist behavior on synthetic fixtures."""

from __future__ import annotations

from autokernel.models import (
    ProposalSource,
    RemovalProposal,
    RiskLevel,
    Snapshot,
)
from autokernel.policy import (
    AutonomyLevel,
    apply_policy,
    compute_load_bearing,
)
from autokernel.resolve import _running_config_symbols, resolve


def _candidates(snap: Snapshot) -> list[tuple[str, str]]:
    return [
        (s, v)
        for s, v in _running_config_symbols(snap.running_config_path).items()
        if v in ("y", "m")
    ]


# ── load-bearing detection ──────────────────────────────────────────────────


def test_root_fs_btrfs_is_load_bearing(intel_laptop: Snapshot):
    res = resolve(intel_laptop)
    lb = compute_load_bearing(intel_laptop, res)
    is_lb, _ = lb.contains("CONFIG_BTRFS_FS")
    assert is_lb


def test_root_fs_ext4_is_load_bearing(amd_desktop: Snapshot):
    res = resolve(amd_desktop)
    lb = compute_load_bearing(amd_desktop, res)
    is_lb, _ = lb.contains("CONFIG_EXT4_FS")
    assert is_lb


def test_efi_chain_load_bearing_when_efi(intel_laptop: Snapshot):
    res = resolve(intel_laptop)
    lb = compute_load_bearing(intel_laptop, res)
    assert lb.contains("CONFIG_EFI")[0]
    assert lb.contains("CONFIG_EFI_STUB")[0]


def test_luks_load_bearing_when_in_chain(intel_laptop: Snapshot):
    """Intel laptop has LUKS root → DM_CRYPT and AES/XTS must be locked."""
    res = resolve(intel_laptop)
    lb = compute_load_bearing(intel_laptop, res)
    assert lb.contains("CONFIG_DM_CRYPT")[0]
    assert lb.contains("CONFIG_CRYPTO_AES")[0]
    assert lb.contains("CONFIG_CRYPTO_XTS")[0]


def test_no_luks_no_dm_crypt_lock(amd_desktop: Snapshot):
    """AMD desktop has no LUKS → DM_CRYPT need not be load-bearing."""
    res = resolve(amd_desktop)
    lb = compute_load_bearing(amd_desktop, res)
    is_lb, reason = lb.contains("CONFIG_DM_CRYPT")
    assert not is_lb or reason != "LUKS in boot chain"


def test_microcode_locked_to_cpu_vendor(intel_laptop: Snapshot, amd_desktop: Snapshot):
    intel_lb = compute_load_bearing(intel_laptop, resolve(intel_laptop))
    amd_lb = compute_load_bearing(amd_desktop, resolve(amd_desktop))
    assert intel_lb.contains("CONFIG_MICROCODE_INTEL")[0]
    assert amd_lb.contains("CONFIG_MICROCODE_AMD")[0]


def test_hard_blocklist_always_present(intel_laptop: Snapshot):
    """Sanity: core-infra blocklist is in every load-bearing set."""
    lb = compute_load_bearing(intel_laptop, resolve(intel_laptop))
    for sym in ("CONFIG_PRINTK", "CONFIG_TTY", "CONFIG_MODULES", "CONFIG_DEVTMPFS"):
        assert lb.contains(sym)[0], sym


# ── apply_policy decision matrix ─────────────────────────────────────────────


def _proposal(
    config: str,
    current: str,
    *,
    risk: RiskLevel,
    conf: float,
    source: ProposalSource = ProposalSource.LLM,
) -> RemovalProposal:
    return RemovalProposal(
        config=config,
        current_value=current,
        proposed_value="n",
        reason="test",
        risk=risk,
        confidence=conf,
        source=source,
        evidence=[],
    )


def test_blocked_proposals_never_auto_apply_at_any_level(intel_laptop: Snapshot):
    """A proposal targeting a load-bearing symbol is blocked at every level."""
    res = resolve(intel_laptop)
    lb = compute_load_bearing(intel_laptop, res)
    p = _proposal(
        "CONFIG_BTRFS_FS",
        "y",
        risk=RiskLevel.LOW,
        conf=0.99,
        source=ProposalSource.DETERMINISTIC,
    )
    for level in AutonomyLevel:
        result = apply_policy([p], level, lb)
        assert not result.auto_applied, (
            f"{level.value}: blocked proposal leaked into auto_applied"
        )
        assert not result.needs_review, (
            f"{level.value}: blocked proposal leaked into needs_review"
        )
        assert result.blocked, f"{level.value}: should be in blocked"
        assert result.blocked[0][0].config == "CONFIG_BTRFS_FS"


def test_auto_safe_applies_low_risk_high_confidence(intel_laptop: Snapshot):
    res = resolve(intel_laptop)
    lb = compute_load_bearing(intel_laptop, res)
    p = _proposal("CONFIG_DRM_AMDGPU", "m", risk=RiskLevel.LOW, conf=0.95)
    r = apply_policy([p], AutonomyLevel.AUTO_SAFE, lb)
    assert len(r.auto_applied) == 1
    assert not r.needs_review


def test_auto_safe_defers_low_confidence(intel_laptop: Snapshot):
    res = resolve(intel_laptop)
    lb = compute_load_bearing(intel_laptop, res)
    p = _proposal("CONFIG_DRM_AMDGPU", "m", risk=RiskLevel.LOW, conf=0.7)
    r = apply_policy([p], AutonomyLevel.AUTO_SAFE, lb)
    assert not r.auto_applied
    assert len(r.needs_review) == 1


def test_auto_bold_defers_high_risk(intel_laptop: Snapshot):
    res = resolve(intel_laptop)
    lb = compute_load_bearing(intel_laptop, res)
    p = _proposal("CONFIG_DRM_AMDGPU", "m", risk=RiskLevel.HIGH, conf=0.95)
    r = apply_policy([p], AutonomyLevel.AUTO_BOLD, lb)
    assert not r.auto_applied
    assert len(r.needs_review) == 1


def test_auto_bold_applies_medium_risk(intel_laptop: Snapshot):
    res = resolve(intel_laptop)
    lb = compute_load_bearing(intel_laptop, res)
    p = _proposal("CONFIG_104_QUAD_8", "m", risk=RiskLevel.MEDIUM, conf=0.7)
    r = apply_policy([p], AutonomyLevel.AUTO_BOLD, lb)
    assert len(r.auto_applied) == 1


def test_explain_produces_only_annotations(intel_laptop: Snapshot):
    """EXPLAIN's contract: zero actionable changes; everything is just
    explanation. So needs_review/auto_applied stay empty and proposals
    show up in annotations."""
    res = resolve(intel_laptop)
    lb = compute_load_bearing(intel_laptop, res)
    p = _proposal("CONFIG_104_QUAD_8", "m", risk=RiskLevel.LOW, conf=0.9)
    r = apply_policy([p], AutonomyLevel.EXPLAIN, lb)
    assert r.auto_applied == []
    assert r.needs_review == []
    assert len(r.annotations) == 1


def test_advise_auto_applies_high_confidence_deterministic(intel_laptop: Snapshot):
    """A deterministic proposal at conf>=0.95 is treated as confirmed at
    ADVISE — the user opted into the deterministic rules."""
    res = resolve(intel_laptop)
    lb = compute_load_bearing(intel_laptop, res)
    p = _proposal(
        "CONFIG_X86_AMD_PSTATE",
        "y",
        risk=RiskLevel.LOW,
        conf=0.99,
        source=ProposalSource.DETERMINISTIC,
    )
    r = apply_policy([p], AutonomyLevel.ADVISE, lb)
    assert len(r.auto_applied) == 1
    assert not r.needs_review


def test_advise_does_not_auto_apply_llm_high_confidence(intel_laptop: Snapshot):
    """LLM proposals — even at high confidence — still need user review at
    ADVISE. Only DETERMINISTIC source is fast-tracked."""
    res = resolve(intel_laptop)
    lb = compute_load_bearing(intel_laptop, res)
    p = _proposal(
        "CONFIG_104_QUAD_8",
        "m",
        risk=RiskLevel.LOW,
        conf=0.99,
        source=ProposalSource.LLM,
    )
    r = apply_policy([p], AutonomyLevel.ADVISE, lb)
    assert not r.auto_applied
    assert len(r.needs_review) == 1


def test_advise_keeps_low_confidence_deterministic_in_review(intel_laptop: Snapshot):
    """A deterministic proposal at confidence below the threshold still
    needs review."""
    res = resolve(intel_laptop)
    lb = compute_load_bearing(intel_laptop, res)
    p = _proposal(
        "CONFIG_104_QUAD_8",
        "m",
        risk=RiskLevel.LOW,
        conf=0.8,
        source=ProposalSource.DETERMINISTIC,
    )
    r = apply_policy([p], AutonomyLevel.ADVISE, lb)
    assert not r.auto_applied
    assert len(r.needs_review) == 1
