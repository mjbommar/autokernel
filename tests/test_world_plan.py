"""Tests for the world manifest + planner (W1). No network, no dpkg:
indices come from tests/fixtures/world/Sources, installed state from
synthetic WorldEntry lists."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from autokernel.optimize_context import Aggression, ThreatModel
from autokernel.world import manifest as manifest_mod
from autokernel.world.closure import classify_cost, plan_world
from autokernel.world.indices import binary_to_source_map, parse_sources
from autokernel.world.models import (
    BaseRelease,
    BuildCost,
    GlobalFlags,
    HardeningTier,
    Lto,
    Ring,
    WorldEntry,
    WorldManifest,
    toolchain_gate_overrides,
)

FIXTURE_SOURCES = (Path(__file__).parent / "fixtures" / "world" / "Sources").read_text(
    encoding="utf-8"
)


def _entry(binary: str, source: str, priority: str = "optional", kb: int = 1000):
    return WorldEntry(
        binary=binary,
        source=source,
        source_version="1.0-1",
        priority=priority,
        installed_kb=kb,
    )


def _base() -> BaseRelease:
    return BaseRelease(
        distro_id="ubuntu",
        suite="resolute",
        mirror="http://archive.ubuntu.com/ubuntu",
        components=["main", "universe"],
    )


def _manifest(entries: list[WorldEntry], **kwargs) -> WorldManifest:
    return WorldManifest(
        created_at=datetime.now(UTC),
        host="testhost",
        base=_base(),
        ring=Ring.REQUIRED,
        flags=GlobalFlags(),
        world=entries,
        **kwargs,
    )


# ── indices parsing ─────────────────────────────────────────────────────────


def test_parse_sources_keeps_highest_version():
    sources = parse_sources(FIXTURE_SOURCES)
    assert sources["zlib"].version == "1:1.3-2"


def test_parse_sources_strips_constraints_arch_and_profiles():
    sources = parse_sources(FIXTURE_SOURCES)
    # 'zlib1g-dev (>= 1:1.2) [amd64] <!nocheck>' → 'zlib1g-dev'
    assert "zlib1g-dev" in sources["foo"].build_depends
    assert all("(" not in d and "[" not in d for d in sources["foo"].build_depends)


def test_parse_sources_flattens_alternatives_and_indep():
    sources = parse_sources(FIXTURE_SOURCES)
    deps = sources["bar"].build_depends
    assert "libfoo-dev" in deps
    assert "libfoo-compat-dev" in deps
    assert "doctool" in deps  # Build-Depends-Indep


def test_binary_to_source_map():
    b2s = binary_to_source_map(parse_sources(FIXTURE_SOURCES))
    assert b2s["zlib1g"] == "zlib"
    assert b2s["libfoo-dev"] == "foo"


# ── ring filtering ──────────────────────────────────────────────────────────


def test_filter_ring_levels():
    entries = [
        _entry("libc6", "glibc", "required"),
        _entry("apt", "apt", "important"),
        _entry("vim", "vim", "optional"),
    ]
    assert [e.binary for e in manifest_mod.filter_ring(entries, Ring.REQUIRED)] == [
        "libc6"
    ]
    assert [e.binary for e in manifest_mod.filter_ring(entries, Ring.IMPORTANT)] == [
        "apt",
        "libc6",
    ]
    assert len(manifest_mod.filter_ring(entries, Ring.EVERYTHING)) == 3


# ── axes → flags ────────────────────────────────────────────────────────────


def test_flags_for_axes_table():
    f = manifest_mod.flags_for_axes(Aggression.CONSERVATIVE, ThreatModel.PERMISSIVE)
    assert (f.march, f.opt, f.lto) == ("x86-64-v3", "-O2", Lto.NONE)
    assert f.hardening == HardeningTier.DISTRO_DEFAULT

    f = manifest_mod.flags_for_axes(Aggression.AGGRESSIVE, ThreatModel.PARANOID)
    assert (f.march, f.opt, f.lto) == ("native", "-O3", Lto.AUTO)
    assert f.hardening == HardeningTier.PARANOID
    assert "nocheck" in f.build_options

    f = manifest_mod.flags_for_axes(Aggression.BALANCED, ThreatModel.BALANCED)
    assert (f.march, f.opt, f.lto) == ("native", "-O2", Lto.NONE)
    assert "-march=native -O2" in f.cflags_append
    assert "-D_FORTIFY_SOURCE=3" in f.cflags_append  # FORTIFY_PLUS


# ── manifest round-trip + toolchain gate ────────────────────────────────────


def test_manifest_save_load_round_trip(tmp_path):
    m = _manifest([_entry("zlib1g", "zlib", "required")])
    path = tmp_path / "manifest.json"
    manifest_mod.save_manifest(m, path)
    loaded = manifest_mod.load_manifest(path)
    assert loaded == m


def test_toolchain_gate_overrides():
    overrides = toolchain_gate_overrides(["zlib", "glibc", "gcc-16", "binutils"])
    gated = {o.source_pkg for o in overrides}
    assert gated == {"glibc", "gcc-16", "binutils"}
    assert all(o.use_stock for o in overrides)


# ── cost model ──────────────────────────────────────────────────────────────


def test_classify_cost():
    assert classify_cost("gcc-16", 50_000) == BuildCost.MONSTER
    assert classify_cost("llvm-toolchain-19", 0) == BuildCost.MONSTER
    assert classify_cost("glibc", 10_000) == BuildCost.HEAVY
    assert classify_cost("zlib", 1_000) == BuildCost.NORMAL
    assert classify_cost("tiny-thing", 100) == BuildCost.TINY


def test_classify_cost_linux_family():
    # Real kernel sources are monsters …
    assert classify_cost("linux", 0) == BuildCost.MONSTER
    assert classify_cost("linux-riscv", 0) == BuildCost.MONSTER
    # … but the rest of the linux-* namespace is not compiled code.
    assert classify_cost("linux-firmware", 900_000) == BuildCost.NORMAL
    assert classify_cost("linux-firmware-realtek", 50_000) == BuildCost.NORMAL
    assert classify_cost("linux-meta-riscv", 10) == BuildCost.TINY
    assert classify_cost("linux-signed", 10) == BuildCost.TINY
    assert classify_cost("linux-base", 100) == BuildCost.TINY


# ── golden plan over the fixture ────────────────────────────────────────────


def _fixture_manifest() -> WorldManifest:
    entries = [
        _entry("zlib1g", "zlib", "required"),
        _entry("libfoo1", "foo", "required"),
        _entry("bar", "bar", "required"),
        _entry("libalpha1", "alpha", "required"),
        _entry("libbeta1", "beta", "required"),
        _entry("libc6", "glibc", "required", kb=12_000),
        _entry("ghost", "ghost", "required"),  # no deb-src entry
    ]
    return _manifest(
        entries,
        overrides=toolchain_gate_overrides(["glibc"]),
    )


def test_golden_plan():
    plan = plan_world(_fixture_manifest(), parse_sources(FIXTURE_SOURCES))
    by_name = {u.source: u for u in plan.units}

    # Wave order: zlib + glibc have no in-closure deps → wave 0;
    # foo needs zlib1g-dev → wave 1; bar needs libfoo-dev → wave 2.
    assert by_name["zlib"].wave == 0
    assert by_name["glibc"].wave == 0
    assert by_name["foo"].wave == 1
    assert by_name["bar"].wave == 2
    assert by_name["foo"].build_deps_in_closure == ["zlib"]
    assert set(by_name["bar"].build_deps_in_closure) == {"foo", "zlib"}

    # alpha/beta form a cycle: same wave, recorded in stats.
    assert by_name["alpha"].wave == by_name["beta"].wave
    assert plan.stats.cycle_sources == ["alpha", "beta"]

    # glibc is toolchain-gated stock; ghost has no source entry.
    assert by_name["glibc"].use_stock
    assert plan.stats.stock_sources == 1
    assert plan.stats.rebuild_sources == 5
    assert plan.stats.unsourced == ["ghost"]

    # Archive version (highest) wins for the unit.
    assert by_name["zlib"].version == "1:1.3-2"

    # Stock units don't contribute to the cost estimate.
    assert plan.stats.est_cpu_hours > 0
    # Cycle members are only forced once no clean nodes remain, so
    # alpha/beta trail in their own wave: 0..2 clean + 3 for the cycle.
    assert plan.stats.wave_count == 4

    # Waves accessor groups consistently.
    waves = plan.waves()
    assert [u.source for u in waves[0]] == sorted(
        u.source for u in plan.units if u.wave == 0
    )


def test_plan_is_deterministic():
    m = _fixture_manifest()
    sources = parse_sources(FIXTURE_SOURCES)
    p1 = plan_world(m, sources)
    p2 = plan_world(m, sources)
    assert p1.units == p2.units
    assert p1.stats == p2.stats
