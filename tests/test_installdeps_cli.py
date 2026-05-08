"""End-to-end tests for `autokernel install-deps`."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from autokernel import cli, installdeps as id_mod
from autokernel.cli import app
from autokernel.distro import parse_os_release

runner = CliRunner()


@pytest.fixture
def patched_debian(monkeypatch):
    monkeypatch.setattr(
        cli,
        "detect_distro",
        lambda: parse_os_release(
            'ID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu Test"\n'
        ),
    )


@pytest.fixture
def captured_runs(monkeypatch):
    calls: list[dict[str, Any]] = []

    class _R:
        def __init__(self, rc=0):
            self.returncode = rc

    def _fake(argv, **kwargs):
        f = kwargs.get("stdout")
        if hasattr(f, "write"):
            f.write(b"")
        calls.append({"argv": list(argv), **kwargs})
        return _R(0)

    monkeypatch.setattr(id_mod.subprocess, "run", _fake)
    return calls


# ── dry-run ────────────────────────────────────────────────────────────────


def test_dry_run_renders_apt_command(monkeypatch, patched_debian, captured_runs):
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: (p, []))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: True)
    result = runner.invoke(app, ["install-deps", "--for", "build"])
    assert result.exit_code == 0
    assert "missing system packages" in result.output.lower()
    assert "apt install" in result.output
    # No subprocess called in dry-run
    assert captured_runs == []


def test_dry_run_when_nothing_missing_says_so(
    monkeypatch, patched_debian, captured_runs
):
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: ([], list(p)))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: True)
    result = runner.invoke(app, ["install-deps", "--for", "build"])
    assert result.exit_code == 0
    assert "already up to date" in result.output.lower()


def test_boot_test_target_lists_virtme_as_optional_uv_tool(
    monkeypatch, patched_debian, captured_runs
):
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: ([], list(p)))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: False)
    result = runner.invoke(app, ["install-deps", "--for", "boot-test"])
    assert result.exit_code == 0
    assert "virtme-ng" in result.output
    assert "uv tool install" in result.output
    # Must NOT suggest pip
    assert "pip install" not in result.output


def test_no_virtme_flag_omits_optional(monkeypatch, patched_debian, captured_runs):
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: ([], list(p)))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: False)
    result = runner.invoke(app, ["install-deps", "--for", "boot-test", "--no-virtme"])
    assert result.exit_code == 0
    assert "virtme-ng" not in result.output


# ── execute ────────────────────────────────────────────────────────────────


def test_execute_invokes_apt_install(monkeypatch, patched_debian, captured_runs):
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: (p, []))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: True)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    result = runner.invoke(app, ["install-deps", "--for", "build", "--execute"])
    assert result.exit_code == 0, result.output
    # Should have called sudo apt install -y …
    assert any(c["argv"][0] == "sudo" and "apt" in c["argv"] for c in captured_runs)


def test_execute_uses_uv_tool_for_virtme(monkeypatch, patched_debian, captured_runs):
    """The optional-python path uses `uv tool install`, not pip."""
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: ([], list(p)))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: False)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    result = runner.invoke(app, ["install-deps", "--for", "boot-test", "--execute"])
    assert result.exit_code == 0, result.output
    virtme_calls = [c for c in captured_runs if "virtme-ng" in c["argv"]]
    assert len(virtme_calls) == 1
    assert virtme_calls[0]["argv"][:3] == ["uv", "tool", "install"]


def test_execute_idempotent_when_nothing_missing(
    monkeypatch, patched_debian, captured_runs
):
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: ([], list(p)))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: True)
    result = runner.invoke(app, ["install-deps", "--for", "build", "--execute"])
    assert result.exit_code == 0
    # No subprocess invoked when there's nothing to install
    assert captured_runs == []


# ── unknown distro ────────────────────────────────────────────────────────


def test_unknown_distro_exits_2(monkeypatch, captured_runs):
    monkeypatch.setattr(
        cli,
        "detect_distro",
        lambda: parse_os_release("ID=mystery\n"),
    )
    result = runner.invoke(app, ["install-deps"])
    assert result.exit_code == 2
    assert captured_runs == []
