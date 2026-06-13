# Boot-time harnesses

Measurement tools used to take the autokernel `world` image from **1619 ms → ~240 ms
boot (−85%)** under QEMU/KVM. The full investigation (six rounds, with the negative
results) is written up in [`docs/experiment/BOOT_TIME.md`](../../docs/experiment/BOOT_TIME.md);
the blow-by-blow log is in [`docs/experiment/DIARY.md`](../../docs/experiment/DIARY.md).

These are deliberately terse research scripts (stdlib only, no deps), not production
code — see the per-file ruff ignore in `pyproject.toml`.

## The tools

| script | what it does |
|---|---|
| `bootbench.py` | The core harness. Boots a disk image under KVM N times, drives the autologin serial shell to a marker, parses `systemd-analyze`'s own `(kernel, userspace, total)`, reports median/min/mean±stdev. `--ab` A/Bs two kernel cmdlines; `--chain` captures the critical-chain. All other tools import `boot_once`/`series`/`stats` from here. |
| `gapprobe.py` | Boots once and reads systemd's **own phase timestamps** (`UserspaceTimestamp…`, `SecurityFinishTimestamp…`, `GeneratorsStartTimestamp…`, `Finish…`) via `systemctl show`, to attribute *where inside userspace* the time goes without perturbing the timeline. This is what localized the 685 ms `SECURITY_FINISH → GENERATORS_START` gap (round 4). |
| `campaign.py` | Noise-floored A/B sweep of **kernel-cmdline** candidates against one shared baseline. Keeps only deltas above half the baseline spread (robust to tmpfs jitter). Used in round 5. |
| `campaign6.py` | Same method but varies **QEMU machine/topology kwargs** (`-smp`, `-m`, `-machine`, minimal device set) instead of the cmdline. Used in round 6. |
| `microvm_boot.py` | Boots the rootfs under QEMU's `microvm` machine (virtio-mmio, no PCI) vs the `pc` path. Kept for the record even though microvm **lost** (round 6 negative result). |

## Inputs (env-overridable)

The scripts default to the experiment's scratch paths; override per-invocation:

```bash
export AK_IMG=/path/to/rootfs.img            # raw disk image (virtio root = /dev/vda)
export AK_KERNEL=/path/to/bzImage            # direct-boot kernel (-kernel)
export AK_KERNEL_MICROVM=/path/to/bzImage    # virtio-mmio kernel (microvm_boot.py only)
```

`bootbench.py` also takes `--img` / `--kernel` flags directly.

## Reproduce the headline result

```bash
# A/B the round-4 fix (the two dead-terminal probes) — expect ~ -67%
python3 bootbench.py --img "$AK_IMG" --kernel "$AK_KERNEL" -n 7 \
  --append "quiet loglevel=3 systemd.show_status=0 tsc=reliable nowatchdog" \
  --ab     "quiet loglevel=3 systemd.show_status=0 tsc=reliable nowatchdog \
            TERM=linux systemd.tty.rows.console=24 systemd.tty.columns.console=80"

# Attribute the userspace gap with systemd's own clock
python3 gapprobe.py

# Sweep cmdline / topology candidates with a noise floor
python3 campaign.py        # kernel-cmdline knobs
python3 campaign6.py       # QEMU machine/topology knobs
```

The validated knobs are baked into `world/image.py`'s boot cmdline; see
[`docs/experiment/BOOT_TIME.md`](../../docs/experiment/BOOT_TIME.md) for the full list
and rationale.
