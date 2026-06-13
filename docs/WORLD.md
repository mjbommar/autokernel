# autokernel world — source-built Debian, Gentoo-style

Design + implementation plan for `autokernel world`: rebuild the entire
installed package set from Debian/Ubuntu sources with one consistent,
per-host flag set, LLM-managed configuration, and apt-native output.
This supersedes the busybox-assembly sketch of v0.18 in
[ROADMAP.md](ROADMAP.md) for the general case; busybox assembly remains
the embedded (~30 MB) special case.

The one-sentence pitch: **Gentoo's consistency and configurability,
Debian's packaging and security infrastructure, and an LLM in place of
the ebuild-maintainer crowd — with every LLM judgment validated by a
real build and persisted as a rule.**

> **Experiment logs:** results from building and tuning the world image
> live in [experiment/](experiment/README.md) — notably
> [experiment/BOOT_TIME.md](experiment/BOOT_TIME.md), which takes the image
> from a 1.6 s boot to ~240 ms (−85%) and documents the measurement
> harnesses in [scripts/boottime/](../scripts/boottime/README.md).

## Goals

1. Every binary package in the world set is rebuilt from `deb-src`
   sources with a consistent global flag set (`-march=native`, opt
   level, LTO, hardening tier) derived from the same four axes the
   kernel pipeline uses (workload × threat × modules → *flags* ×
   aggression).
2. The result stays a normal Debian/Ubuntu system: apt installs it,
   apt upgrades it, `dpkg -l` shows it. No new package format.
3. LLM agents (pydantic-ai, same patterns as `agent.py`) handle the
   judgment work: world manifest proposal, FTBFS triage, per-package
   feature/profile selection — each validated by a real build before
   anything persists.
4. Security updates are tracked automatically. A rebuilt-from-source
   system that lags DSAs/USNs is worse than stock; the watcher is a
   v1 blocker, not a follow-on.

## Non-goals

- A new package format, init system, or installer (unchanged from
  ROADMAP "not building" list).
- Cross-bootstrap from nothing. We seed build chroots with stock
  binaries (Gentoo stage3 model) and rebuild to a fixpoint.
- USE-flag generality. Debian build profiles where they exist,
  patch overlays for a curated few leaf packages, honesty everywhere
  else.
- Multi-distro at launch. Debian/Ubuntu only (`Family.DEBIAN`); the
  `DistroSpec` seam stays so Fedora (`dnf builddep`/mock) can follow.

## Operating philosophy: the chug

What made the kernel side deliver was not scoping down — it was
letting the LLM make thousands of small decisions in batched,
content-addressed, cached calls; gating them with policy + bulk-rule
review; validating with real builds; and feeding a fitness trend back
into the next round. The world side applies the identical loop to
packages, and three consequences follow:

1. **Every package gets LLM attention, not just failures.** Per-
   package decisions (keep/trim, flag deviations, profiles, risk) are
   a batched dimension pass over the whole closure — the package
   analogue of chugging through ~10K candidate CONFIG_ symbols.
2. **Patience is cheap because everything is cached and resumable.**
   Every LLM decision is content-addressed; every build has a keyed
   record. Killing and restarting a multi-day `world build` re-pays
   for nothing already decided or built. Long runs are a feature,
   not a smell.
3. **The world grows in rings, the machinery never changes.**
   Ring 0 = the `required` set; ring 1 = `required+important`;
   ring 2 = everything installed. Each ring is the same pipeline
   with a larger N. Nothing in the design is "MVP-only."

Measured reference points (June 2026, this host / Alpine 3.24):

| System | Binary pkgs | Source pkgs | Installed |
|---|---|---|---|
| Alpine minirootfs (container base) | 16 | ~10 | 8.4 MB |
| Ubuntu 26.04 `required` (≈ minbase) — **ring 0** | 79 | 53 | 87 MB |
| Ubuntu `required+important` — **ring 1** | 228 | 150 | 278 MB |
| Full installed set (this host) — **ring 2** | ~2,800 | ~1,900 | n/a |

Ring 0 is a 3–6 CPU-hour chug with `nocheck nodoc` (toolchain-gated
sources excepted); ring 2 is days — resumable, so fine. Chimera
Linux's `cbuild` (whole distro, one toolchain, consistent flags,
handful of maintainers) is the existence proof for the end state;
Alpine's `/etc/apk/world` file independently validates the manifest
design. **CachyOS** is the closest commercial-grade proof of the
*payoff*: an Arch derivative shipping its whole repo compiled at
x86-64-v3/v4/Zen4 + LTO, with PGO and BOLT on core packages, plus
CPU-optimized kernels — a real user base validating that optimized
rebuilds of a binary distro are wanted and maintainable. The
differences define our niche: CachyOS centrally builds a few fixed
ISA tiers; autokernel builds per-host (`-march=native`, the host's
own workload for PGO) on Debian/Ubuntu sources, with LLM judgment
replacing their human packaging team — and BOLT (post-link layout
optimization from the same perf profiles AutoFDO uses) slots
naturally into the W8+/PGO arc as the third stage of the
clang/llvm pipeline.

## The pipeline

```
┌────────────────────────────────────────────────────────────────┐
│  world init <snap>                                             │
│  dpkg -l + Snapshot → installed set → world manifest skeleton  │
└─────┬──────────────────────────────────────────────────────────┘
      ▼
┌────────────────────────────────────────────────────────────────┐
│  world propose <snap>            (LLM, cached, axes-driven)    │
│  manifest: package set ± trims, GlobalFlags, per-pkg overrides │
└─────┬──────────────────────────────────────────────────────────┘
      ▼
┌────────────────────────────────────────────────────────────────┐
│  world plan                                                    │
│  deb-src indices → source closure → build order heuristic →    │
│  waves + cost estimate (pkg count, est. CPU-hours, monsters)   │
└─────┬──────────────────────────────────────────────────────────┘
      ▼
┌────────────────────────────────────────────────────────────────┐
│  world build [--pass=1|2]        (dry-run by default)          │
│  per source pkg: fetch → version-suffix (+ak1) → sbuild        │
│  (unshare chroot, ccache, flags via build env) → blhc flag     │
│  audit → publish to local repo                                 │
│     │ FTBFS? → triage agent → override → retry (bounded)       │
│     └ pass 2: chroots seeded from own repo → self-hosted       │
└─────┬──────────────────────────────────────────────────────────┘
      ▼
┌────────────────────────────────────────────────────────────────┐
│  world test                                                    │
│  per-pkg: autopkgtest (own + reverse-deps) in qemu/podman      │
│  system: mmdebstrap image from local repo → QEMU boot +        │
│  workload probe → WorldMeasurements                            │
└─────┬──────────────────────────────────────────────────────────┘
      ▼
┌────────────────────────────────────────────────────────────────┐
│  world adopt / world image                                     │
│  adopt: pin local repo on the running host, apt full-upgrade   │
│  image: mmdebstrap --variant=minbase → rootfs/qcow2/USB        │
└─────┬──────────────────────────────────────────────────────────┘
      ▼
┌────────────────────────────────────────────────────────────────┐
│  world watch                     (systemd user timer)          │
│  deb-src index deltas + DSA/USN feeds → delta rebuild →        │
│  test → publish → notify on failure                            │
└────────────────────────────────────────────────────────────────┘
```

Same validation-gate philosophy as the kernel side: every gate has a
single concern and writes findings to disk. `override_check` (static
sanity of a proposed flag override — does the flag exist, does the
package use dpkg-buildflags, is the override syntactically valid)
plays the role `config_check` plays for kernels: catch hallucinations
*before* the slow build.

## Key data models (`world/models.py`, frozen pydantic)

```python
class GlobalFlags(_Frozen):
    march: str                  # "native" | "x86-64-v3" | explicit
    opt: str                    # "-O2" | "-O3"
    lto: Lto                    # NONE | THIN | FULL  (binutils/gcc -flto=auto)
    compiler: str               # "gcc" (default) | "clang"
    hardening: HardeningTier    # DISTRO_DEFAULT | KSPP_LIKE | PARANOID
    build_options: list[str]    # DEB_BUILD_OPTIONS tokens: nocheck, nodoc, ...
    build_profiles: list[str]   # DEB_BUILD_PROFILES tokens: nodoc, ...

class PackageOverride(_Frozen):
    source_pkg: str
    strip_flags: list[str]      # e.g. ["-flto=auto"] for LTO-incompatible
    add_flags: list[str]
    force_compiler: str | None  # "gcc" for clang-hostile packages
    profiles: list[str]         # per-pkg build profiles (pkg.foo.minimal)
    patches: list[Path]         # quilt patches applied over debian/
    use_stock: bool             # escape hatch: don't rebuild, warn loudly
    reason: str                 # human/LLM rationale — required
    provenance: OverrideSource  # LLM_TRIAGE | USER | PRESET

class WorldManifest(_Frozen):
    schema_version: int
    host: str
    base: BaseRelease           # distro id + suite + mirror + deb-src mirror
    axes: dict[str, str]        # OptimizationContext summary
    flags: GlobalFlags
    world: list[str]            # binary package names (the @world set)
    overrides: list[PackageOverride]

class SourceUnit(_Frozen):      # one source package to rebuild
    source: str
    version: str                # upstream Debian version
    local_version: str          # version + "+ak<N>"
    build_deps: list[str]
    wave: int
    est_cost: BuildCost         # TINY | NORMAL | HEAVY | MONSTER

class WorldPlan(_Frozen):
    manifest_hash: str
    units: list[SourceUnit]
    waves: list[list[str]]
    stats: PlanStats            # counts, est CPU-hours, monster list

class FtbfsVerdict(_Frozen):    # LLM triage output (the pydantic-ai schema)
    source: str
    failure_class: FailureClass # LTO_INCOMPAT | NEEDS_GCC | OPT_MISCOMPILE |
                                # TEST_FAILURE | TEST_FLAKE | MARCH_ILLEGAL_INSN |
                                # DEP_SKEW | PACKAGING | UNKNOWN
    remedy: PackageOverride | None   # None → defer to human
    confidence: float
    evidence: list[str]         # quoted log lines that justify the verdict

class PackageBuildRecord(_Frozen):
    unit: SourceUnit
    attempt: int
    ok: bool
    duration_s: float
    flags_audit: FlagsAudit     # blhc result: were our flags actually applied?
    triage: FtbfsVerdict | None
    log_path: Path

class WorldMeasurements(_Frozen):  # feeds iterate fitness
    pkg_count: int
    rebuilt_count: int
    stock_count: int            # use_stock escapees — minimize
    installed_bytes: int
    image_bytes: int | None
    boot_seconds: float | None
    autopkgtest_pass: int
    autopkgtest_fail: int
    probe_metrics: dict[str, float]  # workload-probe benchmark numbers
```

The exceptions table — `world/exceptions.json`, the package-level
analogue of Gentoo's `package.env`, accumulated over time — is just
`list[PackageOverride]` with provenance. Unlike `review.py`'s
in-memory rules this *is* persisted: it's a knowledge base, mostly
host-independent (LTO breaks the same package everywhere), and
eventually shareable/shippable as a preset.

## New modules

```
src/autokernel/world/
├── models.py        # everything above
├── manifest.py      # init-from-snapshot, load/save, validate
├── indices.py       # fetch + parse Sources/Packages via python-debian
├── closure.py       # world → source closure, wave ordering, cost model
├── builder.py       # sbuild driver: chroot mgmt, env injection, retries
├── repo.py          # local apt repo: apt-ftparchive, GPG sign, pinning
├── triage.py        # pydantic-ai FTBFS agent + override_check
├── agent_world.py   # pydantic-ai manifest/flags proposal agent
├── worldtest.py     # autopkgtest + piuparts drivers
├── image.py         # mmdebstrap composition (rootfs / qcow2 / USB)
├── watch.py         # security/index watcher, delta planner
└── measurements.py  # WorldMeasurements collection
```

CLI: `world_app = typer.Typer()` + `app.add_typer(world_app,
name="world")` — same pattern as `config`. Verbs: `init`, `propose`,
`plan`, `build`, `test`, `adopt`, `image`, `watch`, `status`. All
mutative verbs dry-run by default with `--execute`, per house style.

On-disk layout, parallel to the snapshot dir:

```
<world_dir>/                      # default ~/.local/share/autokernel/world/<host>/
├── manifest.json
├── plan.json
├── exceptions.json
├── builds/<source>/<local_version>/{build.log, triage.json, record.json}
├── repo/{Packages,Release,Release.gpg,pool/...}
├── tests/<source>/<timestamp>/...
├── watch/state.json
└── iterations/w<NNN>/record.json
```

## Key technical decisions

**Builder: sbuild in unshare mode, chroots from mmdebstrap.** No root,
no schroot daemon, works in user namespaces; each build gets a clean
ephemeral chroot. The chroot's apt is configured with the local repo
pinned *above* the archive, so as pass 1 progresses, later builds
increasingly build-depend on our own output. ccache mounted into every
chroot. `--jobs` parallelism at the wave level (N concurrent sbuilds)
plus per-build `parallel=` in `DEB_BUILD_OPTIONS`.

**Build ordering is a heuristic, not a correctness requirement.**
Because chroots are seeded from stock (stage3 model), pass 1 succeeds
in any order — ordering (toolchain → core libs → leaves) only
maximizes how much of the closure gets built against our own output.
This kills the hardest problem (dose3-grade dependency solving, cycle
breaking with stage1 profiles) for v1. `--pass=2` rebuilds everything
against the pass-1 repo for full self-hosting; cycles resolve
naturally because pass-1 binaries already exist. Pass 2 is the
`--purist` option: ~2× compute for the last 5% of consistency.

**Flags injection: dpkg-buildflags env, audited by blhc.** Set
`DEB_CFLAGS_APPEND` / `DEB_CXXFLAGS_APPEND` / `DEB_LDFLAGS_APPEND`,
`DEB_BUILD_OPTIONS`, `DEB_BUILD_PROFILES` in the sbuild build
environment. This covers every package that uses dpkg-buildflags
(the overwhelming debhelper majority). Coverage is *verified*, not
assumed: run `blhc` (build-log hardening check) over every build log
and record a `FlagsAudit`; packages that ignored our flags get
flagged for the triage agent rather than silently shipping stock-ish
binaries. Compiler default is **gcc** for world v1 (the archive is
gcc-first; clang stays the kernel-side default), `--compiler=clang`
opt-in per the kernel precedent.

**Toolchain and libc rebuilds are gated.** `glibc`, `gcc`, `binutils`
with `-march=native` is the riskiest slice of the whole idea (a
miscompiled libc takes the system down, and test suites take hours).
v1 default: toolchain + libc stay stock (`use_stock` preset entries);
`--include-toolchain` lifts the gate and is required for pass-2
purity. Honest tiering beats false completeness.

**Versioning: `+ak<N>` suffix via `dch --local`.** `1.2-3` →
`1.2-3+ak1`. Sorts above stock for the same upstream version, sorts
*below* the next stock upload — which is exactly the trigger the
watcher reacts to.

**Local repo: flat apt-ftparchive repo, GPG-signed from day one,
pinned by origin.** `Release` carries `Origin: autokernel-world`;
`/etc/apt/preferences.d/autokernel-world` pins `release
o=autokernel-world` at priority 1001 so stock never silently
displaces a rebuild (the watcher, not apt, closes version gaps).
Per-host throwaway GPG key generated at `world init`
(`gpg --batch`); no `[trusted=yes]` even in v1, because the watcher
will be publishing unattended and an unsigned auto-updating repo is
an attack surface.

**Validation: Debian's own test corpus.** Per-package:
`autopkgtest` against the built `.changes`, with the local repo
added via `--setup-commands`, in the `qemu` backend (universal;
`podman` optional fast path) — and, for library packages, the
autopkgtests of their reverse dependencies (ABI/behavior canary).
`piuparts` for install/remove/upgrade hygiene. System-level: the
existing boot-test machinery pointed at a `world image` artifact
with the workload probe from the PGO design reused as benchmark.

**Watcher: systemd user timer + delta planner.** Poll the deb-src
`InRelease`/`Sources` indices (plus optionally the DSA/USN feeds for
prioritization labels); any source package in the closure with a new
version → delta plan → rebuild → autopkgtest → publish. Failure or
staleness > SLA (default 48 h for security-tagged updates) →
loud notification (exit-nonzero status surfaced in `world status`,
desktop notification, optional email/webhook hook). `world status`
always shows the lag: `12 packages behind archive, oldest 3 days,
1 security-tagged`.

## LLM agents (pydantic-ai, conventions from agent.py)

All three follow the existing pattern: module-level cached `Agent`
with `output_type=` schema, content-addressed result caching under
`<world_dir>/batches/<agent>/<key>.json` (key includes model +
prompt version + input hash), `llm.resolve()` for model selection,
recipes from `knowledge/` rendered into the prompt.

1. **Manifest agent** (`agent_world.py`) — input: Snapshot summary,
   installed-package list with sizes and `apt-mark showmanual`,
   axes. Output: `WorldManifest` draft — package trims ("you have 3
   MTAs"), GlobalFlags per axes (table below), candidate per-package
   profiles. Policy layer applies a load-bearing package blocklist
   (init, libc, apt itself, kernel, bootloader can be flagged but
   never auto-trimmed) — the package-level analogue of the
   load-bearing CONFIG blocklist.

2. **Package dimension agents** (`agent_world.py`, modeled on
   `agent_dims.py`) — the chug. Batched passes over *every* source
   unit in the closure, one dimension at a time:
   - *necessity*: keep / trim / demote-to-optional, given workload +
     the manual-install set (the package analogue of module trims);
   - *flags*: per-package deviations from GlobalFlags worth making
     up front (known LTO-hostile, known `-O3`-fragile, benefits from
     PGO later) — seeded from, and feeding, the exceptions table;
   - *features*: supported build profiles + toggleable configure
     options read from `debian/rules`/`debian/control`, proposed as
     `PackageOverride`s. Leaf-package gate: toggles that alter a
     *library's* ABI surface are rejected by policy (checked via
     `dpkg-gensymbols`-style symbol diff against the stock package)
     unless every reverse dependency is also in the rebuild closure;
   - *risk*: classify each package's blast radius (boot-critical /
     service-critical / leaf) to drive review routing and test
     depth.
   Same batching, caching, confidence floors, and bulk-rule review
   as the kernel dimensions. Re-running a pass after a manifest edit
   pays only for new or invalidated batches.

3. **FTBFS triage agent** (`triage.py`) — input: last ~300 lines of
   build log, `debian/rules` excerpt, the active flag set, the
   exceptions table's prior verdicts for similar failures. Output:
   `FtbfsVerdict`. Remedies pass `override_check` statically, then a
   bounded retry (max 2 per package per pass) validates them with a
   real build; confirmed overrides persist to `exceptions.json` with
   `provenance=LLM_TRIAGE`. Unconfirmed → `use_stock` + defer to
   human in `world status`. Aggression axis sets the confidence
   floor, same as kernel proposals.

   **Remedy escalation ladder.** The triage vocabulary is tiered by
   invasiveness; each tier is tried (and rebuild-validated) before
   the next, and `use_stock`/defer is the floor, not the second
   resort:

   | Tier | Remedy | Mechanism |
   |---|---|---|
   | 0 | retry | flake — same flags, run it again |
   | 1 | flag surgery | strip/add/translate tokens — incl. gcc↔clang dialect translation (`-flto=auto`→`-flto=thin`, drop `-ffat-lto-objects`, …) when the world compiler differs from the archive's |
   | 2 | build shaping | per-pkg `DEB_BUILD_OPTIONS` / profiles / `force_compiler` (NEEDS_GCC verdicts under a clang world) |
   | 3 | **agentic patching** | hand the failing package to a coding agent in headless CLI mode (claude / codex) with the source tree + build log; it produces a quilt patch into `PackageOverride.patches`, validated like any remedy by a real rebuild, persisted with patch + provenance + the agent transcript |
   | 4 | use_stock / defer | honesty debt, surfaced in `world status` |

   Tier 3 is what makes a compiler migration (gcc→clang world) an
   engineering task instead of a research project: material source
   incompatibilities stop being dead ends and become generated,
   validated, *reviewable* patches. Patches are pinned to the source
   version they were generated against; the watcher re-validates
   them on every upstream bump and demotes to defer when they stop
   applying.

Axes → flags mapping (defaults; manifest agent may tighten, never
loosen, hardening):

| Axis value | Effect on GlobalFlags |
|---|---|
| aggression=conservative | `-O2`, march=x86-64-v3, LTO off |
| aggression=balanced | `-O2`, march=native, LTO off |
| aggression=aggressive | `-O3`, march=native, LTO=auto, `nocheck nodoc` profiles |
| threat=permissive | distro-default hardening |
| threat=balanced | distro-default + `-D_FORTIFY_SOURCE=3` where supported |
| threat=paranoid | above + full RELRO/stack-clash everywhere, clang CFI candidates flagged by the features dimension (W3) |
| workload | informs manifest trims + which packages get PGO attention later, not flags |

## Milestones

Each milestone has a live-validated exit criterion, per house
culture. Versions assume this becomes the v0.18–v0.20 arc.

**W0 — groundwork + spike (v0.18.0).**
Add `Target.WORLD` to `installdeps.py` (sbuild, mmdebstrap, uidmap,
devscripts, dpkg-dev, apt-utils, blhc, autopkgtest, python-debian via
uv). Preflight checks: deb-src lines enabled, unshare userns
available, disk budget. Spike script (not yet the real builder):
rebuild **zlib** with `-march=native -O3`, suffix `+ak1`, publish to
a flat signed repo, pin it, `apt install` it on a throwaway
mmdebstrap chroot.
*Exit: `dpkg -s zlib1g` in the chroot shows `+ak1`; `blhc` confirms
flags; reverse-dep smoke (gzip round-trip) passes.*

**W1 — manifest + planner (v0.18.1).**
`world/models.py`, `manifest.py`, `indices.py`, `closure.py`.
`world init` (deterministic skeleton from dpkg state + Snapshot,
`--ring={0,1,2}` selects required / +important / full installed
set), `world plan` (closure, waves, cost estimate with a hardcoded
monster list: llvm, gcc, webkit2gtk, chromium, libreoffice, rustc).
No LLM yet. Unit fixtures: miniature Sources/Packages indices
checked into `tests/fixtures/world/`.
*Exit: `world plan` renders sane waves + cost estimates for both
ring 0 (53 sources) and ring 2 (full installed set) on this host;
golden-plan tests pass.*

**W2 — builder + repo (v0.18.2).**
`builder.py`, `repo.py`. sbuild-unshare driver with env injection,
ccache, per-build logs, blhc audit, `+ak` versioning, repo publish,
pinning installer (`world adopt --execute` writes sources.list.d +
preferences.d + key). Wave-parallel, kill/restart-resumable
`world build` (keyed build records). FTBFS at this stage just
records and continues (`use_stock`).
*Exit: ring 0 rebuilds end-to-end on this host (toolchain-gated
sources as declared stock); a mmdebstrap chroot resolves entirely
from the local repo.*

**W3 — the decision layer (v0.18.3).**
The chug arrives, both agents at once: package dimension agents
(`agent_world.py` — necessity / flags / features / risk batched
over every source unit, cached, bulk-rule reviewed) and the FTBFS
triage agent (`triage.py` + `override_check` + exceptions table +
bounded retry wired into `world build`). Package-level load-bearing
blocklist in the policy layer. Mocked-LLM unit tests with canned
failure logs per FailureClass (collect real ones from W2's
aggressive-flags run).
*Exit: ring 0 chugged at aggression=aggressive (LTO on): every
source unit has a cached decision record across all four
dimensions; ≥80 % of FTBFS resolved by persisted overrides without
human input, every override carrying evidence lines; a killed and
restarted run re-pays ~zero LLM and build cost.*

**W4 — validation layer (v0.18.4).**
`worldtest.py`: autopkgtest (qemu backend) + reverse-dep test
selection + piuparts; `world test` verb; `WorldMeasurements`.
Failures auto-revert the package to its previous local version (or
stock) in the repo — the package-granular analogue of iterate's
auto-revert.
*Exit: a deliberately broken rebuild (e.g. openssl with a
known-bad flag) is caught by a reverse-dep autopkgtest and
auto-reverted; clean run publishes measurements.*

**W5 — images + kernel integration (v0.19.0).**
`image.py`: container rootfs first (mmdebstrap from the local repo
→ tarball → `docker import` — the Alpine-minirootfs-shaped
artifact, instantly verifiable), then bootable qcow2/USB (kernel
`.deb` from the existing `build --target=bindeb-pkg` published into
the same repo, minitram as the initramfs). This *is* the ROADMAP
v0.18 `autokernel distro` verb, apt-flavored.
*Exit: `docker run` of the rootfs shows every package `+ak1` except
declared `use_stock`; `world image --output=qcow2` boots in QEMU
(OVMF/UEFI path) on the existing boot-test machinery; size report
vs stock debootstrap.*

**W6 — security watcher (v0.19.1).** *Adoption blocker: `world adopt`
on a daily-driver host refuses without the watcher enabled —
regenerated images are patched by rebuilding; adopted hosts need
the daemon.*
`watch.py` + systemd user units + `world status` lag report +
notification hooks. Delta planning reuses `closure.py`.
*Exit: simulate an archive update (point at a snapshot.debian.org
pair / bump a fixture index); watcher detects, rebuilds, tests,
publishes within one timer tick; failure path notifies.*

**W7 — rings 1 and 2 at scale (v0.19.2).**
Nothing new architecturally — this milestone is the machinery
proving it scales. `world propose` matures (manifest agent + the
kernel's `hyperoptimize`-style presets gain world-side meaning);
build-profile slimming validated on real packages; multi-day
resumable chugs are the expected mode.
*Exit: ring 2 — this host's full installed set (~1,900 sources) —
chugged end-to-end with green `world test`; `world status` reports
honesty debt (`use_stock` count, flag-audit failures) truthfully;
at least one package demonstrably slimmed via a real build profile
with its autopkgtests green.*

**W8 — closed loop + pass 2 (v0.20).**
Extend `iterate` so fitness spans `BuildMeasurements` +
`WorldMeasurements`; rounds may propose kernel trims *and* manifest/
flag changes; history block carries both trends. `--pass=2`
self-hosting + `--include-toolchain` behind explicit opt-in.
*Exit: 3-round iterate on a VM-guest profile shrinks image size
with green tests each round and at least one auto-revert exercised;
pass-2 run completes on ring 0.*

## Risks

| Risk | Mitigation |
|---|---|
| Watcher gap → stale security-sensitive packages | W6 is an adoption blocker; `world status` lag is loud; SLA notifications; `use_stock` rollback is one command |
| glibc/toolchain miscompile bricks host | Gated behind `--include-toolchain`; adopt-on-host only after image boot-tests; existing rollback machinery patterns reused |
| LTO/-O3 FTBFS tail bigger than expected | Triage agent + exceptions table is the core design bet; worst case `use_stock` keeps the system whole and `world status` shows honesty debt |
| `-march=native` breaks package test suites under qemu-without-KVM | KVM required in preflight for world test (host CPU passthrough); MARCH_ILLEGAL_INSN is a triage class |
| Compute cost surprises users | `world plan` shows CPU-hour estimate + monster list before any build; per-wave resumability via content-addressed build records |
| Packages bypass dpkg-buildflags | blhc audit per build; non-compliant packages surfaced, not silently stock-ish |
| ABI breakage from feature toggles | Leaf-only policy + symbol-diff gate (W3); flag-only rebuilds are ABI-safe by construction |
| Ubuntu deb-src coverage gaps (restricted/multiverse) | Manifest validation flags unsourceable packages at `world init`; they become declared `use_stock` |

## Testing strategy

- **Unit**: fixture Sources/Packages indices; golden plans; mocked
  sbuild subprocess (same convention as build.py tests); canned FTBFS
  logs per FailureClass for triage tests with mocked LLM.
- **Docker validation**: extend `Dockerfile.validation` with sbuild/
  mmdebstrap and run W0-spike-equivalent (1 tiny package) as part of
  the suite — unshare works in privileged containers; CI runs the
  static + unit layers only.
- **Nightly live**: ring 0 (the 53-source `required` set) rebuild + test on
  a runner with KVM, publishing `WorldMeasurements` history — the
  world-side analogue of the existing validation workflow.

## Open questions (decide during W1, none block W0)

- Remote build fan-out: the sbuild-chroot model parallelizes
  trivially over SSH; worth a `--builder=ssh://host` seam in
  `builder.py`'s interface from the start, implementation later.
- PGO for hot packages (postgres, python, nginx ship upstream PGO
  hooks): natural W8+ follow-on, shares the workload-probe design in
  [PGO.md](PGO.md).
- Sharing exceptions tables across users (a community `package.env`):
  needs provenance/trust story; out of scope until the schema has
  survived real use.
