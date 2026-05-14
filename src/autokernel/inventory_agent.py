"""pydantic-ai enrichment agent for Kconfig inventory records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from autokernel.inventory import (
    InventoryTools,
    KconfigSymbolRecord,
)
from autokernel.llm import ServiceTier, normalize_service_tier


DEFAULT_INVENTORY_MODEL = "openai:gpt-5.4-mini"
DEFAULT_INVENTORY_SERVICE_TIER = "flex"
PROMPT_VERSION = "v1"


class EvidenceRef(BaseModel):
    kind: Literal["kconfig", "source", "kbuild", "module", "hardware"]
    path: str | None = None
    line: int | None = None
    detail: str


class InventoryEnrichment(BaseModel):
    symbol: str
    fact_hash: str
    summary: str
    functionality: str
    supported_hardware: str | None = None
    built_artifacts: list[str] = Field(default_factory=list)
    disable_effect: str
    keep_when: list[str] = Field(default_factory=list)
    safe_to_disable_when: list[str] = Field(default_factory=list)
    common_misconfigurations: list[str] = Field(default_factory=list)
    proposal_guidance: Literal[
        "keep_bias",
        "disable_if_absent",
        "workload_choice",
        "never_auto",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    generated_at: str = ""


class InventoryEnrichmentBatch(BaseModel):
    enrichments: list[InventoryEnrichment] = Field(default_factory=list)


SYSTEM_PROMPT = """\
You enrich Linux kernel Kconfig inventory records for a kernel optimizer.

Rules:

1. You are summarizing evidence, not inventing kernel facts.
2. Facts about modules, source paths, hardware buses, firmware, Kbuild
   objects, dependencies, and source usage must come from tools.
3. Prefer uncertainty when evidence is thin.
4. Explain practical effects: what functionality is enabled, what breaks
   when disabled, and when it is probably safe to disable.
5. Every enrichment must cite evidence_refs. Use kconfig refs for prompt/help,
   kbuild refs for built artifacts, and source refs for implementation claims.
6. Do not recommend disabling boot, storage, crypto, console, or network
   symbols aggressively. Use proposal_guidance="never_auto" or "keep_bias"
   when false positives can break boot or remote access.
"""


def build_agent(
    *,
    model: str = DEFAULT_INVENTORY_MODEL,
    service_tier: ServiceTier | str | None = DEFAULT_INVENTORY_SERVICE_TIER,
) -> Agent[InventoryTools, InventoryEnrichmentBatch]:
    """Build an enrichment agent with read-only inventory/source tools."""
    from pydantic_ai.settings import ModelSettings

    tier = normalize_service_tier(cast(str | None, service_tier))
    settings = ModelSettings(service_tier=tier) if tier else ModelSettings()
    agent = cast(
        Agent[InventoryTools, InventoryEnrichmentBatch],
        Agent(
            model,
            deps_type=InventoryTools,
            output_type=InventoryEnrichmentBatch,
            system_prompt=SYSTEM_PROMPT,
            model_settings=settings,
        ),
    )

    @agent.tool
    def list_symbols(
        ctx: RunContext[InventoryTools],
        kind: str | None = None,
        prefix: str | None = None,
        subsystem: str | None = None,
        limit: int = 100,
    ) -> list[str] | dict:
        try:
            return ctx.deps.list_symbols(
                kind=kind, prefix=prefix, subsystem=subsystem, limit=limit
            )
        except Exception as e:
            return _tool_error(e)

    @agent.tool
    def search_symbols(
        ctx: RunContext[InventoryTools], query: str, limit: int = 50
    ) -> list[str] | dict:
        try:
            return ctx.deps.search_symbols(query, limit=limit)
        except Exception as e:
            return _tool_error(e)

    @agent.tool
    def search_kconfig_text(
        ctx: RunContext[InventoryTools], query: str, limit: int = 50
    ) -> list[str] | dict:
        try:
            return ctx.deps.search_kconfig_text(query, limit=limit)
        except Exception as e:
            return _tool_error(e)

    @agent.tool
    def get_symbol(ctx: RunContext[InventoryTools], symbol: str) -> dict:
        try:
            return ctx.deps.get_symbol(symbol).model_dump(mode="json")
        except Exception as e:
            return _tool_error(e)

    @agent.tool
    def get_symbol_relations(ctx: RunContext[InventoryTools], symbol: str) -> dict:
        try:
            return ctx.deps.get_symbol_relations(symbol)
        except Exception as e:
            return _tool_error(e)

    @agent.tool
    def search_config_usages(
        ctx: RunContext[InventoryTools], symbol: str, limit: int = 100
    ) -> list[dict] | dict:
        try:
            return [
                u.model_dump(mode="json")
                for u in ctx.deps.search_config_usages(symbol, limit=limit)
            ]
        except Exception as e:
            return _tool_error(e)

    @agent.tool
    def search_kbuild_usages(
        ctx: RunContext[InventoryTools], symbol: str, limit: int = 100
    ) -> list[dict] | dict:
        try:
            return [
                u.model_dump(mode="json")
                for u in ctx.deps.search_kbuild_usages(symbol, limit=limit)
            ]
        except Exception as e:
            return _tool_error(e)

    @agent.tool
    def list_files(
        ctx: RunContext[InventoryTools],
        path_prefix: str = "",
        globs: list[str] | None = None,
        limit: int = 100,
    ) -> list[str] | dict:
        try:
            return ctx.deps.list_files(path_prefix, globs=globs, limit=limit)
        except Exception as e:
            return _tool_error(e)

    @agent.tool
    def read_file_head(
        ctx: RunContext[InventoryTools], path: str, max_lines: int = 80
    ) -> dict:
        try:
            return ctx.deps.read_file_head(path, max_lines=max_lines).model_dump(
                mode="json"
            )
        except Exception as e:
            return _tool_error(e)

    @agent.tool
    def read_file_excerpt(
        ctx: RunContext[InventoryTools],
        path: str,
        start_line: int,
        end_line: int,
    ) -> dict:
        try:
            return ctx.deps.read_file_excerpt(
                path, start_line=start_line, end_line=end_line
            ).model_dump(mode="json")
        except Exception as e:
            return _tool_error(e)

    @agent.tool
    def read_file_around_match(
        ctx: RunContext[InventoryTools],
        path: str,
        line: int,
        context: int = 40,
    ) -> dict:
        try:
            return ctx.deps.read_file_around_match(
                path, line=line, context=context
            ).model_dump(mode="json")
        except Exception as e:
            return _tool_error(e)

    return agent


def _tool_error(error: Exception) -> dict[str, str]:
    return {"error": f"{type(error).__name__}: {error}"}


def _model_label(model: object) -> str:
    if isinstance(model, str):
        return model
    return str(getattr(model, "model_name", type(model).__name__))


def enrich_records(
    records: list[KconfigSymbolRecord],
    tools: InventoryTools,
    *,
    model: str = DEFAULT_INVENTORY_MODEL,
    service_tier: ServiceTier | str | None = DEFAULT_INVENTORY_SERVICE_TIER,
    max_attempts: int = 3,
) -> list[InventoryEnrichment]:
    """Ask the enrichment agent for a batch of records and validate output."""
    if not records:
        return []
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    records_by_symbol = {r.symbol: r for r in records}
    if len(records_by_symbol) != len(records):
        raise ValueError("duplicate symbols in enrichment batch")

    agent = build_agent(model=model, service_tier=service_tier)
    model_label = _model_label(model)
    accepted: dict[str, InventoryEnrichment] = {}
    validation_errors: list[str] = []
    pending = list(records)

    for attempt in range(1, max_attempts + 1):
        prompt = _batch_prompt(pending, attempt=attempt)
        result = agent.run_sync(prompt, deps=tools)
        for enrichment in result.output.enrichments:
            rec = records_by_symbol.get(enrichment.symbol)
            if rec is None or enrichment.symbol in accepted:
                continue
            try:
                validate_enrichment(enrichment, rec, tools)
            except ValueError as e:
                validation_errors.append(f"{enrichment.symbol}: {e}")
                continue
            accepted[enrichment.symbol] = enrichment.model_copy(
                update={
                    "model": model_label,
                    "prompt_version": PROMPT_VERSION,
                    "generated_at": enrichment.generated_at
                    or datetime.now(UTC).isoformat(),
                }
            )

        pending = [r for r in records if r.symbol not in accepted]
        if not pending:
            return [accepted[r.symbol] for r in records]

    missing = ", ".join(r.symbol for r in pending[:10])
    suffix = "..." if len(pending) > 10 else ""
    details = f"missing enrichments after {max_attempts} attempt(s): {missing}{suffix}"
    if validation_errors:
        details += f"; validation errors: {'; '.join(validation_errors[-5:])}"
    raise ValueError(details)


def validate_enrichment(
    enrichment: InventoryEnrichment,
    record: KconfigSymbolRecord,
    tools: InventoryTools,
) -> None:
    if enrichment.symbol != record.symbol:
        raise ValueError(f"enrichment symbol mismatch: {enrichment.symbol}")
    if enrichment.fact_hash != record.fact_hash:
        raise ValueError(f"fact_hash mismatch for {record.symbol}")
    if not enrichment.evidence_refs:
        raise ValueError(f"{record.symbol} enrichment has no evidence refs")
    for ref in enrichment.evidence_refs:
        if ref.path:
            path = (tools.source_dir / ref.path).resolve()
            if not path.is_relative_to(tools.source_dir) or not path.exists():
                raise ValueError(f"{record.symbol} cites invalid path {ref.path}")


def offline_enrichment(
    record: KconfigSymbolRecord,
    *,
    model: str = "offline",
) -> InventoryEnrichment:
    """Deterministic baseline enrichment for tests and bootstrapping.

    This is not a replacement for the LLM pass. It gives the inventory a
    valid, evidence-cited baseline for symbols that have not been LLM
    enriched yet.
    """
    refs: list[EvidenceRef] = []
    for loc in record.locations[:2]:
        refs.append(
            EvidenceRef(kind="kconfig", path=loc.path, line=loc.line, detail="definition")
        )
    for binding in record.kbuild[:2]:
        refs.append(
            EvidenceRef(
                kind="kbuild",
                path=binding.path,
                line=binding.line,
                detail="Kbuild binding",
            )
        )
    for usage in record.source_usages[:2]:
        refs.append(
            EvidenceRef(
                kind="source",
                path=usage.path,
                line=usage.line,
                detail="CONFIG usage",
            )
        )
    if not refs:
        refs.append(EvidenceRef(kind="kconfig", detail="symbol record"))
    guidance: Literal["keep_bias", "disable_if_absent", "workload_choice", "never_auto"]
    if any(tag in record.risk_tags for tag in ("boot_path", "storage", "security")):
        guidance = "keep_bias"
    elif record.type.value == "tristate" or record.modules:
        guidance = "disable_if_absent"
    else:
        guidance = "workload_choice"
    return InventoryEnrichment(
        symbol=record.symbol,
        fact_hash=record.fact_hash,
        summary=record.prompt or record.symbol,
        functionality=record.help or record.prompt or "No Kconfig help text available.",
        supported_hardware=(
            ", ".join(record.hardware.buses) if record.hardware.buses else None
        ),
        built_artifacts=record.modules,
        disable_effect=f"Disabling {record.symbol} removes the configured functionality and any bound build objects.",
        keep_when=["Keep when the host uses the listed modules, devices, or dependent functionality."],
        safe_to_disable_when=["Consider disabling only when no source, module, or hardware evidence is relevant to the host."],
        common_misconfigurations=[],
        proposal_guidance=guidance,
        confidence=0.5,
        evidence_refs=refs,
        model=model,
        prompt_version=PROMPT_VERSION,
        generated_at=datetime.now(UTC).isoformat(),
    )


def write_enrichments(enrichments: list[InventoryEnrichment], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for enrichment in enrichments:
            f.write(enrichment.model_dump_json(exclude_none=True))
            f.write("\n")


def _batch_prompt(records: list[KconfigSymbolRecord], *, attempt: int = 1) -> str:
    payload = [
        {
            "symbol": r.symbol,
            "fact_hash": r.fact_hash,
            "type": r.type.value,
            "prompt": r.prompt,
            "help": r.help,
            "locations": [loc.model_dump() for loc in r.locations[:3]],
            "depends_symbols": r.depends_symbols,
            "selects": r.selects,
            "selected_by": r.selected_by,
            "modules": r.modules,
            "hardware": r.hardware.model_dump(mode="json"),
            "subsystem_tags": r.subsystem_tags,
            "risk_tags": r.risk_tags,
            "top_paths": r.source_refs.top_paths,
        }
        for r in records
    ]
    required_symbols = [r.symbol for r in records]
    return (
        f"Enrich exactly these {len(records)} Kconfig symbols: "
        f"{', '.join(required_symbols)}.\n"
        "Return exactly one enrichment for every required symbol and do not "
        "include symbols outside that list. If this is a retry, only enrich "
        "the symbols in this retry batch.\n"
        f"Attempt: {attempt}.\n\n"
        "Use tools to inspect "
        "source/Kbuild evidence before making implementation or hardware claims. "
        "Return one enrichment per input symbol.\n\n"
        + json.dumps(payload, indent=2)
    )
