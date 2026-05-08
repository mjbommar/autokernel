# LLM agents reference

autokernel uses **five** pydantic-ai agents, each with a narrow job.

| Agent | Module | Per-call inputs | Output | Per-batch cache |
|---|---|---|---|---|
| `propose` (trim) | `autokernel.agent` | Snapshot, batch of (sym, current) tuples | `[RemovalProposal]` with =n/=m | `<snap>/batches/<key>.json` |
| `propose_choices` | `autokernel.agent_dims` | Snapshot, KconfigSurface, OptimizationContext, batch of ChoiceGroups | `[RemovalProposal]` with selected option name | `<snap>/batches/dim-choices/choice-<key>.json` |
| `propose_toggles` | `autokernel.agent_dims` | Snapshot, KconfigSurface, OptimizationContext, batch of BoolToggles (allowlist+recipe-filtered) | `[RemovalProposal]` with y/n | `<snap>/batches/dim-toggles/toggle-<key>.json` |
| `propose_tunables` | `autokernel.agent_dims` | Snapshot, KconfigSurface, OptimizationContext, batch of NumericTunables (allowlist) | `[RemovalProposal]` with literal value | `<snap>/batches/dim-tunables/tunable-<key>.json` |
| **(planned v0.15+) diagnostic** | TBD | Failed iteration's record + serial log + oops | structured do-not-repeat rule | per-failure |

## What each agent sees

The shared evidence block (built by `_evidence_block()` in
`agent_dims.py`):

```
# iteration history (last 3 of 4) — target=size:           ← only when --history-from given
#   i=1 (baseline): landed=12/18, bzImage=18.2MB, boot PASS
#   i=2:           landed=11/14, bzImage=16.8MB, boot PASS
#   i=3:           landed=5/9,   bzImage=16.5MB, boot PASS
#
# FITNESS TREND (target=size, smaller is better):              ← v0.16
#   i1=18.20MB → i2=16.80MB (-7.7% vs prev) → i3=16.50MB (-1.8% vs prev)
#
# GUIDANCE:                                                     ← v0.16
#   Kernel is shrinking — keep going. Look for additional trims that
#   haven't been considered yet.
#
# rules from past iterations:
#   - i=4 regressed; do NOT re-propose: CONFIG_BTRFS_FS (reason: VFS panic)

# OptimizationContext:
#   workload:   server
#   threat:     paranoid
#   modules:    monolithic
#   aggression: aggressive
#
# Conflict resolution: when axes disagree, threat wins for security
# symbols, workload wins for perf symbols, modules wins for tristate
# composition (=y vs =m), aggression sets the confidence floor
# (drop proposals below 0.40).

# Workload profile: Datacenter / bare-metal throughput-first ...
# Threat profile:   KSPP+. RANDSTRUCT_FULL ...
# Module strategy:  Monolithic build: load-bearing =m → =y, unused → =n.
#   policy:         Prefer =y over =m for everything load-bearing ...

# Host: lab-01  Kernel: 6.19.0-9-generic  Arch: x86_64
# CPU: GenuineIntel Xeon Gold 6248 (40 cores)
# CPU flags: aes, sha_ni, rdrand, avx2, avx512f, vmx
# Boot: efi=True secure_boot=True luks=False root=ext4
# PCI: 88 devices, 0 display controller(s)

# ── workload recipe (perf axis) ────────────
#   CONFIG_PREEMPT_NONE=y  (perf): server throughput-first ...
#   CONFIG_NUMA_BALANCING=y  (perf): ...
# ── threat recipe (security axis) ────────────
#   CONFIG_RANDSTRUCT_FULL=y: Compile-time struct randomization ...
#   CONFIG_INIT_ON_FREE_DEFAULT_ON=y: Zero on free ...
```

Then a per-dimension batch payload — see the agent's
`_format_*_batch` function for the exact shape.

## Output schemas

Each agent's pydantic-ai `output_type` is intentionally simple:

```python
class _ChoiceDecision(BaseModel):
    choice: str           # match what we sent: container name or prompt
    selected_option: str  # bare CONFIG name (no CONFIG_ prefix)
    reason: str
    confidence: float

class _ToggleDecision(BaseModel):
    symbol: str           # bare CONFIG name
    value: Literal["y", "n"]
    reason: str
    risk: RiskLevel
    confidence: float

class _TunableDecision(BaseModel):
    symbol: str
    value: str            # literal: "32", '"-custom"', "0x10000"
    reason: str
    confidence: float
```

The container types (`_ChoiceBatch`, `_ToggleBatch`, `_TunableBatch`)
default `decisions: list[X] = Field(default_factory=list)` because
pydantic-ai sometimes returns `{}` for "nothing to change" and we
don't want that to exhaust the retry budget.

## Cache key shape

```python
sha256(
    SYSTEM_PROMPT_VERSION    # bumped when prompt rules change
    + "\x00" + model         # 'anthropic:claude-sonnet-4-6'
    + "\x00" + service_tier  # '' or 'flex'/'priority'/'auto'
    + "\x00" + batch_sig     # 'sym1=current1|sym2=current2|...'
    + "\x00" + history_text  # iteration history (closed-loop only)
)[:16]
```

Without `history_text`, cache keys are stable across runs — first-time
runs benefit from prior caches; closed-loop iteration regenerates
because each round's history is unique.

## Toggle eligibility

Out of ~4400 bool toggles in a modern kernel, the LLM batches only
~150-300 — the curated `TOGGLE_ALLOWLIST` in `agent_dims.py` plus
every symbol mentioned in the active workload + threat recipes.
Static internal flags (e.g. `CONFIG_HAVE_FOO`) never reach the LLM.

The allowlist categories:

- **MM / scheduler / power**: TRANSPARENT_HUGEPAGE, NUMA_BALANCING,
  SCHED_AUTOGROUP, SCHED_{MC,SMT,CLUSTER,MC_PRIO}, RCU_BOOST,
  RCU_NOCB_CPU, PREEMPT_DYNAMIC, PSI, LRU_GEN, ZSWAP, HUGETLBFS, ...
- **Networking**: BPF_{SYSCALL,JIT,JIT_ALWAYS_ON,JIT_DEFAULT_ON},
  XDP_SOCKETS, TCP_CONG_BBR, NET_SCH_FQ, RPS, XPS, RFS_ACCEL, ...
- **Virt / paravirt**: PARAVIRT, KVM_GUEST, XEN, HYPERV, VIRTIO_*,
  HW_RANDOM_VIRTIO, ...
- **CPU vendor**: INTEL_PSTATE, X86_AMD_PSTATE, INTEL_IDLE,
  X86_NATIVE_CPU, ...
- **Power management**: PM_RUNTIME, HIBERNATION, SUSPEND, ACPI_*,
  BACKLIGHT_CLASS_DEVICE, PCIEASPM, USB_AUTOSUSPEND, ...
- **Security/hardening**: RANDOM_TRUST_CPU, INIT_ON_*_DEFAULT_ON,
  FORTIFY_SOURCE, STACKPROTECTOR_STRONG, RANDOMIZE_BASE,
  SLAB_FREELIST_*, SECURITY_LOCKDOWN_LSM, MODULE_SIG*, ...
- **Surface reduction**: BT, SOUND, INPUT_JOYDEV, DRM_NOUVEAU,
  DEBUG_INFO_NONE, FTRACE, X86_X32_ABI, IA32_EMULATION, ...

## Tunable allowlist

Currently:

- `NR_CPUS` — distros default to 8192; consumer hosts much smaller
- `LOG_BUF_SHIFT`, `LOG_CPU_MAX_BUF_SHIFT`
- `LOCALVERSION`
- `PHYSICAL_START`
- `DEFAULT_MMAP_MIN_ADDR`
- `RCU_FANOUT`, `RCU_FANOUT_LEAF`
- `MAGIC_SYSRQ_DEFAULT_ENABLE`

## Knowledge files

`autokernel.knowledge` exports curated per-axis recipes:

- `workload_recipes` — 136 entries across 6 profiles
  (desktop/laptop/server/vm-guest/realtime/embedded). Source
  citations: CachyOS, XanMod, Liquorix, kernel.org admin-guide, RHEL
  low-latency tuning, KVM guest config, Yocto, Alpine.
- `threat_recipes` — 64 entries across 3 levels
  (permissive/balanced/paranoid). Source: KSPP, kernel.org
  security/, kernel-hardening-checker, Lockheed/CIS hardening guides.
- `module_strategies` — 3 high-level policies (distro/monolithic/
  modular). No per-symbol entries; just prompt-ready guidance text.

The agents pull the **slice** of each recipe that overlaps with the
current batch's symbols — no batch ever gets the full 136-entry
workload recipe, just the 5-15 entries that match the symbols in
front of the LLM.

## How to debug an agent decision

If a proposal looks wrong:

1. Find the cached batch: `find <snap>/batches/ -name '*.json' |
   xargs grep -l CONFIG_X`. The hit's filename is the cache key.
2. To re-query the LLM with the same prompt, delete that cache file
   and re-run. Same prompt + cleared cache + the LLM is generally
   stable, so this is the way to test prompt edits.
3. To inspect the prompt: there's no automatic dump but you can
   monkey-patch `Agent.run_sync` to log its `prompt` arg.
4. The proposal's `reason` field captures the LLM's stated rationale
   — surface it in `review` and `proposal.json` for audit.

## When NOT to use the LLM

The deterministic path is faster + cheaper + no risk of hallucination:

- Anything keyed off `Snapshot` evidence directly (CPU vendor → drop
  cross-vendor drivers; LUKS in chain → require dm-crypt + AES; DRM
  vendor in lspci → keep that GPU driver).
- Symbols already covered by `module_strategies` hint table.
- Symbols where the recipe is unambiguous (KSPP minimum is the same
  for any threat=balanced run).

These all live in `agent.deterministic_proposals()` and the recipes,
not in LLM prompts.
