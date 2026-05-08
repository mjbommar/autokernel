"""End-to-end tests for `autokernel preflight`."""

from __future__ import annotations

import shutil
from pathlib import Path

from typer.testing import CliRunner

from autokernel import preflight as pf
from autokernel.cli import app

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


def test_preflight_exits_0_when_all_pass(monkeypatch):
    """Mock everything to pass."""
    monkeypatch.setattr(pf.shutil, "which", lambda c: f"/usr/bin/{c}")
    monkeypatch.setattr(
        pf.os,
        "statvfs",
        lambda p: type("V", (), {"f_bavail": 10**9, "f_frsize": 4096})(),
    )
    monkeypatch.setattr(
        pf.subprocess,
        "run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": ""})(),
    )
    result = runner.invoke(app, ["preflight", "--for", "scan"])
    assert result.exit_code == 0


def test_preflight_exits_1_on_failure(monkeypatch):
    """Force a build_tools FAIL via a missing tool."""
    monkeypatch.setattr(
        pf.shutil, "which", lambda c: None if c == "flex" else f"/usr/bin/{c}"
    )
    monkeypatch.setattr(
        pf.subprocess,
        "run",
        lambda *a, **kw: type(
            "R",
            (),
            {
                "returncode": 0,
                "stdout": "libssl-dev install ok installed\nlibelf-dev install ok installed\nlibncurses-dev install ok installed",
            },
        )(),
    )
    result = runner.invoke(app, ["preflight", "--for", "build"])
    assert result.exit_code == 1
    assert "fail" in result.output.lower()


def test_preflight_strict_treats_warn_as_fail(monkeypatch):
    """In strict mode, a single WARN flips exit code to 1."""

    # Force pahole missing → recommended_tools WARN
    def _which(c):
        return None if c == "pahole" else f"/usr/bin/{c}"

    monkeypatch.setattr(pf.shutil, "which", _which)
    monkeypatch.setattr(
        pf.os,
        "statvfs",
        lambda p: type("V", (), {"f_bavail": 10**9, "f_frsize": 4096})(),
    )
    monkeypatch.setattr(
        pf.subprocess,
        "run",
        lambda *a, **kw: type(
            "R",
            (),
            {
                "returncode": 0,
                "stdout": "libssl-dev install ok installed\nlibelf-dev install ok installed\nlibncurses-dev install ok installed",
            },
        )(),
    )

    result_normal = runner.invoke(app, ["preflight", "--for", "build"])
    assert result_normal.exit_code == 0  # WARN doesn't fail without --strict

    result_strict = runner.invoke(app, ["preflight", "--for", "build", "--strict"])
    assert result_strict.exit_code == 1


def test_preflight_with_snapshot_runs_snapshot_checks(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(pf.shutil, "which", lambda c: f"/usr/bin/{c}")
    monkeypatch.setattr(
        pf.subprocess,
        "run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": ""})(),
    )

    snap = tmp_path / "snap"
    shutil.copytree(FIXTURES / "intel_laptop", snap)

    result = runner.invoke(app, ["preflight", str(snap), "--for", "propose"])
    assert result.exit_code == 0
    assert "snapshot_running_config" in result.output


def test_preflight_unknown_for_value_exits_2():
    result = runner.invoke(app, ["preflight", "--for", "bogus"])
    assert result.exit_code == 2
    assert "unknown --for" in result.output.lower()


def test_preflight_invalid_snapshot_exits_2(tmp_path: Path):
    not_a_snap = tmp_path / "nope"
    not_a_snap.mkdir()
    result = runner.invoke(app, ["preflight", str(not_a_snap)])
    assert result.exit_code == 2
    assert "not an autokernel snapshot" in result.output.lower()
