# Phase 0 report — symver linker spike

**Question:** can ThinLTO + `.symver` versioned symbols coexist without
per-package opt-outs, by choosing a linker other than lld?

**Answer:** **Yes for the common case — `linker=bfd` (ld.bfd +
LLVMgold) preserves `.symver` versioned symbols under full clang
ThinLTO** where lld's `--no-undefined-version` default hard-fails. A
small hard residual (libxcrypt-style `@@` default-version compat
schemes) still needs whole-package LTO-off. The remedy is therefore
**tiered**: bfd as the global clang-world default, strip-LTO for the
handful bfd can't fix.

## Method

Reproduced in a **bubblewrap** sandbox over the extracted clang sbuild
chroot (clang 21.1.8, ld.lld 21.1.8, ld.bfd 2.46, `LLVMgold-21.so` in
`/usr/lib/bfd-plugins/`; `unshare` is AppArmor-blocked but `bwrap` has
the userns grant). Then validated on **real packages** through the
builder with a new `GlobalFlags.linker` field, refereed by the real
`dpkg-gensymbols`.

## Findings

### The failure has three distinct causes (not one)

Audit of all clang ring-0 failures by true evidence (not the triage
label):

1. **symver/version-script** — `.symver` to a renamed exported symbol +
   `--version-script`; ThinLTO internalizes symbols the script names →
   `dpkg-gensymbols: disappeared` or `ld.lld: version script assignment
   ... symbol not defined`. attr, libbsd, libmd, libselinux, libsemanage,
   pam, systemd, util-linux, ncurses.
2. **deep `@@` default-version compat** (libxcrypt): generated
   `libcrypt.map.in`, `crypt_r@@XCRYPT_2.0`. The defect is *inside* the
   LLVM gold plugin (`default version symbol ... must be defined`, plus a
   plugin **segfault**) — upstream of the linker, so bfd can't fix it.
3. **gcc-got-clang-flag** (libcap2, bash, ncurses-partial): the package's
   build system **ignores `CC`** and invokes gcc, which then chokes on
   clang's `-flto=thin`. Not a symver problem at all — see the integrity
   finding below.

### Synthetic isolation

The isolated `.symver` + version-script pattern works under **all**
linkers including lld+ThinLTO (2/2 symbols). The failure is an
**emergent whole-library / cross-TU effect** — only the real multi-TU
build with `local: *` + `-Bsymbolic-functions` triggers lld's
`--no-undefined-version` error.

### Real-package validation (the decisive evidence)

Built through the builder with `linker=bfd`, full clang, ThinLTO **on**,
no LTO strip, no force-gcc:

| package | result | evidence |
|---|---|---|
| attr | ✓ | 26 versioned ATTR symbols present; 39 clang / 0 gcc compiles; 60 `flto=thin`, 11 `fuse-ld=bfd`; gensymbols passed |
| libbsd | ✓ | built clean |
| libmd | ✓ | built clean |
| libxcrypt | ✗ | `LLVM gold plugin: default version symbol crypt_r@@XCRYPT_2.0 must be defined` + plugin segfault — the hard residual |

(libselinux/pam/systemd confirmation in progress; see DIARY.)

## Decision

- **clang-world default: `linker=bfd`.** Implemented as
  `GlobalFlags.linker` (emits `-fuse-ld=bfd`; bfd auto-loads
  `LLVMgold.so` from `/usr/lib/bfd-plugins/`). Retires the symver class
  with zero per-package surgery, ThinLTO fully preserved, genuinely
  clang. The research rated this "untested/low-confidence"; the
  empirical result on real packages with the gensymbols referee
  confirms it for the common case.
- **Hard residual (libxcrypt-class): triage `strip -flto=thin`** — the
  existing `lto-incompat` remedy. The agent already handles these;
  they're a small number of leaf compat libs and lose only LTO, not
  clang codegen.
- **Per-TU `-fno-lto`** (the research's primary pick) is held in
  reserve; bfd made it unnecessary for the common case and it doesn't
  help the gold-plugin segfault case.

## Integrity finding (reshapes Phase 1)

`ring0-clang` was **not 100% clang**: libcap2 (gcc=19/clang=0) and bash
(gcc=422/clang=0) built entirely with gcc because their build systems
ignore `CC`; ncurses was mixed. The flag-grep audit only checks our
*flags* appeared — not that the *compiler* was clang. Phase 1 must add:
1. a **compiler masquerade** (PATH-prepend `cc`/`gcc`/
   `x86_64-linux-gnu-gcc` → clang) to force clang through hardcoded
   build systems; and
2. a **compiler-identity audit** that fails the build when gcc compiled
   the objects and `force_compiler=gcc` wasn't explicitly declared.

## Exit criterion

✅ `GlobalFlags.linker` implemented and tested; real attr/libbsd/libmd
rebuilt clang+ThinLTO+bfd with all versioned symbols intact; tiered
remedy decided; integrity finding documented and folded into Phase 1.
