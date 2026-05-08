# PGO + AutoFDO design

Profile-Guided Optimization for the kernel. Sketched for v0.17 — not
yet implemented but well-defined enough that anyone can pick it up.

## What it is

Compiler optimizations are static decisions: branch-prediction hints,
inlining choices, basic-block layout. Without runtime data, the
compiler guesses. With runtime data ("which branches actually fire?
which functions actually run hot?"), it can lay out code optimally.

Two flavors, both supported by clang (and increasingly by gcc):

| | Instrumented PGO | AutoFDO (sampling) |
|---|---|---|
| Build flags | `-fprofile-generate` + `-fprofile-use` | `-fauto-profile` |
| Profile collection | run instrumented binary | run stock binary under `perf record` |
| Overhead during collection | high (10-50% slower) | ~1% |
| Headline gain | 3-7% throughput | 1-3% |
| Setup complexity | two-pass build | one-pass build + perf integration |
| Workload representativeness | can be targeted (run 30s of representative work) | needs whatever was being sampled |

## Why this is a natural fit for autokernel

We **already** have:

1. **The four-axis context.** We know workload=server vs workload=desktop —
   that's exactly what makes a profile representative.
2. **`iterate`** the closed-loop verb. PGO is fundamentally a 2-3 round
   loop (build → profile → rebuild). We have the orchestrator.
3. **`boot-test`** providing a controlled QEMU environment for
   profile collection. No risk of corrupting the host.
4. **Per-iteration measurements**. We can compare instrumented vs
   PGO-built kernel performance via the same fitness function.

## Concrete shape

```bash
autokernel iterate <snap> --kernel-source PATH \
    --target=perf --pgo=instrumented \
    --workload-probe="cmd-to-run-inside-vm" \
    --max-iterations=3 --execute
```

Three rounds:

1. **Round 1: instrumented build.** Add `CONFIG_PGO_CLANG=y` to the
   final.config, build. The kernel now records execution counters.

2. **Round 2: profile collection.** Boot the instrumented kernel under
   QEMU/virtme-ng with the user's `--workload-probe`. The probe is
   what makes the profile representative — for a postgres host:
   `pgbench -t 1000`; for nginx: `ab -n 100000 ...`; for desktop: a
   workload script the user provides. Profile data dumps to
   `<snap>/iterations/iN/profile.gcda` (or LLVM equivalent).

3. **Round 3: PGO build.** Add `KCFLAGS=-fprofile-use=...` pointing
   at round 2's profile data. Build a final, PGO-optimized kernel.
   Boot-test as usual.

The orchestrator records per-round measurements: bzImage size delta,
boot-test time delta, per-symbol hot-set (we can dump the top 50 hot
functions if `perf` is available).

## AutoFDO variant — easier path

Skip the instrumented build entirely. Use `perf record` against a
stock-built kernel running the workload, convert with `create_gcov`,
then rebuild once with `-fauto-profile`. Two rounds instead of three;
lower headline gain but much less moving-parts.

```bash
autokernel iterate <snap> --pgo=autofdo --workload-probe=...
# Round 1: build stock; collect perf samples
# Round 2: rebuild with the perf data as profile
```

## Implementation sketch

```python
# autokernel/pgo.py (new module)

@dataclass(frozen=True)
class PGOMode(str, Enum):
    NONE = "none"
    INSTRUMENTED = "instrumented"  # CONFIG_PGO_CLANG=y, two passes
    AUTOFDO = "autofdo"            # -fauto-profile, perf-based

def configure_pgo_pass1(final_config: Path, mode: PGOMode) -> None:
    """Patch final.config for the profile-collection pass."""
    if mode == PGOMode.INSTRUMENTED:
        # Add: CONFIG_PGO_CLANG=y, CONFIG_DEBUG_INFO=y
        ...

def collect_profile(
    bzimage: Path,
    workload_probe: str,
    *,
    out_dir: Path,
    timeout_seconds: int = 60,
) -> Path:
    """Boot the kernel under virtme-ng, run the probe, dump the profile."""
    # Use virtme-ng with --rwdir to allow writes to /tmp where the
    # profile lands, then rsync it out.
    ...
    return out_dir / "default.profraw"

def configure_pgo_pass2(final_config: Path, profile: Path) -> dict[str, str]:
    """Return env vars for the PGO-use rebuild."""
    return {
        "KCFLAGS": f"-fprofile-use={profile} -fprofile-correction",
        # plus turn off CONFIG_PGO_CLANG so the result is "normal" build
    }
```

## Caveats and open questions

- **Profile representativeness is everything.** A PGO build trained
  on `stress-ng --cpu` will be WORSE than a stock build on a database
  host. The user MUST provide a representative `--workload-probe`.
- **CONFIG_PGO_CLANG is x86_64-only as of 6.x.** AutoFDO is more
  portable.
- **Reproducibility tradeoff.** PGO builds aren't bit-reproducible
  unless you check in the profile data. For a build-system that's
  going to ship this, the profile data should land alongside source
  in version control.
- **clang version requirement.** Kernel PGO needs clang ≥ 17 (gcc PGO
  is more mature but kernel-side support trails). Pre-flight should
  refuse on older toolchains.
- **Disk space.** Instrumented kernels are ~30-50% bigger; profile
  data per run is 50-200 MB. Plan for transient ~2 GB scratch space.

## Why not PGO right now (v0.15)

Not because it's hard, because it's easy to get wrong. We need:

1. **The closed-loop fully exercised live** (v0.15 task #125) — proves
   the iterate machinery is solid before we add a 3-pass variant.
2. **AutoFDO infrastructure** — wiring `perf record` into virtme-ng's
   probe phase isn't difficult but it's NEW shell code that should
   be tested separately.
3. **Workload-probe library** — we should ship sensible defaults per
   workload (`nginx ab` for server, `kernbench` for build-server,
   `phoronix-test-suite` for desktop) so users don't have to invent
   the probe.

These can land incrementally as v0.16 (probe library), v0.17 (PGO
itself).

## Related: LTO

Thin-LTO is the prerequisite-ish optimization that's much easier to
ship. Plan to land it in v0.15 alongside the clang default.

```
--lto=none   (default; fast incremental builds)
--lto=thin   (clang thin-LTO; +5-10% throughput, +30% build time)
--lto=full   (clang full-LTO; +5-12%, +200% build time, brittle)
```

LTO + PGO compose: you can have both. PGO without LTO leaves perf
on the table; LTO without PGO is the easy ~3% win.
