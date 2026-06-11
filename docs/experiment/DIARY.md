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
