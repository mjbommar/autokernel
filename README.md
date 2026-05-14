# autokernel

LLM-assisted minimal Linux kernel builder.

`autokernel` probes a real host, combines deterministic system evidence
with bounded LLM judgment, writes a reviewed Kconfig fragment, builds a
distro-native kernel package, boot-tests it in a VM, and can install it
for a one-shot GRUB probation boot.

It is inspired by Gentoo's `localmodconfig`, FreeBSD's `include GENERIC`
+ diff style, and Debian's `make bindeb-pkg`, but the goal is broader:
make a per-machine kernel that is smaller, still bootable, and explainable.

[![tests](https://github.com/mjbommar/autokernel/actions/workflows/test.yml/badge.svg)](https://github.com/mjbommar/autokernel/actions/workflows/test.yml)
[![validation](https://github.com/mjbommar/autokernel/actions/workflows/validation.yml/badge.svg)](https://github.com/mjbommar/autokernel/actions/workflows/validation.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## What it optimizes

- **Hardware fit:** keep the drivers, firmware paths, filesystems, and boot
  features this host actually needs.
- **LLM-bounded choices:** ask the LLM about small, typed Kconfig decision
  sets such as preemption, timer frequency, CPU tuning, hardening toggles,
  and sizing tunables.
- **Module surface:** use live system evidence and `localmodconfig` to avoid
  building thousands of unused loadable modules.
- **Safe install path:** build distro packages, boot-test the kernel, install
  with sudo only when needed, and arm GRUB for a one-shot probation boot.

Measured on a Meteor Lake Ubuntu laptop, one autokernel build shipped **351
module files instead of 6,906** and enabled **2,845 Kconfig symbols instead
of 10,056** versus the stock Ubuntu 7.x kernel. A recent high-impact Linux
CVE review showed **4 strong exposure reductions, 2 partial reductions, and
4 unchanged core-kernel exposures** for that build. Details and caveats are
in [Measured example](#measured-example).

## Status

Current focus: closed-loop optimization with `clang` as the default compiler,
per-batch LLM caching, VM boot-test gating, NVIDIA DKMS handling during
install, and per-host minimal initramfs generation via `autokernel minitram`.
LTO is opt-in with `--lto={thin,full}`.

Project references:
[roadmap](docs/ROADMAP.md),
[architecture](docs/ARCHITECTURE.md),
[agent design](docs/AGENTS.md),
[LLM efficiency plan](docs/LLM_EFFICIENCY_PLAN.md), and
[PGO design](docs/PGO.md).

## Install

```bash
# One command — installs uv if needed, clones, syncs, drops a shim on PATH.
curl -LsSf https://raw.githubusercontent.com/mjbommar/autokernel/master/install.sh | bash

# After it finishes:
~/.local/bin/autokernel preflight
```

The installer is non-destructive: never `sudo`s, never touches `/etc` or
`/boot`, everything lands under `$HOME`.

Then **one verb sets up the rest of the host** — distro-aware,
idempotent, dry-run by default:

```bash
autokernel install-deps                          # see what would be installed
autokernel install-deps --for build --execute    # build deps only
autokernel install-deps --execute                # everything (build + boot-test + install)
```

System packages go via your distro's package manager (`apt` / `dnf` /
`pacman` / `zypper` — sudo is invoked transparently). Optional Python
tools like `virtme-ng` install via `uv tool install` (no sudo, isolated
env). The verb only runs what's actually missing.

For development, clone and `uv sync`:

```bash
git clone https://github.com/mjbommar/autokernel
cd autokernel
uv sync
cp .env.example .env  # add ANTHROPIC_API_KEY, OPENAI_API_KEY, or GOOGLE_API_KEY
uv run autokernel preflight
```

## Easy quickstart: hardware-minimal kernel

From a clone, this is the easiest path to reproduce the kind of
LLM-optimized, host-minimal kernel described above:

```bash
uv sync --frozen
uv run python scripts/hardware-reboot-smoke.py
```

The script uses a work directory under
`~/.local/share/autokernel/hardware-boot/`, not `/tmp`, then runs the full
safe pipeline:

1. preflight the host and install missing build/test dependencies
2. scan hardware, software, firmware, DKMS, boot, and audio evidence
3. fetch matching kernel source
4. ask the LLM only about bounded Kconfig dimensions:
   `choices,toggles,tunables`
5. apply deterministic keep rules and reviewed proposals
6. build with `clang` and `--localmodconfig`
7. boot-test the freshly built kernel in a VM

By default it **does not install anything into `/boot`**. After the VM
boot-test passes, install and arm a one-shot GRUB boot:

```bash
uv run python scripts/hardware-reboot-smoke.py --install --no-deps
```

To install and immediately reboot into the new kernel once:

```bash
uv run python scripts/hardware-reboot-smoke.py --install --reboot --yes --no-deps
```

If the new kernel boots, make it permanent:

```bash
autokernel install ~/.local/share/autokernel/hardware-boot/snapshot --commit --execute
```

If it fails, GRUB should fall back to the previous default. Then run:

```bash
autokernel rollback ~/.local/share/autokernel/hardware-boot/snapshot --execute
```

The bash wrapper runs the same flow if you prefer shell:

```bash
bash scripts/hardware-reboot-smoke.sh
```

For NVIDIA systems, install defaults to `--nvidia=auto`: autokernel detects
NVIDIA hardware/driver packages, installs the matching DKMS package, builds
the driver for the custom kernel release, verifies `nvidia.ko`, and refreshes
the initramfs before arming GRUB. Use `--nvidia=open` or
`--nvidia=proprietary` to force a flavor, or `--nvidia=off` to disable this
handling.

For laptops/desktops, the scan now classifies audio usefulness from DMI,
PCI audio, ALSA devices, SOF/HDA/SoundWire modules, PipeWire/WirePlumber,
and Bluetooth/USB hotplug signals. Useful audio is treated as load-bearing
so `localmodconfig` does not silently remove codec or headset support.

## Development validation

```bash
uv run pre-commit run --all-files
scripts/validate-docker.sh
scripts/validate-qemu.sh /path/to/linux-source  # uses arch/x86/boot/bzImage
scripts/qemu-busybox-shell.sh /path/to/linux-source  # interactive BusyBox shell
```

For more detail on the hardware flow, see
[docs/HARDWARE_BOOT.md](docs/HARDWARE_BOOT.md).

## Measured example

This is a real hardware smoke-test result, not a universal promise. The
point of the comparison is to make the tradeoff visible: autokernel removes
large amounts of unused module surface, but core kernel bugs and user-facing
hardware still need patching and policy.

| Metric | Ubuntu stock 7.x | autokernel | Reduction |
|---|---:|---:|---:|
| Module files shipped | 6,906 | 351 | 94.9% fewer |
| Module tree size | 161.2 MB | 72.9 MB | 54.8% smaller |
| Enabled Kconfig symbols (`y+m`) | 10,056 | 2,845 | 71.7% fewer |
| Module Kconfig symbols (`m`) | 6,712 | 342 | 94.9% fewer |
| Built-in Kconfig symbols (`y`) | 3,344 | 2,503 | 25.1% fewer |
| `vmlinuz` size | 17.3 MB | 15.3 MB | 11.5% smaller |
| `initrd` size | 43.3 MB | 32.5 MB | 25.0% smaller |

CVE exposure is evaluated by subsystem presence, not by counting symbols. In
one recent high-impact Linux CVE sample:

| CVE / class | Affected area | Exposure change in the measured build |
|---|---|---|
| CVE-2026-31431 | AF_ALG / AEAD crypto userspace API | Strong reduction: AEAD userspace API removed |
| Dirty Frag-style ESP/RXRPC bugs | `esp4`, `esp6`, `rxrpc` | Strong reduction: modules/configs removed |
| KSMBD bugs | in-kernel SMB server | Strong reduction: `ksmbd` removed |
| EROFS bugs | EROFS filesystem | Strong reduction: EROFS removed |
| CVE-2024-1086, CVE-2023-32233 | `nf_tables` | No meaningful reduction: nftables still needed |
| CVE-2023-0386 | OverlayFS | No meaningful reduction when containers are in use |
| Dirty Pipe / ELF / timers | core kernel paths | No meaningful config reduction |
| USB audio / UVC bugs | hotplug media devices | Workload-dependent: removable on headless hosts, kept on laptops/desktops when useful |

That last row is important. Autokernel now treats laptop/desktop audio as
load-bearing when the snapshot shows PCI audio, ALSA devices, SOF/HDA/
SoundWire modules, PipeWire/WirePlumber, or similar evidence. A smaller
kernel that boots without speakers, microphones, or headsets is not a useful
kernel for most users.

The Docker validation image runs the same static checks and pytest suite
inside Ubuntu 24.04 with QEMU installed:

```bash
docker build -f Dockerfile.validation -t autokernel-validation .
docker run --rm autokernel-validation
```

## Guided CLI quickstart

```bash
autokernel quickstart                # walks you through the whole pipeline
autokernel quickstart -y --skip-llm  # non-interactive, no LLM cost
```

This guided CLI path prompts before each config step
(preflight -> scan -> propose -> review -> apply). It is useful for learning
the verbs or producing `final.config`; use the hardware-minimal quickstart
above when you want a full build, VM boot-test, and one-shot reboot path.
Output lives under `~/.local/share/autokernel/quickstart/` by default.

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

# See what would run, without spending money:
autokernel config show

# Verify the connection with a tiny ping (~$0.001):
autokernel config test

# Run propose with a mode preset instead of a literal model id:
autokernel propose /tmp/snap --llm-mode=cheap     # haiku / gpt-5-mini / gemini-flash
autokernel propose /tmp/snap --llm-mode=quality   # opus / gpt-5 / gemini-2.5-pro

# Or pin a specific model:
autokernel propose /tmp/snap --model=anthropic:claude-opus-4-7
```

When multiple providers are available, autokernel prefers them in this
order: `anthropic`, `openai`, `google-gla`. Override with
`--model <provider>:<id>`.

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
9818 candidate symbol(s) deferred (module-trim LLM cost guard)
wrote /tmp/myhost/proposal.json

# 2b. Workload-aware LLM tuning for bounded Kconfig dimensions:
$ autokernel propose /tmp/myhost \
    --dimension=choices,toggles,tunables --workload=desktop \
    --kernel-source ~/.cache/autokernel/kernels/linux-6.19
CPU tune: Intel Core Ultra 7 165H → CONFIG_X86_NATIVE_CPU=y (microarch: METEORLAKE)
deterministic proposals: 9
choice proposals:  24       # PREEMPT_VOLUNTARY → PREEMPT, HZ_250 → HZ_1000, …
toggle proposals:  3        # X86_AMD_PSTATE: y→n (Intel host), HYPERV: y→n (bare metal), …
tunable proposals: 3        # NR_CPUS: 8192 → 32, LOCALVERSION="", …
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
#     Add --localmodconfig to also disable every module not currently loaded
#     (cuts module count ~6000 → ~250 on stock Ubuntu, build time ~5-10× faster):
$ autokernel build /tmp/myhost --kernel-source ~/.cache/autokernel/kernels/linux-6.19 \
    --localmodconfig
✓ prepared (325 modules, was 6579)

# 6b. Compile (slow — 15-60 min, ~3-5 min with --localmodconfig):
$ autokernel build /tmp/myhost --kernel-source … --execute
✓ built — linux-image-6.19.0_amd64.deb
```

For a real hardware smoke build, prefer `--dimension=choices,toggles,tunables`
plus `build --localmodconfig`: the LLM tunes bounded policy/sizing decisions,
while `localmodconfig` trims modules from live system use. The module-trim LLM
path is still available. By default it is uncapped; set `--max-candidates N`
only as an explicit cost guard.

Cost-sensitive runs: `autokernel propose --skip-llm` produces a deterministic-only
proposal. LLM batches are content-addressed and cached at `<snapshot>/batches/`,
so reruns are free.

## Verbs

| Verb | What it does | Output |
|---|---|---|
| `preflight [DIR] --for=... [--kernel-source PATH]` | Distro detection + system checks (tools, libs, disk, RAM, snapshot/package/boot-test health) | exit code; rendered table |
| `scan [DIR]` | Run bash collectors → typed Snapshot | `DIR/snapshot.json` |
| `propose DIR [--dimension=all] [--workload=…]` | Resolver + deterministic trim + LLM (4 dimensions: modules, choices, toggles, tunables) | `DIR/proposal.json` |
| `inventory scan SOURCE --out DIR` | Build a source-derived Kconfig inventory for LLM tools | `DIR/manifest.json`, `DIR/symbols.jsonl` |
| `inventory enrich DIR [--jobs N]` | Enrich inventory records with evidence-cited summaries (`openai:gpt-5.4-mini`, flex by default; resumable by `symbol + fact_hash`) | `DIR/enrichments.jsonl` |
| `review DIR --rules…` | Bulk-decision rules over `needs_review` | `DIR/review.json` + `DIR/auto.kfrag` |
| `apply DIR` | Merge kfrag into running `.config`, validate load-bearing | `DIR/final.config` |
| `fetch-source [--method=…]` | Distro-aware kernel source acquisition | a kernel source tree |
| `build DIR --kernel-source PATH [--compiler=clang] [--lto=thin] [--target=kernel-only] [--localmodconfig] [--execute]` | Drop config + olddefconfig; `--localmodconfig` trims modules to host's lsmod; `--target=kernel-only` skips packaging (just `make bzImage modules`); `--execute` compiles | logs + (`--execute`) bzImage / `.deb`/`.rpm`/`.tar.zst` |
| `iterate DIR --kernel-source PATH [--preset=NAME] [--max-iterations=N] [--target=size]` | Closed-loop optimizer — propose → check → apply → build → boot-test for N rounds; history (with fitness trend) feeds the next round | `DIR/iterations/i<NNN>/` per round |
| `minitram DIR [--dropbear] [--execute]` | Build a per-host minimal initramfs from snapshot evidence (LUKS / LVM / RAID / DKMS / fs detected). Pure deterministic | `DIR/initramfs.cpio.zst` (~3-5 MB) |

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
   │                  ┌── workload detection  ◄── kconfig walker ◄── kernel source
   │                  │     ▼                                                 │
   │                  │   v0.13 dimension agents (per-batch cached) ──────────┤
   │                  │     ├─ propose_choices  (PREEMPT, HZ, IOSCHED, …)     │
   │                  │     ├─ propose_toggles  (THP, BPF_JIT, NUMA_BALANCING)│
   │                  │     └─ propose_tunables (NR_CPUS, LOG_BUF_SHIFT)      │
   │                  │                                                       │
   │                  │                                          policy filter│
   │                  │                                          (autonomy +  │
   │                  │                                          load-bearing │
   │                  │                                          + DKMS gate) │
   ▼                                                  ▼                       │
   not_considered                              proposal.json ◄─────────────── │
                                                       │
                       review rules ──────────────────┤
                                                       │
                            auto.kfrag ── apply ──> final.config
                                                       │
                                              build (--localmodconfig)
                                                       │
                                                  bzImage + modules
                                                       │
                                              boot-test (QEMU/virtme)
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

## Closed-loop iteration *(v0.14)*

`autokernel iterate` runs the full pipeline as a loop: propose →
config-check → apply → build → boot-test → measure → record. Each
round's measurements feed back into the next propose call as prompt
context, so the LLM stops re-proposing things that broke prior boots
and converges on a stable, smaller, faster kernel.

```bash
autokernel iterate ~/build/snap \
    --kernel-source ~/.cache/autokernel/kernels/linux-6.19 \
    --preset=lean-static --workload=desktop \
    --max-iterations=3 --target=size --execute
```

Each iteration's artifacts land in `~/build/snap/iterations/i<NNN>/`
(proposal.json, final.config, build.log, record.json). When an
iteration regresses (build fails or boot-test fails), `--auto-revert`
*(default)* restores the prior `final.config` and feeds the failed
proposals into the next round's prompt as do-not-repeat rules.

### Four-axis intent

| Axis | Levels | What it controls |
|---|---|---|
| `--workload` | desktop / laptop / server / vm-guest / realtime / embedded | Perf-axis recipes (PREEMPT, HZ, IOSCHED, NUMA_BALANCING) |
| `--threat` | permissive / balanced / paranoid | KSPP-aligned hardening (PTI, RETPOLINE, INIT_ON_FREE, KFENCE) |
| `--modules` | distro / monolithic / modular | =y vs =m composition (initramfs vs built-in) |
| `--aggression` | conservative / balanced / aggressive | Confidence floor for proposals (0.85 / 0.65 / 0.40) |

### Presets

Common combinations have short names. Per-axis flags override the preset.

```bash
autokernel propose ~/build/snap --preset=gaming-desktop ...
# = --workload=desktop --threat=permissive --modules=monolithic --aggression=aggressive

autokernel propose ~/build/snap --preset=hardened-server ...
# = --workload=server --threat=paranoid --modules=monolithic --aggression=balanced
```

Available: `desktop`, `gaming-desktop`, `paranoid-desktop`, `laptop`,
`paranoid-laptop`, `server`, `hardened-server`, `cloud-vm`, `realtime`,
`embedded`, `lean-static`, `lean-module`, `hyperoptimize`.

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
- **`build`** — disk, RAM, build tools (`gcc make flex bison bc ld perl awk tar`), recommended (`ccache pahole`), dev libs (`libssl-dev libelf-dev libdw-dev libncurses-dev` or distro equivalents), distro package deps (`debhelper`, `llvm`, etc.), Secure Boot.
- **`boot-test`** — QEMU/virtme availability. With `--kernel-source PATH`, also warns when the built `.config` cannot support virtme's host-backed rootfs.
- **`install`** — GRUB tools, root/sudo, `/boot` writable, fallback kernel presence, installable package discovery, and boot-test record state.

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
| **virtme-ng** (preferred) | `uv tool install virtme-ng` | Boots the kernel against the host's read-only `/` over virtio-fs. Reaches userspace. |
| **QEMU kernel-only** (fallback) | `autokernel install-deps --for boot-test --execute` (or `apt install qemu-system-x86` directly) | Boots the kernel with no rootfs. Success = kernel reaches the VFS-mount stage without an earlier panic. |

`virtme-ng` requires the built kernel to include either `CONFIG_VIRTIO_FS`
or the 9P stack (`CONFIG_NET_9P`, `CONFIG_NET_9P_VIRTIO`, `CONFIG_9P_FS`).
`--localmodconfig` can trim those out if they are not loaded on the host.
Run `autokernel preflight SNAPSHOT --for boot-test --kernel-source PATH`
to surface that before a boot-test.

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
