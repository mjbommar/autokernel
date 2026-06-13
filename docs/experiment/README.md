# Experiment logs

Working notes and results from the autokernel `world` experiments (the clang/ThinLTO
Debian-source rebuild and its minimal-kernel image). Design lives in
[`../WORLD.md`](../WORLD.md); these are the lab notebooks.

| doc | what's in it |
|---|---|
| [`BOOT_TIME.md`](BOOT_TIME.md) | **Top-down summary** of the boot-time work: 1619 ms → ~240 ms (−85%), the final cmdline, the rounds, and the negative results. Start here. |
| [`DIARY.md`](DIARY.md) | The chronological blow-by-blow: clang/systemd boot fix, PGO, and boot-time rounds 1–6 with every measurement and wrong turn. |
| [`iouring/`](iouring/) | io_uring batched unit-file enumeration — mechanism proven (26× on cold NFS) but a **negative result** for boot (systemd's in-memory cache already wins). |
| [`kernel/`](kernel/) | Reproducible minimal KVM-guest kernel recipe (`build-minimal-kernel.sh` + config) used as the boot-time baseline. |
| [`phase0-symver.md`](phase0-symver.md) | Symbol-versioning notes from the clang world rebuild. |

The measurement harnesses are in [`../../scripts/boottime/`](../../scripts/boottime/README.md).
