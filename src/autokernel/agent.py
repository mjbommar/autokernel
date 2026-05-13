"""pydantic-ai agent that proposes CONFIG_* removals for the *uncertain*
subset of candidates.

Inputs the agent receives:
    * A compact human-readable evidence summary derived from the Snapshot
      (CPU vendor, GPUs, NICs, USB devices, mounted filesystems, …).
    * A batch of candidate CONFIG_ symbols with their current value.

Output: a list of RemovalProposal objects with reason, risk, confidence.

The agent is **advisory only** — it never edits files. The policy filter
in autokernel.policy decides what's actually applied based on autonomy level.

**Per-batch persistence**: each batch is content-addressable; the cache key
is a hash of (model, system_prompt_version, batch contents). Results are
persisted to ``<snapshot_dir>/batches/<key>.json`` so an interrupted run
can resume without re-paying for completed batches.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from autokernel.audio import render_audio_summary
from autokernel.llm import ServiceTier, normalize_service_tier
from autokernel.models import (
    ProposalSource,
    RemovalProposal,
    RiskLevel,
    Snapshot,
)


DEFAULT_MODEL = os.environ.get("AUTOKERNEL_MODEL", "anthropic:claude-sonnet-4-6")
BATCH_SIZE = int(os.environ.get("AUTOKERNEL_BATCH_SIZE", "60"))
# AUTOKERNEL_SERVICE_TIER: 'flex' / 'priority' / 'auto' (OpenAI-specific tiers).
# pydantic-ai's ModelSettings.service_tier is a passthrough; provider-specific
# semantics apply (currently OpenAI honours it via the Responses/Chat APIs).
DEFAULT_SERVICE_TIER = normalize_service_tier(os.environ.get("AUTOKERNEL_SERVICE_TIER"))
# Bump when the system prompt changes in a way that should invalidate cached
# batches (different decision rules → different proposals).
SYSTEM_PROMPT_VERSION = "v2"


class _ProposalDraft(BaseModel):
    """Output schema the LLM populates per candidate.

    Kept minimal so the model isn't tempted to hallucinate extra fields.
    The CLI promotes these into full :class:`RemovalProposal` instances,
    stamping ``source=LLM``.
    """

    config: str = Field(description="Exact CONFIG_ symbol name")
    decision: str = Field(
        description="'remove' to disable, 'keep' to leave alone, 'demote' to switch =y to =m"
    )
    reason: str = Field(description="One-sentence rationale tied to specific evidence")
    risk: RiskLevel
    confidence: float = Field(ge=0.0, le=1.0)


class _ProposalBatch(BaseModel):
    """Container the LLM populates with one entry per candidate.

    ``proposals`` defaults to an empty list because some batches have no
    actionable changes (every candidate should be kept) and the model
    sometimes returns ``{}`` instead of ``{"proposals": []}``. With a
    strict schema, pydantic-ai exhausts retries; with a default-empty
    list we treat "no proposals" as a valid answer (== "keep all").
    """

    proposals: list[_ProposalDraft] = Field(default_factory=list)


_SYSTEM_PROMPT = """\
You are a Linux kernel configuration reviewer. Your job: given hardware
evidence from one specific machine, decide whether each candidate CONFIG_*
symbol can safely be DISABLED, DEMOTED to a module, or must be KEPT.

Decision rules — apply in order:

1. KEEP anything the evidence shows is in use, or whose absence would brick
   the machine: filesystems on active mounts, drivers for present devices,
   firmware loaders, EFI/boot, microcode for the CPU vendor present.

2. KEEP if you're not sure. False positives (incorrectly removing) brick
   the machine; false negatives (incorrectly keeping) just mean a slightly
   bigger kernel. Bias hard toward keeping.

3. REMOVE only when evidence affirmatively contradicts the symbol — e.g.
   CONFIG_INTEL_* on an AuthenticAMD CPU; CONFIG_NOUVEAU on a machine
   with only Intel iGPU and no NVIDIA hardware in lspci.

4. DEMOTE (y → m) for drivers that aren't currently used but might be
   plugged in (USB classes, optional filesystems).

5. Confidence: 1.0 = certain (e.g. wrong CPU vendor); 0.7 = strong
   evidence; 0.4 = guess. NEVER fabricate evidence — if unsure, KEEP at
   high confidence.

6. Risk: LOW = wrong-vendor / clearly unused; MEDIUM = obscure subsystems;
   HIGH = anything touching boot, storage, network, console.

Return a proposal for EVERY candidate symbol in the batch.
"""


_agent: Agent[None, _ProposalBatch] | None = None
_agent_signature: tuple[str, str | None] | None = None


def _get_agent(
    model: str = DEFAULT_MODEL,
    service_tier: ServiceTier | None = DEFAULT_SERVICE_TIER,
) -> Agent[None, _ProposalBatch]:
    """Return a cached Agent, rebuilding it if (model, service_tier) changes."""
    global _agent, _agent_signature

    sig = (model, service_tier)
    if _agent is not None and _agent_signature == sig:
        return _agent

    from pydantic_ai.settings import ModelSettings

    settings = ModelSettings()
    if service_tier:
        settings = ModelSettings(service_tier=service_tier)

    _agent = cast(
        Agent[None, _ProposalBatch],
        Agent(
            model,
            system_prompt=_SYSTEM_PROMPT,
            output_type=_ProposalBatch,
            model_settings=settings,
        ),
    )
    _agent_signature = sig
    if _agent is None:
        raise RuntimeError("failed to initialize proposal agent")
    return _agent


def _evidence_summary(snap: Snapshot) -> str:
    """Compact, LLM-friendly summary of the snapshot."""
    lines: list[str] = []
    lines.append(
        f"# Host: {snap.host}  Kernel: {snap.kernel.release}  Arch: {snap.kernel.arch}"
    )
    lines.append(
        f"# CPU: {snap.cpu.vendor_id} {snap.cpu.model_name or ''} ({snap.cpu.cores} cores)"
    )
    lines.append(
        f"# Boot: efi={snap.boot.efi} secure_boot={snap.boot.secure_boot} "
        f"luks={snap.boot.luks_in_chain} root={snap.boot.root_fstype} boot={snap.boot.boot_fstype}"
    )
    if snap.system.chassis_type is not None:
        lines.append(
            "# System: "
            f"vendor={snap.system.sys_vendor or '-'} "
            f"product={snap.system.product_name or '-'} "
            f"chassis_type={snap.system.chassis_type}"
        )
    lines.append(render_audio_summary(snap.audio))

    lines.append("# PCI devices:")
    for d in snap.pci[:50]:
        lines.append(
            f"  {d.slot} {d.vendor_id}:{d.device_id} {d.description or ''} (driver={d.driver or '-'})"
        )

    if snap.usb:
        lines.append(f"# USB devices ({len(snap.usb)}):")
        for d in snap.usb[:30]:
            lines.append(f"  {d.vendor_id}:{d.product_id} {d.description or ''}")

    lines.append("# Mounted filesystems (real):")
    for m in snap.mounts:
        if m.fstype not in {
            "proc",
            "sysfs",
            "devpts",
            "cgroup2",
            "tmpfs",
            "mqueue",
            "tracefs",
            "debugfs",
            "configfs",
            "fusectl",
            "pstore",
            "bpf",
            "securityfs",
            "hugetlbfs",
            "rpc_pipefs",
            "nsfs",
        }:
            lines.append(f"  {m.target} ({m.fstype})")

    lines.append("# Network interfaces:")
    for n in snap.network:
        lines.append(
            f"  {n.name} driver={n.driver or '-'} state={n.operstate or '-'} active={n.is_active}"
        )

    if snap.dkms:
        lines.append("# DKMS modules (rebuild required for any new kernel):")
        for d in snap.dkms:
            lines.append(f"  {d.name} {d.version} ({d.status})")

    if snap.software_features:
        lines.append("# Software intent signals:")
        for s in snap.software_features[:30]:
            detail = f" ({s.detail})" if s.detail else ""
            lines.append(f"  {s.feature}: {s.source}:{s.name}{detail}")

    if snap.firmware:
        lines.append(f"# Firmware blobs in use ({len(snap.firmware)}):")
        for fw in snap.firmware[:10]:
            lines.append(f"  {fw.name}")

    return "\n".join(lines)


def _format_batch(symbols: list[tuple[str, str]]) -> str:
    """symbols = list of (CONFIG_NAME, current_value)"""
    lines = ["# Candidate symbols (decide for each):"]
    for sym, val in symbols:
        lines.append(f"  {sym}={val}")
    return "\n".join(lines)


def _batch_cache_key(
    model: str, service_tier: str | None, evidence: str, chunk: list[tuple[str, str]]
) -> str:
    """Content-address a batch: same (model, service_tier, prompt version,
    symbol set) yields the same key. Service tier is part of the key because
    different tiers can return materially different responses on the same
    prompt (latency, retry behaviour, occasional output drift).
    """
    payload = json.dumps(
        {
            "model": model,
            "service_tier": service_tier,
            "prompt_version": SYSTEM_PROMPT_VERSION,
            "evidence_sha256": hashlib.sha256(evidence.encode()).hexdigest(),
            "symbols": sorted(chunk),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _read_cached_batch(path: Path) -> list[RemovalProposal] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        return [RemovalProposal.model_validate(p) for p in raw["proposals"]]
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


def _write_cached_batch(path: Path, proposals: list[RemovalProposal]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"proposals": [p.model_dump(mode="json") for p in proposals]}
    path.write_text(json.dumps(payload, indent=2))


def propose(
    snap: Snapshot,
    candidates: list[tuple[str, str]],
    *,
    model: str = DEFAULT_MODEL,
    service_tier: ServiceTier | None = DEFAULT_SERVICE_TIER,
    batch_size: int = BATCH_SIZE,
    progress: Callable[..., None] | None = None,
    cache_dir: Path | None = None,
) -> list[RemovalProposal]:
    """Run the agent over ``candidates`` and return RemovalProposals for
    every item the agent says to remove or demote.

    ``candidates`` is the output of :func:`autokernel.resolve.candidate_trims`
    paired with each symbol's current ``=y``/``=m`` value.

    Per-batch caching: when ``cache_dir`` is given (typically
    ``<snapshot_dir>/batches/``), each batch's result is persisted to
    ``<cache_dir>/<key>.json``. On rerun, batches whose content hash matches
    a cached file are loaded instead of re-invoking the LLM.

    Yields proposals in batches; flatten and pass to :func:`policy.apply_policy`.
    """
    if not candidates:
        return []

    evidence = _evidence_summary(snap)
    out: list[RemovalProposal] = []
    agent: Agent[None, _ProposalBatch] | None = None  # lazy: skip if all cached

    n_batches = (len(candidates) + batch_size - 1) // batch_size
    for i in range(0, len(candidates), batch_size):
        chunk = candidates[i : i + batch_size]
        batch_idx = i // batch_size + 1

        cache_path = (
            cache_dir / f"{_batch_cache_key(model, service_tier, evidence, chunk)}.json"
            if cache_dir is not None
            else None
        )

        cached = _read_cached_batch(cache_path) if cache_path else None
        if cached is not None:
            if progress:
                progress(batch_idx, n_batches, len(chunk), cached=True)
            out.extend(cached)
            continue

        if progress:
            progress(batch_idx, n_batches, len(chunk), cached=False)

        if agent is None:
            agent = _get_agent(model, service_tier)

        prompt = f"{evidence}\n\n{_format_batch(chunk)}"
        result = agent.run_sync(prompt)
        batch = result.output

        chunk_lookup = dict(chunk)
        batch_proposals: list[RemovalProposal] = []
        for d in batch.proposals:
            current = chunk_lookup.get(d.config)
            if current is None:
                # Hallucinated symbol — skip
                continue
            if d.decision not in {"remove", "demote"}:
                continue
            proposed = "n" if d.decision == "remove" else "m"
            if proposed == current:
                continue
            batch_proposals.append(
                RemovalProposal(
                    config=d.config,
                    current_value=current,
                    proposed_value=proposed,
                    reason=d.reason,
                    risk=d.risk,
                    confidence=d.confidence,
                    source=ProposalSource.LLM,
                    evidence=[],
                )
            )

        if cache_path is not None:
            _write_cached_batch(cache_path, batch_proposals)
        out.extend(batch_proposals)

    return out


def deterministic_proposals(
    snap: Snapshot, candidates: Iterable[tuple[str, str]]
) -> list[RemovalProposal]:
    """Hard rules, no LLM. Cheap. These are the guaranteed-safe trims:
    wrong-CPU-vendor, missing-class-of-device.

    Returns proposals tagged source=DETERMINISTIC.
    """
    candidate_list = list(candidates)
    out: list[RemovalProposal] = []
    cpu = snap.cpu.vendor_id

    # PCI class 03xx = display controller (0300 VGA, 0302 3D, 0380 other).
    # Vendor IDs: 10de NVIDIA, 1002 AMD, 8086 Intel.
    def _is_display(p) -> bool:
        return bool(p.class_id and p.class_id.startswith("03"))

    has_nvidia = any(p.vendor_id == "10de" and _is_display(p) for p in snap.pci)
    has_amdgpu = any(p.vendor_id == "1002" and _is_display(p) for p in snap.pci)
    has_intel_gpu = any(p.vendor_id == "8086" and _is_display(p) for p in snap.pci)

    for sym, val in candidate_list:
        s = sym.upper()
        proposal: RemovalProposal | None = None

        if cpu == "AuthenticAMD" and (
            "INTEL_IDLE" in s or s.startswith("CONFIG_X86_INTEL_") or "INTEL_RAPL" in s
        ):
            proposal = RemovalProposal(
                config=sym,
                current_value=val,
                proposed_value="n",
                reason=f"AMD CPU, Intel-specific symbol ({sym})",
                risk=RiskLevel.LOW,
                confidence=0.99,
                source=ProposalSource.DETERMINISTIC,
                evidence=[f"cpu.vendor_id={cpu}"],
            )
        elif cpu == "GenuineIntel" and (
            s.startswith("CONFIG_X86_AMD_") or "AMD_PSTATE" in s
        ):
            proposal = RemovalProposal(
                config=sym,
                current_value=val,
                proposed_value="n",
                reason=f"Intel CPU, AMD-specific symbol ({sym})",
                risk=RiskLevel.LOW,
                confidence=0.99,
                source=ProposalSource.DETERMINISTIC,
                evidence=[f"cpu.vendor_id={cpu}"],
            )
        elif not has_nvidia and ("NOUVEAU" in s or "NVIDIA" in s):
            proposal = RemovalProposal(
                config=sym,
                current_value=val,
                proposed_value="n",
                reason="No NVIDIA GPU present in lspci",
                risk=RiskLevel.LOW,
                confidence=0.95,
                source=ProposalSource.DETERMINISTIC,
                evidence=["pci has no NVIDIA vendor 10de"],
            )
        elif not has_amdgpu and ("AMDGPU" in s or s.startswith("CONFIG_DRM_RADEON")):
            proposal = RemovalProposal(
                config=sym,
                current_value=val,
                proposed_value="n",
                reason="No AMD GPU present in lspci",
                risk=RiskLevel.LOW,
                confidence=0.95,
                source=ProposalSource.DETERMINISTIC,
                evidence=["pci has no AMD vendor 1002"],
            )
        elif not has_intel_gpu and "DRM_I915" in s:
            proposal = RemovalProposal(
                config=sym,
                current_value=val,
                proposed_value="n",
                reason="No Intel GPU present in lspci",
                risk=RiskLevel.LOW,
                confidence=0.9,
                source=ProposalSource.DETERMINISTIC,
                evidence=["pci has no Intel VGA"],
            )

        if proposal:
            out.append(proposal)

    # ── CPU microarch tuning ─────────────────────────────────────────────
    # Independent of the candidate scan above: a deterministic recommendation
    # to swap CONFIG_GENERIC_CPU=y for the host's actual microarchitecture
    # (CONFIG_MZEN3 / CONFIG_MMETEORLAKE / …). Emits two proposals — one
    # "disable GENERIC_CPU" and one "enable M<arch>" — so the apply step
    # produces a coherent .config.
    out.extend(_microarch_proposals(snap, candidate_list))

    return out


def _microarch_proposals(
    snap: Snapshot, candidates: list[tuple[str, str]]
) -> list[RemovalProposal]:
    """Build the CPU-microarch swap pair, or return empty when:

    * the host's CPU isn't recognized (Microarch.GENERIC),
    * the running kernel is too old for the recommended symbol,
    * the running config already has the right symbol set,
    * the CPU's microarch isn't in the candidate-trim pool's running-config
      (i.e. we don't know what value to swap from).
    """
    from autokernel.cpu import recommend

    rec = recommend(snap.cpu, snap.kernel.release)
    if rec is None:
        return []
    arch, target_symbol = rec

    # Look up current values from the candidate list (it's the running
    # config's =y/=m pile). If the target is already set, no-op.
    by_sym = dict(candidates)
    target_current = by_sym.get(target_symbol)
    generic_current = by_sym.get("CONFIG_GENERIC_CPU")

    proposals: list[RemovalProposal] = []
    if generic_current == "y":
        proposals.append(
            RemovalProposal(
                config="CONFIG_GENERIC_CPU",
                current_value="y",
                proposed_value="n",
                reason=(
                    f"Host CPU is {arch.value} ({snap.cpu.model_name or 'unknown model'}); "
                    f"swap to a tuned microarch symbol for better codegen."
                ),
                risk=RiskLevel.LOW,
                confidence=0.95,
                source=ProposalSource.MICROARCH,
                evidence=[
                    f"cpu.vendor_id={snap.cpu.vendor_id}",
                    f"cpu.cpu_family={snap.cpu.cpu_family}",
                    f"cpu.model={snap.cpu.model}",
                ],
            )
        )

    # Only emit the enable when the running config doesn't already have it.
    if target_current != "y":
        proposals.append(
            RemovalProposal(
                config=target_symbol,
                current_value=target_current or "n",
                proposed_value="y",
                reason=(
                    f"Set CPU microarch tuning for {arch.value} "
                    f"({snap.cpu.model_name or 'unknown model'})."
                ),
                risk=RiskLevel.LOW,
                confidence=0.95,
                source=ProposalSource.MICROARCH,
                evidence=[
                    f"cpu.vendor_id={snap.cpu.vendor_id}",
                    f"cpu.cpu_family={snap.cpu.cpu_family}",
                    f"cpu.model={snap.cpu.model}",
                    f"kernel.release={snap.kernel.release}",
                ],
            )
        )

    return proposals
