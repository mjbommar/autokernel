# Boot time: 1619 ms → ~240 ms (−85%)

How the autokernel `world` image (clang/ThinLTO Debian-source rebuild, minimal
KVM-guest kernel) was taken from a 1.6 s boot to ~240 ms under QEMU/KVM, measured
end-to-end with [`scripts/boottime/`](../../scripts/boottime/README.md).

This is the **top-down reference**: the result, the final config, the rounds, and the
two ideas that *didn't* work. The chronological blow-by-blow (every measurement,
wrong turn, and fix) is in [`DIARY.md`](DIARY.md) under "Boot time, round 1…6".

---

## Result

| milestone | total | what changed |
|---|---:|---|
| baseline (stock generic kernel, untuned) | **1619 ms** | — |
| after console/device/kernel trimming (rounds 1–3) | **1018 ms** | minimal kernel + quiet console + leaner device model |
| **after killing PID1's dead-terminal probes (round 4)** | **323 ms** | the single biggest win — see below |
| after RCU-expedite + unit masking (round 5) | **246 ms** | |
| floor (round 6) | **~240 ms** | topology sweep, deopt fix; microvm disproven |

Split at the floor: **~72 ms kernel + ~158 ms userspace**. The userspace critical
chain is now structural — `multi-user.target ← serial-getty@ttyS0 ← dev-ttyS0.device`,
i.e. udev coldplug → console device appears → getty. That residual is not reducible by
tuning (see round 6).

## The headline finding (round 4)

After the kernel was minimal (126 ms) the remaining ~890 ms "userspace" was a mystery.
Attributing it with systemd's **own phase timestamps** (`gapprobe.py`) localized the
entire cost to a flat **685 ms gap between `SECURITY_FINISH` and `GENERATORS_START`** —
*before any unit runs*. It was two terminal escape-sequence probes that PID1 issues
during early startup, each blocking the full **333 ms** `CONSOLE_ANSI_SEQUENCE_TIMEOUT`
because QEMU's serial console never answers:

1. `fixup_environment()` → `query_term_for_tty()` → `terminal_get_terminfo_by_dcs()`:
   a **DCS `+q` terminfo query**. Short-circuited by a cmdline `TERM=`.
2. `console_setup()` → `terminal_fix_size()` → `terminal_get_size()`: a
   **CSI `18t` / DSR `\e[6n` window-size query**. Short-circuited by a cmdline console
   size.

Both are *documented systemd opt-outs* that PID1 checks **first** — so the fix is not a
patch, just declaring on the kernel cmdline the two facts a serial line physically
cannot report. The gap collapsed 685 ms → 6 ms; total boot 988 ms → 323 ms.

This is headless-serial-specific (our appliance/VM target). On real hardware a terminal
answers in microseconds and the cost never exists — which is *why* systemd probes. Pure
win where we deploy, no-op elsewhere; nothing upstream to fix.

## The production cmdline

All validated knobs live in [`src/autokernel/world/image.py`](../../src/autokernel/world/image.py)
(`boot_image()`), with per-knob rationale inline. Grouped:

```
# console I/O (round 1)
quiet loglevel=3 systemd.show_status=0
# KVM-guest no-ops (round 1)
tsc=reliable nowatchdog
# the dead-terminal fix (round 4) — biggest single win
TERM=linux systemd.tty.rows.console=24 systemd.tty.columns.console=80
# kernel boot-speed (round 5)
rcupdate.rcu_expedited=1 audit=0 no_timer_check
random.trust_cpu=on random.trust_bootloader=on
# mask appliance-irrelevant units (round 5) — drop a mask if your workload needs it
systemd.mask=e2scrub_reap.service systemd.mask=getty-static.service
systemd.mask=modprobe@drm.service systemd.mask=modprobe@efi_pstore.service
systemd.mask=modprobe@configfs.service systemd.mask=modprobe@fuse.service
systemd.mask=dev-hugepages.mount systemd.mask=dev-mqueue.mount
systemd.mask=sys-kernel-debug.mount systemd.mask=sys-kernel-tracing.mount
```

QEMU launch (also in `image.py`): `-machine pc` (**not** q35 — round 6 measured q35's
kernel-init ~33 ms slower here), `-cpu host` + KVM (mandatory for `-march=native`
worlds), `-smp min(4,cpus)` (boot is userspace-parallelism-bound). The minimal kernel
recipe is in [`kernel/`](kernel/).

## What the rounds did

| round | lever | delta |
|---|---|---:|
| 1 | console quieting (`quiet loglevel=3 systemd.show_status=0`) | −11% |
| 2 | leaner device model + service masks | trim |
| 3 | **minimal KVM-guest kernel** — drop SATA (288 ms link-down stall), i8042, md | −21% |
| 4 | **`TERM=` + console size** — kill the two 333 ms PID1 terminal probes | −67% |
| 5 | `rcu_expedited` + `audit=0` + unit masking (10 units) | −21% |
| 6 | topology sweep (smp4), q35→pc deopt fix; **floor reached** | ~0% |

Method note (round 5/6): single masks are sub-noise (~10 ms each, mostly parallel) on
the ±25 ms tmpfs jitter — only the *bundle* clears the floor. The honest approach is
bundle-then-confirm at higher n, not chase each sliver. n=9 sweeps over-promise; n=13
confirmation is the real read (several round-6 "wins" dissolved on confirm).

## Negative results (don't re-tread these)

- **io_uring batched unit enumeration** — the maintainer-endorsed idea for cutting PID1
  startup. The batchable `statx` storm is **already eliminated** by systemd's in-memory
  `unit_path_cache` (0 syscalls, 8500× better than io_uring could do), and the residual
  walk uses `getdents`/`readlinkat` which **have no io_uring op**. Mechanism proven (26×
  on a genuinely cold NFS `statx` storm) but inapplicable to boot. Full write-up:
  [`iouring/DIARY.md`](iouring/DIARY.md).
- **QEMU `microvm` + virtio-mmio** (round 6) — built a `CONFIG_VIRTIO_MMIO` kernel;
  every variant **regressed** (best 421 ms vs pc 237 ms; `acpi=on` ~1.1 s kernel). The
  premise was wrong: pc+KVM kernel-init is already only ~88 ms because **kvmclock** hands
  over the TSC frequency, so PCI/ACPI probe was never the bottleneck. microvm strips the
  timer/ACPI infra this kernel relies on and hits slow calibration/AP-bringup paths.

## Going below ~230 ms

Not a tuning problem anymore — an **architectural** one (a decision about what the image
*is*):

- **snapshot/restore** (QEMU `savevm` / Firecracker-style) — boot once, restore in tens
  of ms, skipping the whole udev/getty chain.
- **drop systemd from the critical path** — a single-purpose init for a one-workload
  appliance, no getty/login plumbing.

Both are deliberately left for a human decision rather than bolted on.

## Reproduce

See [`scripts/boottime/README.md`](../../scripts/boottime/README.md). In short:
`bootbench.py --ab` to A/B a cmdline change, `gapprobe.py` to attribute the userspace
phases, `campaign.py` / `campaign6.py` to sweep candidates with a noise floor.
