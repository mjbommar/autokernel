# LLM Efficiency Plan

This project should not ask an LLM to rediscover facts the host can expose
directly. The correct shape is:

1. Collect rich local evidence from `/proc`, `/sys`, initramfs, boot config,
   firmware logs, module metadata, and Kconfig.
2. Classify the easy cases deterministically.
3. Send the LLM only compact, structured records where local evidence leaves a
   real policy or inference question.
4. Require the LLM to cite the evidence record it used.

## Current State

The bounded Kconfig dimensions are close to this design:

- `src/autokernel/agent_dims.py` uses separate pydantic-ai agents for choices,
  toggles, and tunables.
- Choices, toggles, and tunables are now allowlisted to high-impact knobs plus
  active workload/threat recipe entries.
- These dimensions need kernel source, walk Kconfig, include current values,
  and ask the LLM for actual policy/sizing tradeoffs.

The module-trim path is not yet in the right shape:

- `src/autokernel/resolve.py:candidate_trims()` returns every enabled `=y` or
  `=m` symbol not proven required.
- `src/autokernel/agent.py` receives batches of mostly anonymous `CONFIG_*`
  symbols plus a general evidence summary.
- The pydantic-ai agent is schema-bound and cached, but it has no tools and no
  per-candidate evidence records.
- `--max-candidates` only controls spend. It defaults to unlimited, does not
  improve the candidate set, and should not be treated as an optimization
  strategy.

On this machine, the current snapshot shape is:

- loaded modules: 280
- modaliases: 331
- bound drivers: 251
- firmware loads: 195
- enabled running config symbols: 10,017 (`m`: 6,680, `y`: 3,337)
- required modules after deterministic resolution: 350
- required configs after deterministic resolution: 189
- unresolved modules: 138
- unresolved modaliases: 232
- broad trim candidates: 9,831
- module-backed broad candidates from `modules.dep`: about 3,450

That explains the bad LLM count: the LLM is being asked about the complement
of the distro config, not a host-derived ambiguity set.

## Target Design

The module-trim path should produce a `ModuleReviewRecord` list, not a raw
symbol list.

Each record should have:

- `config`: resolved `CONFIG_*`
- `current_value`: `y` or `m`
- `module`: module name when applicable
- `source_path`: kernel source path from `modules.dep`, `modules.builtin`, or
  `modinfo`
- `subsystem`: normalized bucket such as `drivers/media`, `drivers/net/wireless`,
  `fs`, `net/netfilter`, `crypto`
- `local_status`: `keep`, `drop`, or `review`
- `local_reason`: deterministic classifier reason
- `evidence_ids`: stable references into collected evidence
- `risk`: boot/storage/net/console/security risk bucket

Only records with `local_status=review` should reach the LLM. Records with
`keep` feed the load-bearing policy. Records with `drop` can become
deterministic proposals when confidence is high enough.

## Always-LLM Knobs

These are policy/sizing choices that should always be eligible for the LLM
when present in the target Kconfig surface:

- CPU and architecture: native CPU/microarch, generic CPU, mitigations,
  speculation controls, microcode vendor.
- Scheduler and latency: preemption mode, dynamic preemption, timer frequency,
  autogroup, SMT/MC scheduler knobs.
- Capacity sizing: `NR_CPUS`, log buffer sizing, CPU masks, RCU offload
  defaults where present.
- Memory policy: THP, multi-gen LRU, zswap, NUMA balancing, hugetlbfs.
- Networking policy: BPF/JIT, unprivileged BPF, XDP, fq/fq_codel, BBR.
- Security posture: lockdown, module signatures, init-on-alloc/free,
  hardened usercopy, slab freelist hardening/randomization, Landlock/Yama,
  strict devmem, legacy ABI toggles.
- Laptop/desktop usability: hibernation, suspend, runtime PM, ACPI battery/fan,
  platform profile, backlight, ASPM, USB autosuspend.
- Build/package tradeoffs: kernel compression, optimize for size/performance,
  debug info, local version.

These knobs are what the LLM is good at: resolving workload, threat, power,
latency, and size tradeoffs from structured context.

## Evidence To Collect Or Preserve

Already collected and parsed:

- loaded modules from `lsmod`
- `/sys/devices/**/modalias`
- bound drivers from `/sys/bus/*/devices/*/driver`
- PCI devices and loaded/candidate PCI modules from `lspci -vmmnk`
- USB inventory from `lsusb`
- mounts and root/boot filesystems
- `lsblk -J -O`
- firmware from dmesg/journal and `modinfo -F firmware`
- initramfs modules and firmware
- boot context, cmdline, Secure Boot, DKMS

Gaps to close:

- Preserve `/proc/modules` refcounts and dependency edges as first-class
  evidence, not just parsed `lsmod` text.
- Map each bound sysfs device to its backing module when available:
  `/sys/.../driver/module`, `/sys/module/<name>/drivers/*`, and PCI/USB driver
  names.
- Parse network drivers from `/sys/class/net/<iface>/device/driver`, not only
  `ip -j link` `info_kind`, which describes virtual link type for many devices.
- Preserve block root chain from `findmnt -J`, `lsblk -J -O`, holders/slaves,
  `root=`, `resume=`, LUKS, LVM, MD RAID, NVMe, VMD, and filesystem modules.
- Parse dmesg/journal for probe success/failure, firmware loads, and hardware
  family strings into evidence records rather than a flat firmware list.
- Preserve negative hardware facts: no PCI vendor/class, no USB class, no DMI
  match, no active mount type, no active netdev class.
- Emit a candidate report artifact so users can see why a symbol was kept,
  dropped, reviewed, or ignored.

## Deterministic Classifiers

The first classifier should split module candidates into these buckets:

- `KEEP_LOADED`: module is loaded now.
- `KEEP_REFCOUNTED`: module has refcount or dependency edges in `/proc/modules`.
- `KEEP_BOUND`: module or driver is bound to a sysfs device.
- `KEEP_INITRAMFS`: module is present in initramfs.
- `KEEP_BOOT_PATH`: block, crypto, filesystem, EFI, console, keyboard, or root
  chain evidence depends on it.
- `KEEP_NET_ACTIVE`: active network interface depends on it.
- `KEEP_FIRMWARE`: firmware evidence points at the module/family.
- `DROP_WRONG_VENDOR`: CPU/GPU/NIC/storage vendor contradicts the symbol.
- `DROP_ABSENT_BUS_CLASS`: candidate belongs to an absent physical bus/class.
- `DROP_BLACKLISTED`: cmdline explicitly blacklists the module.
- `REVIEW_OPTIONAL_HARDWARE`: shipped module for optional physical hardware but
  absence evidence is incomplete.
- `REVIEW_OPTIONAL_PROTOCOL`: netfilter, scheduler, filesystem, crypto, or
  protocol symbol where workload/threat matters.

Only `REVIEW_*` should be sent to the module LLM.

## pydantic-ai Rework

Revise `src/autokernel/agent.py` from a raw symbol batch agent into a structured
review agent:

- Input model: `ModuleReviewBatch(records: list[ModuleReviewRecord],
  evidence_summary: EvidenceSummary, policy: OptimizationContext)`.
- Output model: `ModuleReviewDecision(config, decision, reason, risk,
  confidence, cited_evidence_ids)`.
- Prompt rule: if no cited evidence id supports removal, return `keep`.
- Cache key: include record content, model, prompt version, service tier, and
  workload/threat/module policy.
- Batch by token weight, not just `60 symbols`; structured records vary in size.
- Reject hallucinated symbols, invalid evidence IDs, and decisions that
  contradict `KEEP_*` statuses.

Tooling can be added later with pydantic-ai tools, but pre-materialized evidence
should remain the default because it is testable and cacheable.

## Candidate Count Goal

For normal laptops/desktops:

- deterministic keep set: hundreds
- deterministic drop set: hundreds to low thousands, no LLM needed
- module LLM review set: target under 500 without using a hard cap
- always-LLM policy/sizing knobs: under 150 combined choices/toggles/tunables

`--max-candidates` remains useful as an explicit safety valve for API cost, but
the pipeline should be considered wrong if the natural module review set is
still thousands.

## Implementation Todo

Phase 1: make the current hardware smoke path sane.

- Default `scripts/hardware-reboot-smoke.sh` to
  `--dimension choices,toggles,tunables`.
- Use `build --localmodconfig` for the first hardware boot's module reduction.
- Keep module LLM available behind `--dimension modules` or `--dimension all`.
- Make CLI reporting clear when module LLM was not requested.
- Treat `--max-candidates` as a cost guard in docs, not as the design answer.

Phase 2: evidence model.

- Add pydantic evidence models for modules, devices, filesystems, block chains,
  firmware, and negative hardware facts.
- Extend `scripts/collect.sh` to collect sysfs module links and netdev driver
  links explicitly.
- Extend `snapshot.py` and `models.py` to parse and preserve those records.
- Add `autokernel evidence report SNAPSHOT` or a JSON artifact from `propose`.

Phase 3: deterministic candidate classification.

- Add `autokernel.module_candidates` with:
  - module inventory from `modules.dep`, `modules.builtin`, and Kconfig mapping
  - required/used evidence joins
  - subsystem classification
  - deterministic keep/drop/review statuses
- Add fixture tests for loaded, bound, initramfs, root block, active net, absent
  GPU vendor, absent USB class, and cmdline blacklist cases.
- Add a live count test helper that prints bucket counts without calling an LLM.

Phase 4: structured module LLM.

- Replace raw `(CONFIG, value)` prompt formatting with `ModuleReviewRecord`
  prompt formatting.
- Require evidence ID citations in the pydantic output schema.
- Add validation that demotes/removals cannot cite missing or unrelated evidence.
- Include workload/threat/module strategy in the module-trim prompt.
- Add per-record and per-bucket metrics to the proposal output.

Phase 5: safety and boot loop.

- Feed all `KEEP_*` records into `compute_load_bearing`.
- Block high-risk removals unless explicitly reviewed.
- Persist `candidate-report.json` next to `proposal.json`.
- Add a regression budget: fail or warn if module LLM review count exceeds a
  configured threshold after classification.
- Use boot-test and post-boot scan comparison to promote/demote classifier
  confidence over iterations.

## Acceptance Criteria

- A hardware smoke run does not call the module LLM by default.
- A full `--dimension all` run prints bucket counts before any LLM calls.
- The module LLM receives structured records with evidence IDs, not naked
  symbols.
- On this machine, the natural module LLM review set is below 500 without a
  hard cap.
- Proposal artifacts explain each candidate's path: kept, deterministic drop,
  LLM reviewed, or ignored.
- Tests cover both candidate counts and load-bearing safety behavior.
