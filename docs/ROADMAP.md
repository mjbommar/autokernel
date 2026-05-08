# Roadmap

The honest framing: **autokernel is becoming "Linux from your
hardware"** — an LLM-driven generator that takes a host's hardware +
intent and produces a minimal, fast, secure system tuned to it.

Current state (v0.14): we optimize the **kernel**. The natural arc
is to push outward layer-by-layer until autokernel can build a
complete bootable image.

```
v0.10  optimize kernel config (deterministic + LLM trim)         [done]
v0.13  multi-axis kernel optimization (4 dimensions)             [done]
v0.14  closed-loop hill-climber                                  [done]
─────  ↑ kernel only ↑ │ ↓ whole system ↓  ─────────────────────
v0.15  clang default + LTO + iterate --execute live + docs       [next]
v0.16  minitram — minimal initramfs from snapshot evidence
v0.17  PGO + AutoFDO (clang) — workload-profiled kernel
v0.18  autokernel distro — minimal userspace generator
v0.19  closed-loop iterate over kernel + userspace
v1.0   "Linux from your hardware" — bootable image generator
```

## Layer-by-layer goals

| Layer | Ubuntu typical | autokernel target | Win source |
|---|---:|---:|---|
| Bootloader | GRUB ~5 MB | EFI-stub built into kernel | Skip GRUB on UEFI |
| Kernel image | 17 MB | **16 MB** *(done)* | LLM judgment over choices/toggles |
| Initramfs | 40 MB | **3-5 MB** *(v0.16)* | Only what THIS host needs to boot |
| Modules installed | 700 MB | **~50-100 MB** *(v0.15)* | localmodconfig already + cleanup |
| Userspace base | 800 MB-2 GB | **30-150 MB** *(v0.18)* | busybox-static + chosen init + only used services |
| **Total bootable** | **2-4 GB** | **~80-200 MB** | Whole system fits on a small EFI partition |

## Each release in detail

### v0.15: hardening + reach (compiler, live e2e, docs)

| Item | Why |
|---|---|
| **clang as default compiler** | Required for CFI/LTO/KCSAN; better optimization passes; native `-march=native` works on both. Fall back to gcc with `--compiler=gcc`. |
| **`--lto={thin,full}`** | Clang thin-LTO is the modern recipe for kernel perf — typically 2-5% throughput improvement at significant build-time cost. |
| **`iterate --execute` live e2e** | The v0.14 closed loop has only been dry-run-tested. Need to see auto-revert under fire, real boot-test failures, real per-iteration size deltas. |
| **`docs/`** *(this folder)* | Roadmap + architecture + agents reference, so future sessions can pick up the thread. |
| **iterate dry-run wiring fix** | The dry-run path's review+apply doesn't currently chain because propose writes to `iter_dir/proposal.json` but review reads from `<snap>/proposal.json`. Small fix. |

### v0.16: `minitram` — minimal initramfs

Today Ubuntu's `update-initramfs` builds a 40 MB initramfs containing
every module that *might* be needed across all possible hosts.
autokernel knows what's load-bearing for THIS host (LUKS-in-chain?
LVM? RAID? specific DKMS?) and can build a **3-5 MB initramfs** with
exactly those.

```
autokernel minitram <snap> --kernel-source PATH
# → <snap>/initramfs.cpio.zst (~3-5 MB)
# Contents:
#   /init                   busybox-static early shell
#   /lib/modules/X.Y.Z/...  exactly the boot-path modules + their fw
#   /sbin/cryptsetup        only if LUKS in chain
#   /sbin/lvm               only if LVM in chain
#   /sbin/mdadm             only if MD/RAID in chain
#   /sbin/dropbear          (optional) headless rescue SSH
```

Reuses Snapshot evidence: `boot.luks_in_chain`, `boot.root_fstype`,
`block_devices`, `dkms`. No LLM in the hot path — pure deterministic
composition.

### v0.17: PGO + AutoFDO

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
