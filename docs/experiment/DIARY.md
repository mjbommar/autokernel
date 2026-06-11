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
