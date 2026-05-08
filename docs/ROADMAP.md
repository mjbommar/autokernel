# Roadmap

The honest framing: **autokernel is becoming "Linux from your
hardware"** — an LLM-driven generator that takes a host's hardware +
intent and produces a minimal, fast, secure system tuned to it.

Current state (v0.16.3): we optimize the **kernel** and generate a
**per-host minimal initramfs**. Closed-loop iteration with fitness
feedback. Clang validated end-to-end. The natural arc is to push
outward layer-by-layer until autokernel can build a complete
bootable image.

```
v0.10  optimize kernel config (deterministic + LLM trim)         [done]
v0.13  multi-axis kernel optimization (4 dimensions)             [done]
v0.14  closed-loop hill-climber                                  [done]
v0.15  clang default + LTO + docs                                [done]
v0.15.1 iterate --execute live e2e + kernel-only target          [done]
v0.16  closed loop closes (post-build chain, fitness trend,
       config_check in iterate)                                  [done]
v0.16.1 clang setup coverage all 6 distros + build pre-flight    [done]
v0.16.2 minitram — minimal initramfs from snapshot evidence      [done]
v0.16.3 clang actually used (CC=clang on argv) +
       kernel-only success panel + live-validated build           [done]
─────  ↑ kernel only + initramfs ↑ │ ↓ whole system ↓  ─────────
v0.17  PGO + AutoFDO (clang) — workload-profiled kernel          [next]
v0.18  autokernel distro — minimal userspace generator
v0.19  closed-loop iterate over kernel + userspace
v1.0   "Linux from your hardware" — bootable image generator
```

## Layer-by-layer goals

| Layer | Ubuntu typical | autokernel target | Win source |
|---|---:|---:|---|
| Bootloader | GRUB ~5 MB | EFI-stub built into kernel | Skip GRUB on UEFI |
| Kernel image | 17.43 MB | **15.44 MB** *(done — clang + four-axis)* | LLM judgment + clang `-march=native` |
| Initramfs | 41.5 MB | **3-5 MB** *(done — `autokernel minitram`)* | Only what THIS host needs to boot |
| Modules installed | 774 MB | **~50-100 MB** *(done — localmodconfig)* | localmodconfig + cleanup |
| Userspace base | 800 MB-2 GB | **30-150 MB** *(v0.18)* | busybox-static + chosen init + only used services |
| **Total bootable** | **2-4 GB** | **~80-200 MB** | Whole system fits on a small EFI partition |

## Live-validated bzImage comparison (Meteor Lake / Linux 6.19)

Same `final.config`, three compilers:

| Build | Compiler | bzImage | vs Ubuntu | Boot test |
|---|---|---:|---:|---|
| Ubuntu stock | gcc 14.x | 17.43 MB | — | not tested |
| autokernel + gcc 15.2 | gcc | 16.58 MB | **−4.9%** | PASS 0.4s |
| autokernel + clang 21.1.8 | clang | **15.44 MB** | **−11.4%** | **PASS 0.2s** |

Clang produces a ~7% smaller binary than gcc on the exact same Kconfig.
With `--lto=thin` (clang-only) the gap widens further.

## Each release in detail

### v0.15.0–v0.15.1: clang as default + iterate live e2e *(done)*

* **clang as default compiler.** `--compiler={clang,gcc,llvm}`; clang
  is the default. installdeps brings clang/lld/llvm in for all 6
  distro families. Required for CFI/LTO/KCSAN, smaller binaries.
  Fall back to gcc with `--compiler=gcc`.
* **`--lto={thin,full}`** opt-in flag.
* **`docs/`** (this folder) memorialized the layer-by-layer plan.
* Live `iterate --execute` end-to-end run uncovered three real bugs
  (`bindeb-pkg` deps wall, stale-artifact reads, dry-run review
  wiring) — all fixed in v0.15.1.
* New `--target=kernel-only` skips packaging deps; iterate uses it
  to validate the build+boot loop without `debhelper-compat`.

### v0.16.0: closing the closed loop *(done)*

The v0.15 live run found that the loop was open in three places —
all fixed:

* **`--base-config` chains from post-build `.config`** (what
  olddefconfig + localmodconfig actually settled on), not from the
  kfrag-merged `final.config`. Otherwise iter N+1 re-proposes
  symbols olddefconfig had already kept.
* **`config_check` invoked between apply and build** in iterate.
  Catches LLM hallucinations + dead-letter choices + out-of-range
  tunables before the build wastes time.
* **History block carries the fitness target's value** across rounds
  with steering guidance ("Kernel has GROWN — favor proposals that
  reduce binary size") so the LLM can correct course.

### v0.16.1: clang setup coverage *(done)*

* All 6 distro families (Debian, Fedora, Arch, SUSE, Gentoo, Alpine)
  ship clang/lld/llvm in `DistroSpec.build_deps`.
* `preflight --for build` requires clang + ld.lld in addition to
  gcc/make/ld; install-deps maps each to the right package per family.
* `autokernel build --execute` pre-flights the compiler binary —
  fails fast with `→ autokernel install-deps --for build --execute`
  hint instead of a confusing mid-make "command not found".

### v0.16.2: `minitram` — minimal initramfs *(done)*

Today Ubuntu's `update-initramfs` builds a 41.5 MB initramfs
containing every module that *might* be needed across all possible
hosts. `autokernel minitram` knows what's load-bearing for THIS host
(LUKS-in-chain? LVM? RAID? specific DKMS?) and packs only those — a
**3-5 MB initramfs**.

```
autokernel minitram <snap> [--dropbear] [--execute]
# → <snap>/initramfs.cpio.zst
# Contents:
#   /init                   busybox-static early shell (always)
#   /lib/modules/X.Y.Z/...  exactly the boot-path modules + their fw
#   /sbin/cryptsetup        only if LUKS in chain
#   /sbin/lvm               only if LVM in chain
#   /sbin/mdadm             only if MD/RAID in chain
#   /sbin/dropbear          (--dropbear) headless rescue SSH
```

Reuses Snapshot evidence: `boot.luks_in_chain`, `boot.root_fstype`,
`block_devices`, `dkms`. No LLM in the hot path — pure deterministic
composition.

### v0.16.3: clang actually used + kernel-only success panel *(done)*

A real clang build on Meteor Lake revealed two final bugs from the
v0.15 default change:

* **Setting `CC=clang` in env wasn't enough.** The kernel's top-level
  Makefile reassigns CC, so env-only CC=clang gets shadowed and gcc
  ends up doing the actual compile. Fix: pass
  `CC=clang HOSTCC=clang` (or `LLVM=1`) on the make argv as
  command-line variables, which Kbuild honors.
* **`BuildResult.ok` required `deb_paths`** — but `--target=kernel-only`
  skips packaging, so success was always reported as failure. Fix:
  `BuildResult` gains `target` and `bzimage_path` fields; `ok`
  distinguishes kernel-only success (bzImage exists) from
  packaging-target success (deb/rpm/tar in deb_paths).

Live verified on Meteor Lake / clang 21.1.8: 15.44 MB bzImage, boot
test PASS in 0.2s. See live comparison table above.

### v0.17: PGO + AutoFDO *(next)*

Profile-Guided Optimization for the kernel itself. Both gcc and clang
support it; clang's AutoFDO (sampling-based) is the easier path. Two
main flavors:

* **Instrumented PGO**: build kernel-A with profiling instrumentation
  → boot it under representative workload → collect counters → build
  kernel-B using counters. Two-pass build; ~3-7% throughput improvement.
* **AutoFDO**: same idea but uses `perf` sampling on a stock kernel.
  Less invasive, lower headline gain (~1-3%), but no separate
  instrumented build needed.

**Why this is a natural fit for autokernel:**

1. We *already know the workload* (the four-axis context).
2. We *already have iterate* — turn it into a 3-pass loop:
   1. Round 1: build with `-fprofile-generate`
   2. Round 2: boot-test + run a workload-shaped probe
   3. Round 3: rebuild with `-fprofile-use` against round 2's data
3. We *already have boot-test* providing a controlled run environment
   for collecting samples.

Concrete CLI shape:

```
autokernel iterate <snap> --kernel-source PATH \
    --target=perf --pgo=instrumented \
    --workload-probe="apt full-upgrade --dry-run; stress-ng --cpu 8 --timeout 30s"
```

The `--workload-probe` is what gets executed inside the QEMU/virtme
boot to drive the profile collection. It should be representative of
the real workload — running `pgbench` for a postgres host, `ab` for an
nginx host, etc.

Detailed design doc: [`docs/PGO.md`](PGO.md).

### v0.18: `autokernel distro` — minimal userspace generator

The big new verb. Generates a complete minimal Linux based on the
optimized kernel + minitram. Drives off the same axes as the kernel.

```
autokernel distro <snap> --kernel-source PATH --output=usb \
    --workload=server --threat=balanced --aggression=aggressive
# Pipeline:
#   1. autokernel propose → final.config
#   2. autokernel build   → bzImage + modules
#   3. autokernel minitram → initramfs.cpio.zst
#   4. NEW: assemble userspace (busybox + chosen services + libc)
#   5. NEW: compose squashfs/erofs root
#   6. NEW: package as USB / qcow2 / OCI base layer
```

**Workload-aware userspace choices:**

| Workload | What's in /sbin | Init | Image size target |
|---|---|---|---|
| `server` | sshd, chrony, logrotate; nothing else | systemd or s6 | ~150 MB |
| `desktop` | minimal Wayland compositor (sway/labwc), audio (pipewire), font set | systemd | ~200 MB |
| `embedded` | busybox-only; whatever the user explicitly adds | runit or sinit | ~30 MB |
| `vm-guest` | virtio-tools, ssh, cloud-init | systemd | ~80 MB |

### v0.19: closed-loop iterate over the whole system

Once we have a generator, `iterate` becomes powerful:

```
autokernel iterate <snap> --kernel-source PATH --target=size --execute
# Round 1: full propose → ~150 MB image
# Round 2: trim systemd services that didn't run during 30-sec boot probe → ~120 MB
# Round 3: trim libraries that no binary linked against → ~80 MB
# Converged: 80 MB total, boot-tested, ready to ship.
```

Same hill-climber, fitness function spans kernel + userspace + services.
Auto-revert if removing systemd-resolved breaks DNS, etc.

## Comparable projects, for context

This isn't a new niche — but the **LLM judgment + per-host
customization** angle is.

| Project | Niche | What autokernel does differently |
|---|---|---|
| **Talos Linux** | Immutable Kubernetes nodes (~80 MB) | Generic, not per-host. autokernel customizes to YOUR hardware + workload. |
| **Buildroot / Yocto** | Embedded distro generator | Verbose UI (~thousand make options). autokernel = "scan + propose" = much shorter UX, LLM judgment. |
| **Alpine** | Lightweight general-purpose | Generic. autokernel = host-specific minimum. |
| **Distroless** *(Google)* | Container base; ~30 MB | Container-only. autokernel covers bare-metal + VMs too. |
| **NixOS minimal** | Reproducible, small | Declarative-by-hand. autokernel = LLM proposes the declarations. |
| **Clear Linux** | Intel-tuned; AVX-aggressive | Single-vendor optimized. autokernel responds to whatever CPU/GPU is actually present. |

## When does autokernel "make sense" vs. just install a distro

| Use case | Use autokernel | Use a stock distro |
|---|---|---|
| Cloud VM where you control workload | ✓ | |
| Appliance / kiosk with fixed hardware | ✓ | |
| Hardened-desktop with specific threat model | ✓ | |
| K8s node (minimum surface, fast boot) | ✓ | |
| Embedded device with tight flash budget | ✓ | |
| General-purpose desktop where you plug random hardware | | ✓ Stock |
| Distro you've never administered before | | ✓ Stock |
| Production server you don't fully understand | | ✓ Stock |

## What we're explicitly **not** building

- A new package format. Built-on-Debian/Fedora/Arch packages stay.
- A new init system. autokernel picks among existing options.
- A GUI / TUI distro installer. The CLI + the existing `autokernel`
  TUI for review is enough.
- A general-purpose distro. autokernel produces a host-specific
  distro per-snapshot — the artifact is opinionated to one machine.
