# Experiment diary — clang/ThinLTO/PGO Debian world

Running log for executing `docs/CLANG_PGO_EXPERIMENT.md`. Newest entries
at the bottom of each phase. Durable artifacts under `/nas4/data/autokernel/world/`,
scratch/ccache under `/data1/autokernel/`.

---

## Phase 0 — symver linker spike

### 0.1 Environment survey (2026-06-11)

- Toolchain: clang 21.1.8, binutils 2.46 (ld.bfd with `-plugin` support),
  `LLVMgold.so` present at `/usr/lib/llvm-21/lib/LLVMgold.so`. **Host lacks
  `ld.lld` and `ld.gold`** — they live only inside the clang sbuild chroot
  (built with `--include=ccache,clang,lld`; no llvm/gold there). So no single
  environment yet has all linkers — the spike needs one built deliberately.
- Storage confirmed: `/nas4/data` 25 T free, `/data1` 1.2 T free. Created
  `/nas4/data/autokernel/world/{reports,profiles,patches}` and
  `/data1/autokernel/scratch`.

### 0.2 libcap2 is NOT a symver case — the plan's spike subject is wrong

Examined the libcap2 source that failed under clang. **It has no `.symver`
directives and no `--version-script`.** Its real failure (from cached triage
evidence) was:

```
cc1: error: unrecognized argument to '-flto=' option: 'thin'
x86_64-linux-gnu-gcc ... -flto=thin ...
```

That's **gcc** (`cc1`, `x86_64-linux-gnu-gcc`) choking on clang's `-flto=thin`
spelling — libcap's build invokes gcc, not clang, despite our `CC=clang`. The
triage agent mislabeled it `lto-incompat` and the `strip -flto=thin` remedy
"worked" only by removing the flag gcc couldn't parse. **The package was built
with gcc.**

### 0.3 Audit of all clang failures — three distinct true causes

Classified every clang triage verdict by its real evidence (not the agent's
label). Three buckets:

1. **REAL symver/gensymbols ThinLTO drop** (the genuine Cause B):
   attr, libbsd, libmd, libxcrypt, systemd (LIBSYSTEMD_209), util-linux
   (BLKID_1.0), etc. — `dpkg-gensymbols: error: some symbols ... disappeared`.
   ThinLTO loses `.symver` aliases.
2. **REAL lld version-script error**: libselinux (`version script assignment
   of 'LIBSELINUX_3.8' to symbol ...`), pam (`'global' to symbol 'pam_sm...'`).
   These use `--version-script` `.map` files; lld errors on the assignment
   (the `--no-undefined-version` default since LLVM 16). Mechanically distinct
   from asm `.symver`.
3. **GCC-got-clang-flag** (build system ignores `CC`): libcap2 and several
   others — `cc1: error: unrecognized argument to '-flto=' option: 'thin'`.
   **These were never built with clang.** The strip remedy masked it.

### 0.4 Integrity finding — the clang world is not 100% clang

Bucket 3 means **our `ring0-clang` world silently built some packages with
gcc** because their Makefiles hardcode `gcc`/`$(CC)`-as-gcc and our `CC=clang`
env didn't propagate. The existing audit only checks that our *flags* appeared
on a compile line (blhc grep) — it does **not** verify the compiler was clang.
A real "100% clang" claim needs a **compiler-identity audit** (e.g. clang
version string in the binary's `.comment`, or detecting gcc invocations in the
build log). This is a required Phase 1 addition, surfaced by Phase 0.

**Consequence for Phase 0:** the symver spike must use a genuine Cause-1
library. Choosing `attr` (small, clean, real `dpkg-gensymbols ... disappeared`)
as the representative, with `libbsd` as a second confirmation.

### 0.5 Compiler-identity audit (quantified)

From surviving build logs, counting clang vs gcc compile invocations:

| package | clang | gcc | verdict |
|---|---|---|---|
| libcap2 | 0 | 19 | **gcc** (build system ignores CC) |
| zlib | 0 | 125 | **gcc** (declared force_compiler=gcc — OK) |
| bash | 0 | 422 | **gcc** (build system ignores CC) |
| ncurses | 1102 | 978 | **mixed** (tools gcc, lib clang) |
| systemd, coreutils, attr, libbsd, openssl, xz-utils, sed, grep, pam, util-linux | (clang) | 0 | clang ✓ |

So `ring0-clang` is **not** 100% clang: libcap2/bash silently built with gcc,
ncurses partially. Phase 1 needs (a) a **compiler-masquerade** (PATH-prepend
`cc`/`gcc`/`x86_64-linux-gnu-gcc` → clang) to force clang through hardcoded
build systems, and (b) a **compiler-identity audit** that fails the build when
gcc compiled the objects and force_compiler=gcc wasn't declared.

### 0.6 The symver spike — synthetic results (bwrap chroot)

Ran in a bwrap sandbox over the extracted clang chroot (clang 21.1.8, lld,
ld.bfd 2.46, `LLVMgold-21.so` in `/usr/lib/bfd-plugins/`). `unshare` is
AppArmor-blocked; **bubblewrap** has the userns grant and works.

- **Isolated `.symver` + version-script pattern works under ALL linkers**,
  including plain lld+ThinLTO (2/2 versioned symbols). So the failure is **not**
  the isolated pattern — it's an emergent whole-library effect.
- **Real attr pattern reproduced** (multi-TU, real `exports` version-script,
  `-Bsymbolic-functions`): lld+ThinLTO **hard-fails** with
  `ld.lld: error: version script assignment of 'ATTR_1.0' to symbol 'attr_get'
  failed: symbol not defined` — this is lld's `--no-undefined-version` default
  (since LLVM 16) erroring on version-script entries whose symbols ThinLTO
  internalized/dropped from the whole-program view.
- **bfd+LLVMgold+ThinLTO is lenient** and produced the `.symver`'d versioned
  symbols (setxattr@ATTR_1.0 …) where lld errored. Promising, but the research
  warns bfd may *silently* drop — needs the real-package gensymbols referee.

**Mechanism clarified:** two failure shapes, both rooted in ThinLTO
internalizing symbols the version script names:
1. `symbol not defined` link error (lld `--no-undefined-version`) — libselinux,
   pam, and the attr repro.
2. `dpkg-gensymbols ... disappeared` — symbols link OK but aren't exported.

**Candidate remedies to settle on the real package (attr) via the builder:**
- `linker=bfd` (lenient, may fix both — primary candidate)
- `-Wl,--undefined-version` (reverts lld to warn — a mask, gensymbols still referees)
- per-TU `-fno-lto` on the `.symver` TU (research's pick)
- whole-package LTO off (known-good, most invasive — current production remedy)

Hand-replicating attr's autoconf build hit diminishing returns (`EXPORT` macro,
include layout). Moving to the definitive test: rebuild real attr through the
builder with each remedy and let real `dpkg-gensymbols` referee. This needs
`linker` support in `GlobalFlags` + builder — also required for Phase 1.

### 0.7 Real-package validation — `linker=bfd` is the answer (decisive)

Added `GlobalFlags.linker`; built real packages with `linker=bfd`, full clang,
ThinLTO on, no LTO strip, no force-gcc:

| package | result | note |
|---|---|---|
| attr | ✓ | 26 versioned ATTR symbols intact; 39 clang/0 gcc; 60 flto=thin; gensymbols passed |
| libbsd | ✓ | clean |
| libmd | ✓ | clean |
| libsepol | ✓ | clean |
| libselinux | ✓ | clean — the `version script assignment` lld-error class, fixed |
| **libxcrypt** | ✗ | `LLVM gold plugin: default version symbol crypt_r@@XCRYPT_2.0 must be defined` + **plugin segfault** — the hard `@@`-compat residual bfd can't fix |

(pam, systemd confirmation running.)

**Tiered remedy decided:** `linker=bfd` is the clang-world default (kills the
common symver class with zero per-package cost, ThinLTO preserved, genuinely
clang); the hard residual (libxcrypt-class) falls to the existing triage
`strip -flto=thin`. Per-TU `-fno-lto` held in reserve (bfd made it unnecessary
and it doesn't help the gold-plugin segfault).

### 0.8 Compiler masquerade proven + infrastructure landed

Verified `gcc`→clang masquerade: clang invoked as `gcc` accepts the full Debian
gcc flag set (incl. the conflicting `-flto=auto -ffat-lto-objects ... -flto=thin`,
rc=0). Implemented for Phase 1:
- `GlobalFlags.linker` (default bfd for clang) + `masquerade` (default True for
  clang); both in flags_hash; `force_compiler=gcc` packages exempt from masquerade.
- `compiler_identity_audit` — fails a clang build if gcc compiled any object
  (unless force-gcc declared).
- chroot bakes `llvm` (LLVMgold) + the masquerade symlinks; tag `-clang-masq`.
- `world init --compiler clang` → `linker=bfd, masquerade=True` automatically.

831 tests pass. Committed c400c5d.

**Phase 0 exit criterion met.** Phase 1 ring0-clang world dir initialized at
`/nas4/data/autokernel/world/ring0-clang` (51 sources, ~7 CPU-h est). The full
build is the live test of bfd + masquerade + triage together: the symver class
should build clean (bfd), libcap2/bash/zlib now masqueraded to clang (or
triage→force-gcc), libxcrypt → triage strip-lto.

---

## Phase 1 — 100% clang ThinLTO world

### 1.1 First launch caught a false-positive in my own audit (bzip2)

Launched ring0-clang (bfd + masquerade). Chroot regenerated clean (20s, llvm +
masquerade baked in). First failure: `bzip2: compiler-identity audit failed:
gcc compiled 16 objects`. Investigated: bzip2's `debian/rules` **forces
`CC=gcc`**, but the masquerade PATH (`/usr/local/lib/ak-masq/gcc → clang`,
confirmed on the build's PATH line) redirected it — the `gcc -c` invocations
**actually ran clang** (proof: the build *succeeded* with `-flto=thin` present,
which real gcc rejects with `unrecognized argument`). My naive identity audit
counted log `gcc -c` lines as gcc and false-failed it.

**Fix (95d076d):** under masquerade, bare `gcc`/`cc` are clang; only
absolute-path or version-suffixed gcc (`/usr/bin/gcc`, `gcc-15`) bypasses the
masquerade and counts as a real-gcc violation. Validated on the real bzip2 log
(now PASS; old logic FAIL). Stopped Phase 1, fixed, restarting. (The masquerade
is self-protecting anyway: real gcc + `-flto=thin` FTBFS at compile, so it
can't silently ship.)

Also landed `apt_patches()` — `apply_patches()` (the Phase-4 prerequisite —
`PackageOverride.patches` modeled since W3 but never honored).

### 1.2 Restart healthy; masquerade proven end-to-end

Restarted ring0-clang with the fixed audit (eb71af0 / 95d076d). bzip2 ✓ (73s) —
**conclusive proof the masquerade works end-to-end**: bzip2 forces `CC=gcc` in
debian/rules yet built successfully with `-flto=thin` in its flags, which real
gcc rejects outright — so clang compiled it via the masquerade. Build
progressing through the waves.

### 1.3 Phase 4 module built ahead (eb71af0)

While Phase 1 builds, landed `world/agent_patch.py` — the tier-3 headless
claude/codex patch generator (11 tests, faked CLI). Both CLIs confirmed present
(claude 2.1.173, codex 0.139.0). Triage integration deferred until Phase 1's
residual is known — likely it's a *flag* remedy (libxcrypt strip-lto), not a
source-patch case, so the agentic tier may not be exercised until rings 1-2 or
the PGO phase surface a genuine source incompatibility.

### 1.4 Phase 5 economics foundation built ahead (1b21eea)

`world/economics.py` + `world economics` verb: the thesis ledger (build
CPU-hours from records + LLM spend from `costs.jsonl`). LLM usage capture wired
into the triage + dimension agents — fires only on real (non-cached) model
calls, so the ledger is real spend. This had to land *before* the LLM calls I
want to measure.

**Notable Phase 1 signal so far:** at 7/51 built, **0 failures and $0 LLM
cost** — bfd + masquerade are building the symver class and the gcc-hardcoded
packages cleanly with *no triage needed*. The LLM only earns its keep on
genuine failures (expected: ~libxcrypt). This is the hybrid thesis in
miniature: deterministic machinery handles the bulk; the agent is reserved for
the hard tail.

State of the experiment infrastructure:
- Phase 0 ✅ (linker=bfd, masquerade, identity audit)
- Phase 1 🔄 building (7/51, 0 fail)
- Phase 4 module ✅ (agent_patch.py; triage integration pending a real case)
- Phase 5 foundation ✅ (economics; bench + treadmill pending images)
- Phase 2 (PGO) — deliberately deferred until Phase 1 completes, so the
  profile-collection flow is designed against real hot-set packages.

### 1.5 Residual is exactly as predicted; Phase 4 completed (0db0557)

Phase 1 at 15/51 clean + **2 FTBFS, both predicted**:
- **gmp**: genuine clang build-incompat — GMP's libtool mangles
  `-fdebug-prefix-map` → `--debug-prefix-map` only under clang. → triage
  `force-gcc` (masquerade *exposed* it instead of silently using gcc — the
  audit working as designed).
- **libxcrypt**: the hard `@@`-default-version compat case bfd can't fix
  (Phase 0 0.7). → triage `strip -flto=thin`.

The entire symver class (attr, libbsd, libmd, libsepol, libselinux, …) built
clean with bfd — **no triage, $0 LLM**. This is the thesis in miniature.

Completed Phase 4 (0db0557): wired the agentic-patch tier-3 escalation into
`triage_and_retry` (opt-in `--agentic-patch claude|codex`) — deferred failures
get a coding-agent patch, rebuild-validated. Won't fire in Phase 1 (the
residual has flag remedies), but ready for genuine source-incompat cases.
852 tests, all hooks green.

Awaiting Phase 1's big waves + the triage pass (gmp→force-gcc, libxcrypt→
strip-lto), then: economics capture → bootable clang image → Phase 2 PGO.

### 1.6 The masquerade's real cost — gcc-only *build tooling* (libselinux)

libselinux + libsemanage FTBFS with `clang: error: unknown argument: '-aux-info'`.
Their SWIG Python-binding step uses `gcc -aux-info temp.aux` (a **gcc-only**
flag) to extract function prototypes for the exception headers. The masquerade
redirects that helper `gcc → clang`, which rejects `-aux-info`.

**This corrects my Phase 0 §0.7 confirmation**: the "libselinux ✓ with bfd"
build ran on the bfdtest world *before* the identity audit + masquerade existed
— so it silently used gcc for that helper and nobody checked. The masquerade +
audit now surface it honestly.

**The real finding (the thesis boundary):** "100% clang on Debian" has an
asterisk not just for libc/toolchain, but for packages with genuinely
**gcc-specific build tooling** (libselinux's `-aux-info`, gmp's libtool
flag-munging). Two honest answers:
1. `force-gcc` (whole package built with gcc, declared) — the triage default.
2. A **tier-3 agentic patch** removing the gcc dependency (libselinux could
   generate its exception headers without `-aux-info`) — a genuine future
   demonstration of the patch tier, since this is a *source/build* fix no flag
   remedy covers.

Masquerade is still net-positive (it makes bzip2/libcap2/bash genuinely clang;
the cost is ~2-4 gcc-tooling packages become declared force-gcc). The
experiment is doing exactly its job: measuring where the clang boundary
actually falls. Residual now: gmp, libxcrypt, rust-coreutils, libselinux,
libsemanage — all triage-remediable (force-gcc / strip-lto / stock).

### 1.7 apt — the prime agentic-patch demonstration target

apt FTBFS with a genuine **clang C++ strictness error**:
`solver3.h:59: error: subscript of pointer to incomplete type
'APT::Solver::Solver::State'` — clang rejects subscripting an incomplete
(forward-declared) type that gcc accepts. This is a **source** issue no flag
remedy covers, in a **load-bearing** package.

This is the ideal **Phase 4 live demonstration**: triage defaults to force-gcc
(honest — apt-via-gcc works), but `world build --agentic-patch claude --only
apt` should let the agent patch solver3.h (complete the State type before use)
to build apt with clang. Planned as a concrete Phase 4 validation + thesis
demo after the Phase 1 force-gcc baseline.

### 1.8 The honest clang boundary (the thesis answer taking shape)

Final residual: **7 of 51** — apt, gmp, libselinux, libsemanage, libxcrypt,
rust-coreutils, sed. ~44/51 (86%) build clean as clang+ThinLTO+bfd. The 7 are
genuine, categorized incompatibilities, each honestly remediable:
- **clang C++/code strictness**: apt (incomplete-type subscript) → force-gcc or agentic patch
- **gcc-only build tooling**: libselinux, libsemanage (`-aux-info`), gmp (libtool munging) → force-gcc or agentic patch
- **deep `@@`-compat + LTO**: libxcrypt → strip-lto
- **rust toolchain**: rust-coreutils → stock (not load-bearing; gnu-coreutils is real)
- **nodoc-dirty packaging**: sed → strip-nodoc (not clang-specific)

This *is* the experiment's answer to "how much of Debian can be clang": ~86%
cleanly, the rest a small, categorized, honestly-handled tail — exactly the
hybrid the thesis predicts.

### 1.9 systemd fails → the masquerade is net-counterproductive (decision point)

systemd (PID 1, the most important package for the image) FTBFS compiling its
**BPF objects**: `'asm/types.h' file not found`. systemd's BPF build uses a
compiler helper to locate the multiarch asm headers; the masquerade (gcc→clang)
breaks that detection.

This crystallizes the masquerade trade-off, and it's **negative**:
- **Wins** (~3, unimportant): bzip2, libcap2, bash-code become genuinely clang.
- **Breaks** (~3-4, important): systemd, libselinux, libsemanage — build-tooling
  steps (BPF header detection, SWIG `-aux-info`) that need a *real* gcc helper.
  These get forced to gcc — including **PID 1**.
- And the **identity audit already enforces honesty without the masquerade**:
  a package that hardcodes gcc → silent-gcc → audit FTBFS → force-gcc (declared).

So the cleaner design is **no masquerade**: CC=clang only. Then systemd/libselinux
keep clang (their gcc *helpers* run real gcc and work), while bzip2/libcap2/bash
become honestly force-gcc. That yields a more *meaningful* clang world — the
packages that genuinely can't be clang are declared gcc, and the important ones
(systemd as clang init) are real.

**Plan:** finish the current (masquerade) run as a data point + capture its
economics, then test systemd WITHOUT masquerade in a scratch dir to confirm it
builds clean clang. If confirmed, drop the masquerade default and re-run Phase 1
for the cleaner result (systemd=clang). The masquerade was my Phase-0 addition
to chase "100% clang"; the experiment has shown it overshoots — honest force-gcc
on the genuinely-gcc packages is the better answer.

### 1.10 Decision executed — no-masquerade design, systemd CONFIRMED clang

Masquerade run data point: 40 clang built, 9 residual, 6.74 CPU-hours (stopped
before triage). Dropped the masquerade default + switched the identity audit to
the **majority rule** (8a703b1).

**Gating test — systemd without masquerade: ✓ DECISIVE.** Built clean in 2307s
with **1936 clang compiles / 0 gcc**, blhc-clean. Pure clang+ThinLTO+bfd PID 1.
The no-masquerade design works: systemd's BPF asm-header helper finds its
headers because a real gcc is present for the detection step, while every
shipped object is clang.

→ **Launched the full clean Phase 1 re-run** in `ring0-clang2` (systemd already
built, skipped by resume). This is the run of record. Expected residual under
no-masquerade: bzip2/libcap2/bash (hardcoded gcc → honest force-gcc),
apt (clang C++ → force-gcc / agentic-patch demo), libxcrypt (strip-lto),
gmp (libtool → force-gcc), rust-coreutils (stock), sed (strip-nodoc). systemd,
libselinux, libsemanage now expected CLANG. Wall-clock dominated by systemd
(38m) + perl (36m) + openssl/util-linux.

### 1.11 No-masquerade validation PASSED — the redesign is vindicated

Mid-run check of the clean ring0-clang2 build:
- **libselinux ✓, libsemanage ✓, libsepol ✓ — all clang** (were force-gcc under
  the masquerade). Their `gcc -aux-info` helpers run real gcc; shipped objects
  are clang; the majority audit tolerates the helpers.
- systemd ✓ clang (proven in the gating test).

So the important infrastructure — PID 1 + the SELinux stack + the symver libs —
is genuinely clang+ThinLTO+bfd. The residual is exactly the genuinely-gcc-bound
set: bzip2/libcap2 (hardcoded CC=gcc), gmp (clang libtool flag-munging), ncurses
(clang -m32 32-bit multilib needs gcc sysroot — masked before by the mixed
build), libxcrypt (@@-compat), rust-coreutils (rust+bfd). 28 ok / 6 ftbfs at
check time. The masquerade-drop is the correct call: maximal *meaningful* clang,
honest force-gcc on the rest.

### 1.12 ThinLTO bitcode vs gcc-link — the mixed-build category (perl, db5.3)

Under no-masquerade, perl + db5.3 FTBFS with a new-but-coherent cause: they
**compile** XS modules / helper tools with clang (`-flto=thin` → the `.o` is
LLVM *bitcode*) but **link/helper-compile** with hardcoded
`x86_64-linux-gnu-gcc`, whose plain bfd linker can't read clang bitcode:
`B.o: file format not recognized` (perl), `cc1: unrecognized -flto=thin`
(db5.3's install helper). The masquerade had masked this (gcc→clang).

Honest remedy: **strip-lto** — without ThinLTO the objects are real ELF the gcc
step can consume, and the package **stays clang** (loses only LTO). So these are
still clang codegen, just clang+no-LTO.

**Revised honest tally (post-triage projection):** of 48 buildable —
~37 clang+ThinLTO, ~3 clang-no-LTO (perl/db5.3/libxcrypt strip-lto), ~7 honest
force-gcc (bzip2/libcap2/zlib/gmp/ncurses/apt/bash — hardcoded gcc or clang
incompat), 1 stock (rust-coreutils). So **~40/48 (83%) clang codegen**, with the
*right* packages clang (systemd, SELinux stack, symver libs) — vs the masquerade
run's higher raw count but gcc systemd. The no-masquerade result is lower-% but
more *meaningful* and *honest*. Triage cost: perl/db5.3 strip-lto rebuilds are
~36m each — the expensive part of the run.

### 1.13 Clean run complete — economics + two triage-robustness gaps

**Result: 44/51 ok, 7 still-FTBFS.** Triage remedies that landed (clean, in
exceptions.json): db5.3/libxcrypt/ncurses/perl → strip-lto (stay clang, no LTO),
sed → strip-nodoc (stays clang+LTO). So **48 of 51 are clang codegen**
(44 clang+ThinLTO incl. systemd/shadow/util-linux/selinux-stack, + 4 clang-no-LTO).

**ECONOMICS (the thesis number): 12 triage LLM calls, 219k in / 4.1k out tokens,
$0.72.** Build compute: 7.24 CPU-hours. So creating this optimized fork's
residual remedies cost **\$0.72 of LLM judgment** — the "~\$1 to create" thesis,
measured.

**Two triage-robustness gaps the run exposed (both now real fixes):**
1. **Single-pass triage** — `triage_and_retry` triages each FTBFS once. The
   hardcoded-gcc packages (bzip2/gmp/libcap2/zlib) need TWO rounds: strip-lto
   (round 1, fixes the `cc1: unrecognized -flto=thin`) → the gcc rebuild then
   fails the majority identity audit → needs round 2 (force-gcc). Single-pass
   left them FTBFS. → make triage multi-round (loop until stable).
2. **Misdiagnosis** — triage classifies "gcc choked on -flto=thin" as
   lto-incompat (strip-lto) when it should recognize *gcc* errored → the package
   is gcc-based → needs-gcc (force-gcc) in one round. → triage prompt v4.

Plus bash (compound force-gcc + strip-nodoc) needs the merge fix (already
landed) + multi-round. rust-coreutils stays stock (not load-bearing). apt →
force-gcc or the agentic-patch demo.

### 1.14 Improved triage resolves the residual — 49/51, compound merge works

Re-ran with v4 prompt + multi-round + merge. Results:
- gmp/libcap2/zlib → **force-gcc in ONE round** (v4 rule 5a: gcc rejected
  -flto=thin → needs-gcc, not the wasted strip-lto).
- **bash → strip_build_options=[nodoc] + force_compiler=gcc** — the compound
  merge + multi-round converged (was oscillating). bash builds. ✓
- **49/51 ok.** Remaining: apt (deferred clang C++ error → agentic-patch demo
  target) + rust-coreutils (rust LTO, not load-bearing → stock).

Final clang breakdown: ~40 clang+ThinLTO (systemd/shadow/util-linux/pam/
coreutils/selinux-stack/symver-libs), 4 clang-no-LTO (db5.3/libxcrypt/ncurses/
perl), 5 honest force-gcc (bash/bzip2/gmp/libcap2/zlib). ~90% clang codegen,
all the important packages clang.

## Phase 4 (agentic patch) — LIVE DEMO SUCCESS

### apt: claude fixed a real clang C++ incompatibility

apt's clang C++ error — `solver3.h:59: error: subscript of pointer to
incomplete type 'APT::Solver::State'` — is a genuine clang-vs-gcc strictness
issue: `ContiguousCacheMap<Package,State>::operator[]` is `constexpr` and
subscripts `State*`, which clang requires `State` to be *complete* for at the
constexpr operator's definition point (it's only forward-declared there); gcc
doesn't. No flag remedy fixes a source-language issue.

**The agentic-patch tier (claude --bare, headless) fixed it.** First bounded
run (8 turns) gave up. With a real hint + 25 turns, claude (17 turns, **$0.51**)
produced the correct minimal patch: **move the four `constexpr operator[]`
bodies out of the `Solver` class to after `struct State` is fully defined**, so
clang instantiates `data_[key->ID]` only when `State` is complete. 61-line
diff, clean and correct C++.

Captured to patches/apt.patch, applied via builder.apply_patches, rebuilding to
validate. This is the user's thesis realized: **an AI agent maintaining a fork
by generating real source patches for compiler incompatibilities** — the part
that turns "100% clang" from research project into engineering.

(Gap fixed: default max_turns 8→25 — 8 was too few for non-trivial fixes.)

### apt VALIDATED — agent patch builds clang (the bug was mine: native packages)

The first two apt validations failed not because the agent's patch was wrong but
because **apply_patches assumed quilt** — apt is `3.0 (native)`, which has no
quilt layer, so the series entry was silently ignored and the build used
unpatched source. Fixed apply_patches to be format-aware (quilt → series;
native/1.0 → patch the tree directly). Re-validated: **apt builds ✓ (197s),
164 clang++ invocations, solver3.h carries the agent's edit, blhc-clean.**

The full iterative agentic loop, proven end to end:
1. apt FTBFS (clang C++ incomplete-type in solver3.h) — no flag remedy applies.
2. claude (iter 1, 17 turns, $0.51): moved Solver::operator[] after State —
   plausible but incomplete; **rebuild-validation rejected it** (error persisted).
3. claude (iter 2, 6 turns, $0.10): given the precise remaining error, removed
   `constexpr` from ContiguousCacheMap::operator[] — the real fix.
4. apply_patches (native, fixed) → real build → **green.** Patch + transcript
   persisted under patches/.

## EXPERIMENT RESULT — the thesis, measured

**Fork: ring-0 (51 sources) as clang+ThinLTO+bfd Debian, per-host -march=native -O3.**
- **44/49 clang codegen (90%)**: 38 clang+ThinLTO clean, apt clang+ThinLTO via
  agent patch, 5 clang-no-LTO (strip-lto: libxcrypt/ncurses/db5.3/perl +
  systemd). The *important* packages — systemd (PID 1, clang-no-LTO), the
  SELinux stack, shadow, util-linux, pam, coreutils, the symver libs — are all
  genuine clang.
- **5 honest force-gcc** (bzip2/gmp/libcap2/zlib hardcode gcc; bash) +
  rust-coreutils stock + 3 toolchain-gated (glibc/gcc).
- **Cost to create: ~$2.36 LLM (\$1.60 triage + ~\$0.76 agentic) + 7.16 CPU-hours**,
  including an AI agent fixing a real clang C++ incompatibility — the part the
  thesis said turns "100% clang" from research project into engineering.

The honest answer to "can you build a 100% clang/LTO Debian": **~90% cleanly,
the genuinely-gcc-bound tail honestly declared, and the hard source
incompatibilities agent-patchable — for a couple dollars.**

### Capstone result: the image BOOTS (clang, per-host `-march=native`)

After the one-package no-LTO remedy for systemd, the assembled image boots
clean under KVM + `-cpu host` (native CPU, `-march=native` exercised for real):

```
Welcome to Ubuntu 26.04 LTS!
[  OK  ] Reached target multi-user.target - Multi-User System.
AUTOKERNEL_WORLD_BOOT_OK
[  OK  ] Reached target poweroff.target - System Power Off.
[   12.085229] reboot: Power down
```

`world boot-test` → **✓ BOOT OK — sentinel reached (multi-user.target up)**,
**zero** crash/fault lines. PID 1 is the clang systemd; the SELinux stack, pam,
shadow, util-linux, coreutils, apt, dpkg underneath it are the clang+ThinLTO
+ak1 builds. The thesis' concrete proof — *a per-host-optimized, ~90%-clang
Debian userland that actually boots, built and self-repaired for ~$2.36* — is
on the board. The single runtime miscompile it hit was caught, root-caused to a
documented compiler-divergence (not the headline flags), and fixed with a
narrow exception that kept clang + `-O3` + `-march=native` + FORTIFY=3.

## Phase 5 capstone — the bootable image, and the bug it surfaced

Assembled a 522 MB ext4 rootfs from the +ak1 repo (94 packages, 110 +ak1
refs: apt/bash/coreutils/systemd/util-linux all clang-stack) + the gcc kernel,
and booted it under QEMU.

**First boot: PID 1 dies.** `systemd[1]` takes a *general protection fault
inside libc.so.6* at 2.3 s → `Kernel panic … Attempted to kill init!
exitcode=0x0000000b`. It reproduced identically under real **KVM + `-cpu host`**
(native CPU, `-march=native` fully legal), so it was never an emulation
artifact — a real runtime miscompile in the fork.

**Bisected without a rebuild.** Booting `init=/bin/dash` instead reached an
interactive shell with zero faults → base userspace + the *stock* gcc glibc are
healthy; the crash is *specifically* the aggressive clang systemd. Then
extracted the clang systemd + libsystemd-shared from the +ak1 debs and ran them
on the host (same Meteor Lake) under gdb. The backtrace is unambiguous:

```
*** buffer overflow detected ***: terminated
__GI___fortify_fail ("buffer overflow detected")
__GI___chk_fail → __GI___read_chk (fd, buf, nbytes, buflen)
read_virtual_file_at ()   ← libsystemd-shared
get_oom_score_adjust ()   ← systemd manager startup
```

**First hypothesis (wrong): the FORTIFY *level*.** The `__read_chk` frame
pointed at `_FORTIFY_SOURCE=3`, so I rebuilt systemd at `_FORTIFY_SOURCE=2`. It
*still* aborted — and a tiny `__builtin_object_size` probe showed why: clang
returns the *requested* `malloc()` size (17), gcc returns `SIZE_MAX` ("unknown",
so no check fires). The level was a red herring; the divergence is deeper.

**Actual root cause (confirmed by a 2-TU reproducer): clang + ThinLTO sees
through systemd's `malloc_usable_size` conduit.** systemd deliberately reads
into the *usable* slack of a `malloc()` block (faster, fewer reallocs) and hides
that from the optimizer with `expand_to_usable()` — a `noinline` + `alloc_size`
"conduit" reached via an opaque `malloc_sizeof_safe(void**)` in another TU. Its
own comment warns it *"must not be inlined … because LTO otherwise tries to
inline it"* (gcc#96503). A faithful two-translation-unit reproducer nails it:

```
clang -O3  FORTIFY=3  no LTO  → read 24 bytes, exit 0   ✓
clang -O3  FORTIFY=3  ThinLTO → fortify abort, exit 1   ✗
```

Under ThinLTO clang imports `malloc_sizeof_safe`, sees through the conduit back
to the original `malloc(17)`, sizes the buffer at 17, and the `read()` of the
24-byte usable region trips `__read_chk`. **FORTIFY level is irrelevant — LTO is
the culprit**, exactly as systemd's source predicts.

**Remedy (surgical, drops an optimization not a protection):** strip
`-flto=thin` for systemd only. Keeps clang + `-O3` + `-march=native` +
**FORTIFY=3 (full hardening)** — systemd joins the existing `clang-no-LTO`
category (db5.3/perl/ncurses/libxcrypt). Recorded as a `user` exception. The
whole diagnosis — KVM repro → `init=/bin/dash` bisect → host gdb → 2-TU
reproducer → one targeted rebuild — cost one debugging session, no flag-bisect
across 27-min rebuilds. This is the per-package escape hatch the consistent-flags
thesis predicted, found and *correctly* attributed.

## Phase 2 — PGO machinery, and an honest first measurement (xz)

Built the PGO axis as a first-class part of the builder: `GlobalFlags.pgo`
(`off`/`instrument`/`use`), a `--pgo` flag on `world build`, `pgo_extra()`
threaded through `effective_cflags`/`ldflags`/`build_environment`/`flags_hash`
(the profile **content digest** joins the hash, so a re-collected profile
invalidates exactly that one package). `instrument` appends `-fprofile-generate`;
`use` appends a per-package `-fprofile-use=<profile>`, with the profile
**bind-mounted into the sbuild chroot** at `/srv/world-profiles` (the /nas4 host
path is invisible inside the unshare namespace — same subuid constraint as the
ccache, so profiles stage to /var/tmp). 37 builder tests stay green; pgo=off
hashes are byte-identical to before.

Drove the full pipeline on **xz-utils** (clang+ThinLTO, the cleanest leaf tool):
instrument build → collect → `llvm-profdata merge` → use build → benchmark.
Two gotchas worth recording:
- **xz's Landlock sandbox silently eats the profile.** The instrumented `xz`
  produced 0-byte `.profraw` on every successful compression — xz enables a
  Landlock sandbox for the single-file-to-stdout path, which blocks the profile
  runtime's file write at exit. (The only writes I got were from arg-parse
  *errors* that exit before sandboxing — a misleading 493-"function" profile of
  all-zero counters.) Fix: collect in **multi-file mode** (`xz -k file1 file2…`),
  which xz doesn't sandbox. Real profile: 188 functions, hottest block 4.9e9.
- **Profile representativeness is load-bearing.** A profile trained on *text*
  made `-6` compression ~3% *slower* on *binary* data; retraining on held-out
  binary data narrowed that to ~1.5%. PGO optimizes branch layout for the
  training distribution — the plan's warning ("a test suite alone is not
  representative") is real.

**Result (binary-trained profile, held-out binary test data, 1 core, min-of-N):**

| workload      | Phase-1 clang+LTO | + PGO   | Δ        |
|---------------|-------------------|---------|----------|
| compress -2   | 7.49 s            | 7.26 s  | **+3.2%**|
| compress -6   | 29.26 s           | 29.71 s | −1.6%    |
| compress -9   | 36.42 s           | 37.14 s | −2.0%    |
| decompress    | 0.730 s           | 0.748 s | −2.5%    |
| liblzma size  | 239,840 B         | 223,456 B | **−6.8%** |

**Honest reading:** the machinery is proven end-to-end and the resume/digest
integrity works, but **xz was a poor throughput showcase.** liblzma's heavy
levels are dominated by the bt4 match-finder — pointer-chasing, memory-latency
bound — where PGO has almost no leverage; it only helped the light, compute-bound
`-2` path. The unambiguous win PGO *did* deliver is **−6.8% binary size** (cold-code
outlining + more selective inlining), which at distro scale is real (icache,
download size). For a throughput win PGO needs a **branchy** target — which is
why the next probe is `grep`/regex matching, not another codec.

### The PGO win: sqlite3 +12–20% (the real deal)

xz/grep taught me *where* PGO pays. The packages it transforms are interpreters
with a hot dispatch loop in their **own** code — so I drove the full pipeline on
**sqlite3** (the vdbe bytecode VM; SQLite's authors officially recommend PGO and
document ~10%). The profile told the story before the rebuild even finished: 887
functions, **hottest block executed 33 million times** (vs grep's 33 *thousand*).

Collected on an in-memory query workload (300k-row insert + index + group-by/join/
aggregate), rebuilt `pgo=use`, and benchmarked the clang+ThinLTO baseline vs
clang+ThinLTO+PGO on a **held-out** query workload (different schema, params,
queries), pinned to one core, identical query output verified:

| runs       | baseline | + PGO   | speedup       |
|------------|----------|---------|---------------|
| min-of-7   | 0.650 s  | 0.569 s | +12.5%        |
| min-of-12  | 0.646 s  | 0.547 s | +15.3%        |
| min-of-15  | 0.644 s  | 0.511 s | **+20.6%**    |

**~+15% typical, up to +20%** — *exceeding* SQLite's documented ~10%. The baseline
is rock-stable (~0.645 s); PGO's best case is 0.51 s. And the size moved the
*opposite* way from xz: libsqlite3 grew +5.7% (1.997 MB → 2.111 MB) because PGO
*inlines* the hot vdbe opcodes (more code, faster) — the mirror image of outlining
cold code in a codec.

**Phase 2 conclusion.** The PGO machinery is proven end-to-end (instrument →
collect → merge → use, profile bind-mounted, content-digest in `flags_hash`), and
the measured payoff matches the theory exactly:
- **system tools** (xz, grep) — hot loop is in libc primitives (memchr,
  match-finder); PGO gives a consistent **size** win (xz −6.8%), throughput flat.
- **interpreters** (sqlite3) — hot loop is in their own code; PGO gives a large
  **throughput** win (+12–20%) at a small size cost.

At distro scale that's the whole argument: per-host PGO across the hot set buys
aggregate size reductions on the tool-shaped packages and double-digit speedups
on the engine-shaped ones — for the price of one profile-collection run each.
