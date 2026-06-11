# Experiment: a 100% clang + ThinLTO + PGO Debian world

A focused, phased experiment to build the bootable ring-0 package set
(~50 sources, what `autokernel world image` boots) as a world that is
**clang-compiled, ThinLTO end to end, and profile-guided** — then prove
it boots, measure it against the gcc world, and **price the whole
lifecycle** (creation + ongoing maintenance) to test the
agent-maintained-fork thesis, not just the toolchain.

This builds on the W0–W5 machinery already live (manifest, planner,
sbuild builder, FTBFS triage agent, package dimension agents, signed
repo, ext4 image, QEMU boot test). It exercises the parts still on
paper — PGO, BOLT, agentic patching — and resolves the one finding that
blocked the clang comparison: ThinLTO drops `.symver` versioned symbols.

> **Status: planning.** Research complete (see "Research findings" and
> the citations at the end). Phase 0 is a 30-minute spike that decides
> the LTO strategy for everything downstream.

## The honest "100%"

"100% clang" has exactly one asterisk, and it's load-bearing: **glibc,
gcc, and binutils stay stock** (the toolchain gate). glibc *officially
does not build with clang*; the gcc-source runtime libs (libgcc-s1,
libstdc++6) come from gcc. Everything else in ring 0 — including the
hot path (bash, coreutils, openssl, xz, zlib) and init (systemd, udev,
util-linux) — is clang+ThinLTO+PGO. We state the asterisk in every
report rather than hide it; lifting it is a research project of its own
(`--include-toolchain`), out of scope here.

## Storage layout

Artifacts live on the big volume, not `$HOME` (per-host world dirs were
under `~/.local/share`; these experiments are large and worth keeping):

```
/nas4/data/autokernel/world/
├── ring0-gcc/          # the existing gcc-aggressive baseline (move here)
├── ring0-clang/        # Phase 1: clang + ThinLTO, no PGO
├── ring0-clang-pgo/    # Phase 2: + PGO
├── ring0-clang-bolt/   # Phase 3 (stretch): + BOLT
├── profiles/           # checked-in, version-pinned PGO/AutoFDO/BOLT profiles
├── patches/            # agentic + curated quilt patches, keyed by src version
└── reports/            # measurement runs, cost ledgers
```

`/data1` (1.2 T, faster) holds the ccache and scratch build trees;
`/nas4/data` (25 T) holds the durable artifacts. The builder's
`--world-dir` already parameterizes all of this; only the ccache path
(currently `/var/tmp`) and a scratch override need wiring.

## Research findings that shaped this plan

Four parallel deep-research passes (Opus sub-agents + web) settled the
unknowns. The load-bearing conclusions:

1. **The `.symver` → `__attribute__((__symver__))` rewrite is a dead
   end under clang.** The attribute is GCC-only (LLVM #59438 still open);
   clang requires inline-asm `.symver`, which is exactly what ThinLTO
   mishandles. So the W3 idea of "agentic patch rewrites the directive"
   *cannot* be the clang fix. The real levers, in confidence order:
   - **per-TU `-fno-lto`** on just the translation unit(s) containing
     `asm(".symver` — surgical, high-confidence, and what Gentoo
     actually does. Keeps ThinLTO everywhere else; the compat shims
     lose only cross-module inlining (negligible).
   - **`force_compiler=gcc`** for the whole library (what our triage
     already does today — heavier but trivially correct).
   - **bfd/gold + LLVMgold plugin** instead of lld — *genuinely
     untested under ThinLTO* (LLVM docs call bfd+plugin "not
     supported"; the defect may live upstream of the linker in the IR
     symbol table). One `readelf` experiment settles it. This is
     Phase 0.
   - Two of our "symbols disappeared" failures may be **benign**: lld
     omits version-node tag ABS symbols that GNU ld emits; gensymbols
     flags them but the functions still export (Debian #992796). Worth
     distinguishing per-library before spending a build.

2. **PGO: use IR instrumentation (`-fprofile-generate`/`-fprofile-use`),
   not frontend (`-fprofile-instr-*`).** ~1.17× vs 1.11× in Chrome's
   measurement; smaller profiles; fewer threading gotchas. Pin clang +
   llvm-profdata + compiler-rt to one major version across both passes
   (profile format is versioned). Profile matching is per-function
   content-hash, so minor upstream bumps degrade gracefully instead of
   breaking — which is what makes the watcher story viable. Leaf libs
   collect their profile from `dh_auto_test` *in the chroot*; the boot
   path (systemd) must profile *in the QEMU VM*. AutoFDO (one build,
   sample-based) is tempting but needs PMU/LBR that the unprivileged
   unshare chroot blocks via seccomp — VM-only.

3. **Headless agents** (`claude` / `codex`) work for the residual
   FTBFS that flag-surgery can't fix: run in a disposable git-init'd
   copy of the source, capture the patch as `git diff` (don't have the
   model hand-format quilt), bound with `--max-turns`/`--max-budget-usd`
   + an OS timeout, validate the patch by rebuild like any other
   remedy. ~$0.10–0.55/attempt on Sonnet. This is tier-3 of the remedy
   ladder, finally made concrete.

4. **BOLT** adds low-single-digit to ~8% on top of PGO+LTO for
   large/branchy binaries (systemd yes, libcrypto modest, bash no);
   needs `-Wl,--emit-relocs`, must run before `dh_strip`, and `perf` is
   seccomp-blocked in the chroot (use BOLT `-instrument` mode in-chroot,
   or `perf` in the VM). It's a **stretch**: it triples the moving
   parts and reuses PGO's profile-pinning/venue solutions, so it only
   makes sense after PGO is reproducible.

## Phase 0 — the symver linker spike (≈30 min, gates everything)

**Question:** can we get ThinLTO + `.symver` to coexist without
per-package opt-outs, by swapping lld for bfd/gold + LLVMgold?

**Method:** take one small affected library (`libcap2` — clean, fast,
versioned), build it four ways in the clang chroot and let
`dpkg-gensymbols` + `readelf --dyn-syms --version-info` referee against
the gcc-built reference:
1. lld + ThinLTO (the known failure — reproduce it)
2. bfd + LLVMgold + ThinLTO
3. gold + LLVMgold + ThinLTO
4. lld + ThinLTO but the one `.symver` TU at `-fno-lto` (the
   high-confidence fallback)

**Decision:**
- If (2) or (3) preserves all versioned symbols → the LTO remedy
  becomes a *linker choice in `GlobalFlags`*, the ~15 exceptions
  collapse to zero, and "100% ThinLTO" is real with no per-package
  surgery. (Low prior probability per the research, but cheap to check
  and high-value if it holds.)
- If only (4) works → the remedy is **per-TU `-fno-lto`**, implemented
  as a new `PackageOverride` capability + a `LTO_SYMVER` triage class,
  and "100% ThinLTO" means "ThinLTO except the compat shims," which is
  honest and still ~99% of objects.

Exit criterion: a one-page `reports/phase0-symver.md` with the
readelf/gensymbols diffs and the chosen remedy, committed.

## Phase 1 — the 100% clang ThinLTO world

Build all ~50 ring-0 sources clang+ThinLTO with the Phase-0 remedy,
in `ring0-clang/`. Reuses the existing builder + triage loop verbatim;
the only new code is whatever Phase 0 chose (linker-in-flags or per-TU
`-fno-lto`).

**Strict exit criterion:** zero `force_compiler=gcc` and zero
whole-package LTO disables. Allowed exceptions: per-TU `-fno-lto` on
symver shims (if Phase 0 picked that), test-env `nocheck`, packaging
`strip nodoc`. Plus the two global clang accommodations already found:
`-gdwarf-4` (Ubuntu dwz can't parse clang DWARF5) and
`DEB_BUILD_MAINT_OPTIONS=optimize=-lto`.

**Also fixes a known gap:** `bash` needs a *compound* remedy (LTO strip
**and** nodoc strip), but `save_exception` replaces the whole entry, so
the second verdict clobbers the first. Phase 1 makes the exceptions
table *merge* remedies for the same package (union the strip sets,
options, profiles) instead of replace — a small change with broad
payoff, surfaced live by bash in both gcc and clang worlds.

Boot the resulting image (`world image` + `world boot-test`) to confirm
a clang+ThinLTO init actually runs. Record binary sizes vs the gcc
world (clang was already 4–9% smaller on systemd/util-linux *without*
PGO — quantify across the set).

## Phase 2 — PGO over the hot set

New machinery, following the PGO.md kernel design ported to packages.

**Flag tiers** (extend `GlobalFlags` / a new `pgo` axis):
- `pgo=off` (default, Phases 0–1)
- `pgo=instrument` → append `-fprofile-generate` to C/CXX/LD
- `pgo=use` → append `-fprofile-use=<profiles/<src>.profdata>`

**Hot set** (where PGO pays, ~10 packages): bash, coreutils, sed, grep,
gzip, xz-utils, zlib, openssl, pcre2, ncurses. systemd is hot but
boot-profiled (VM venue).

**Pipeline per package:**
1. **Instrument build** (`pgo=instrument`) in chroot.
2. **Profile collection**, venue by class:
   - *leaf libs/tools*: set `LLVM_PROFILE_FILE` and run the package's
     own `dh_auto_test` **plus** a synthetic workload (openssl speed,
     xz round-trips on a corpus, a shell-heavy script for bash/
     coreutils) — `.profraw` lands in-chroot. The research flags that
     a test suite alone is *convenient but not representative*;
     synthetic workloads matter most for openssl/pcre2.
   - *systemd*: boot the instrumented image in QEMU, copy `.profraw`
     out via the existing virtiofs/hook path.
3. `llvm-profdata merge -sparse` → `profiles/<src>-<version>.profdata`,
   **checked in and version-pinned** (never regenerated inside the
   reproducible build — the profile is a build *input*).
4. **Use build** (`pgo=use`), validated by the same audit + boot test.

**Resume integrity:** the profile digest joins `flags_hash`, so a
changed workload or profile correctly invalidates exactly the affected
builds and nothing else.

**Exit criterion:** the hot set rebuilt with PGO, image boots, and the
benchmark probe (Phase 5) shows a measurable delta over Phase-1 clang.

## Phase 3 — BOLT (stretch)

Only after Phase 2 is reproducible. Apply `llvm-bolt` to the 2–3
biggest, branchiest binaries (systemd, libcrypto). Requires
`-Wl,--emit-relocs` at link, a BOLT step in `override_dh_auto_install`
*before* `dh_strip`, and a pinned `.fdata` profile. Collection uses
BOLT `-instrument` mode in-chroot (perf is seccomp-blocked) or perf in
the VM for systemd. Expected marginal gain is single digits — this
phase is about proving the *pipeline* (compile-PGO → ThinLTO → post-link
BOLT, the CachyOS stack) more than chasing the number. Skippable
without affecting the thesis test.

## Phase 4 — agentic patching for the residual

Whatever Phases 0–2 still can't fix with flags goes to tier-3: a
headless coding agent generates a source patch, validated by rebuild.

**New module `world/agent_patch.py`:**
- A unified `run_coding_agent(backend, tree, prompt, …)` shelling out
  to either `claude --bare -p … --output-format json --permission-mode
  dontAsk --max-turns 8 --max-budget-usd 1.00` or `codex exec --sandbox
  workspace-write --json -o …`, in a disposable git-init'd copy of the
  unpacked source.
- Prompt = the active flags + build-log tail + "make the minimal source
  fix; do **not** disable the optimization; edit only this tree; then
  stop." (Mirrors the triage agent's discipline.)
- Capture the patch as `git diff`; the orchestrator serializes it to
  `debian/patches/NNNN-autokernel.patch` + `series`, then validates
  with `quilt push -a` + a real rebuild. Persist patch + transcript +
  cost as provenance under `patches/`.
- Wired as a `FtbfsVerdict` remedy tier *below* flag surgery and *above*
  `use_stock`/defer: triage proposes `agentic-patch` only when no flag
  remedy fits. `PackageOverride.patches` (already modeled) finally gets
  *applied* by the builder (dpkg-source patch application before build).

This is the concrete realization of the remedy escalation ladder in
WORLD.md, and the piece that makes the "$1 fork, pennies to maintain"
economics testable: each patch is pinned to its source version and
re-validated by the watcher on every upstream bump.

## Phase 5 — proof and price

The point of the whole experiment: does it boot, is it faster, and what
did it cost?

**Boot proof:** each world's image boots in QEMU to the sentinel; serial
timestamps give boot time for free.

**Benchmark probe** (`world bench`, new): a fixed suite run inside each
image in QEMU — `openssl speed` (AES/SHA/RSA), xz/zstd compress+
decompress on a fixed corpus, a bash/coreutils-heavy script, pcre2
match throughput. Three images compared: **stock Ubuntu**, **ring0-gcc**,
**ring0-clang-pgo** (+ ring0-clang and ring0-clang-bolt if built).
Report wall-clock + binary sizes + boot time, with variance across N
runs.

**The economics ledger** (`reports/economics.md`): every phase logs LLM
tokens (triage + dimension + agentic-patch) and CPU-minutes. The
deliverable sentence is concrete: *"creating this fork cost \$X in
agent judgment + Y CPU-hours; an upstream-bump delta rebuild costs
\$Z + W CPU-minutes."* Then **simulate the maintenance treadmill**:
point at a snapshot.debian.org mirror pair (older → newer), run a
watcher-shaped delta rebuild, and verify profiles + patches carry
forward untouched (per-function profile hashing + version-pinned
patches should mean near-zero re-judgment). That number — the marginal
monthly cost — is the thesis.

## New code inventory

Most phases reuse existing machinery. Genuinely new:

| Phase | New code | Size |
|---|---|---|
| 0 | spike script (throwaway) | small |
| 1 | Phase-0 remedy (linker-in-flags *or* per-TU `-fno-lto` override field + `LTO_SYMVER` triage class); exceptions-table *merge* | small |
| 2 | `pgo` axis + flag tiers; profile collection (chroot test-run wrapper + VM `.profraw` extraction); `llvm-profdata` step; profile digest in `flags_hash` | medium |
| 3 | `--emit-relocs` link flag; BOLT `override_dh_auto_install` injection; `-instrument` collection | medium |
| 4 | `world/agent_patch.py` (claude/codex abstraction); `PackageOverride.patches` *application* in builder; `agentic-patch` remedy tier | medium |
| 5 | `world bench` (probe suite + per-image QEMU runner); economics ledger | medium |

Schema additions to `PackageOverride`: `lto_exclude_tus: list[str]`
(per-TU `-fno-lto`), `emit_relocs: bool`, `bolt: bool`. To `GlobalFlags`:
`linker: str` (lld/bfd/gold), `pgo: Pgo` enum. All additive and
key-only-when-set, preserving existing flags-hash stability.

## Risks

| Risk | Mitigation |
|---|---|
| bfd/gold+LLVMgold+ThinLTO doesn't fix symver (likely) | Phase 0 fails cheap; per-TU `-fno-lto` is the high-confidence fallback, already planned |
| Test suites are unrepresentative PGO workloads | synthetic workloads for openssl/pcre2; measure use-pass delta and fall back to test-only where synthetic adds nothing |
| systemd boot-profiling is fragile | systemd PGO is optional; the hot-set leaf packages carry the thesis even if systemd stays `pgo=off` |
| Agentic patch is wrong-but-compiles | same gate as every remedy: rebuild + audit + ABI symbol diff + boot test; nothing persists unvalidated |
| Profile/patch staleness on upstream bump | per-function hash matching (warns, doesn't break) + version-pinned patches re-validated by the watcher; this is the *measured* thesis, not an assumption |
| Compute cost of 3–4 full worlds + PGO double-builds | resumable + ccache on `/data1`; the hot set is ~10 packages, not 50, for the double-build cost |
| Reproducibility (PGO/BOLT non-deterministic) | profiles checked in as fixed inputs; builds deterministic given a pinned profile |

## Sequencing

Phase 0 → 1 are the spine and can start now (Phase 1 reuses the live
builder). Phase 2 is the main new build. Phases 3–4 are independent and
can interleave. Phase 5 runs against whatever worlds exist. The thesis
is testable after Phase 2 + Phase 5; Phases 3–4 deepen it.

Each phase ends with a committed `reports/phaseN-*.md` carrying its
exit-criterion evidence — same discipline as the W-milestone live
validations.

## Citations

Symbol versioning / LTO: [maskray — All about symbol versioning](https://maskray.me/blog/2020-11-26-all-about-symbol-versioning),
[LLVM #59438 (clang symver attr, open)](https://github.com/llvm/llvm-project/issues/59438),
[wxWidgets #25438 (clang20 ThinLTO symver)](https://github.com/wxWidgets/wxWidgets/issues/25438),
[Debian #992796 (gensymbols vs lld tags)](https://bugs.debian.org/992796),
[LLVM GoldPlugin](https://llvm.org/docs/GoldPlugin.html),
[Gentoo LTO wiki](https://wiki.gentoo.org/wiki/LTO).
PGO: [Clang Source-based Coverage](https://clang.llvm.org/docs/SourceBasedCodeCoverage.html),
[LLVM Discourse — IR vs frontend PGO](https://discourse.llvm.org/t/status-of-ir-vs-frontend-pgo-fprofile-generate-vs-fprofile-instr-generate/58323),
[Red Hat — PGO with modified sources](https://developers.redhat.com/blog/2020/07/06/profile-guided-optimization-in-clang-dealing-with-modified-sources),
[kernel AutoFDO docs](https://docs.kernel.org/dev-tools/autofdo.html),
[Ubuntu Server PGO](https://ubuntu.com/server/docs/explanation/performance/perf-pgo/),
[Clear Linux autospec](https://github.com/clearlinux/autospec).
BOLT: [LLVM BOLT README](https://github.com/llvm/llvm-project/blob/main/bolt/README.md),
[BOLT paper (CGO 2019)](https://arxiv.org/abs/1807.06735),
[Meta BOLT](https://engineering.fb.com/2018/06/19/data-infrastructure/accelerate-large-scale-applications-with-bolt/),
[LWN — kernel BOLT](https://lwn.net/Articles/993828/),
[ptr1337/llvm-bolt-scripts](https://github.com/ptr1337/llvm-bolt-scripts).
Headless agents: [Claude Code headless](https://code.claude.com/docs/en/headless),
[Codex non-interactive](https://developers.openai.com/codex/noninteractive).
Prior art: [CachyOS optimized repos](https://wiki.cachyos.org/features/optimized_repos/),
[Arch RFC 0004 LTO](https://rfc.archlinux.page/0004-lto-by-default/).
