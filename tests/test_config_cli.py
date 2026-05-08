"""End-to-end tests for `autokernel config show` and `autokernel config test`.

The pydantic-ai round-trip in ``test_connection`` is mocked so these
tests don't touch the network or burn credits.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from autokernel import llm as llm_mod
from autokernel.cli import app
from autokernel.llm import ConnectionResult, LLMConfig, Provider

runner = CliRunner()


# ── config show ────────────────────────────────────────────────────────────


def test_config_show_with_anthropic_key(monkeypatch):
    monkeypatch.setattr(
        llm_mod.os, "environ",
        {"ANTHROPIC_API_KEY": "sk-ant-test"},
    )
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0, result.output
    assert "anthropic" in result.output.lower()
    assert "ANTHROPIC_API_KEY" in result.output
    # Default model resolution: should see claude-sonnet
    assert "claude-sonnet" in result.output.lower()


def test_config_show_with_no_keys_prints_diagnostic(monkeypatch):
    monkeypatch.setattr(llm_mod.os, "environ", {})
    result = runner.invoke(app, ["config", "show"])
    # show does not exit non-zero — it just reports unresolved.
    assert result.exit_code == 0
    assert "cannot resolve" in result.output.lower()


def test_config_show_specific_mode(monkeypatch):
    monkeypatch.setattr(
        llm_mod.os, "environ",
        {"ANTHROPIC_API_KEY": "sk-ant-test"},
    )
    result = runner.invoke(app, ["config", "show", "--mode", "cheap"])
    assert result.exit_code == 0
    assert "haiku" in result.output.lower()


def test_config_show_lists_all_providers_even_when_unset(monkeypatch):
    monkeypatch.setattr(
        llm_mod.os, "environ",
        {"ANTHROPIC_API_KEY": "sk-ant-test"},
    )
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    # Every provider in the enum should appear in the report
    for p in Provider:
        assert p.value in result.output


# ── config test ────────────────────────────────────────────────────────────


def _patch_test_connection(monkeypatch, ok: bool, message: str) -> list:
    """Replace llm_mod.test_connection so the verb doesn't actually hit
    the network. Returns the call list so tests can assert what was
    invoked with."""
    calls: list[LLMConfig] = []

    def _fake(cfg: LLMConfig, *, prompt: str = "ok") -> ConnectionResult:
        calls.append(cfg)
        return ConnectionResult(ok=ok, config=cfg, message=message)

    monkeypatch.setattr(llm_mod, "test_connection", _fake)
    return calls


def test_config_test_success(monkeypatch):
    monkeypatch.setattr(
        llm_mod.os, "environ",
        {"ANTHROPIC_API_KEY": "sk-ant-test"},
    )
    calls = _patch_test_connection(monkeypatch, ok=True, message="got: 'ok'")
    result = runner.invoke(app, ["config", "test"])
    assert result.exit_code == 0, result.output
    assert "✓" in result.output
    assert len(calls) == 1
    assert calls[0].provider == Provider.ANTHROPIC


def test_config_test_failure_exits_1(monkeypatch):
    monkeypatch.setattr(
        llm_mod.os, "environ",
        {"ANTHROPIC_API_KEY": "sk-ant-test"},
    )
    _patch_test_connection(monkeypatch, ok=False, message="401 invalid_api_key")
    result = runner.invoke(app, ["config", "test"])
    assert result.exit_code == 1
    assert "failed" in result.output.lower()
    assert "ANTHROPIC_API_KEY" in result.output  # fix hint


def test_config_test_no_provider_exits_1(monkeypatch):
    monkeypatch.setattr(llm_mod.os, "environ", {})
    result = runner.invoke(app, ["config", "test"])
    assert result.exit_code == 1
    assert "no llm provider" in result.output.lower()


def test_config_test_with_specific_mode(monkeypatch):
    monkeypatch.setattr(
        llm_mod.os, "environ",
        {"ANTHROPIC_API_KEY": "sk-ant-test"},
    )
    calls = _patch_test_connection(monkeypatch, ok=True, message="got: 'ok'")
    result = runner.invoke(app, ["config", "test", "--mode", "quality"])
    assert result.exit_code == 0
    assert calls[0].model.endswith("opus-4-7")


def test_config_test_with_provider_unavailable(monkeypatch):
    """Asking to test 'openai:gpt-5' when only ANTHROPIC_API_KEY is set
    must fail clearly without invoking the LLM."""
    monkeypatch.setattr(
        llm_mod.os, "environ",
        {"ANTHROPIC_API_KEY": "sk-ant-test"},
    )
    calls = _patch_test_connection(monkeypatch, ok=True, message="should not be called")
    result = runner.invoke(app, ["config", "test", "--mode", "openai:gpt-5"])
    assert result.exit_code == 1
    assert "OPENAI_API_KEY" in result.output
    # Must not have hit the network
    assert calls == []
