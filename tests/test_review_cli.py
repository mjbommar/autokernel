"""Integration tests for the `autokernel review` CLI verb.

Uses Typer's CliRunner so we don't fork; verifies that flag composition
produces the expected review.json + auto.kfrag artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from autokernel.cli import app
from autokernel.kfrag import parse_kfrag
from autokernel.models import (
    ConfigDiff,
    ProposalSource,
    RemovalProposal,
    RiskLevel,
)


runner = CliRunner()


def _seed_snapshot(tmp_path: Path) -> Path:
    """Materialize a minimal snapshot dir with a hand-crafted proposal.json
    that exercises mixed risk/source/subsystem combinations."""
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "manifest").write_text("schema_version=1\nhost=test\nuser=t\n")

    def _p(config, *, risk=RiskLevel.LOW, conf=0.9, source=ProposalSource.LLM, val="m"):
        return RemovalProposal(
            config=config,
            current_value=val,
            proposed_value="n",
            reason=f"test reason for {config}",
            risk=risk,
            confidence=conf,
            source=source,
            evidence=[],
        )

    proposals = [
        _p(
            "CONFIG_DRM_NOUVEAU",
            risk=RiskLevel.LOW,
            source=ProposalSource.DETERMINISTIC,
            conf=0.95,
        ),
        _p(
            "CONFIG_DRM_RADEON",
            risk=RiskLevel.LOW,
            source=ProposalSource.DETERMINISTIC,
            conf=0.95,
        ),
        _p("CONFIG_104_QUAD_8", risk=RiskLevel.LOW, conf=0.85),
        _p("CONFIG_DEBUG_KERNEL", risk=RiskLevel.HIGH, conf=0.7),
        _p("CONFIG_CRYPTO_AES_GENERIC", risk=RiskLevel.LOW, conf=0.6),
        _p("CONFIG_USB_OPTIONAL_GADGET", risk=RiskLevel.MEDIUM, conf=0.7),
    ]
    diff = ConfigDiff(
        base_config_path=snap / "running_config",
        autonomy="advise",
        needs_review=proposals,
    )
    (snap / "proposal.json").write_text(diff.model_dump_json(indent=2))
    return snap


def test_review_no_rules_defers_everything(tmp_path: Path):
    snap = _seed_snapshot(tmp_path)
    result = runner.invoke(app, ["review", str(snap)])
    assert result.exit_code == 0, result.output

    rs = json.loads((snap / "review.json").read_text())
    assert len(rs["deferred"]) == 6
    assert rs["accepted"] == []
    assert rs["rejected"] == []


def test_review_accept_recommended(tmp_path: Path):
    snap = _seed_snapshot(tmp_path)
    result = runner.invoke(app, ["review", str(snap), "--accept-recommended"])
    assert result.exit_code == 0, result.output

    rs = json.loads((snap / "review.json").read_text())
    accepted = {r["proposal"]["config"] for r in rs["accepted"]}
    deferred = {r["proposal"]["config"] for r in rs["deferred"]}

    # CONFIG_DEBUG_KERNEL is HIGH → deferred
    assert "CONFIG_DEBUG_KERNEL" in deferred
    # everything else is low/medium → accepted
    assert {
        "CONFIG_DRM_NOUVEAU",
        "CONFIG_DRM_RADEON",
        "CONFIG_104_QUAD_8",
        "CONFIG_CRYPTO_AES_GENERIC",
        "CONFIG_USB_OPTIONAL_GADGET",
    } <= accepted


def test_review_reject_subsystem_then_accept(tmp_path: Path):
    """Veto crypto, accept-recommended for the rest. Expect crypto rejected,
    DEBUG_KERNEL deferred, others accepted."""
    snap = _seed_snapshot(tmp_path)
    result = runner.invoke(
        app,
        [
            "review",
            str(snap),
            "--reject-subsystem",
            "crypto",
            "--accept-recommended",
        ],
    )
    assert result.exit_code == 0, result.output

    rs = json.loads((snap / "review.json").read_text())
    rejected = {r["proposal"]["config"] for r in rs["rejected"]}
    accepted = {r["proposal"]["config"] for r in rs["accepted"]}
    deferred = {r["proposal"]["config"] for r in rs["deferred"]}

    assert rejected == {"CONFIG_CRYPTO_AES_GENERIC"}
    assert deferred == {"CONFIG_DEBUG_KERNEL"}
    assert "CONFIG_DRM_NOUVEAU" in accepted


def test_review_emits_kfrag(tmp_path: Path):
    snap = _seed_snapshot(tmp_path)
    result = runner.invoke(app, ["review", str(snap), "--accept-recommended"])
    assert result.exit_code == 0, result.output

    kfrag_path = snap / "auto.kfrag"
    assert kfrag_path.exists()

    parsed = parse_kfrag(kfrag_path)
    # All accepted disables should appear in the kfrag.
    assert "CONFIG_DRM_NOUVEAU" in parsed.disables
    assert "CONFIG_104_QUAD_8" in parsed.disables
    # Deferred + rejected must not appear.
    assert "CONFIG_DEBUG_KERNEL" not in parsed.disables
    assert "CONFIG_DEBUG_KERNEL" not in parsed.assignments


def test_review_pattern_filter(tmp_path: Path):
    snap = _seed_snapshot(tmp_path)
    result = runner.invoke(
        app,
        [
            "review",
            str(snap),
            "--reject-pattern",
            "CONFIG_DRM_*",
            "--accept-recommended",
        ],
    )
    assert result.exit_code == 0, result.output

    rs = json.loads((snap / "review.json").read_text())
    rejected = {r["proposal"]["config"] for r in rs["rejected"]}
    assert "CONFIG_DRM_NOUVEAU" in rejected
    assert "CONFIG_DRM_RADEON" in rejected


def test_review_missing_proposal_exits_2(tmp_path: Path):
    snap = tmp_path / "empty"
    snap.mkdir()
    (snap / "manifest").write_text("schema_version=1\n")
    result = runner.invoke(app, ["review", str(snap)])
    assert result.exit_code == 2
    assert "proposal not found" in result.output.lower()


def test_review_missing_snapshot_dir_exits_2(tmp_path: Path):
    result = runner.invoke(app, ["review", str(tmp_path / "nonexistent")])
    assert result.exit_code == 2
