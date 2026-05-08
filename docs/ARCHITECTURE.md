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
│  iterate (closed loop, v0.14)                                │  │
│  for round in 1..N:                                          │  │
│    propose --history-from=iN.txt --base-config=i(N-1)/final  │◄─┘
│    config_check (catches LLM hallucinations)                 │
│    review + apply                                            │
│    build (with --execute)                                    │
│    boot-test                                                 │
│    measure (size, time, what landed, what got stripped)      │
│    record → iterations/iN/record.json                        │
│    converged-on-size? → break                                │
│    regressed? → auto-revert + add do-not-repeat to history   │
└──────────────────────────────────────────────────────────────┘
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
  `summarize_history_for_prompt()` renders a compact text block fed
  to the next round's agents.

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
`boot-test`, `install`, `iterate`) shell out to each other when
composition is needed. That's why `iterate` is a thin orchestrator:

```python
# iterate's body, simplified:
for n in range(1, N+1):
    write_history_block(iter_dir/n/'history.txt')
    subprocess.run(['autokernel', 'propose', ..., '--history-from=...'])
    subprocess.run(['autokernel', 'review', ...])
    subprocess.run(['autokernel', 'apply', ...])
    if execute:
        subprocess.run(['autokernel', 'build', '--execute', ...])
        subprocess.run(['autokernel', 'boot-test', ...])
    measure_and_record(...)
```

This keeps each verb independently runnable + testable, at the cost
of subprocess overhead and PYTHONUNBUFFERED=1 plumbing for live
progress.

## Why pydantic-ai

- **Structured output** forces the LLM to commit to a schema. No
  natural-language postprocessing.
- **Typed deps** (we plan to use deps in v0.15+) for tool access
  backed by Snapshot — agent can ask "is module X loaded?" without
  giving it raw filesystem access.
- **Provider-agnostic** — we resolve `--llm-mode={auto,cheap,fast,
  quality}` to a model from whichever provider the user has keys for
  (`autokernel.llm.resolve()`).
