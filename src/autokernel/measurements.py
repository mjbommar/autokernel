"""Collect kernel-build + boot-test measurements for closed-loop iteration.

Each iteration of ``autokernel iterate`` produces a build + a
boot-test. To know whether iteration N is *better* than iteration N-1
we need a few mechanical numbers — bzImage size (smaller = better
for surface), compile time, boot-test pass + duration, and which of
our proposed symbols actually landed in the final .config (vs got
stripped by olddefconfig).

This module is pure data collection. It doesn't decide what to do
about the numbers — that's the job of :mod:`autokernel.iteration`.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class BuildMeasurements:
    """Numbers we can measure from a built kernel + boot-test record.

    Sizes are in bytes. Times are in seconds. Counts are integers.
    Booleans for boot test outcome. ``None`` where the relevant
    artifact wasn't present (e.g. boot_test_seconds when boot-test
    wasn't run yet).
    """

    bzimage_bytes: int | None = None
    vmlinux_bytes: int | None = None
    module_count: int | None = None
    module_total_bytes: int | None = None
    compile_seconds: float | None = None

    boot_test_passed: bool | None = None
    boot_test_seconds: float | None = None
    boot_failure_mode: str | None = (
        None  # 'early-panic' | 'vfs-panic' | 'init-panic' | None
    )

    proposed_count: int | None = None
    actually_landed_count: int | None = None
    olddefconfig_dropped: list[str] = field(default_factory=list)

    @property
    def boot_test_passed_or_skipped(self) -> bool:
        """True if boot-test passed or wasn't run; only False when explicitly failed."""
        return self.boot_test_passed in (True, None)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


# ── collectors ────────────────────────────────────────────────────────────


def measure_bzimage(source_dir: Path) -> int | None:
    """Read arch/<arch>/boot/bzImage size. Returns None if not built."""
    for arch in ("x86", "arm64", "riscv", "powerpc"):
        candidate = source_dir / "arch" / arch / "boot" / "bzImage"
        if candidate.exists():
            return candidate.stat().st_size
    # arm64 puts it elsewhere
    for candidate in (
        source_dir / "arch/arm64/boot/Image",
        source_dir / "arch/arm64/boot/Image.gz",
    ):
        if candidate.exists():
            return candidate.stat().st_size
    return None


def measure_vmlinux(source_dir: Path) -> int | None:
    """Top-level vmlinux size — uncompressed kernel ELF."""
    candidate = source_dir / "vmlinux"
    return candidate.stat().st_size if candidate.exists() else None


def measure_modules(source_dir: Path) -> tuple[int, int]:
    """Walk the source tree for .ko files. Returns (count, total_bytes).

    NOTE: incremental builds leave stale .ko files in the tree from
    previous configs — this count is "what's on disk" not "what's
    in the current .config". For the latter, count =m lines in the
    final .config instead.
    """
    total = 0
    count = 0
    for ko in source_dir.rglob("*.ko"):
        try:
            total += ko.stat().st_size
            count += 1
        except OSError:
            continue
    return count, total


def count_modules_in_config(config_path: Path) -> int:
    """Count =m lines in a .config file. Authoritative for "how many
    modules will the next build produce" (vs measure_modules() which
    reports leftovers)."""
    if not config_path.exists():
        return 0
    return sum(
        1
        for line in config_path.read_text().splitlines()
        if re.match(r"^CONFIG_[A-Z0-9_]+=m\s*$", line)
    )


def parse_compile_seconds_from_log(build_log: str) -> float | None:
    """Extract the build duration from autokernel build's structured log.

    Format expected: ``make-bindeb-pkg │  0 │   123.4 │ …`` or similar
    table rows. Falls back to None if no match.
    """
    # Pattern: rich Table cells separated by │. Look for the "make-..." step
    # row; column 3 is the duration in seconds.
    for line in build_log.splitlines():
        m = re.search(
            r"\b(make-\w+|build|olddefconfig)\b.*?│\s*0\s*│\s*([\d.]+)\s*│", line
        )
        if m and "make-" in line.lower():  # prefer the actual build step
            try:
                return float(m.group(2))
            except ValueError:
                continue
    return None


def diff_proposed_vs_actual(
    proposed_config_text: str,
    actual_config_text: str,
) -> tuple[int, int, list[str]]:
    """Compare proposed final.config vs source's actual .config (post-
    olddefconfig). Returns (proposed_count, actually_landed_count,
    list_of_dropped_symbols)."""
    from autokernel.config_check import _parse_config

    proposed = _parse_config(proposed_config_text)
    actual = _parse_config(actual_config_text)

    landed: list[str] = []
    dropped: list[str] = []
    for sym, value in proposed.items():
        actual_value = actual.get(sym)
        if actual_value is None:
            dropped.append(sym)
            continue
        if _normalize(actual_value) == _normalize(value):
            landed.append(sym)
        else:
            dropped.append(sym)
    return len(proposed), len(landed), dropped


def _normalize(v: str) -> str:
    v = v.strip()
    if v in {"y", "m", "n"}:
        return v
    if v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    return v


# ── boot-test record reading ──────────────────────────────────────────────


def read_boot_test_record(snapshot_dir: Path) -> dict | None:
    """Load <snap>/boot-test.json, returns None if missing."""
    p = snapshot_dir / "boot-test.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def measure(
    *,
    snapshot_dir: Path,
    source_dir: Path | None = None,
    proposed_config_text: str | None = None,
    actual_config_text: str | None = None,
    build_log: str | None = None,
) -> BuildMeasurements:
    """Compose a :class:`BuildMeasurements` from whatever's on disk.

    Each input is optional — when missing, the corresponding fields
    stay ``None`` instead of failing. That lets the iteration loop
    measure progressively (after build, after boot-test, etc.).
    """
    bzimage_bytes = measure_bzimage(source_dir) if source_dir else None
    vmlinux_bytes = measure_vmlinux(source_dir) if source_dir else None
    module_count = None
    module_total_bytes = None
    if source_dir is not None:
        # Prefer counting .ko on disk; fall back to .config =m lines.
        on_disk_count, on_disk_total = measure_modules(source_dir)
        if on_disk_count > 0:
            module_count, module_total_bytes = on_disk_count, on_disk_total
        else:
            cfg = source_dir / ".config"
            if cfg.exists():
                module_count = count_modules_in_config(cfg)

    compile_seconds = parse_compile_seconds_from_log(build_log) if build_log else None

    boot_test = read_boot_test_record(snapshot_dir)
    if boot_test is not None:
        bt_passed = bool(boot_test.get("verdict_ok"))
        bt_seconds = boot_test.get("duration_seconds")
        bt_failure = None if bt_passed else boot_test.get("verdict_reason") or "unknown"
    else:
        bt_passed = None
        bt_seconds = None
        bt_failure = None

    if proposed_config_text and actual_config_text:
        prop_count, landed_count, dropped = diff_proposed_vs_actual(
            proposed_config_text, actual_config_text
        )
    else:
        prop_count = landed_count = None
        dropped = []

    return BuildMeasurements(
        bzimage_bytes=bzimage_bytes,
        vmlinux_bytes=vmlinux_bytes,
        module_count=module_count,
        module_total_bytes=module_total_bytes,
        compile_seconds=compile_seconds,
        boot_test_passed=bt_passed,
        boot_test_seconds=bt_seconds,
        boot_failure_mode=bt_failure,
        proposed_count=prop_count,
        actually_landed_count=landed_count,
        olddefconfig_dropped=dropped,
    )
