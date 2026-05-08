"""Tests for the kfrag → .config merge engine."""

from __future__ import annotations

from pathlib import Path


from autokernel.merge import (
    merge_kfrag,
    validate_load_bearing,
)


def _write(p: Path, content: str) -> Path:
    p.write_text(content)
    return p


def test_merge_disables_y_to_not_set(tmp_path: Path):
    base = _write(tmp_path / "base", "CONFIG_FOO=y\nCONFIG_BAR=m\n")
    kfrag = _write(
        tmp_path / "k.kfrag",
        "# autokernel-kfrag schema=1\n# CONFIG_FOO is not set\n",
    )
    merged, report = merge_kfrag(base, kfrag)
    assert "# CONFIG_FOO is not set" in merged
    assert "CONFIG_BAR=m" in merged
    assert ("CONFIG_FOO", "y", "n") in report.overrides
    assert report.fragment_only == []


def test_merge_demotes_y_to_m(tmp_path: Path):
    base = _write(tmp_path / "b", "CONFIG_FOO=y\n")
    kfrag = _write(tmp_path / "k", "CONFIG_FOO=m\n")
    merged, report = merge_kfrag(base, kfrag)
    assert "CONFIG_FOO=m" in merged
    assert "CONFIG_FOO=y" not in merged
    assert ("CONFIG_FOO", "y", "m") in report.overrides


def test_merge_no_op_when_values_match(tmp_path: Path):
    base = _write(tmp_path / "b", "CONFIG_FOO=y\n")
    kfrag = _write(tmp_path / "k", "CONFIG_FOO=y\n")
    merged, report = merge_kfrag(base, kfrag)
    assert "CONFIG_FOO=y" in merged
    assert report.no_ops == ["CONFIG_FOO"]
    assert report.overrides == []


def test_kfrag_wins_on_conflict(tmp_path: Path):
    """Per merge_config.sh -m semantics, the fragment overrides the base
    even when both set the same symbol."""
    base = _write(tmp_path / "b", "CONFIG_FOO=y\nCONFIG_BAR=y\n")
    kfrag = _write(
        tmp_path / "k",
        "# CONFIG_FOO is not set\nCONFIG_BAR=m\n",
    )
    merged, _ = merge_kfrag(base, kfrag)
    assert "# CONFIG_FOO is not set" in merged
    assert "CONFIG_FOO=y" not in merged
    assert "CONFIG_BAR=m" in merged
    assert "CONFIG_BAR=y" not in merged


def test_fragment_only_symbols_appended(tmp_path: Path):
    base = _write(tmp_path / "b", "CONFIG_BASE=y\n")
    kfrag = _write(tmp_path / "k", "CONFIG_NEW=m\n")
    merged, report = merge_kfrag(base, kfrag)
    assert "CONFIG_BASE=y" in merged
    assert "CONFIG_NEW=m" in merged
    assert "CONFIG_NEW" in report.fragment_only
    # And the appended marker is present
    assert "appended from kfrag" in merged


def test_comments_and_blanks_preserved(tmp_path: Path):
    base_text = "# Top comment\n\nCONFIG_FOO=y\n# trailing\n"
    base = _write(tmp_path / "b", base_text)
    kfrag = _write(tmp_path / "k", "")
    merged, _ = merge_kfrag(base, kfrag)
    assert "# Top comment" in merged
    assert "# trailing" in merged
    assert "CONFIG_FOO=y" in merged


def test_string_values_quoted(tmp_path: Path):
    base = _write(tmp_path / "b", 'CONFIG_LOCALVERSION=""\n')
    kfrag = _write(tmp_path / "k", "CONFIG_LOCALVERSION=-autokernel\n")
    merged, _ = merge_kfrag(base, kfrag)
    assert '"-autokernel"' in merged


def test_numeric_values_unquoted(tmp_path: Path):
    base = _write(tmp_path / "b", "CONFIG_LOG_BUF_SHIFT=17\n")
    kfrag = _write(tmp_path / "k", "CONFIG_LOG_BUF_SHIFT=20\n")
    merged, _ = merge_kfrag(base, kfrag)
    assert "CONFIG_LOG_BUF_SHIFT=20" in merged
    assert '"20"' not in merged


def test_hex_values_unquoted(tmp_path: Path):
    base = _write(tmp_path / "b", "CONFIG_PHYSICAL_START=0x1000000\n")
    kfrag = _write(tmp_path / "k", "CONFIG_PHYSICAL_START=0x200000\n")
    merged, _ = merge_kfrag(base, kfrag)
    assert "CONFIG_PHYSICAL_START=0x200000" in merged


def test_merge_idempotent(tmp_path: Path):
    base = _write(tmp_path / "b", "CONFIG_FOO=y\nCONFIG_BAR=y\n")
    kfrag = _write(tmp_path / "k", "# CONFIG_FOO is not set\n")
    once, _ = merge_kfrag(base, kfrag)
    twice_path = tmp_path / "twice"
    twice_path.write_text(once)
    twice, report = merge_kfrag(twice_path, kfrag)
    # Re-merging the kfrag into its own output should be a no-op.
    assert twice == once
    assert "CONFIG_FOO" in [n for n in report.no_ops]


def test_realistic_kfrag_round_trip(tmp_path: Path):
    """End-to-end: build a kfrag with the writer, merge it back, verify."""
    from autokernel.kfrag import write_kfrag
    from autokernel.models import (
        ProposalSource,
        RemovalProposal,
        Reviewer,
        ReviewedProposal,
        ReviewSet,
        RiskLevel,
        ReviewDecision,
    )

    def _accepted(c: str, p: str = "n") -> ReviewedProposal:
        return ReviewedProposal(
            proposal=RemovalProposal(
                config=c,
                current_value="m" if p == "n" else "y",
                proposed_value=p,
                reason="t",
                risk=RiskLevel.LOW,
                confidence=0.9,
                source=ProposalSource.LLM,
                evidence=[],
            ),
            decision=ReviewDecision.ACCEPT,
            reviewer=Reviewer.POLICY,
            rule="t",
        )

    rs = ReviewSet(
        base_diff_path=tmp_path / "p.json",
        accepted=[_accepted("CONFIG_DRM_NOUVEAU"), _accepted("CONFIG_DRM_AMDGPU", "m")],
    )
    kfrag = tmp_path / "auto.kfrag"
    write_kfrag(kfrag, rs, snapshot_dir=tmp_path, autonomy="advise")

    base = _write(
        tmp_path / "base",
        "CONFIG_DRM=y\nCONFIG_DRM_NOUVEAU=m\nCONFIG_DRM_AMDGPU=y\nCONFIG_KEEP=y\n",
    )
    merged, report = merge_kfrag(base, kfrag)

    assert "# CONFIG_DRM_NOUVEAU is not set" in merged
    assert "CONFIG_DRM_AMDGPU=m" in merged
    assert "CONFIG_DRM_NOUVEAU=m" not in merged
    assert "CONFIG_DRM_AMDGPU=y" not in merged
    assert "CONFIG_KEEP=y" in merged
    assert len(report.overrides) == 2


# ── validation ──────────────────────────────────────────────────────────────


def test_validate_load_bearing_passes_when_all_set():
    text = "CONFIG_BTRFS_FS=m\nCONFIG_DM_CRYPT=y\n"
    findings = validate_load_bearing(
        text, {"CONFIG_BTRFS_FS": "root fs", "CONFIG_DM_CRYPT": "LUKS"}
    )
    assert findings == []


def test_validate_load_bearing_flags_disabled():
    text = "# CONFIG_BTRFS_FS is not set\nCONFIG_DM_CRYPT=y\n"
    findings = validate_load_bearing(text, {"CONFIG_BTRFS_FS": "root fs"})
    assert len(findings) == 1
    assert findings[0].symbol == "CONFIG_BTRFS_FS"
    assert findings[0].actual_value == "n"


def test_validate_load_bearing_flags_missing():
    text = "CONFIG_OTHER=y\n"
    findings = validate_load_bearing(text, {"CONFIG_BTRFS_FS": "root fs"})
    assert len(findings) == 1
    assert findings[0].actual_value == "missing"
