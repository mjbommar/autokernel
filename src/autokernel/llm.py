"""LLM configuration: provider auto-detection, mode presets, model resolution.

Today's UX problem: the ``propose`` verb ships with
``AUTOKERNEL_MODEL=anthropic:claude-sonnet-4-6`` baked in. A user who only
has ``OPENAI_API_KEY`` set runs ``propose`` and gets an
authentication error from a model they aren't trying to use. This module
fixes that by:

1. **Detecting** which providers the user actually has credentials for
   (env-var probe; pure read-only).
2. **Resolving** a *spec* (``'auto'``, ``'cheap'``, ``'quality'``, ``'fast'``,
   or a literal pydantic-ai model id) to a concrete model string for an
   available provider.
3. **Reporting** the resolved configuration so ``autokernel config show``
   can render it to the user.

The module is **pure**: no pydantic-ai imports, no network calls. The
``test_connection`` helper that ``autokernel config test`` uses lives
here too but is opt-in (only invoked when the user explicitly runs that
verb).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


# ── providers + models ─────────────────────────────────────────────────────


class Provider(str, Enum):
    """Cloud LLM provider, named to match pydantic-ai's prefix string.

    The enum value is exactly what goes before the colon in a pydantic-ai
    model id (``anthropic:claude-...``). Lets us round-trip a model id
    through ``Provider(model.split(':', 1)[0])`` without an extra map.
    """

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google-gla"      # gemini API key (the older "google generative ai" one)
    GROQ = "groq"
    MISTRAL = "mistral"
    DEEPSEEK = "deepseek"
    XAI = "xai"
    OPENROUTER = "openrouter"


class LLMMode(str, Enum):
    """Mode preset that abstracts away exact model ids.

    The user picks a mode (``'auto'``, ``'cheap'``, ``'quality'``,
    ``'fast'``); we resolve to the right concrete model for whatever
    provider they have credentials for.

    AUTO == FAST today — the cheapest "good enough" option from the most
    preferred available provider. CHEAP is the actual cheapest tier
    (haiku / gpt-5-mini / gemini-flash). QUALITY is the slowest /
    most-capable tier.
    """

    AUTO = "auto"
    CHEAP = "cheap"
    FAST = "fast"
    QUALITY = "quality"


# Per-provider env vars. The first key in the tuple is the canonical one
# we'll mention in error messages.
_API_KEY_VARS: dict[Provider, tuple[str, ...]] = {
    Provider.ANTHROPIC: ("ANTHROPIC_API_KEY",),
    Provider.OPENAI: ("OPENAI_API_KEY",),
    Provider.GOOGLE: ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    Provider.GROQ: ("GROQ_API_KEY",),
    Provider.MISTRAL: ("MISTRAL_API_KEY",),
    Provider.DEEPSEEK: ("DEEPSEEK_API_KEY",),
    Provider.XAI: ("XAI_API_KEY",),
    Provider.OPENROUTER: ("OPENROUTER_API_KEY",),
}


# Per-provider per-mode default model id (the suffix after the colon).
# These are best-effort defaults — power users override via --model with
# a literal pydantic-ai id, or via AUTOKERNEL_MODEL.
_DEFAULTS: dict[Provider, dict[LLMMode, str]] = {
    Provider.ANTHROPIC: {
        LLMMode.AUTO: "claude-sonnet-4-6",
        LLMMode.FAST: "claude-sonnet-4-6",
        LLMMode.CHEAP: "claude-haiku-4-5",
        LLMMode.QUALITY: "claude-opus-4-7",
    },
    Provider.OPENAI: {
        LLMMode.AUTO: "gpt-5",
        LLMMode.FAST: "gpt-5",
        LLMMode.CHEAP: "gpt-5-mini",
        LLMMode.QUALITY: "gpt-5",
    },
    Provider.GOOGLE: {
        LLMMode.AUTO: "gemini-2.0-flash",
        LLMMode.FAST: "gemini-2.0-flash",
        LLMMode.CHEAP: "gemini-2.0-flash-lite",
        LLMMode.QUALITY: "gemini-2.5-pro",
    },
    Provider.GROQ: {
        LLMMode.AUTO: "llama-3.3-70b-versatile",
        LLMMode.FAST: "llama-3.3-70b-versatile",
        LLMMode.CHEAP: "llama-3.1-8b-instant",
        LLMMode.QUALITY: "llama-3.3-70b-versatile",
    },
    Provider.MISTRAL: {
        LLMMode.AUTO: "mistral-large-latest",
        LLMMode.FAST: "mistral-medium-latest",
        LLMMode.CHEAP: "mistral-small-latest",
        LLMMode.QUALITY: "mistral-large-latest",
    },
    Provider.DEEPSEEK: {
        LLMMode.AUTO: "deepseek-chat",
        LLMMode.FAST: "deepseek-chat",
        LLMMode.CHEAP: "deepseek-chat",
        LLMMode.QUALITY: "deepseek-reasoner",
    },
    Provider.XAI: {
        LLMMode.AUTO: "grok-2-latest",
        LLMMode.FAST: "grok-2-latest",
        LLMMode.CHEAP: "grok-2-mini",
        LLMMode.QUALITY: "grok-2-latest",
    },
    Provider.OPENROUTER: {
        LLMMode.AUTO: "anthropic/claude-sonnet-4",
        LLMMode.FAST: "anthropic/claude-sonnet-4",
        LLMMode.CHEAP: "openai/gpt-4o-mini",
        LLMMode.QUALITY: "anthropic/claude-opus-4",
    },
}


# Preference order when multiple providers are available. The user can
# override per-call with --model; this is just the default picker.
_PROVIDER_PREFERENCE: tuple[Provider, ...] = (
    Provider.ANTHROPIC,
    Provider.OPENAI,
    Provider.GOOGLE,
    Provider.MISTRAL,
    Provider.GROQ,
    Provider.XAI,
    Provider.DEEPSEEK,
    Provider.OPENROUTER,
)


# ── exceptions ─────────────────────────────────────────────────────────────


class NoProviderConfigured(Exception):
    """No provider has credentials in env vars and no literal model id was
    given. The CLI surfaces this with a "set ANTHROPIC_API_KEY or ..." hint.
    """


class ProviderNotAvailable(Exception):
    """User asked for a literal model from a specific provider, but that
    provider's API key isn't in env. Tells the user which env var to set."""

    def __init__(self, provider: Provider, env_vars: tuple[str, ...]):
        self.provider = provider
        self.env_vars = env_vars
        super().__init__(
            f"model targets provider {provider.value!r} but none of "
            f"{list(env_vars)} are set in the environment"
        )


# ── detection ──────────────────────────────────────────────────────────────


def detect_available_providers(
    *, env: dict[str, str] | None = None
) -> list[Provider]:
    """Return providers that have credentials in ``env`` (defaults to
    :data:`os.environ`), ordered by :data:`_PROVIDER_PREFERENCE`.

    A provider is "available" iff at least one of its known env vars is
    present and non-empty. Empty-string values are treated as absent so
    a placeholder ``ANTHROPIC_API_KEY=`` in ``.env`` doesn't claim the
    provider is configured.
    """
    e = env if env is not None else os.environ
    out: list[Provider] = []
    for provider in _PROVIDER_PREFERENCE:
        for var in _API_KEY_VARS[provider]:
            value = e.get(var, "")
            if value and value.strip():
                out.append(provider)
                break
    return out


# ── config / resolution ────────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMConfig:
    """Resolved LLM configuration for one ``propose`` run."""

    provider: Provider
    model: str
    """Pydantic-ai model id, e.g. ``'anthropic:claude-sonnet-4-6'``."""

    service_tier: str | None = None
    batch_size: int = 60
    mode: LLMMode | None = None
    """The mode preset that resolved to this config (when applicable)."""

    api_key_var: str = ""
    """Which env var actually holds the credentials, for ``config show``."""

    @property
    def model_id(self) -> str:
        """The full pydantic-ai model id, including provider prefix."""
        return self.model

    @property
    def model_name(self) -> str:
        """Just the suffix after the colon (e.g. ``'claude-sonnet-4-6'``)."""
        return self.model.split(":", 1)[1] if ":" in self.model else self.model


def resolve(
    *,
    spec: str = "auto",
    service_tier: str | None = None,
    batch_size: int = 60,
    available: list[Provider] | None = None,
    env: dict[str, str] | None = None,
) -> LLMConfig:
    """Resolve ``spec`` to a concrete :class:`LLMConfig`.

    ``spec`` can be:

    * ``'auto'`` — best provider available, that provider's AUTO model.
    * ``'cheap'`` / ``'fast'`` / ``'quality'`` — preset for the best
      available provider.
    * ``'<provider>:<model>'`` — literal pydantic-ai model id. The
      provider is validated against env-var presence; raises
      :class:`ProviderNotAvailable` if its key isn't set.

    Raises :class:`NoProviderConfigured` when no provider is detected
    and the spec was a preset (rather than a literal).
    """
    e = env if env is not None else os.environ
    avail = available if available is not None else detect_available_providers(env=e)

    # Literal model id → validate the provider has credentials.
    if ":" in spec and spec.lower() not in {m.value for m in LLMMode}:
        provider_str, _, model_name = spec.partition(":")
        try:
            provider = Provider(provider_str)
        except ValueError as exc:
            raise NoProviderConfigured(
                f"unknown provider prefix in {spec!r}; "
                f"expected one of {[p.value for p in Provider]}"
            ) from exc
        if provider not in avail:
            raise ProviderNotAvailable(provider, _API_KEY_VARS[provider])
        return LLMConfig(
            provider=provider,
            model=spec,
            service_tier=service_tier,
            batch_size=batch_size,
            api_key_var=_first_set_env(provider, e),
        )

    # Preset
    try:
        mode = LLMMode(spec.lower())
    except ValueError as exc:
        raise NoProviderConfigured(
            f"unknown spec {spec!r}; expected a literal model id "
            f"(provider:model) or one of {[m.value for m in LLMMode]}"
        ) from exc

    if not avail:
        raise NoProviderConfigured(
            "no provider env vars detected; set one of: "
            + ", ".join(
                f"{var}=..." for vars_ in _API_KEY_VARS.values() for var in vars_
            )
        )

    provider = avail[0]
    model_suffix = _DEFAULTS[provider][mode]
    return LLMConfig(
        provider=provider,
        model=f"{provider.value}:{model_suffix}",
        service_tier=service_tier,
        batch_size=batch_size,
        mode=mode,
        api_key_var=_first_set_env(provider, e),
    )


def _first_set_env(provider: Provider, env: dict[str, str]) -> str:
    for var in _API_KEY_VARS[provider]:
        if env.get(var, "").strip():
            return var
    return ""


# ── reporting (used by config show) ────────────────────────────────────────


@dataclass(frozen=True)
class ProviderStatus:
    provider: Provider
    available: bool
    api_key_var: str  # the env var that's set, or "" if none
    env_vars: tuple[str, ...]
    """All env vars autokernel will probe for this provider."""


def status_report(*, env: dict[str, str] | None = None) -> list[ProviderStatus]:
    """Per-provider availability snapshot for ``config show`` rendering."""
    e = env if env is not None else os.environ
    out: list[ProviderStatus] = []
    for provider in _PROVIDER_PREFERENCE:
        env_vars = _API_KEY_VARS[provider]
        which = _first_set_env(provider, e)
        out.append(ProviderStatus(
            provider=provider,
            available=bool(which),
            api_key_var=which,
            env_vars=env_vars,
        ))
    return out


def model_options_for(provider: Provider) -> dict[LLMMode, str]:
    """Return the per-mode default model suffixes for ``provider``."""
    return dict(_DEFAULTS[provider])


# ── connection test (opt-in; the only function that calls pydantic-ai) ────


@dataclass(frozen=True)
class ConnectionResult:
    """Outcome of a :func:`test_connection` ping.

    Named to avoid the pytest test-collection name-mangling that fires
    for any class starting with ``Test``.
    """

    ok: bool
    config: LLMConfig
    message: str


def test_connection(config: LLMConfig, *, prompt: str = "ok") -> ConnectionResult:
    """Send a tiny prompt to the configured model. Used by
    ``autokernel config test`` to verify credentials before the user
    pays for a real propose run.

    The cost is on the order of $0.001 for any reasonable model.
    """
    try:
        from pydantic_ai import Agent
        from pydantic_ai.settings import ModelSettings

        settings = ModelSettings()
        if config.service_tier:
            settings = ModelSettings(service_tier=config.service_tier)

        agent = Agent(
            config.model,
            system_prompt="You are a configuration-test bot. Reply with the exact word 'ok' and nothing else.",
            model_settings=settings,
        )
        result = agent.run_sync(prompt)
        text = (str(result.output) if result.output is not None else "").strip()
        return ConnectionResult(ok=True, config=config, message=f"got: {text[:40]!r}")
    except Exception as exc:  # noqa: BLE001 — surface any provider error
        return ConnectionResult(
            ok=False,
            config=config,
            message=f"{type(exc).__name__}: {exc}",
        )
