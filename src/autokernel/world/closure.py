"""World plan: source closure → build waves → cost estimate.

Ordering is a heuristic, not a correctness requirement (docs/WORLD.md):
sbuild chroots are seeded from stock binaries, so any order builds.
Waves only maximize how much of the closure gets built against our own
output. Consequently cycles are broken bluntly — every member of a
dependency cycle lands in the same wave — and that's fine.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from autokernel.world.indices import SourceMeta, binary_to_source_map
from autokernel.world.models import (
    COST_CPU_HOURS,
    BuildCost,
    PlanStats,
    SourceUnit,
    WorldManifest,
    WorldPlan,
)

# Hardcoded cost lists (docs/WORLD.md W1). Prefix match on source name.
# The W2 builder records real durations; these seed the estimate.
_MONSTER_PREFIXES = (
    "llvm",
    "gcc-",
    "webkit2gtk",
    "chromium",
    "libreoffice",
    "rustc",
    "firefox",
)
# The linux-* namespace is mostly NOT kernel builds: linux-firmware-*
# (data), linux-meta* (deps only), linux-signed* (repack), linux-base
# (scripts). Only actual kernel sources are monsters.
_LINUX_NON_MONSTER_MARKERS = ("firmware", "meta", "base", "signed")
_HEAVY_PREFIXES = (
    "glibc",
    "systemd",
    "perl",
    "python3.",
    "openssl",
    "binutils",
    "gnutls28",
    "cmake",
    "nodejs",
    "openjdk-",
    "qt6-base",
    "mesa",
)
_TINY_KB_THRESHOLD = 300


def classify_cost(source: str, installed_kb: int) -> BuildCost:
    if source == "linux" or (
        source.startswith("linux-")
        and not any(m in source for m in _LINUX_NON_MONSTER_MARKERS)
    ):
        return BuildCost.MONSTER
    for prefix in _MONSTER_PREFIXES:
        if source.startswith(prefix):
            return BuildCost.MONSTER
    for prefix in _HEAVY_PREFIXES:
        if source.startswith(prefix):
            return BuildCost.HEAVY
    if installed_kb and installed_kb < _TINY_KB_THRESHOLD:
        return BuildCost.TINY
    return BuildCost.NORMAL


def _assign_waves(
    deps: dict[str, set[str]],
) -> tuple[dict[str, int], list[str]]:
    """Kahn-style level assignment, cycle-tolerant.

    Returns ``(source → wave, cycle_members)``. When no node is ready
    (a cycle), every remaining node with the minimum number of
    unresolved deps is forced into the current wave and recorded.
    """
    waves: dict[str, int] = {}
    cycle_members: list[str] = []
    remaining = {name: set(d) for name, d in deps.items()}
    wave = 0
    while remaining:
        ready = sorted(n for n, d in remaining.items() if not d)
        if not ready:
            min_deps = min(len(d) for d in remaining.values())
            ready = sorted(n for n, d in remaining.items() if len(d) == min_deps)
            cycle_members.extend(ready)
        for name in ready:
            waves[name] = wave
            del remaining[name]
        for d in remaining.values():
            d.difference_update(ready)
        wave += 1
    return waves, sorted(set(cycle_members))


def plan_world(
    manifest: WorldManifest, sources_meta: dict[str, SourceMeta]
) -> WorldPlan:
    bin2src = binary_to_source_map(sources_meta)

    # Group world binaries by source; spot binaries with no deb-src.
    by_source: dict[str, list[str]] = {}
    kb_by_source: dict[str, int] = {}
    unsourced: list[str] = []
    for entry in manifest.world:
        # Trust the archive's binary→source mapping first (the archive
        # may have moved a binary between sources since install), then
        # dpkg's recorded source.
        source = bin2src.get(entry.binary, entry.source)
        if source not in sources_meta:
            unsourced.append(entry.binary)
            continue
        by_source.setdefault(source, []).append(entry.binary)
        kb_by_source[source] = kb_by_source.get(source, 0) + entry.installed_kb

    # In-closure build-dep edges: source → set of closure sources that
    # must (ideally) build first. Out-of-closure deps come stock.
    closure = set(by_source)
    deps: dict[str, set[str]] = {}
    for source in closure:
        edges = {
            bin2src[dep] for dep in sources_meta[source].build_depends if dep in bin2src
        }
        deps[source] = (edges & closure) - {source}

    waves, cycle_members = _assign_waves(deps)

    units: list[SourceUnit] = []
    monsters: list[str] = []
    est_hours = 0.0
    stock_count = 0
    for source in sorted(closure):
        override = manifest.override_for(source)
        use_stock = bool(override and override.use_stock)
        cost = classify_cost(source, kb_by_source.get(source, 0))
        if cost == BuildCost.MONSTER:
            monsters.append(source)
        if use_stock:
            stock_count += 1
        else:
            est_hours += COST_CPU_HOURS[cost]
        units.append(
            SourceUnit(
                source=source,
                version=sources_meta[source].version,
                binaries=sorted(by_source[source]),
                build_deps_in_closure=sorted(deps[source]),
                wave=waves[source],
                cost=cost,
                use_stock=use_stock,
                note=override.reason if override else None,
            )
        )

    manifest_hash = hashlib.sha256(manifest.model_dump_json().encode()).hexdigest()
    stats = PlanStats(
        total_sources=len(units),
        rebuild_sources=len(units) - stock_count,
        stock_sources=stock_count,
        total_binaries=sum(len(u.binaries) for u in units),
        wave_count=(max(waves.values()) + 1) if waves else 0,
        est_cpu_hours=round(est_hours, 2),
        monsters=sorted(monsters),
        unsourced=sorted(unsourced),
        cycle_sources=cycle_members,
    )
    return WorldPlan(
        created_at=datetime.now(UTC),
        manifest_hash=manifest_hash,
        suite=manifest.base.suite,
        units=units,
        stats=stats,
    )
