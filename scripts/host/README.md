# Host scripts (daily-driver laptop)

Operational scripts written while running autokernel kernels on the
Framework 13 (Meteor Lake, Ubuntu 26.04, LUKS). They are host-specific and
not part of the `autokernel` CLI. Each one documents its usage in its header.

| script | what for |
|---|---|
| `install-kernel-oneshot.sh RELEASE [--reboot]` | install an autokernel `.deb` pair from `~/.cache/autokernel/kernels` and arm a one-shot GRUB boot (the default entry is untouched) |
| `debug-boot.sh list\|arm\|status\|clean` | one-shot boot with a crash-capture cmdline (lockup panics, sysrq, loglevel=7) for freezes that leave no logs |
| `enable-crash-notifiers.sh` | `crash_kexec_post_notifiers` so panics land in EFI pstore (kdump can't unlock LUKS) |
| `purge-old-autokernels.sh [--yes]` | purge every installed autokernel kernel except the running/KEEP one; never touches `*-generic` |
| `sysctl-harden.sh [--mtu\|--undo]` | `/etc/sysctl.d/61-autokernel-hardening.conf`. **Never** sets `user.max_user_namespaces=0`; userns is restricted via AppArmor instead (see [docs/PATCHES.md](../../docs/PATCHES.md)) |
| `system-trim.sh` | disable unused services on this host; writes an undo file next to itself (gitignored) |
| `cups-probe-bisect.sh` | fsync'd breadcrumb bisect of CUPS backends/drivers. This is how the 7.3-rc5 Printers-panel reboot was traced to xz → nested Landlock → clang `__counted_by` FORTIFY panic |
| `verify-landlock-fix.sh` | post-boot check for that Landlock fix (nested `landlock_restrict_self`, xz round-trip, dmesg) |

Related, in `scripts/`:

- `vmtest/`: boot any bzImage in QEMU/KVM with a self-contained initramfs
  (nested Landlock repro, xz 5.8 round-trip, dmesg scan, CFI mode, userns
  sysctl, micro-benchmarks). Run it before installing a kernel:
  `scripts/vmtest/run.sh -p 2 name=/path/to/arch/x86/boot/bzImage`
- `bench-kernel.sh`: on-host A/B benchmark. Refuses to run on battery.
- `verify-userns-restrict.sh`: checks the AppArmor userns restriction (10 checks).
- `landlock-nested-repro.c`: the minimal reproducer (used by vmtest and verify).

## Kernel release routine (as done for 7.3-rc7)

1. In the kernel worktree: `git fetch --tags && git checkout --detach vX.Y-rcN`
2. `cp -a snapshots/<prev> snapshots/<new>`, drop its `build/`, and edit
   `final.config` (`CONFIG_LOCALVERSION`, anything else).
3. `uv run autokernel patches check <sets> --kernel-source <tree>`. Mark sets
   that come back all-`present` as merged in their `meta.toml`.
4. `uv run autokernel build snapshots/<new> --kernel-source <tree> --compiler llvm --lto thin --patches <sets> --execute`
5. `scripts/vmtest/run.sh -p 2 new=<tree>/arch/x86/boot/bzImage`
6. `sudo bash scripts/host/install-kernel-oneshot.sh <release> --reboot`, then
   check dmesg (FineIBT/CFI, oops), userns, and xz before running `grub-set-default`.
