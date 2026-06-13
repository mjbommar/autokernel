# io_uring batched unit-file enumeration — research/impl diary

Goal: research, design, implement, test, document, and iterate on io_uring-batched
unit-file enumeration for systemd — the maintainer-endorsed (Poettering, systemd
#16736) approach to cutting PID1 manager-startup time. Keep a careful testing log.

Origin: the autokernel boot-time work bottomed out at ~1018ms boot on a minimal
kernel; ~690ms of that is systemd PID1 manager-init *before any unit activates*.
Three design sub-agents + the source confirmed the cost is NOT parsing (already
lazy, closure-only) but **`unit_file_build_name_map()`** — the `readdir`/`stat`/
`readlinkat` metadata walk over the ~12-dir search path. systemd #16736 (open
since 2020) + #26950 (42,162 dir lookups, ~6s on eMMC) corroborate.

## The target function (systemd main @ 92e4afc, src/shared/unit-file.c:364)

`unit_file_build_name_map()` algorithm:
1. mtime-hash early-out (`lookup_paths_timestamp_hash_same`) — if unchanged, skip.
2. `chase()` each of ~12 search dirs (resolve symlinked dirs; readlink/stat each).
3. For each search dir, in priority order:
   - `opendir()` + `FOREACH_DIRENT_ALL` (getdents loop).
   - DT_REG valid unit name → add to ids map (no per-file stat — d_type from getdents).
   - DT_LNK valid unit name → `unit_file_resolve_symlink()` = **`readlinkat`** + resolve.
   - DT_LNK `.wants`/`.requires`/`.d` dir-symlink → `readlinkat_malloc` + `is_dir` (statx).
4. Build reverse (alias) map from the ids map (in-memory, no syscalls).

**Syscall inventory per build:**
- `opendir`/`openat`: ~12 (+ chase opens)
- `getdents64`: ~12+ (one loop per dir; NOT io_uring-batchable — no GETDENTS op)
- `readlinkat`: **one per symlink** — the `.wants`/`.requires` enabled-unit symlinks
  + unit aliases. This is the storm (hundreds on a populated system).
- `statx`: `is_dir` checks on dir-symlinks.

**io_uring-batchable:** `IORING_OP_OPENAT`, `IORING_OP_READLINKAT` (5.16+),
`IORING_OP_STATX`. NOT batchable: `getdents` (must readdir serially first to learn
which entries are symlinks). So the design is: serial readdir to enumerate symlinks
(cheap, ~12 dirs), then **batch all the readlinkat/statx** in one ring.

## Plan
- [P1] Research: exact syscall pattern (done above). Verify io_uring op availability.
- [P2] Attribution: microbench replicating the serial walk on a real unit tree;
       count + time each syscall class, across storage tiers (tmpfs/NVMe/NFS) to
       span the warm-cache (syscall-overhead) → cold/slow (I/O-latency) spectrum.
       GATE: if readlinkat/openat/statx aren't the bulk, io_uring can't help.
- [P3] Implement io_uring batched readlinkat (+openat+statx); compare vs serial.
- [P4] Iterate: queue depth, SQPOLL, registered fds, linked SQEs.
- [P5] Correctness: io_uring build produces byte-identical name map vs serial.
- [P6] Document + draft the systemd patch / RFC framing.

## Setup
- Host: kernel 6.18.0-9, systemd 259, 20 cores. liburing 2.14 (static .a extracted
  no-sudo to /data1/iouring/src/deps/prefix). systemd source @92e4afc in
  /data1/iouring/src/systemd. Work + benches in /data1/iouring/.
- Storage tiers for I/O-latency spectrum: /dev/shm (tmpfs/RAM), /data1 (NVMe),
  /nas4 (NFS — high per-syscall metadata latency, our eMMC stand-in).

---

## Log

### [P1] CRITICAL research finding — readlinkat/getdents are NOT io_uring ops

Checked the kernel UAPI op enum (linux 6.18 + 7.0.12): `IORING_OP_*` for fs metadata
is only OPENAT / OPENAT2 / STATX (+ the mutating mkdirat/symlinkat/linkat/renameat/
unlinkat). There is **no READLINKAT op and no GETDENTS op**. So:
- `getdents` (readdir loop) — NOT batchable → directory enumeration stays serial.
- `readlinkat` (alias/symlink resolution in name-map build) — NOT batchable → serial.
- `openat`, `statx` — batchable. ✅

So the naive "batch the readlinkat storm" is impossible. The real io_uring-batchable
storm is **`statx`** — the drop-in/dir-existence checks (#26950: 42,162 lookups). systemd
checks, per unit, whether `<searchdir>/<unit>.d/` (and `.wants`/`.requires`) exist across
ALL ~12 search dirs — overwhelmingly misses (ENOENT). That's a statx storm io_uring can
hide. Corpus: 546 unit files, 341 symlinks (46 top-level aliases → serial readlinkat;
295 inside .wants → not in name-map build), 70 .d/.wants/.requires dirs.

Revised hypothesis: io_uring helps the **statx (drop-in existence) + openat** storm, and
only meaningfully on **slow-metadata storage** (the #16736 eMMC case). On warm tmpfs each
statx is ~1µs, so the storm is ~ms not the 690ms — meaning the warm-VM 690ms is likely
NOT enumeration syscalls (more likely graph-build CPU or device/mount enumerate). GATE
stands: measure the real split before claiming a VM win.

### [P2/P3] Attribution + the mechanism PROVEN

Microbench (enum_bench.c) replicates Phase A (serial walk: opendir/getdents/readlinkat)
+ Phase B (the io_uring-batchable drop-in statx storm: 574 units × 12 dirs × 2 = 13,776
statx, ~systemd #26950's pattern). Results:

| storage           | PhaseB serial | PhaseB io_uring qd256 | speedup |
|-------------------|---------------|------------------------|---------|
| warm (NVMe/tmpfs) | 4–9 ms        | ~7.4 ms                | ~neutral (ring overhead ≥ cached statx) |
| NVMe cold (uniq)  | 13.6 ms       | 7.72 ms                | 1.8×   |
| **NFS cold (uniq)** | **122,151 ms** | **7.30 ms**          | **~16,700×** |

Plus PhaseA on NFS = 92 ms (getdents+readlinkat, NOT io_uring-batchable).

**Verdict: the io_uring batching mechanism is real and, on latency-bound storage,
enormous.** 13,776 cold NFS statx serially = 122 s of serialized ~9 ms round-trips;
with 256 in flight, the latency is fully hidden → 7.3 ms (now ring-throughput-bound,
~1.9M statx/s). Correctness verified: serial and io_uring report identical hit/miss
counts (13,776 miss cold; 586 hit warm).

**Honest scope (this is the crux for the upstream pitch):**
- Win is on COLD / slow-metadata storage: first boot after image deploy, slow eMMC
  (#16736's actual environment), NFS-root. There the drop-in storm is seconds.
- On WARM cache (most reboots; our tmpfs VM) the storm is ~ms and io_uring is neutral-
  to-slightly-worse — so it must be opt-in / auto-fallback, never forced.
- Our specific VM 690ms is NOT this storm (warm tmpfs → storm ~5ms). So io_uring won't
  speed OUR boot; it speeds the slow-storage first-boot case the upstream issue is about.
  Reporting this honestly rather than overclaiming a VM win.

### [P2/P3] Measurement-integrity catch + corrected fair numbers

CAUGHT a confound: the first cold run reused the same `-cN` tokens across the serial
and io_uring processes, so NFS served io_uring's paths from the negative-dentry cache
the serial run had just populated → io_uring's "7.30 ms" was secretly WARM. Fix: tokens
now include getpid() so every run probes globally-unique, genuinely-cold paths.

Corrected FAIR cold NFS (process-unique paths, 13,776 statx):
| serial      | io_uring qd64 | qd256    | qd1024   |
|-------------|---------------|----------|----------|
| 116,174 ms  | **4,439 ms**  | 4,473 ms | 4,821 ms |

**Real cold win ≈ 26× (116 s → 4.4 s)** — io_uring hides ~96% of the per-statx round-trip
latency. qd≈64 is the sweet spot; deeper queues regress slightly (NFS client/ring overhead).
The earlier "16,700×" was a cache-contamination artifact — recording the correction because
honest testing > a flashy wrong number.

Standing picture: io_uring statx batching is a large, real win on cold/slow-metadata
storage (first boot, eMMC, NFS-root — the #16736 case), ~neutral (≤2 ms) on warm cache.

### [P3 DECISIVE] systemd already eliminates the io_uring-batchable storm — in-memory, better

Read the real boot path: `unit_find_dropin_paths(u, use_unit_path_cache=true)` →
`unit_file_find_dirs()` → the guard `if (!unit_path_cache || set_contains(unit_path_cache, path))`
(src/shared/dropin.c:164). `m->unit_path_cache` is the `path_cache` built once by the first
`unit_load_fragment` → `unit_file_build_name_map(..., &u->manager->unit_path_cache)`
(load-fragment.c:6127), reused for all units (mtime-hash early-out). So before any expensive
chase()/statx, systemd checks an **in-memory set**; the ~13K non-existent drop-in candidates
are rejected with **zero syscalls**. Measured (NFS cold):

| drop-in search variant                         | time        | statx done |
|------------------------------------------------|-------------|------------|
| serial (raw storm, no cache)                   | 153,188 ms  | 13,776     |
| **io_uring qd64 (my batched impl)**            | **4,439 ms**| 13,776     |
| **systemd boot path (in-memory cache)**        | **0.52 ms** | **0**      |

**systemd's existing cache beats my io_uring batching by ~8,500×** — because it does *no I/O*,
while io_uring still issues 13,776 (batched) round-trips. The storm io_uring targets does not
occur on boot.

## VERDICT (comprehensive)

The io_uring-batched-enumeration idea, rigorously pursued:
1. **Mechanism: real and proven.** On a genuinely cold/uncached `statx` storm, io_uring (qd≈64)
   hides ~96% of per-syscall latency — 116s → 4.4s on NFS (~26×); correctness identical to serial.
2. **But it does NOT apply to systemd's boot enumeration**, for two independent reasons:
   - The only io_uring-batchable storm (drop-in `statx`) is **already eliminated** by systemd's
     in-memory `unit_path_cache` (0.52ms / 0 syscalls — 8,500× better than io_uring could do).
   - The residual one-time cost (the name-map walk that *builds* the cache) is `opendir`/
     `getdents`/`readlinkat` — and **io_uring has no GETDENTS or READLINKAT op**, so it can't
     batch the part that's actually serial-and-slow on cold storage.
3. **Where io_uring would legitimately help:** any *uncached* cold metadata storm —
   `unit_file_find_dropin_paths(cache=NULL)` as called by `systemctl`/`systemd-analyze`/install
   ops without the manager cache, on slow storage. Niche, not boot-critical.

**Recommendation: NO systemd boot PR.** Proposing io_uring here would duplicate an existing,
superior optimization — exactly the kind of unjustified patch to avoid. The honest, valuable
output is this negative result + the proven general technique. It also re-confirms our earlier
finding: the VM's ~690ms is NOT unit-file enumeration (warm: ~5ms, cached drop-ins: 0.5ms) — it
lives in graph-build CPU / device-mount enumeration, a different target entirely.

## Artifacts
- `bench/enum_bench.c` — faithful enumeration microbench (serial / iouring / cached modes,
  cold/warm via process-unique tokens). Builds against extracted liburing 2.14 (static).
- Reproduce: `enum_bench <searchpath_root> <serial|iouring|cached> [qd] [iters] [cold]`.
