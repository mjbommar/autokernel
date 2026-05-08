# Architecture

A walkthrough of how autokernel's pieces fit together. Written for
future-Claude (or future-me) coming back to extend this. For the
historical/strategic view see [ROADMAP.md](ROADMAP.md).

## The pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│  scan                                                           │
│  bash collectors → typed Snapshot                               │
│  /proc /sys lspci lsusb lsmod /etc/os-release /sys/class/dmi    │
└─────┬───────────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────────────────────────────┐
│  workload detection      ────────────────────────┐              │
│  Snapshot + /sys probes → WorkloadProfile        │              │
│  vm-guest > embedded > laptop > server > desktop │              │
└─────────────────────────────────────────────────┬┘              │
                                                  ▼               │
┌──────────────────────────────────────────────────────────────┐  │
│  OptimizationContext (the four axes)                         │  │
│  workload × threat × modules × aggression                    │  │
│  Composed via --preset=NAME or per-axis flags.               │  │
└──────────────────────────────────┬───────────────────────────┘  │
                                   │                              │
                                   ▼                              │
┌──────────────────────────────────────────────────────────────┐  │
│  resolve                                                     │  │
│  Snapshot → required_modules + required_configs              │  │
│  modules.builtin.modinfo + modinfo --filename + path-aware   │  │
│  CONFIG_* mapping. Deterministic.                            │  │
└──────────────────────────────────┬───────────────────────────┘  │
                                   ▼                              │
┌──────────────────────────────────────────────────────────────┐  │
│  candidate_trims                                             │  │
│  running .config − required_configs → ~10K candidates        │  │
└──────────────────────────────────┬───────────────────────────┘  │
                                   ▼                              │
┌──────────────────────────────────────────────────────────────┐  │
│  propose (4 dimensions)                                      │  │
│  modules    → autokernel.agent.propose          (=y/=m → =n) │  │
│  choices    → autokernel.agent_dims.propose_choices          │  │
│  toggles    → autokernel.agent_dims.propose_toggles          │  │
│  tunables   → autokernel.agent_dims.propose_tunables         │  │
│                                                              │  │
│  Each is a pydantic-ai Agent with workload+threat recipes    │  │
│  in the prompt, per-batch caching, structured RemovalProposal│  │
│  output. Aggression sets a confidence floor.                 │  │
└──────────────────────────────────┬───────────────────────────┘  │
                                   ▼                              │
┌──────────────────────────────────────────────────────────────┐  │
│  policy filter                                               │  │
│  autonomy + load-bearing blocklist + arch + DKMS gate        │  │
│  → diff.auto_applied + diff.needs_review                     │  │
└──────────────────────────────────┬───────────────────────────┘  │
                                   ▼                              │
                              proposal.json                       │
                                   │                              │
                                   ▼                              │
┌──────────────────────────────────────────────────────────────┐  │
│  review                                                      │  │
│  bulk rules → ReviewSet (accepted/rejected/deferred)         │  │
│  auto_applied items pass through as pre-accepted             │  │
└──────────────────────────────────┬───────────────────────────┘  │
                                   ▼                              │
                              auto.kfrag                          │
                                   │                              │
                                   ▼                              │
┌──────────────────────────────────────────────────────────────┐  │
│  apply                                                       │  │
│  merge_kfrag(running .config, auto.kfrag)                    │  │
│  validate_load_bearing                                       │  │
│  → final.config                                              │  │
└──────────────────────────────────┬───────────────────────────┘  │
                                   ▼                              │
┌──────────────────────────────────────────────────────────────┐  │
│  build (with --localmodconfig)                               │  │
│  drop final.config → source/.config                          │  │
│  strip distro cert paths whose .pem doesn't exist            │  │
│  make olddefconfig                                           │  │
│  yes "" │ make LSMOD=<snap>/lsmod localmodconfig             │  │
│  make olddefconfig (re-canonicalize)                         │  │
│  make -j$(nproc) bzImage modules  (or distro target pkg)     │  │
└──────────────────────────────────┬───────────────────────────┘  │
                                   ▼                              │
                          bzImage + modules                       │
                                   │                              │
                                   ▼                              │
┌──────────────────────────────────────────────────────────────┐  │
│  boot-test                                                   │  │
│  virtme-ng (host's ro / via virtio-fs) OR QEMU kernel-only   │  │
│  → boot-test.json {verdict_ok, duration_seconds, ...}        │  │
└──────────────────────────────────┬───────────────────────────┘  │
                                   ▼                              │
                          (with --execute) install                │
                                                                  │
─── OR ────────────────────────────────────────────────────────  │
                                                                  │
┌──────────────────────────────────────────────────────────────┐  │
│  iterate (closed loop, v0.14 → v0.16)                        │  │
│  for round in 1..N:                                          │  │
│    history block summary (proposals + fitness trend)         │  │
│      → iter_dir/history.txt                                  │  │
│    propose --history-from=iter_dir/history.txt               │  │
│            --base-config=i(N-1)/post_build.config            │◄─┘
│      (← v0.16: post_build.config not final.config — what     │
│       olddefconfig actually settled on)                      │
│    review + apply                                            │
│    config_check (← v0.16: catches hallucinations,            │
│                  dead-letter choices, out-of-range tunables) │
│    build --target=kernel-only --execute                      │
│      (← v0.15.1: kernel-only skips packaging deps;           │
│       v0.16.3: CC=clang on argv, not just env)               │
│    boot-test                                                 │
│    measure (size, time, what landed, what got stripped)      │
│    record → iterations/iN/record.json                        │
│    converged-on-size? → break                                │
│    regressed? → auto-revert + add do-not-repeat to history   │
└──────────────────────────────────────────────────────────────┘

   And in parallel — `autokernel minitram` (v0.16.2):                
                                                                     
   Snapshot evidence → MinitramPlan → cpio.zst (~3-5 MB)             
     boot.luks_in_chain  → cryptsetup tool + dm_crypt module         
     boot.root_fstype    → fs module (e.g. btrfs.ko)                 
     block_devices LVM   → lvm tool + dm_mod module                  
     block_devices RAID  → mdadm tool + raid* modules                
     dkms list           → each DKMS module's .ko                    
     plus busybox-static + a generated /init shell script            
   Pure deterministic (no LLM in hot path).                          
```

## Key data structures

- **`Snapshot`** (`models.py`) — typed result of `scan`. PCI/USB/
  modaliases/loaded_modules/mounts/network/firmware/dkms +
  `BootContext`/`KernelInfo`/`CpuInfo`. Frozen pydantic models.

- **`OptimizationContext`** (`optimize_context.py`) — the four-axis
  intent. `workload × threat × modules × aggression`. Composed via
  `--preset=NAME` or per-axis flags. `Aggression` enum's
  `confidence_floor` property gates which proposals make it through.

- **`KconfigSurface`** (`kconfig_walk.py`) — what's in the target
  kernel's Kconfig: choices (with options), toggles (bool), tunables
  (int/string). Built by walking the source tree's Kconfig via
  `kconfiglib`. Tolerant of post-6.19 keywords (`transitional`,
  `modules`) the released kconfiglib doesn't yet parse.

- **`RemovalProposal`** (`models.py`) — one proposed change. The
  workhorse that flows through propose → review → apply. Since v0.13
  it carries non-trim values: choice options (`proposed_value` is
  the bare option name → kfrag emits `=y`), bool toggles, int/string
  tunables. `ProposalSource` discriminates: DETERMINISTIC, MICROARCH,
  LLM, CHOICE, TOGGLE, TUNABLE, USER.

- **`IterationRecord`** (`iteration.py`) — one round of the closed
  loop. ctx_summary + proposals + measurements + regressed flag.
  Persisted to `<snap>/iterations/i<NNN>/record.json`.
  `summarize_history_for_prompt(target=...)` renders a compact text
  block including a **fitness trend** (`i1=18.0MB → i2=16.8MB`) and
  per-direction guidance ("Kernel has GROWN — favor proposals that
  reduce binary size") that gets fed to the next round's agents.

- **`MinitramPlan`** (`minitram.py`) — composition plan for a
  per-host initramfs. Lists tools (cryptsetup, lvm, mdadm, optional
  dropbear) with their ldd-resolved libs, kernel modules from
  `/lib/modules/<release>/...`, and a generated `/init` shell
  script. Built by `plan(snapshot)`; packed to `cpio.zst` by
  `build(plan)`.

- **`BuildResult`** (`build.py`) — result of `build()`. Now carries
  `target` and `bzimage_path` so `BuildResult.ok` can correctly
  report success for `--target=kernel-only` (no .deb needed —
  bzImage existence is the success criterion).

## Per-batch caching

Every LLM call is content-addressed. `(model, service_tier,
batch_signature[, history_text])` → `sha256()[:16]`. Result persists
to `<snap>/batches/<dim-N>/<key>.json`. Re-running propose pays for
nothing already computed — restart-friendly, no rate limits.

When closed-loop iterate runs, the history block is hashed into the
key so different rounds don't share cached responses.

## Conflict resolution between axes

When the four axes disagree on a symbol:

| Symbol kind | Axis that wins | Why |
|---|---|---|
| Security mitigations (PTI, RETPOLINE, INIT_ON_FREE) | **threat** | KSPP/KSPP+ is the source of truth |
| Performance choices (PREEMPT, HZ, IOSCHED, NUMA) | **workload** | Workload-specific recipes |
| `=y` vs `=m` for tristates | **modules** | Build composition is its own dimension |
| Confidence floor for ALL proposals | **aggression** | A meta-axis that gates the others |

This is documented in the system prompt for each agent so the LLM
applies it consistently. The `OptimizationContext.render_for_prompt()`
embeds the rule into every batch's evidence block.

## Validation gates (defense in depth)

```
candidate → propose → policy filter → review → kfrag → apply  → check  → build  → boot-test
                          │                                  │
                          ▼                                  ▼
                   load-bearing blocklist           catches LLM hallucinations
                   DKMS gate                         dead-letter choices
                   risk threshold                    out-of-range tunables
```

Each gate has a single concern and writes its findings to disk for
post-hoc inspection. No silent drops.

## Subprocess model

The CLI verbs (`scan`, `propose`, `review`, `apply`, `build`,
`boot-test`, `install`, `iterate`, `minitram`) shell out to each
other when composition is needed. That's why `iterate` is a thin
orchestrator:

```python
# iterate's body, simplified:
for n in range(1, N+1):
    write_history_block(iter_dir/n/'history.txt')
    subprocess.run(['autokernel', 'propose', ...,
                    '--history-from=iter_dir/history.txt',
                    '--base-config=i(N-1)/post_build.config'])  # v0.16
    copy iter_dir/proposal.json → snap_dir/proposal.json  # v0.15.1 wiring
    subprocess.run(['autokernel', 'review', ...])
    subprocess.run(['autokernel', 'apply', ...])
    snapshot final.config → iter_dir/final.config  # for chaining
    run config_check(final.config, kconfig_surface)  # v0.16
    if execute:
        subprocess.run(['autokernel', 'build', '--execute',
                        '--target=kernel-only',                # v0.15.1
                        '--compiler=clang', ...])               # v0.16.3
        subprocess.run(['autokernel', 'boot-test', ...])
        snapshot kernel_source/.config → iter_dir/post_build.config
    measure_and_record(...)
```

This keeps each verb independently runnable + testable, at the cost
of subprocess overhead and `PYTHONUNBUFFERED=1` plumbing for live
progress (each step transition logs to `iter_dir/progress.log`).

## Why pydantic-ai

- **Structured output** forces the LLM to commit to a schema. No
  natural-language postprocessing.
- **Typed deps** (planned for v0.17+) for tool access backed by
  Snapshot — agent can ask "is module X loaded?" without giving it
  raw filesystem access.
- **Provider-agnostic** — we resolve `--llm-mode={auto,cheap,fast,
  quality}` to a model from whichever provider the user has keys for
  (`autokernel.llm.resolve()`).

## Compiler plumbing (v0.16.3)

A real clang build on Meteor Lake found that setting `CC=clang` in
the env isn't enough — the kernel's top-level Makefile reassigns CC,
shadowing the env variable. The fix:

```python
# build._compiler_make_vars(compiler) returns:
#   "clang" → ["CC=clang", "HOSTCC=clang"]
#   "llvm"  → ["LLVM=1"]
#   "gcc"   → ["CC=gcc", "HOSTCC=gcc"]
# These get spliced into every make invocation as command-line
# variables (Kbuild honors them) — not just env (which Kbuild
# overwrites for CC).
argv = ["make", f"-j{jobs}", *compiler_vars, *targets]
```

Same pattern applies to olddefconfig + localmodconfig within
`prepare()`. Live verified: `vmlinux` strings show
`Ubuntu clang version 21.1.8` after the fix; before, gcc was
silently winning despite `--compiler=clang`.
