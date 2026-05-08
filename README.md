# autokernel

LLM-assisted minimal Linux kernel builder. Probe a host, propose a trim,
review with bulk rules, merge into a final `.config`, and build a
distro-native kernel package — all from one CLI. Multi-distro:
Debian/Ubuntu, Fedora/RHEL, Arch, openSUSE, Gentoo, Alpine.

Inspired by Gentoo's `localmodconfig`, FreeBSD's `include GENERIC` + diff
style, and Debian's `make bindeb-pkg`.

> **Status: 0.11.** Full pipeline + CPU microarch tuning + LLM
> auto-detection + **boot-test in a VM before installing**. Build →
> verify in QEMU/virtme-ng → install. `autokernel install --execute`
> now refuses to proceed without a recent passing boot-test record.

[![tests](https://github.com/mjbommar/autokernel/actions/workflows/test.yml/badge.svg)](https://github.com/mjbommar/autokernel/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Install

```bash
# One command — installs uv if needed, clones, syncs, drops a shim on PATH.
curl -LsSf https://raw.githubusercontent.com/mjbommar/autokernel/master/install.sh | bash

# After it finishes:
~/.local/bin/autokernel preflight
```

The installer is non-destructive: never `sudo`s, never touches `/etc` or
`/boot`, everything lands under `$HOME`.

For development, clone and `uv sync`:

```bash
git clone https://github.com/mjbommar/autokernel
cd autokernel
uv sync
cp .env.example .env  # add ANTHROPIC_API_KEY or OPENAI_API_KEY
uv run autokernel preflight
```

## Easiest path: `quickstart`

```bash
autokernel quickstart                # walks you through the whole pipeline
autokernel quickstart -y --skip-llm  # non-interactive, no LLM cost
```

Prompts before each step (preflight → scan → propose → review → apply).
Hit Enter to accept defaults; Ctrl-C to bail; the failure of any step
prints exactly what to do next. Output lives under
`~/.local/share/autokernel/quickstart/` by default.

## LLM configuration

`autokernel propose` calls a cloud LLM to judge config trims. It
**auto-detects** which provider you have credentials for and picks a
sensible default model — you don't need to memorize pydantic-ai model
ids.

```bash
# Set any one of these in your shell or .env:
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export GOOGLE_API_KEY=...        # or GEMINI_API_KEY
export MISTRAL_API_KEY=...
export GROQ_API_KEY=...
export XAI_API_KEY=...
export DEEPSEEK_API_KEY=...
export OPENROUTER_API_KEY=...

# See what would run, without spending money:
autokernel config show

# Verify the connection with a tiny ping (~$0.001):
autokernel config test

# Run propose with a mode preset instead of a literal model id:
autokernel propose /tmp/snap --llm-mode=cheap     # haiku / gpt-mini / gemini-flash
autokernel propose /tmp/snap --llm-mode=quality   # opus / gpt-5 / gemini-2.5-pro

# Or pin a specific model:
autokernel propose /tmp/snap --model=anthropic:claude-opus-4-7
```

When multiple providers are available, autokernel prefers them in this
order: `anthropic`, `openai`, `google-gla`, `mistral`, `groq`, `xai`,
`deepseek`, `openrouter`. Override with `--model <provider>:<id>`.

## Quick start (manual verbs)

```bash
# 0. Confirm the host has what it needs (~0.5s; distro-aware fix hints):
$ autokernel preflight --for build
host: Ubuntu 26.04 LTS (family=debian)
  ✓ python_version, cpu_cores, free_ram, secure_boot
  ✗ build_tools     missing: flex, bison      → sudo apt install -y bison flex
  ✗ kernel_dev_libs missing: libssl-dev, libelf-dev, libncurses-dev

# 1. Snapshot the host (writes a typed Snapshot to /tmp/myhost):
$ autokernel scan /tmp/myhost
✓ snapshot saved
  pci: 35   usb: 7   modaliases: 331   loaded modules: 259   firmware loads: 195

# 2. LLM-judged trim proposal (deterministic rules + pydantic-ai agent):
$ autokernel propose /tmp/myhost --autonomy advise
auto-applied (8): …      # high-confidence deterministic trims
needs review (17): …     # LLM proposals — tagged risk + confidence
9818 candidate symbol(s) deferred (re-run with --max-candidates higher to widen)
wrote /tmp/myhost/proposal.json

# 3. Bulk-review the proposal — keep crypto/security at current values, accept rest:
$ autokernel review /tmp/myhost \
    --reject-subsystem crypto --reject-subsystem security \
    --accept-recommended
accepted: 12   rejected: 3   deferred: 2
wrote /tmp/myhost/auto.kfrag

# 4. Merge the kfrag into your running .config; refuses to write if a
#    load-bearing symbol would be disabled:
$ autokernel apply /tmp/myhost
  override: 12 symbols (kfrag wins)
wrote /tmp/myhost/final.config

# 5. Acquire kernel source (auto-picks per distro: apt-get source / kernel.org / SRPM / …):
$ autokernel fetch-source --kernel-version 6.13.5
✓ source ready at ~/.cache/autokernel/kernels/linux-6.13.5

# 6a. Prepare the source tree (drop config + run olddefconfig; ~1s):
$ autokernel build /tmp/myhost --kernel-source ~/.cache/autokernel/kernels/linux-6.13.5
✓ prepared

# 6b. Compile (slow — 15-60 min; auto-target = bindeb-pkg / rpm-pkg / targz-pkg):
$ autokernel build /tmp/myhost --kernel-source … --execute
✓ built — linux-image-6.13.5_amd64.deb
```

Cost-sensitive runs: `autokernel propose --skip-llm` produces a deterministic-only
proposal. LLM batches are content-addressed and cached at `<snapshot>/batches/`,
so reruns are free.

## Verbs

| Verb | What it does | Output |
|---|---|---|
| `preflight [DIR] --for=...` | Distro detection + system checks (tools, libs, disk, RAM, snapshot health) | exit code; rendered table |
| `scan [DIR]` | Run bash collectors → typed Snapshot | `DIR/snapshot.json` |
| `propose DIR` | Resolver + deterministic trim + LLM agent → typed proposals | `DIR/proposal.json` |
| `review DIR --rules…` | Bulk-decision rules over `needs_review` | `DIR/review.json` + `DIR/auto.kfrag` |
| `apply DIR` | Merge kfrag into running `.config`, validate load-bearing | `DIR/final.config` |
| `fetch-source [--method=…]` | Distro-aware kernel source acquisition | a kernel source tree |
| `build DIR --kernel-source PATH [--execute]` | Drop config + olddefconfig; with `--execute`, compile | logs + (`--execute`) `.deb`/`.rpm`/`.tar.zst` |

## Pipeline

```
bash collectors  ──>  pydantic Snapshot  ──>  deterministic resolver
   │                                                    │
   │  /proc/cmdline, /sys, lspci, lsusb, lsinitramfs    │  modules.builtin.modinfo +
   │  modinfo per-module firmware                       │    modinfo --filename →
   │  journalctl -k (dmesg fallback)                    │    path-aware CONFIG mapping
   │  DKMS, mokutil, /sys/firmware/efi                  ▼
   │                              required modules + required configs
   │                                                    │
   │                        running .config ─ required ─> candidate trims
   │                                                          │
   │                  ┌── deterministic rules (CPU vendor, GPU) ──┤
   │                  │                                            │
   │                  └── pydantic-ai agent (per-batch cached) ────┘
   │                                                  │
   │                                          policy filter
   │                                          (autonomy + load-bearing
   │                                          blocklist + arch + DKMS gate)
   ▼                                                  ▼
   not_considered                              proposal.json
   (deferred, surfaced — never silently dropped)
                                              │
                       review rules ──────────┘
                                              │
                                    auto.kfrag ── apply ──> final.config
                                                              │
                                                            build
                                                              │
                                                          .deb / .rpm / .tar.zst
```

## Architecture

```
scripts/collect.sh             ── bash; dumb data dumper, no JSON deps
src/autokernel/
    models.py                  ── pydantic types (Snapshot, RemovalProposal, ReviewSet, …)
    snapshot.py                ── parse collector output → Snapshot
    modinfo.py                 ── modules.builtin.modinfo + modinfo --filename
    kconfig_map.py             ── path-aware module → CONFIG_* candidate generator
    resolve.py                 ── modalias→module→CONFIG_* (deterministic)
    policy.py                  ── autonomy levels + load-bearing blocklist
    agent.py                   ── pydantic-ai ConfigMinimizer + per-batch cache
    subsystem.py               ── classify CONFIG_* into ~50 subsystem buckets
    review.py                  ── composable bulk-decision rules → ReviewSet
    kfrag.py                   ── emit/parse Kconfig fragments (.kfrag)
    merge.py                   ── pure-Python kfrag → .config merge + load-bearing check
    build.py                   ── prepare (config + olddefconfig) + build (make <target>)
    distro.py                  ── parse /etc/os-release; per-family DistroSpec
    preflight.py               ── system checks: tools, libs, disk, RAM, snapshot health
    fetch.py                   ── kernel-source acquisition (per-family + kernel.org tarball)
    bootloader.py              ── detect GRUB2 / systemd-boot / rEFInd; per-kind argv recipes
    install.py                 ── distro-aware install plan + one-shot probation
    rollback.py                ── undo the most recent install (record-driven)
    errors.py                  ── shared error/hint helpers (every error has a "→ fix" line)
    quickstart.py              ── guided walk-through verb (preflight → ... → apply)
    tui/                       ── interactive review TUI (Textual)
        state.py               ──   pure working-state + filter cyclers (no Textual deps)
        widgets.py             ──   CountsBar, ProposalTable, EvidencePanel
        app.py                 ──   ReviewApp orchestrator
        review.tcss            ──   styles
    cli.py                     ── typer CLI: 10 verbs (quickstart + 9 pipeline verbs)
install.sh                     ── one-line bootstrap (curl | bash)
.claude/skills/autokernel/     ── thin Claude skill driving the CLI
tests/                         ── 426 tests, fixture-driven, no host coupling
    fixtures/os_release/       ── synthetic distro samples (Ubuntu, Debian, Fedora, RHEL, Arch, …)
    fixtures/intel_laptop/     ── synthetic full-host snapshot
    fixtures/amd_desktop/      ── synthetic NVIDIA + DKMS host
```

### Why this split

| Layer | Form | Reason |
|---|---|---|
| Hardware/sys probe | bash | Native — `lspci`, `lsusb`, `find /sys`. Stable text files; no `jq` dep. |
| Snapshot model | pydantic | Typed boundary between bash and the rest of the world. |
| Module → CONFIG mapping | python (no kernel sources) | Path-aware prefix table + running-config check. Avoids `linux-source-*` dep. |
| Modalias resolution | python | Bus-prefix bucketing → ~10× speedup vs naive fnmatch. |
| Policy / blocklist | python | Determinism > prompts. The hard "don't brick the box" rules are code. |
| Config minimization advice | pydantic-ai agent | Judgment, not arithmetic. Structured output forces calibration. Per-batch cache makes interrupted runs cheap to resume. |
| Distro adaptation | python (`distro.py` + `DistroSpec`) | Per-family knowledge in one table; verbs dispatch on family. |
| Orchestration | typer + Claude skill | Skill stays thin; Python carries the logic. |

## Autonomy levels

| Level | Behavior |
|---|---|
| `explain` | LLM annotates only. No actionable changes. Proposals appear in `annotations`. |
| `advise` *(default)* | LLM proposes; user/Claude approves each. Deterministic proposals at confidence ≥ 0.95 are auto-applied. |
| `auto-safe` | Auto-applies proposals where `risk=low ∧ confidence≥0.9`. |
| `auto-bold` | Auto-applies everything except `risk=high` and a per-snapshot **load-bearing blocklist** (root fs, active NIC, EFI, microcode, LUKS, architecture fundamentals). |

The load-bearing blocklist is enforced regardless of level. **DKMS gate**:
when DKMS modules are present, `auto-safe`/`auto-bold` refuse to run
unless `--force-dkms`.

## review decision rules

Rules are applied in order; the first match decides each proposal.
Anything unmatched stays in `deferred`.

| Flag | Effect |
|---|---|
| `--accept-recommended` | Accept everything not `risk=high`. |
| `--accept-low-risk` | Accept only `risk=low`. |
| `--accept-deterministic` | Accept only `source=deterministic` proposals. |
| `--reject-subsystem X` (repeatable) | Veto a whole subsystem (`crypto`, `security`, `kasan`, `debug`, …). |
| `--reject-pattern GLOB` (repeatable) | Veto by glob (`'CONFIG_DEBUG_*'`). |
| `--interactive` | After bulk rules, open a Textual TUI to step through remaining deferred items. |

The interactive TUI bindings: `a`/`r`/`d` to accept/reject/defer, `j`/`k`
or arrow keys to navigate, `s` to cycle the subsystem filter, `f` to
cycle the view (deferred / all / accepted / rejected), `w` to save and
exit, `q` to quit without saving.

`autokernel apply` enforces an additional **load-bearing safety check**:
if the merge would disable a working symbol that's load-bearing, it
refuses to write `final.config` and reports which symbol would brick.

## Multi-distro support

| Family | Detected `ID` / `ID_LIKE` | Default build target | Default fetch method |
|---|---|---|---|
| **Debian** | `debian`, `ubuntu`, `linuxmint`, `pop`, `kali`, `mx`, … | `bindeb-pkg` | `apt-get source linux` (no root) |
| **Fedora** | `fedora`, `rhel`, `centos`, `rocky`, `almalinux`, `ol`, `amzn` | `rpm-pkg` | kernel.org tarball |
| **Arch** | `arch`, `manjaro`, `endeavouros`, `garuda`, `artix` | `tarzst-pkg` | kernel.org tarball |
| **openSUSE** | `opensuse-*`, `sles`, `sled` | `rpm-pkg` | `zypper install kernel-source` |
| **Gentoo** | `gentoo` | `targz-pkg` | `emerge sys-kernel/gentoo-sources` |
| **Alpine** | `alpine` | `targz-pkg` | kernel.org tarball |
| **Other / unknown** | — | `targz-pkg` | kernel.org tarball |

The package-name knowledge per family lives in `src/autokernel/distro.py`'s
`DistroSpec`. PRs welcome to refine the per-family build-deps list or
add families.

## Pre-flight

`autokernel preflight --for=VERB` runs the relevant subset of checks:

- **Always** — distro recognized, Python ≥ 3.12.
- **`scan`** — `dmesg` readability (degrades to `journalctl -k`).
- **`propose`** — running `.config` and `modules.builtin.modinfo` reachable.
- **`build`** — disk, RAM, build tools (`gcc make flex bison bc ld perl awk tar`), recommended (`ccache pahole`), dev libs (`libssl-dev libelf-dev libncurses-dev` or distro equivalents), Secure Boot.
- **`install`** *(future)* — GRUB tools, root/sudo, `/boot` writable.

Each check returns PASS/WARN/FAIL/SKIP. The CLI exits non-zero on any
FAIL; `--strict` also fails on WARN. Every FAIL/WARN includes a
distro-specific fix hint phrased in the local family's package manager.

## proposal.json / review.json shape

```json
{
  "base_config_path": "/path/to/running_config",
  "autonomy": "advise",
  "auto_applied":   [RemovalProposal, ...],
  "needs_review":   [RemovalProposal, ...],
  "annotations":    [RemovalProposal, ...],
  "blocked":        [[RemovalProposal, "load-bearing reason"], ...],
  "not_considered": ["CONFIG_FOO", ...]
}
```

Buckets are mutually exclusive; together they account for every candidate
symbol. `RemovalProposal` carries `config`, `current_value` /
`proposed_value`, `reason`, `risk` (low/medium/high), `confidence`
(0..1), `source` (deterministic/llm/user), and `evidence`.

## Roadmap

- [x] `scan`, `propose`, `review`, `apply`, `build`, `preflight`, `fetch-source` *(0.1–0.6)*
- [x] Interactive review TUI (Textual) *(0.7)*
- [x] `install --probation` + `rollback` (manual --commit; one-shot grub-reboot) *(0.8)*
- [x] `quickstart` walk-through + centralized error hints *(0.8)*
- [x] CPU microarch tuning: auto-detect Zen 1–5 / Sandy Bridge → Lunar Lake; auto-apply `CONFIG_M<arch>=y` *(0.9)*
- [x] LLM provider auto-detection + `config show / test`; `--llm-mode` preset shorthand; 8 provider families *(0.10)*
- [x] **`boot-test`** verb (QEMU kernel-only + virtme-ng) + `install --execute` gates on a recent passing record *(0.11)*
- [ ] systemd watchdog for auto-promoting after N successful boots (currently `--commit` is manual)
- [ ] systemd-boot / rEFInd support for `install`
- [ ] systemd-boot / rEFInd support for `install`
- [ ] PEP 723 single-file scripts for kernel-dev workflows: bisect, patch series, Kconfig fragment composer.

## CPU microarch tuning

When the host CPU is recognized AND the running kernel ships the
matching `CONFIG_M*` symbol (the symbol's "added-in" version is checked
against `uname -r`), `propose` emits a high-confidence MICROARCH
proposal that gets auto-applied at ADVISE. Examples on a few hosts:

| Host CPU | Detected | Proposed |
|---|---|---|
| Intel Core Ultra 7 165H (family 6 / model 170) | Meteor Lake | `CONFIG_MMETEORLAKE=y` |
| AMD Ryzen 9 7950X (family 25 / model 97) | Zen 4 | `CONFIG_MZEN4=y` |
| AMD Ryzen 7 5800X3D (family 25 / model 33) | Zen 3 | `CONFIG_MZEN3=y` |
| Intel i7-1165G7 (family 6 / model 140) | Tiger Lake | `CONFIG_MTIGERLAKE=y` |
| Intel i7-2600K (family 6 / model 42) | Sandy Bridge | `CONFIG_MSANDYBRIDGE=y` |

Unknown CPUs fall back to leaving `CONFIG_GENERIC_CPU` alone — better
to be generic than wrong. Opt out per-run with `--no-cpu-tune`.

The mapping table lives in `src/autokernel/cpu.py`. PRs welcome for
new microarchitectures.

## Boot-test in a VM before installing

`autokernel boot-test SNAPSHOT --kernel-source PATH` boots the
freshly-built kernel in a VM (5-15 sec) so a broken kernel never
touches your live `/boot`. Two methods, picked automatically:

| Method | Setup | What it tests |
|---|---|---|
| **virtme-ng** (preferred) | `pip install virtme-ng` | Boots the kernel against the host's read-only `/` over virtio-fs. Reaches userspace. |
| **QEMU kernel-only** (fallback) | `apt install qemu-system-x86` (or distro equivalent) | Boots the kernel with no rootfs. Success = kernel reaches the VFS-mount stage without an earlier panic. |

A passing test writes `<snapshot>/boot-test.json` with the bzImage's
SHA-256. `autokernel install --execute` then refuses to proceed unless:

- a `boot-test.json` exists, AND
- its verdict is PASS

Override (you've verified some other way) with `--skip-boot-test`. The
gate is dry-run-friendly: `autokernel install` (no `--execute`) just
nudges you to run boot-test first instead of erroring.

## License

[MIT](LICENSE) © 2026 Michael Bommarito

## Known limits

- Module → CONFIG_ symbol mapping resolves ~60% of modules on a stock Ubuntu kernel via the path-aware prefix table; the rest fall back to "load-bearing by default" (conservative).
- Hot-pluggable hardware never connected won't appear in `/sys/devices/**/modalias`. Mitigation lives in the agent prompt.
- `lsinitramfs` requires read access to `/boot/initrd.img-*`, typically root-only on Ubuntu. The collector degrades gracefully.
- The path-prefix table is a maintained heuristic, not a Kbuild parser. PRs welcome for missing subsystems.
- Subsystem classifier has ~50 buckets; symbols not matched are bucketed as `misc`. Misclassification is a UX paper-cut only — every proposal still receives the same policy treatment.
