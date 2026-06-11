"""Typed data models for `autokernel world` (docs/WORLD.md).

The manifest is the declarative intent (Gentoo's make.conf + @world in
one file): which binary packages constitute the world, which global
flags apply, and which per-package overrides deviate. The plan is the
derived work-list: source units grouped into build waves with a cost
estimate.

Conventions follow ``autokernel.models``: frozen pydantic models,
JSON-serializable, ``extra="forbid"``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum, IntEnum

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


WORLD_SCHEMA_VERSION = 1


# ── rings ───────────────────────────────────────────────────────────────────


class Ring(IntEnum):
    """How much of the installed system the world covers.

    The machinery is identical at every ring; only N grows
    (docs/WORLD.md "Operating philosophy").
    """

    REQUIRED = 0  # dpkg Priority: required (≈ mmdebstrap minbase)
    IMPORTANT = 1  # + Priority: important
    EVERYTHING = 2  # every installed package


# ── flags ───────────────────────────────────────────────────────────────────


class Lto(str, Enum):
    NONE = "none"
    AUTO = "auto"  # gcc -flto=auto
    THIN = "thin"  # clang ThinLTO
    FULL = "full"


class HardeningTier(str, Enum):
    DISTRO_DEFAULT = "distro-default"
    FORTIFY_PLUS = "fortify-plus"  # + -D_FORTIFY_SOURCE=3 where supported
    PARANOID = "paranoid"  # + full RELRO/stack-clash everywhere


class GlobalFlags(_Frozen):
    """The make.conf analogue: one consistent flag set for the world."""

    march: str = "native"
    opt: str = "-O2"
    lto: Lto = Lto.NONE
    compiler: str = "gcc"  # world default is gcc; clang stays kernel-side
    hardening: HardeningTier = HardeningTier.DISTRO_DEFAULT
    build_options: list[str] = Field(default_factory=list)  # DEB_BUILD_OPTIONS
    build_profiles: list[str] = Field(default_factory=list)  # DEB_BUILD_PROFILES

    @property
    def cflags_append(self) -> str:
        """Value for DEB_CFLAGS_APPEND / DEB_CXXFLAGS_APPEND."""
        parts = [f"-march={self.march}", self.opt]
        if self.lto in (Lto.AUTO, Lto.FULL):
            parts.append("-flto=auto" if self.lto == Lto.AUTO else "-flto")
        elif self.lto == Lto.THIN:
            parts.append("-flto=thin")
        if self.hardening != HardeningTier.DISTRO_DEFAULT:
            parts.append("-D_FORTIFY_SOURCE=3")
        return " ".join(parts)


# ── overrides ───────────────────────────────────────────────────────────────


class OverrideSource(str, Enum):
    PRESET = "preset"  # shipped defaults (e.g. toolchain gate)
    USER = "user"
    LLM_TRIAGE = "llm-triage"  # W3: confirmed FTBFS triage verdicts


class PackageOverride(_Frozen):
    """Per-source-package deviation from GlobalFlags.

    The accumulated set of these (exceptions.json) is the package.env
    analogue. ``reason`` is mandatory: an override nobody can explain
    is an override nobody can retire.
    """

    source_pkg: str
    strip_flags: list[str] = Field(default_factory=list)
    add_flags: list[str] = Field(default_factory=list)
    force_compiler: str | None = None
    profiles: list[str] = Field(default_factory=list)
    patches: list[str] = Field(default_factory=list)  # paths to quilt patches
    use_stock: bool = False
    reason: str
    provenance: OverrideSource = OverrideSource.USER


# Toolchain + libc stay stock until --include-toolchain (docs/WORLD.md:
# a miscompiled libc takes the system down; gcc sources appear in every
# ring only because libgcc-s1/libstdc++6 binaries come from them).
TOOLCHAIN_GATE_SOURCES = ("glibc", "gcc-", "binutils")


def toolchain_gate_overrides(sources: list[str]) -> list[PackageOverride]:
    """PRESET use_stock overrides for toolchain-gated sources present
    in ``sources``."""
    out: list[PackageOverride] = []
    for src in sorted(set(sources)):
        if src == "glibc" or src == "binutils" or src.startswith("gcc-"):
            out.append(
                PackageOverride(
                    source_pkg=src,
                    use_stock=True,
                    reason=(
                        "toolchain gate: rebuilt toolchain/libc is opt-in "
                        "via --include-toolchain (docs/WORLD.md)"
                    ),
                    provenance=OverrideSource.PRESET,
                )
            )
    return out


# ── manifest ────────────────────────────────────────────────────────────────


class BaseRelease(_Frozen):
    distro_id: str  # "ubuntu" | "debian"
    suite: str  # "resolute", "trixie", ...
    mirror: str
    components: list[str]


class WorldEntry(_Frozen):
    """One installed binary package in the world set.

    Captured at init time so the plan is reproducible even if the host
    drifts between init and plan.
    """

    binary: str
    source: str
    source_version: str
    priority: str
    installed_kb: int = 0


class WorldManifest(_Frozen):
    schema_version: int = WORLD_SCHEMA_VERSION
    created_at: datetime
    host: str
    base: BaseRelease
    ring: Ring
    axes: dict[str, str] = Field(default_factory=dict)
    flags: GlobalFlags
    world: list[WorldEntry]
    overrides: list[PackageOverride] = Field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        return sorted({e.source for e in self.world})

    def override_for(self, source_pkg: str) -> PackageOverride | None:
        for o in self.overrides:
            if o.source_pkg == source_pkg:
                return o
        return None


# ── plan ────────────────────────────────────────────────────────────────────


class BuildCost(str, Enum):
    TINY = "tiny"
    NORMAL = "normal"
    HEAVY = "heavy"
    MONSTER = "monster"


# Rough per-build CPU-hour estimates used for plan totals. Calibrated
# from the W0 spike (zlib ≈ 1 min) and known archive monsters; the W2
# builder records real durations that can replace these.
COST_CPU_HOURS: dict[BuildCost, float] = {
    BuildCost.TINY: 0.04,
    BuildCost.NORMAL: 0.12,
    BuildCost.HEAVY: 1.0,
    BuildCost.MONSTER: 6.0,
}


class SourceUnit(_Frozen):
    """One source package to rebuild."""

    source: str
    version: str  # archive version (what apt-get source will fetch)
    binaries: list[str]  # world binaries this source produces
    build_deps_in_closure: list[str]  # source names; drives wave order
    wave: int
    cost: BuildCost
    use_stock: bool = False
    note: str | None = None


class PlanStats(_Frozen):
    total_sources: int
    rebuild_sources: int
    stock_sources: int  # use_stock escapees — the honesty debt
    total_binaries: int
    wave_count: int
    est_cpu_hours: float
    monsters: list[str] = Field(default_factory=list)
    unsourced: list[str] = Field(default_factory=list)
    """Binaries whose source has no deb-src entry (e.g. restricted
    without sources) — candidates for declared use_stock."""

    cycle_sources: list[str] = Field(default_factory=list)
    """Sources whose wave was assigned by cycle-breaking rather than
    clean topological order. Harmless in the stage3 model (build-deps
    come stock from the chroot); listed for transparency."""


class WorldPlan(_Frozen):
    schema_version: int = WORLD_SCHEMA_VERSION
    created_at: datetime
    manifest_hash: str  # sha256 of the manifest JSON this plan derives from
    suite: str
    units: list[SourceUnit]
    stats: PlanStats

    def waves(self) -> list[list[SourceUnit]]:
        if not self.units:
            return []
        n = max(u.wave for u in self.units) + 1
        out: list[list[SourceUnit]] = [[] for _ in range(n)]
        for u in self.units:
            out[u.wave].append(u)
        return out
