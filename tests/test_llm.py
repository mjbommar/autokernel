"""Tests for the LLM config / auto-detection module.

These are pure-logic tests. ``test_connection`` (which calls pydantic-ai)
is exercised by the config-CLI tests with the agent mocked.
"""

from __future__ import annotations

import pytest

from autokernel.llm import (
    LLMConfig,
    LLMMode,
    NoProviderConfigured,
    Provider,
    ProviderNotAvailable,
    detect_available_providers,
    model_options_for,
    resolve,
    status_report,
)


# ── detect_available_providers ─────────────────────────────────────────────


def test_detect_no_providers_returns_empty():
    assert detect_available_providers(env={}) == []


def test_detect_anthropic_only():
    env = {"ANTHROPIC_API_KEY": "sk-ant-x"}
    assert detect_available_providers(env=env) == [Provider.ANTHROPIC]


def test_detect_openai_only():
    env = {"OPENAI_API_KEY": "sk-x"}
    assert detect_available_providers(env=env) == [Provider.OPENAI]


def test_detect_multiple_providers_orders_by_preference():
    env = {
        "OPENAI_API_KEY": "sk-x",
        "ANTHROPIC_API_KEY": "sk-ant-x",
        "GOOGLE_API_KEY": "g-x",
    }
    # Anthropic first, then OpenAI, then Google per _PROVIDER_PREFERENCE
    avail = detect_available_providers(env=env)
    assert avail == [Provider.ANTHROPIC, Provider.OPENAI, Provider.GOOGLE]


def test_detect_empty_string_treated_as_absent():
    """A placeholder ANTHROPIC_API_KEY= in .env shouldn't claim the
    provider is configured."""
    env = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "sk-x"}
    avail = detect_available_providers(env=env)
    assert Provider.ANTHROPIC not in avail
    assert Provider.OPENAI in avail


def test_detect_whitespace_only_treated_as_absent():
    env = {"ANTHROPIC_API_KEY": "   "}
    assert Provider.ANTHROPIC not in detect_available_providers(env=env)


def test_detect_gemini_api_key_alias():
    """Google supports both GOOGLE_API_KEY and GEMINI_API_KEY."""
    env = {"GEMINI_API_KEY": "g-x"}
    assert Provider.GOOGLE in detect_available_providers(env=env)


# ── resolve(): preset modes ────────────────────────────────────────────────


def test_resolve_auto_picks_first_available():
    env = {"ANTHROPIC_API_KEY": "sk-ant"}
    cfg = resolve(spec="auto", env=env)
    assert cfg.provider == Provider.ANTHROPIC
    assert cfg.model.startswith("anthropic:claude-")
    assert cfg.mode == LLMMode.AUTO


def test_resolve_cheap_picks_cheap_model():
    env = {"ANTHROPIC_API_KEY": "sk-ant"}
    cfg = resolve(spec="cheap", env=env)
    assert "haiku" in cfg.model.lower()


def test_resolve_quality_picks_quality_model():
    env = {"ANTHROPIC_API_KEY": "sk-ant"}
    cfg = resolve(spec="quality", env=env)
    assert "opus" in cfg.model.lower()


def test_resolve_fast_for_openai_only_user():
    env = {"OPENAI_API_KEY": "sk"}
    cfg = resolve(spec="fast", env=env)
    assert cfg.provider == Provider.OPENAI
    assert cfg.model.startswith("openai:gpt-")


def test_resolve_no_provider_raises():
    with pytest.raises(NoProviderConfigured) as e:
        resolve(spec="auto", env={})
    msg = str(e.value)
    # The error must name at least the most common env vars so users
    # know what to set.
    assert "ANTHROPIC_API_KEY" in msg
    assert "OPENAI_API_KEY" in msg


def test_resolve_unknown_preset_raises():
    with pytest.raises(NoProviderConfigured):
        resolve(spec="bogus", env={"ANTHROPIC_API_KEY": "x"})


# ── resolve(): literal model id ────────────────────────────────────────────


def test_resolve_literal_anthropic_model_when_key_present():
    cfg = resolve(
        spec="anthropic:claude-opus-4-7",
        env={"ANTHROPIC_API_KEY": "sk-ant"},
    )
    assert cfg.model == "anthropic:claude-opus-4-7"
    assert cfg.provider == Provider.ANTHROPIC
    assert cfg.mode is None  # literal model id, not a preset


def test_resolve_literal_openai_when_only_anthropic_set_raises():
    """Asking for an OpenAI model when only Anthropic is configured must
    fail with a specific ProviderNotAvailable that names the right env var."""
    with pytest.raises(ProviderNotAvailable) as e:
        resolve(
            spec="openai:gpt-5",
            env={"ANTHROPIC_API_KEY": "sk-ant"},
        )
    assert e.value.provider == Provider.OPENAI
    assert "OPENAI_API_KEY" in str(e.value)


def test_resolve_literal_unknown_provider_raises():
    with pytest.raises(NoProviderConfigured):
        resolve(spec="madeup:my-model", env={"OPENAI_API_KEY": "x"})


def test_resolve_records_api_key_var():
    cfg = resolve(spec="auto", env={"ANTHROPIC_API_KEY": "sk-ant"})
    assert cfg.api_key_var == "ANTHROPIC_API_KEY"


def test_resolve_records_gemini_api_key_var_when_thats_what_user_set():
    cfg = resolve(spec="auto", env={"GEMINI_API_KEY": "g-x"})
    assert cfg.api_key_var == "GOOGLE_API_KEY" or cfg.api_key_var == "GEMINI_API_KEY"
    # Either is acceptable — what matters is that it points at a real var
    # the user can reference.
    assert cfg.provider == Provider.GOOGLE


def test_resolve_passes_through_service_tier_and_batch_size():
    cfg = resolve(
        spec="auto",
        service_tier="flex",
        batch_size=120,
        env={"OPENAI_API_KEY": "sk"},
    )
    assert cfg.service_tier == "flex"
    assert cfg.batch_size == 120


# ── status_report ──────────────────────────────────────────────────────────


def test_status_report_lists_every_provider():
    """The report lists every supported provider so the user sees what
    they could opt into, not just what's currently set."""
    rep = status_report(env={})
    assert {s.provider for s in rep} == set(Provider)
    for s in rep:
        assert s.available is False
        assert s.api_key_var == ""


def test_status_report_marks_set_providers_available():
    rep = status_report(env={"ANTHROPIC_API_KEY": "x", "OPENAI_API_KEY": "y"})
    by_prov = {s.provider: s for s in rep}
    assert by_prov[Provider.ANTHROPIC].available is True
    assert by_prov[Provider.ANTHROPIC].api_key_var == "ANTHROPIC_API_KEY"
    assert by_prov[Provider.OPENAI].available is True
    assert by_prov[Provider.GOOGLE].available is False


# ── model_options_for ──────────────────────────────────────────────────────


def test_model_options_for_anthropic_includes_all_modes():
    opts = model_options_for(Provider.ANTHROPIC)
    assert set(opts.keys()) == set(LLMMode)
    assert "haiku" in opts[LLMMode.CHEAP].lower()
    assert "opus" in opts[LLMMode.QUALITY].lower()


# ── LLMConfig accessors ────────────────────────────────────────────────────


def test_llmconfig_model_name_strips_provider_prefix():
    cfg = LLMConfig(provider=Provider.ANTHROPIC, model="anthropic:claude-x")
    assert cfg.model_name == "claude-x"


def test_llmconfig_model_name_no_colon_returns_full_string():
    cfg = LLMConfig(provider=Provider.ANTHROPIC, model="bare-id")
    assert cfg.model_name == "bare-id"
