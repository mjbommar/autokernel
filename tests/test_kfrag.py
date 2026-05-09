"""Tests for the Kconfig fragment writer / reader."""

from __future__ import annotations

from pathlib import Path

from autokernel.kfrag import parse_kfrag, write_kfrag
from autokernel.models import (
    ProposalSource,
    RemovalProposal,
    ReviewDecision,
    Reviewer,
    ReviewedProposal,
    ReviewSet,
    RiskLevel,
)


def _proposal(
    config: str,
    proposed: str = "n",
    *,
    current: str | None = None,
    source: ProposalSource = ProposalSource.DETERMINISTIC,
) -> RemovalProposal:
    return RemovalProposal(
        config=config,
        current_value=current
        if current is not None
        else ("m" if proposed == "n" else "y"),
        proposed_value=proposed,
        reason="not present on this host",
        risk=RiskLevel.LOW,
        confidence=0.95,
        source=source,
        evidence=[],
    )


def _accepted(
    config: str,
    proposed: str = "n",
    *,
    current: str | None = None,
    source: ProposalSource = ProposalSource.DETERMINISTIC,
    rule: str = "test",
) -> ReviewedProposal:
    return ReviewedProposal(
        proposal=_proposal(config, proposed, current=current, source=source),
        decision=ReviewDecision.ACCEPT,
        reviewer=Reviewer.POLICY,
        rule=rule,
    )


def test_write_kfrag_basic_disables(tmp_path: Path):
    rs = ReviewSet(
        base_diff_path=tmp_path / "proposal.json",
        accepted=[_accepted("CONFIG_DRM_NOUVEAU"), _accepted("CONFIG_DRM_RADEON")],
    )
    out = tmp_path / "auto.kfrag"
    header = write_kfrag(out, rs, snapshot_dir=tmp_path, autonomy="advise")

    text = out.read_text()
    assert "autokernel-kfrag" in text
    assert "# CONFIG_DRM_NOUVEAU is not set" in text
    assert "# CONFIG_DRM_RADEON is not set" in text
    assert header.n_disable == 2
    assert header.n_demote == 0


def test_write_kfrag_demotions(tmp_path: Path):
    rs = ReviewSet(
        base_diff_path=tmp_path / "p.json",
        accepted=[_accepted("CONFIG_DRM_AMDGPU", proposed="m")],
    )
    out = tmp_path / "x.kfrag"
    header = write_kfrag(out, rs, snapshot_dir=tmp_path, autonomy="advise")

    text = out.read_text()
    assert "CONFIG_DRM_AMDGPU=m" in text
    assert header.n_demote == 1
    assert header.n_disable == 0


def test_write_kfrag_only_accepted_make_it(tmp_path: Path):
    """Rejected and deferred proposals must not appear in the kfrag."""
    accepted = _accepted("CONFIG_KEEP_ME")
    rejected_rp = ReviewedProposal(
        proposal=_proposal("CONFIG_REJECT_ME"),
        decision=ReviewDecision.REJECT,
        reviewer=Reviewer.POLICY,
        rule="test",
    )
    deferred_rp = ReviewedProposal(
        proposal=_proposal("CONFIG_DEFER_ME"),
        decision=ReviewDecision.DEFER,
        reviewer=Reviewer.POLICY,
        rule="test",
    )
    rs = ReviewSet(
        base_diff_path=tmp_path / "p.json",
        accepted=[accepted],
        rejected=[rejected_rp],
        deferred=[deferred_rp],
    )

    out = tmp_path / "kf"
    write_kfrag(out, rs, snapshot_dir=tmp_path, autonomy="advise")
    text = out.read_text()

    assert "CONFIG_KEEP_ME" in text
    assert "CONFIG_REJECT_ME" not in text
    assert "CONFIG_DEFER_ME" not in text


def test_write_kfrag_includes_provenance(tmp_path: Path):
    rs = ReviewSet(base_diff_path=tmp_path / "p.json", accepted=[_accepted("CONFIG_X")])
    out = tmp_path / "k"
    write_kfrag(
        out,
        rs,
        snapshot_dir=tmp_path,
        autonomy="auto-safe",
        model="anthropic:claude-sonnet-4-6",
        service_tier="flex",
    )
    text = out.read_text()
    assert "anthropic:claude-sonnet-4-6" in text
    assert "flex" in text
    assert "auto-safe" in text


def test_round_trip_through_parse_kfrag(tmp_path: Path):
    rs = ReviewSet(
        base_diff_path=tmp_path / "p.json",
        accepted=[
            _accepted("CONFIG_DRM_NOUVEAU"),
            _accepted("CONFIG_DRM_AMDGPU", proposed="m"),
            _accepted("CONFIG_104_QUAD_8"),
        ],
    )
    out = tmp_path / "rt.kfrag"
    write_kfrag(out, rs, snapshot_dir=tmp_path, autonomy="advise")

    parsed = parse_kfrag(out)
    assert set(parsed.disables) == {"CONFIG_DRM_NOUVEAU", "CONFIG_104_QUAD_8"}
    assert parsed.assignments == {"CONFIG_DRM_AMDGPU": "m"}
    assert any("autokernel-kfrag" in h for h in parsed.header_lines)


def test_empty_review_set_writes_only_header(tmp_path: Path):
    rs = ReviewSet(base_diff_path=tmp_path / "p.json")
    out = tmp_path / "empty.kfrag"
    header = write_kfrag(out, rs, snapshot_dir=tmp_path, autonomy="advise")
    text = out.read_text()
    assert "autokernel-kfrag" in text
    # No actionable lines
    parsed = parse_kfrag(out)
    assert parsed.disables == []
    assert parsed.assignments == {}
    assert header.n_disable == 0
    assert header.n_demote == 0


# ── v0.13 multi-dimensional values (choice/toggle/tunable) ───────────────


def test_kfrag_emits_choice_option_as_y(tmp_path: Path):
    """A choice-option proposal sets the option's CONFIG to =y so
    merge_config + olddefconfig flip the others to =n."""
    rs = ReviewSet(
        base_diff_path=tmp_path / "p.json",
        accepted=[
            _accepted(
                "CONFIG_PREEMPT",
                proposed="PREEMPT",
                current="PREEMPT_VOLUNTARY",
                source=ProposalSource.CHOICE,
            )
        ],
    )
    out = tmp_path / "choice.kfrag"
    header = write_kfrag(out, rs, snapshot_dir=tmp_path, autonomy="advise")
    text = out.read_text()
    assert "# CONFIG_PREEMPT_VOLUNTARY is not set" in text
    assert "CONFIG_PREEMPT=y" in text
    assert header.n_other == 1


def test_kfrag_emits_int_tunable_unquoted(tmp_path: Path):
    rs = ReviewSet(
        base_diff_path=tmp_path / "p.json",
        accepted=[_accepted("CONFIG_NR_CPUS", proposed="64")],
    )
    out = tmp_path / "int.kfrag"
    header = write_kfrag(out, rs, snapshot_dir=tmp_path, autonomy="advise")
    text = out.read_text()
    assert "CONFIG_NR_CPUS=64" in text
    assert header.n_other == 1


def test_kfrag_emits_string_tunable_quoted(tmp_path: Path):
    rs = ReviewSet(
        base_diff_path=tmp_path / "p.json",
        accepted=[_accepted("CONFIG_LOCALVERSION", proposed="-autokernel")],
    )
    out = tmp_path / "str.kfrag"
    header = write_kfrag(out, rs, snapshot_dir=tmp_path, autonomy="advise")
    text = out.read_text()
    assert 'CONFIG_LOCALVERSION="-autokernel"' in text
    assert header.n_other == 1


def test_kfrag_emits_hex_tunable(tmp_path: Path):
    rs = ReviewSet(
        base_diff_path=tmp_path / "p.json",
        accepted=[_accepted("CONFIG_DEFAULT_MMAP_MIN_ADDR", proposed="0x10000")],
    )
    out = tmp_path / "hex.kfrag"
    write_kfrag(out, rs, snapshot_dir=tmp_path, autonomy="advise")
    text = out.read_text()
    assert "CONFIG_DEFAULT_MMAP_MIN_ADDR=0x10000" in text


def test_kfrag_preserves_already_quoted_string(tmp_path: Path):
    rs = ReviewSet(
        base_diff_path=tmp_path / "p.json",
        accepted=[_accepted("CONFIG_LOCALVERSION", proposed='"-quoted"')],
    )
    out = tmp_path / "quoted.kfrag"
    write_kfrag(out, rs, snapshot_dir=tmp_path, autonomy="advise")
    text = out.read_text()
    # The quotes are preserved verbatim — not double-quoted.
    assert 'CONFIG_LOCALVERSION="-quoted"' in text
    assert 'CONFIG_LOCALVERSION=""-quoted""' not in text
