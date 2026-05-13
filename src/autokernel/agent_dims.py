"""LLM agents for the v0.13 multi-dimensional optimization passes.

The original :mod:`autokernel.agent` only judges *trim* candidates
(tristate =y/=m → =n). This module adds three more dimensions:

* :func:`propose_choices` — pick the right option per Kconfig choice
  group (PREEMPT, HZ, default I/O sched, TCP cong, kernel image
  compression, …).
* :func:`propose_toggles` — judge bool feature toggles where the
  workload changes the right answer (TRANSPARENT_HUGEPAGE, BPF_JIT_
  ALWAYS_ON, KVM_GUEST, RANDOM_TRUST_CPU, NUMA_BALANCING, …).
* :func:`propose_tunables` — pick numeric/string values
  (NR_CPUS, LOG_BUF_SHIFT, LOCALVERSION).

Each agent returns :class:`autokernel.models.RemovalProposal` objects
so they flow through the existing review/apply/build path. The
``proposed_value`` field holds the selected option (CHOICE), 'y'/'n'
(TOGGLE), or the literal value (TUNABLE). The ``source`` is one of
``ProposalSource.{CHOICE,TOGGLE,TUNABLE}``.

Persistence + caching follows the same pattern as the trim agent:
each batch is content-addressable and persisted under
``<snapshot_dir>/batches/dim-<dimension>/`` so re-running a propose
pass picks up cached batches without paying for the LLM call again.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from autokernel.audio import render_audio_summary
from autokernel.kconfig_walk import (
    BoolToggle,
    ChoiceGroup,
    KconfigSurface,
    NumericTunable,
)
from autokernel.knowledge import (
    module_strategies,
    threat_recipes,
    workload_recipes,
)
from autokernel.llm import ServiceTier, normalize_service_tier
from autokernel.models import (
    ProposalSource,
    RemovalProposal,
    RiskLevel,
    Snapshot,
)
from autokernel.optimize_context import OptimizationContext


DEFAULT_MODEL = os.environ.get("AUTOKERNEL_MODEL", "anthropic:claude-sonnet-4-6")
DEFAULT_SERVICE_TIER = normalize_service_tier(os.environ.get("AUTOKERNEL_SERVICE_TIER"))
SYSTEM_PROMPT_VERSION = "v1"

# Per-dim batch sizes. Choices are small (~6-8 options each) so we can
# fit more per batch. Toggles and tunables are pure scalars — bigger
# batches.
CHOICE_BATCH_SIZE = int(os.environ.get("AUTOKERNEL_CHOICE_BATCH_SIZE", "12"))
TOGGLE_BATCH_SIZE = int(os.environ.get("AUTOKERNEL_TOGGLE_BATCH_SIZE", "30"))
TUNABLE_BATCH_SIZE = int(os.environ.get("AUTOKERNEL_TUNABLE_BATCH_SIZE", "20"))


# ── output schemas ────────────────────────────────────────────────────────


class _ChoiceDecision(BaseModel):
    """One choice-group decision."""

    choice: str = Field(
        description="Choice container name OR prompt — match what we sent"
    )
    selected_option: str = Field(
        description="Bare CONFIG_NAME (no CONFIG_ prefix) of the chosen option"
    )
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)


class _ChoiceBatch(BaseModel):
    """LLM output for a choice-group batch.

    ``decisions`` defaults to ``[]`` for the same reason
    ``_ProposalBatch.proposals`` does in agent.py: pydantic-ai
    sometimes returns ``{}`` ('nothing to change') and we don't want
    that to exhaust the retry budget.
    """

    decisions: list[_ChoiceDecision] = Field(default_factory=list)


class _ToggleDecision(BaseModel):
    """One bool-toggle decision."""

    symbol: str = Field(description="Bare CONFIG_NAME (no prefix)")
    value: Literal["y", "n"]
    reason: str
    risk: RiskLevel
    confidence: float = Field(ge=0.0, le=1.0)


class _ToggleBatch(BaseModel):
    decisions: list[_ToggleDecision] = Field(default_factory=list)


class _TunableDecision(BaseModel):
    """One numeric/string tunable decision."""

    symbol: str = Field(description="Bare CONFIG_NAME (no prefix)")
    value: str = Field(description="Literal value: '64', '\"-custom\"', '0x1000', etc.")
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)


class _TunableBatch(BaseModel):
    decisions: list[_TunableDecision] = Field(default_factory=list)


# ── system prompts ────────────────────────────────────────────────────────


_CHOICE_SYSTEM_PROMPT = """\
You are a Linux kernel configuration tuner. Your job: pick the right
option for each Kconfig **choice group** (a Kconfig "choice" block
forces exactly one option to be =y, the rest =n).

Rules:

1. Reason from the **workload profile** the user is running and the
   evidence about the host — same evidence the trim agent gets.
2. Consult the **workload recipe** when one is provided: it lists the
   community-consensus answer for many common choices on this profile.
   Recipe values are strong recommendations, not absolutes.
3. KEEP the current selection unless changing it produces a clear,
   defensible win. Conservative > aggressive.
4. Return a decision for **every** choice in the batch. If you'd
   keep the current option, still emit a decision — just set
   selected_option to the current option and explain why no change
   is warranted.
5. Format ``selected_option`` as the BARE option symbol — e.g.
   ``"PREEMPT_VOLUNTARY"``, NOT ``"CONFIG_PREEMPT_VOLUNTARY"`` or
   ``"voluntary"``.
6. Risk axis is implicit; just be honest with confidence (lower
   confidence when the workload signal is ambiguous).
"""

_TOGGLE_SYSTEM_PROMPT = """\
You are a Linux kernel configuration tuner. Your job: judge each
**bool feature toggle** in the batch (CONFIG_FOO=y or =n) for the
target workload.

Rules:

1. Reason from the workload profile + host evidence + workload recipe.
2. Many toggles trade **performance vs surface area** vs **security**.
   Make the tradeoff that fits the workload. A laptop optimizes for
   power-management knobs; a server prefers throughput; a vm-guest
   drops physical-hardware drivers; a hardened desktop accepts mild
   perf cost for KSPP-style hardening.
3. KEEP the current value unless the change is meaningfully better.
4. Return a decision for **every** symbol in the batch.
5. ``symbol`` is the BARE name (no CONFIG_ prefix); ``value`` is
   exactly ``"y"`` or ``"n"``.
6. Be conservative on risk: HIGH for anything that touches boot path
   (BPF unprivileged, MITIGATIONS, lockdown LSM, USERFAULTFD), MEDIUM
   for security/perf tradeoffs (THP, NUMA_BALANCING), LOW for clear
   wins (PSI, BPF_JIT_ALWAYS_ON when JIT is on).
"""

_TUNABLE_SYSTEM_PROMPT = """\
You are a Linux kernel configuration tuner. Your job: pick the right
numeric or string value for each **tunable** (CONFIG_FOO=64,
CONFIG_LOCALVERSION="-custom", etc.).

Rules:

1. Stay inside the symbol's stated ``ranges`` — out-of-range answers
   will be silently rejected.
2. For NR_CPUS: distros default to 8192 for universality (large
   bitmask, ~+8KB image, real perf hit on small hosts). For consumer/
   single-host builds, set to actual core count rounded up to a power
   of 2 (e.g. 22 cores → 32; 64 cores → 128). Server builds with
   future-headroom-needed: set 256-512.
3. For LOG_BUF_SHIFT: default 17 (128KB) is fine for most; raise to
   18-19 (256-512KB) on big servers / RT to keep early boot/oops
   messages.
4. For LOCALVERSION: leave empty unless you have a specific reason.
5. KEEP current_value unless you have a clear reason to change.
6. ``symbol`` is the BARE name; ``value`` is the literal as it would
   appear after the ``=``: ``"32"``, ``"\"-autokernel\""``, ``"0x10000"``.
7. Return a decision for **every** symbol in the batch.
"""


# ── shared helpers ────────────────────────────────────────────────────────


def _evidence_block(
    snap: Snapshot,
    ctx: OptimizationContext,
    *,
    history_text: str | None = None,
) -> str:
    """Compose the evidence + four-axis context shared by every dim agent.

    When ``history_text`` is given (a prompt-ready summary of prior
    iterations from :func:`autokernel.iteration.summarize_history_for_prompt`),
    it's prepended to the block. The agents then reason about prior
    rounds' outcomes — what was proposed, what landed, which iteration
    regressed — and avoid re-proposing things that broke prior boots.
    """
    lines: list[str] = []
    if history_text:
        lines.append(history_text)
        lines.append("")
    lines.append(ctx.render_for_prompt())
    lines.append("")
    if ctx.workload.value in workload_recipes:
        spec = workload_recipes[ctx.workload.value]
        lines.append(f"# Workload profile: {spec.description}")
    if ctx.threat.value in threat_recipes:
        spec = threat_recipes[ctx.threat.value]
        lines.append(f"# Threat profile:   {spec.description}")
    if ctx.modules.value in module_strategies:
        spec = module_strategies[ctx.modules.value]
        lines.append(f"# Module strategy:  {spec.description}")
        lines.append(f"#   policy:         {spec.policy}")
    lines.append(
        f"# Host: {snap.host}  Kernel: {snap.kernel.release}  Arch: {snap.kernel.arch}"
    )
    lines.append(
        f"# CPU: {snap.cpu.vendor_id} {snap.cpu.model_name or ''} ({snap.cpu.cores} cores)"
    )
    if snap.cpu.flags:
        notable = [
            f
            for f in snap.cpu.flags
            if f
            in {
                "hypervisor",
                "aes",
                "sha_ni",
                "rdrand",
                "avx",
                "avx2",
                "avx512f",
                "vmx",
                "svm",
                "tdx_guest",
                "sev",
                "sev_es",
            }
        ]
        if notable:
            lines.append(f"# CPU flags: {', '.join(notable)}")
    lines.append(
        f"# Boot: efi={snap.boot.efi} secure_boot={snap.boot.secure_boot} "
        f"luks={snap.boot.luks_in_chain} root={snap.boot.root_fstype}"
    )
    if snap.system.chassis_type is not None:
        lines.append(
            "# System: "
            f"vendor={snap.system.sys_vendor or '-'} "
            f"product={snap.system.product_name or '-'} "
            f"chassis_type={snap.system.chassis_type}"
        )
    lines.append(render_audio_summary(snap.audio))
    if snap.software_features:
        grouped: dict[str, list[str]] = {}
        for signal in snap.software_features:
            grouped.setdefault(signal.feature, []).append(signal.name)
        summary = "; ".join(
            f"{feature}={', '.join(sorted(set(names))[:8])}"
            for feature, names in sorted(grouped.items())
        )
        lines.append(f"# Software intent: {summary}")
    if snap.pci:
        gpu_count = sum(
            1 for d in snap.pci if (d.class_id or "").startswith(("0300", "0302"))
        )
        lines.append(
            f"# PCI: {len(snap.pci)} devices total, {gpu_count} display controller(s)"
        )
    return "\n".join(lines)


def _recipe_block(ctx: OptimizationContext, symbols: set[str]) -> str:
    """Pull the slices of the workload + threat recipes that match
    symbols in this batch. Helps the LLM align with community
    consensus without drowning the prompt.

    Conflict-resolution intent:

    * Workload recipe entries are *perf-axis* recommendations.
    * Threat recipe entries are *security-axis* recommendations.
    * When the same symbol appears in both, render both — the LLM
      reasons about the trade.
    """
    out: list[str] = []

    wspec = workload_recipes.get(ctx.workload.value)
    if wspec is not None:
        wrel = [r for r in wspec.recipes if r.symbol in symbols]
        if wrel:
            out.append("# ── workload recipe (perf axis) ────────────")
            for r in wrel:
                out.append(
                    f"#   CONFIG_{r.symbol}={r.value}  ({r.axis}): {r.rationale}  [{r.source}]"
                )

    tspec = threat_recipes.get(ctx.threat.value)
    if tspec is not None:
        trel = [r for r in tspec.recipes if r.symbol in symbols]
        if trel:
            out.append("# ── threat recipe (security axis) ────────────")
            for r in trel:
                out.append(
                    f"#   CONFIG_{r.symbol}={r.value}: {r.rationale}  [{r.source}]"
                )

    return "\n".join(out)


def _batch_cache_key(
    model: str,
    service_tier: str | None,
    batch_signature: str,
    *,
    history_text: str | None = None,
) -> str:
    """Content-address a batch.

    Salts in ``history_text`` so closed-loop iterations don't share
    cached answers across rounds — i=2's prompt has different prior
    context than i=1's, so it should re-query the LLM. Without history
    (the default trim/dim run), cache key is unchanged so first-time
    runs benefit from existing caches.
    """
    h = hashlib.sha256()
    h.update(SYSTEM_PROMPT_VERSION.encode())
    h.update(b"\x00")
    h.update(model.encode())
    h.update(b"\x00")
    h.update((service_tier or "").encode())
    h.update(b"\x00")
    h.update(batch_signature.encode())
    if history_text:
        h.update(b"\x00")
        h.update(history_text.encode())
    return h.hexdigest()[:16]


def _read_cached(path: Path | None) -> list[RemovalProposal] | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return [RemovalProposal.model_validate(d) for d in data]
    except Exception:
        return None


def _write_cached(path: Path | None, proposals: list[RemovalProposal]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [json.loads(p.model_dump_json()) for p in proposals]
    path.write_text(json.dumps(payload, indent=2))


def _prompt_cache_signature(
    *,
    evidence: str,
    recipe_block: str,
    batch_signature: str,
) -> str:
    """Cache salt for all prompt inputs that can change a decision.

    The raw batch signature captures the Kconfig surface/current values. The
    evidence and recipe blocks capture workload, threat, module strategy,
    aggression, and host facts. Without this salt, a cached vm-guest answer can
    be replayed for a later laptop run over the same symbols.
    """
    return "\n".join((evidence, recipe_block, batch_signature))


# ── propose_choices ───────────────────────────────────────────────────────


CHOICE_OPTION_ALLOWLIST: tuple[str, ...] = (
    # Latency / scheduler
    "PREEMPT_NONE",
    "PREEMPT_VOLUNTARY",
    "PREEMPT",
    "PREEMPT_RT",
    # Timer frequency
    "HZ_100",
    "HZ_250",
    "HZ_300",
    "HZ_1000",
    # Compiler optimization mode
    "CC_OPTIMIZE_FOR_PERFORMANCE",
    "CC_OPTIMIZE_FOR_SIZE",
    # Slab allocator
    "SLAB",
    "SLUB",
    "SLOB",
    # Compression defaults that affect boot/package size
    "KERNEL_GZIP",
    "KERNEL_BZIP2",
    "KERNEL_LZMA",
    "KERNEL_XZ",
    "KERNEL_LZO",
    "KERNEL_LZ4",
    "KERNEL_ZSTD",
    # TCP congestion default choices commonly exposed by Kconfig
    "DEFAULT_CUBIC",
    "DEFAULT_RENO",
    "DEFAULT_BBR",
    # CPU frequency governor defaults
    "CPU_FREQ_DEFAULT_GOV_PERFORMANCE",
    "CPU_FREQ_DEFAULT_GOV_POWERSAVE",
    "CPU_FREQ_DEFAULT_GOV_USERSPACE",
    "CPU_FREQ_DEFAULT_GOV_ONDEMAND",
    "CPU_FREQ_DEFAULT_GOV_CONSERVATIVE",
    "CPU_FREQ_DEFAULT_GOV_SCHEDUTIL",
)


def _eligible_choices(
    surface: KconfigSurface, ctx: OptimizationContext
) -> list[ChoiceGroup]:
    """Filter to high-impact choice groups worth LLM tokens."""
    extra: set[str] = set()
    wspec = workload_recipes.get(ctx.workload.value)
    if wspec is not None:
        extra |= {r.symbol for r in wspec.recipes}
    tspec = threat_recipes.get(ctx.threat.value)
    if tspec is not None:
        extra |= {r.symbol for r in tspec.recipes}
    allow = set(CHOICE_OPTION_ALLOWLIST) | extra
    return [c for c in surface.choices if any(o.name in allow for o in c.options)]


def _format_choice_for_prompt(c: ChoiceGroup) -> str:
    label = c.name or c.prompt or "(unnamed choice)"
    options_str = []
    for o in c.options:
        marker = " ★" if o.is_current else ""
        prompt = o.prompt or ""
        options_str.append(f"      - {o.name}{marker} ({prompt!r})")
    return (
        f"  choice: {label}\n"
        f"    prompt: {c.prompt!r}\n"
        f"    options (★=current):\n" + "\n".join(options_str)
    )


def _format_choice_batch(choices: list[ChoiceGroup]) -> str:
    return "\n\n".join(_format_choice_for_prompt(c) for c in choices)


def propose_choices(
    snap: Snapshot,
    surface: KconfigSurface,
    ctx: OptimizationContext,
    *,
    model: str = DEFAULT_MODEL,
    service_tier: ServiceTier | None = DEFAULT_SERVICE_TIER,
    batch_size: int = CHOICE_BATCH_SIZE,
    cache_dir: Path | None = None,
    progress: Callable[..., None] | None = None,
    max_choices: int | None = None,
    history_text: str | None = None,
) -> list[RemovalProposal]:
    """Pick one option for each Kconfig choice group on the host's surface.

    Returns ``RemovalProposal`` objects where:

    * ``config`` is the SELECTED option's full ``CONFIG_<name>``
    * ``proposed_value`` is the bare option name (so the kfrag writer
      emits ``CONFIG_<name>=y``)
    * ``source`` is ``ProposalSource.CHOICE``

    Choices whose current selection equals what the LLM would pick are
    skipped (no proposal emitted). Proposals below the
    ``ctx.aggression`` confidence floor are also dropped.
    """
    choices = _eligible_choices(surface, ctx)
    if max_choices is not None:
        choices = choices[:max_choices]
    if not choices:
        return []

    evidence = _evidence_block(snap, ctx, history_text=history_text)
    floor = ctx.aggression.confidence_floor
    out: list[RemovalProposal] = []
    agent: Agent[None, _ChoiceBatch] | None = None
    n_batches = (len(choices) + batch_size - 1) // batch_size

    for i in range(0, len(choices), batch_size):
        chunk = choices[i : i + batch_size]
        batch_idx = i // batch_size + 1

        # Cache key includes the choices' signature.
        batch_sig = "|".join(
            f"{c.name or c.prompt}::{','.join(o.name for o in c.options)}"
            for c in chunk
        )
        recipe_syms = {o.name for c in chunk for o in c.options}
        recipe_block = _recipe_block(ctx, recipe_syms)
        prompt_sig = _prompt_cache_signature(
            evidence=evidence,
            recipe_block=recipe_block,
            batch_signature=batch_sig,
        )
        cache_path = (
            cache_dir
            / f"choice-{_batch_cache_key(model, service_tier, prompt_sig, history_text=history_text)}.json"
            if cache_dir is not None
            else None
        )
        cached = _read_cached(cache_path)
        if cached is not None:
            if progress:
                progress(batch_idx, n_batches, len(chunk), cached=True)
            out.extend(cached)
            continue
        if progress:
            progress(batch_idx, n_batches, len(chunk), cached=False)

        if agent is None:
            agent = _build_choice_agent(model, service_tier)

        prompt = (
            f"{evidence}\n\n{recipe_block}\n\n"
            f"# Pick one option for each of the {len(chunk)} choice group(s) below.\n\n"
            f"{_format_choice_batch(chunk)}"
        )
        result = agent.run_sync(prompt)
        decisions = result.output.decisions

        # Match each decision back to its source choice.
        choice_lookup = {}
        for c in chunk:
            key1 = (c.name or "").upper()
            key2 = (c.prompt or "").upper()
            for k in {key1, key2}:
                if k:
                    choice_lookup[k] = c

        batch_out: list[RemovalProposal] = []
        for d in decisions:
            choice = choice_lookup.get(d.choice.upper())
            if choice is None:
                # LLM emitted a choice we didn't ask about; skip.
                continue
            opt = next((o for o in choice.options if o.name == d.selected_option), None)
            if opt is None:
                continue  # hallucinated option
            if opt.is_current:
                continue  # no change
            if d.confidence < floor:
                continue  # below aggression floor
            current_opt = next((o for o in choice.options if o.is_current), None)
            current_str = current_opt.name if current_opt else "?"
            batch_out.append(
                RemovalProposal(
                    config=f"CONFIG_{opt.name}",
                    current_value=current_str,
                    proposed_value=opt.name,
                    reason=d.reason,
                    risk=RiskLevel.LOW,
                    confidence=d.confidence,
                    source=ProposalSource.CHOICE,
                    evidence=[],
                )
            )

        _write_cached(cache_path, batch_out)
        out.extend(batch_out)

    return out


# ── propose_toggles ───────────────────────────────────────────────────────


# Hand-curated allowlist of the toggles autokernel actually cares
# about. Out of ~4400 bools in a modern kernel, most are internal
# subsystem flags; this is the high-impact subset that's worth LLM
# tokens. The LLM-proposed values combine with the per-workload recipe
# entries (extra toggles unique to a profile join automatically).
TOGGLE_ALLOWLIST: tuple[str, ...] = (
    # MM / scheduler / power
    "TRANSPARENT_HUGEPAGE",
    "NUMA_BALANCING",
    "NUMA_BALANCING_DEFAULT_ENABLED",
    "SCHED_AUTOGROUP",
    "SCHED_MC",
    "SCHED_SMT",
    "SCHED_CLUSTER",
    "SCHED_MC_PRIO",
    "HIGH_RES_TIMERS",
    "RCU_BOOST",
    "RCU_NOCB_CPU",
    "RCU_NOCB_CPU_DEFAULT_ALL",
    "PREEMPT_DYNAMIC",
    "PSI",
    "PSI_DEFAULT_DISABLED",
    "LRU_GEN",
    "LRU_GEN_ENABLED",
    "ZSWAP",
    "ZSWAP_DEFAULT_ON",
    "HUGETLBFS",
    # Networking
    "BPF_SYSCALL",
    "BPF_JIT",
    "BPF_JIT_ALWAYS_ON",
    "BPF_JIT_DEFAULT_ON",
    "BPF_UNPRIV_DEFAULT_OFF",
    "XDP_SOCKETS",
    "TCP_CONG_BBR",
    "NET_SCH_FQ",
    "NET_SCH_FQ_CODEL",
    "RPS",
    "XPS",
    "RFS_ACCEL",
    "NET_RX_BUSY_POLL",
    # Virt / paravirt
    "PARAVIRT",
    "PARAVIRT_SPINLOCKS",
    "KVM_GUEST",
    "XEN",
    "HYPERV",
    "HYPERV_GUEST",
    "VIRTIO",
    "VIRTIO_PCI",
    "VIRTIO_BLK",
    "VIRTIO_NET",
    "VIRTIO_CONSOLE",
    "VIRTIO_BALLOON",
    "VIRTIO_INPUT",
    "VIRTIO_FS",
    "VIRTIO_VSOCK",
    "HW_RANDOM_VIRTIO",
    # CPU vendor / pstate
    "INTEL_PSTATE",
    "X86_AMD_PSTATE",
    "INTEL_IDLE",
    "X86_NATIVE_CPU",
    # Power management (laptops)
    "PM",
    "PM_RUNTIME",
    "HIBERNATION",
    "SUSPEND",
    "ACPI",
    "ACPI_AC",
    "ACPI_BATTERY",
    "ACPI_FAN",
    "ACPI_THERMAL",
    "ACPI_DOCK",
    "ACPI_PLATFORM_PROFILE",
    "BACKLIGHT_CLASS_DEVICE",
    "PCIEASPM",
    "USB_AUTOSUSPEND",
    "PCIE_PME",
    # Security / hardening
    "RANDOM_TRUST_CPU",
    "RANDOM_TRUST_BOOTLOADER",
    "INIT_ON_ALLOC_DEFAULT_ON",
    "INIT_ON_FREE_DEFAULT_ON",
    "FORTIFY_SOURCE",
    "STACKPROTECTOR_STRONG",
    "VMAP_STACK",
    "RANDOMIZE_BASE",
    "RANDOMIZE_MEMORY",
    "SLAB_FREELIST_HARDENED",
    "SLAB_FREELIST_RANDOM",
    "SHUFFLE_PAGE_ALLOCATOR",
    "HARDENED_USERCOPY",
    "SECURITY_LOCKDOWN_LSM",
    "SECURITY_LANDLOCK",
    "SECURITY_YAMA",
    "MODULE_SIG",
    "MODULE_SIG_FORCE",
    "MODULE_SIG_ALL",
    "STRICT_DEVMEM",
    "IO_STRICT_DEVMEM",
    "X86_X32_ABI",
    "IA32_EMULATION",
    "MODIFY_LDT_SYSCALL",
    "PROC_KCORE",
    "DEVKMEM",
    "KEXEC",
    "KEXEC_FILE",
    "KEXEC_SIG",
    # Surface reduction
    "BT",
    "SOUND",
    "INPUT_JOYDEV",
    "DRM",
    "DRM_NOUVEAU",
    "DEBUG_INFO",
    "DEBUG_INFO_NONE",
    "DEBUG_KERNEL",
    "DEBUG_FS",
    "FTRACE",
    "KPROBES",
    "X86_GENERIC",
    # Build / size
    "CC_OPTIMIZE_FOR_SIZE",
    "CC_OPTIMIZE_FOR_PERFORMANCE",
    "EXPERT",
    "EMBEDDED",
)


def _eligible_toggles(
    surface: KconfigSurface, ctx: OptimizationContext
) -> list[BoolToggle]:
    """Filter to high-signal toggles worth LLM tokens.

    Allowlist combines: the static :data:`TOGGLE_ALLOWLIST`, every
    symbol in the active workload recipe, AND every symbol in the
    active threat recipe (so paranoid users see KSPP+ knobs the LLM
    can adjust)."""
    extra: set[str] = set()
    wspec = workload_recipes.get(ctx.workload.value)
    if wspec is not None:
        extra |= {r.symbol for r in wspec.recipes}
    tspec = threat_recipes.get(ctx.threat.value)
    if tspec is not None:
        extra |= {r.symbol for r in tspec.recipes}
    allow = set(TOGGLE_ALLOWLIST) | extra
    return [t for t in surface.toggles if t.name in allow]


def _format_toggle(t: BoolToggle) -> str:
    help_brief = (t.help or "").splitlines()[0][:120] if t.help else ""
    return (
        f"  - {t.name} = {t.current_value}\n"
        f"      prompt: {t.prompt!r}\n"
        f"      help:   {help_brief!r}"
    )


def _format_toggle_batch(toggles: list[BoolToggle]) -> str:
    return "\n\n".join(_format_toggle(t) for t in toggles)


def propose_toggles(
    snap: Snapshot,
    surface: KconfigSurface,
    ctx: OptimizationContext,
    *,
    model: str = DEFAULT_MODEL,
    service_tier: ServiceTier | None = DEFAULT_SERVICE_TIER,
    batch_size: int = TOGGLE_BATCH_SIZE,
    cache_dir: Path | None = None,
    progress: Callable[..., None] | None = None,
    history_text: str | None = None,
) -> list[RemovalProposal]:
    """Judge bool feature toggles against the four-axis context.

    Only emits proposals for toggles where the LLM picks a value
    DIFFERENT from the current — no-change decisions are silently
    dropped — AND that meet the aggression confidence floor.
    """
    toggles = _eligible_toggles(surface, ctx)
    if not toggles:
        return []

    evidence = _evidence_block(snap, ctx, history_text=history_text)
    floor = ctx.aggression.confidence_floor
    out: list[RemovalProposal] = []
    agent: Agent[None, _ToggleBatch] | None = None
    n_batches = (len(toggles) + batch_size - 1) // batch_size

    for i in range(0, len(toggles), batch_size):
        chunk = toggles[i : i + batch_size]
        batch_idx = i // batch_size + 1

        batch_sig = "|".join(f"{t.name}={t.current_value}" for t in chunk)
        recipe_block = _recipe_block(ctx, {t.name for t in chunk})
        prompt_sig = _prompt_cache_signature(
            evidence=evidence,
            recipe_block=recipe_block,
            batch_signature=batch_sig,
        )
        cache_path = (
            cache_dir
            / f"toggle-{_batch_cache_key(model, service_tier, prompt_sig, history_text=history_text)}.json"
            if cache_dir is not None
            else None
        )
        cached = _read_cached(cache_path)
        if cached is not None:
            if progress:
                progress(batch_idx, n_batches, len(chunk), cached=True)
            out.extend(cached)
            continue
        if progress:
            progress(batch_idx, n_batches, len(chunk), cached=False)

        if agent is None:
            agent = _build_toggle_agent(model, service_tier)

        prompt = (
            f"{evidence}\n\n{recipe_block}\n\n"
            f"# Decide y or n for each of the {len(chunk)} toggle(s) below.\n\n"
            f"{_format_toggle_batch(chunk)}"
        )
        result = agent.run_sync(prompt)

        chunk_lookup = {t.name: t for t in chunk}
        batch_out: list[RemovalProposal] = []
        for d in result.output.decisions:
            t = chunk_lookup.get(d.symbol)
            if t is None:
                continue
            if d.value == t.current_value:
                continue  # no change
            if d.confidence < floor:
                continue  # below aggression floor
            batch_out.append(
                RemovalProposal(
                    config=f"CONFIG_{t.name}",
                    current_value=t.current_value,
                    proposed_value=d.value,
                    reason=d.reason,
                    risk=d.risk,
                    confidence=d.confidence,
                    source=ProposalSource.TOGGLE,
                    evidence=[],
                )
            )
        _write_cached(cache_path, batch_out)
        out.extend(batch_out)

    return out


# ── propose_tunables ──────────────────────────────────────────────────────


# These are the tunables worth LLM judgment. Most int/string Kconfigs
# in the kernel are obscure subsystem parameters; the LLM doesn't
# improve on the kernel default for those.
TUNABLE_ALLOWLIST: tuple[str, ...] = (
    "NR_CPUS",
    "LOG_BUF_SHIFT",
    "LOG_CPU_MAX_BUF_SHIFT",
    "LOCALVERSION",
    "PHYSICAL_START",
    "DEFAULT_MMAP_MIN_ADDR",
    "RCU_FANOUT",
    "RCU_FANOUT_LEAF",
    "MAGIC_SYSRQ_DEFAULT_ENABLE",
)


def _eligible_tunables(surface: KconfigSurface) -> list[NumericTunable]:
    return [t for t in surface.tunables if t.name in TUNABLE_ALLOWLIST]


def _format_tunable(t: NumericTunable) -> str:
    help_brief = (t.help or "").splitlines()[0][:120] if t.help else ""
    rng = ", ".join(f"[{lo}-{hi}]" for lo, hi in t.ranges) or "(no range)"
    return (
        f"  - {t.name} ({t.type.value}) = {t.current_value!r}\n"
        f"      prompt: {t.prompt!r}\n"
        f"      ranges: {rng}\n"
        f"      help:   {help_brief!r}"
    )


def _format_tunable_batch(tunables: list[NumericTunable]) -> str:
    return "\n\n".join(_format_tunable(t) for t in tunables)


def propose_tunables(
    snap: Snapshot,
    surface: KconfigSurface,
    ctx: OptimizationContext,
    *,
    model: str = DEFAULT_MODEL,
    service_tier: ServiceTier | None = DEFAULT_SERVICE_TIER,
    batch_size: int = TUNABLE_BATCH_SIZE,
    cache_dir: Path | None = None,
    progress: Callable[..., None] | None = None,
    history_text: str | None = None,
) -> list[RemovalProposal]:
    """Pick numeric/string values for whitelisted tunables."""
    tunables = _eligible_tunables(surface)
    if not tunables:
        return []

    evidence = _evidence_block(snap, ctx, history_text=history_text)
    floor = ctx.aggression.confidence_floor
    out: list[RemovalProposal] = []
    agent: Agent[None, _TunableBatch] | None = None
    n_batches = (len(tunables) + batch_size - 1) // batch_size

    for i in range(0, len(tunables), batch_size):
        chunk = tunables[i : i + batch_size]
        batch_idx = i // batch_size + 1

        batch_sig = "|".join(f"{t.name}={t.current_value}" for t in chunk)
        recipe_block = _recipe_block(ctx, {t.name for t in chunk})
        prompt_sig = _prompt_cache_signature(
            evidence=evidence,
            recipe_block=recipe_block,
            batch_signature=batch_sig,
        )
        cache_path = (
            cache_dir
            / f"tunable-{_batch_cache_key(model, service_tier, prompt_sig, history_text=history_text)}.json"
            if cache_dir is not None
            else None
        )
        cached = _read_cached(cache_path)
        if cached is not None:
            if progress:
                progress(batch_idx, n_batches, len(chunk), cached=True)
            out.extend(cached)
            continue
        if progress:
            progress(batch_idx, n_batches, len(chunk), cached=False)

        if agent is None:
            agent = _build_tunable_agent(model, service_tier)

        prompt = (
            f"{evidence}\n\n{recipe_block}\n\n"
            f"# Pick a value for each of the {len(chunk)} tunable(s) below.\n\n"
            f"{_format_tunable_batch(chunk)}"
        )
        result = agent.run_sync(prompt)

        chunk_lookup = {t.name: t for t in chunk}
        batch_out: list[RemovalProposal] = []
        for d in result.output.decisions:
            t = chunk_lookup.get(d.symbol)
            if t is None:
                continue
            # Strip enclosing quotes from current_value comparison only —
            # store decision verbatim.
            cur_norm = t.current_value.strip().strip('"')
            new_norm = d.value.strip().strip('"')
            if cur_norm == new_norm:
                continue
            if d.confidence < floor:
                continue  # below aggression floor
            batch_out.append(
                RemovalProposal(
                    config=f"CONFIG_{t.name}",
                    current_value=t.current_value,
                    proposed_value=d.value,
                    reason=d.reason,
                    risk=RiskLevel.LOW,
                    confidence=d.confidence,
                    source=ProposalSource.TUNABLE,
                    evidence=[],
                )
            )
        _write_cached(cache_path, batch_out)
        out.extend(batch_out)

    return out


# ── agent factories ───────────────────────────────────────────────────────


_choice_agent: Agent[None, _ChoiceBatch] | None = None
_choice_agent_sig: tuple[str, ServiceTier | None] | None = None


def _build_choice_agent(
    model: str, service_tier: ServiceTier | None
) -> Agent[None, _ChoiceBatch]:
    global _choice_agent, _choice_agent_sig
    sig = (model, service_tier)
    if _choice_agent is not None and _choice_agent_sig == sig:
        return _choice_agent
    from pydantic_ai.settings import ModelSettings

    settings = (
        ModelSettings(service_tier=service_tier) if service_tier else ModelSettings()
    )
    _choice_agent = cast(
        Agent[None, _ChoiceBatch],
        Agent(
            model,
            system_prompt=_CHOICE_SYSTEM_PROMPT,
            output_type=_ChoiceBatch,
            model_settings=settings,
        ),
    )
    _choice_agent_sig = sig
    if _choice_agent is None:
        raise RuntimeError("failed to initialize choice agent")
    return _choice_agent


_toggle_agent: Agent[None, _ToggleBatch] | None = None
_toggle_agent_sig: tuple[str, ServiceTier | None] | None = None


def _build_toggle_agent(
    model: str, service_tier: ServiceTier | None
) -> Agent[None, _ToggleBatch]:
    global _toggle_agent, _toggle_agent_sig
    sig = (model, service_tier)
    if _toggle_agent is not None and _toggle_agent_sig == sig:
        return _toggle_agent
    from pydantic_ai.settings import ModelSettings

    settings = (
        ModelSettings(service_tier=service_tier) if service_tier else ModelSettings()
    )
    _toggle_agent = cast(
        Agent[None, _ToggleBatch],
        Agent(
            model,
            system_prompt=_TOGGLE_SYSTEM_PROMPT,
            output_type=_ToggleBatch,
            model_settings=settings,
        ),
    )
    _toggle_agent_sig = sig
    if _toggle_agent is None:
        raise RuntimeError("failed to initialize toggle agent")
    return _toggle_agent


_tunable_agent: Agent[None, _TunableBatch] | None = None
_tunable_agent_sig: tuple[str, ServiceTier | None] | None = None


def _build_tunable_agent(
    model: str, service_tier: ServiceTier | None
) -> Agent[None, _TunableBatch]:
    global _tunable_agent, _tunable_agent_sig
    sig = (model, service_tier)
    if _tunable_agent is not None and _tunable_agent_sig == sig:
        return _tunable_agent
    from pydantic_ai.settings import ModelSettings

    settings = (
        ModelSettings(service_tier=service_tier) if service_tier else ModelSettings()
    )
    _tunable_agent = cast(
        Agent[None, _TunableBatch],
        Agent(
            model,
            system_prompt=_TUNABLE_SYSTEM_PROMPT,
            output_type=_TunableBatch,
            model_settings=settings,
        ),
    )
    _tunable_agent_sig = sig
    if _tunable_agent is None:
        raise RuntimeError("failed to initialize tunable agent")
    return _tunable_agent
