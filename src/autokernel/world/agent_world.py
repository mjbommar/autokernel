"""Package dimension agents — the chug (docs/WORLD.md W3).

Batched LLM passes over *every* source unit in the world, one
dimension at a time, modeled on ``autokernel.agent_dims``:

* necessity — keep / trim, given the ring and workload axes;
* flags — proactive per-package hazards of the global flag set
  (known LTO-hostile build systems, -O3-fragile code), seeding the
  exceptions table before builds fail instead of after;
* features — build profiles / configure toggles likely supported
  (speculative until W6 reads debian/rules; flagged as such);
* risk — blast radius (boot-critical / service-critical / leaf) that
  drives review routing and test depth.

All advisory. The policy layer forces LOAD_BEARING_SOURCES to 'keep'
in the necessity dimension regardless of model confidence, and the
aggression axis sets the confidence floor downstream. Every batch is
content-addressed under ``<world_dir>/batches/world-<dimension>/`` —
re-running after a manifest edit pays only for changed batches.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from autokernel.world.models import (
    LOAD_BEARING_SOURCES,
    Dimension,
    PackageDecision,
    WorldManifest,
)

DEFAULT_MODEL = os.environ.get("AUTOKERNEL_MODEL", "anthropic:claude-sonnet-4-6")
BATCH_SIZE = int(os.environ.get("AUTOKERNEL_WORLD_BATCH_SIZE", "40"))
SYSTEM_PROMPT_VERSION = "v1"


class _DecisionDraft(BaseModel):
    source: str = Field(description="Exact source package name from the batch")
    decision: str
    reason: str = Field(description="One sentence tied to the evidence")
    confidence: float = Field(ge=0.0, le=1.0)
    strip: list[str] = Field(
        default_factory=list, description="flags dim: tokens to strip"
    )
    add: list[str] = Field(default_factory=list, description="flags dim: tokens to add")
    profiles: list[str] = Field(
        default_factory=list, description="features dim: candidate build profiles"
    )


class _DecisionBatch(BaseModel):
    decisions: list[_DecisionDraft] = Field(default_factory=list)


_COMMON = """\
You are reviewing Debian source packages for a per-host from-source
rebuild (autokernel world). Evidence: the host's axes and the package
list with sizes. Return a decision for EVERY package in the batch.
"""

_PROMPTS: dict[Dimension, str] = {
    Dimension.NECESSITY: _COMMON
    + """
Dimension: NECESSITY — can this package leave the world set?
decision ∈ {keep, trim}.
- keep: required for boot, init, packaging, login, or anything the
  workload axis implies. When unsure, keep (a wrong trim breaks the
  image; a wrong keep wastes megabytes).
- trim: clearly not needed for this ring/workload (e.g. docs-only or
  locale packages on a server ring, alternate implementations where
  the world already has the default one).
Confidence 0.9+ only for obviously redundant packages.
""",
    Dimension.FLAGS: _COMMON
    + """
Dimension: FLAGS — will the global appended flags hurt this package?
decision ∈ {default, override}.
- default: the global flags are fine.
- override: this package is known to misbehave under one of the
  appended flags. Fill `strip` with the exact offending tokens from
  the shown flag set, and `add` with safe replacements if needed
  (-O3→-O2). Known patterns: interpreters/JITs and crypto cores are
  -O3/march fragile; packages doing their own symbol versioning or
  custom linker scripts break under LTO; bootloaders and anything
  freestanding must not inherit appended flags at all.
Only propose overrides you can justify from known package behavior —
the builder validates with a real build, but wrong guesses waste a
build each. confidence < 0.5 means: just say default.
""",
    Dimension.FEATURES: _COMMON
    + """
Dimension: FEATURES — slimming via build profiles.
decision ∈ {none, profiles}.
- profiles: this package plausibly supports Debian build profiles
  worth enabling for a minimal host (fill `profiles`, e.g. noinsttest,
  nodoc, pkg.<src>.minimal where such a profile is known to exist).
This is SPECULATIVE until debian/rules is read (a later milestone
verifies); mark confidence accordingly (≤0.6 unless you are sure the
profile exists).
""",
    Dimension.RISK: _COMMON
    + """
Dimension: RISK — blast radius if this package's rebuild is subtly
broken. decision ∈ {boot-critical, service-critical, leaf}.
- boot-critical: system cannot boot or log in if broken (init, libc
  consumers in early boot, mount tooling, shells).
- service-critical: core services degrade (network, crypto, packaging).
- leaf: only the package's own functionality suffers.
""",
}

_VALID_DECISIONS: dict[Dimension, set[str]] = {
    Dimension.NECESSITY: {"keep", "trim"},
    Dimension.FLAGS: {"default", "override"},
    Dimension.FEATURES: {"none", "profiles"},
    Dimension.RISK: {"boot-critical", "service-critical", "leaf"},
}

_agents: dict[tuple[str, Dimension], Agent[None, _DecisionBatch]] = {}


def _get_agent(model: str, dimension: Dimension) -> Agent[None, _DecisionBatch]:
    key = (model, dimension)
    if key not in _agents:
        _agents[key] = cast(
            Agent[None, _DecisionBatch],
            Agent(
                model,
                system_prompt=_PROMPTS[dimension],
                output_type=_DecisionBatch,
            ),
        )
    return _agents[key]


# ── evidence + batches ──────────────────────────────────────────────────────


def _evidence(manifest: WorldManifest) -> str:
    return (
        f"# Host: {manifest.host}  base: {manifest.base.distro_id} "
        f"{manifest.base.suite}  ring: {int(manifest.ring)}\n"
        f"# Axes: {json.dumps(manifest.axes, sort_keys=True)}\n"
        f"# Appended flags: {manifest.flags.cflags_append} "
        f"(compiler={manifest.flags.compiler})\n"
        f"# World: {len(manifest.world)} binaries from "
        f"{len(manifest.sources)} sources\n"
    )


def _unit_lines(manifest: WorldManifest) -> list[tuple[str, str]]:
    """(source, evidence line) per source, stable order."""
    by_source: dict[str, list] = {}
    for e in manifest.world:
        by_source.setdefault(e.source, []).append(e)
    out: list[tuple[str, str]] = []
    for source in sorted(by_source):
        entries = by_source[source]
        kb = sum(e.installed_kb for e in entries)
        bins = ", ".join(e.binary for e in entries[:6])
        out.append((source, f"  {source}: binaries=[{bins}] installed={kb}KB"))
    return out


def _cache_key(model: str, dimension: Dimension, chunk: list[tuple[str, str]]) -> str:
    payload = json.dumps(
        {
            "model": model,
            "prompt_version": SYSTEM_PROMPT_VERSION,
            "dimension": dimension.value,
            "units": sorted(chunk),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ── policy ──────────────────────────────────────────────────────────────────


def apply_package_policy(decisions: list[PackageDecision]) -> list[PackageDecision]:
    """Force load-bearing sources to 'keep' in the necessity dimension —
    the package-level load-bearing blocklist."""
    out: list[PackageDecision] = []
    for d in decisions:
        if (
            d.dimension == Dimension.NECESSITY
            and d.decision == "trim"
            and d.source in LOAD_BEARING_SOURCES
        ):
            out.append(
                d.model_copy(
                    update={
                        "decision": "keep",
                        "reason": f"policy: load-bearing package (was: {d.reason})",
                    }
                )
            )
        else:
            out.append(d)
    return out


# ── the chug ────────────────────────────────────────────────────────────────


def decide_dimension(
    manifest: WorldManifest,
    dimension: Dimension,
    world_dir: Path,
    *,
    model: str = DEFAULT_MODEL,
    batch_size: int = BATCH_SIZE,
    progress=None,
) -> list[PackageDecision]:
    """One batched, cached pass of one dimension over every source."""
    units = _unit_lines(manifest)
    evidence = _evidence(manifest)
    cache_dir = world_dir / "batches" / f"world-{dimension.value}"
    decisions: list[PackageDecision] = []

    n_batches = (len(units) + batch_size - 1) // batch_size
    for i in range(0, len(units), batch_size):
        chunk = units[i : i + batch_size]
        cache_path = cache_dir / f"{_cache_key(model, dimension, chunk)}.json"
        batch: _DecisionBatch | None = None
        if cache_path.exists():
            try:
                batch = _DecisionBatch.model_validate(
                    json.loads(cache_path.read_text(encoding="utf-8"))
                )
            except (ValueError, OSError):
                batch = None
        cached = batch is not None
        if batch is None:
            prompt = (
                evidence
                + f"\n# Packages ({dimension.value} decision for each):\n"
                + "\n".join(line for _, line in chunk)
            )
            batch = _get_agent(model, dimension).run_sync(prompt).output
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                batch.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
        if progress is not None:
            progress(dimension, i // batch_size + 1, n_batches, cached)

        chunk_sources = {s for s, _ in chunk}
        valid = _VALID_DECISIONS[dimension]
        for d in batch.decisions:
            if d.source not in chunk_sources or d.decision not in valid:
                continue  # hallucinated package or vocabulary
            params: dict[str, list[str]] = {}
            if dimension == Dimension.FLAGS and d.decision == "override":
                params = {"strip": d.strip, "add": d.add}
            elif dimension == Dimension.FEATURES and d.decision == "profiles":
                params = {"profiles": d.profiles}
            decisions.append(
                PackageDecision(
                    source=d.source,
                    dimension=dimension,
                    decision=d.decision,
                    reason=d.reason,
                    confidence=d.confidence,
                    params=params,
                )
            )
    return apply_package_policy(decisions)


def decide_world(
    manifest: WorldManifest,
    world_dir: Path,
    *,
    model: str = DEFAULT_MODEL,
    batch_size: int = BATCH_SIZE,
    progress=None,
) -> dict[Dimension, list[PackageDecision]]:
    """All four dimensions over the whole world; persists each
    dimension's decisions to ``<world_dir>/decisions/<dim>.json``."""
    out: dict[Dimension, list[PackageDecision]] = {}
    decisions_dir = world_dir / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    for dimension in Dimension:
        decisions = decide_dimension(
            manifest,
            dimension,
            world_dir,
            model=model,
            batch_size=batch_size,
            progress=progress,
        )
        out[dimension] = decisions
        (decisions_dir / f"{dimension.value}.json").write_text(
            json.dumps([d.model_dump(mode="json") for d in decisions], indent=2) + "\n",
            encoding="utf-8",
        )
    return out
