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
    linker: str = ""  # "" = compiler default (lld for clang); "bfd"/"gold"/"lld"
    masquerade: bool = False  # PATH-prepend gcc/cc → compiler (force clang
    # through build systems that hardcode gcc; clang-world default)
    hardening: HardeningTier = HardeningTier.DISTRO_DEFAULT
    build_options: list[str] = Field(default_factory=list)  # DEB_BUILD_OPTIONS
    build_profiles: list[str] = Field(default_factory=list)  # DEB_BUILD_PROFILES

    @property
    def cflags_append(self) -> str:
        """Value for DEB_CFLAGS_APPEND / DEB_CXXFLAGS_APPEND."""
        return self.cflags_for(self.compiler)

    def cflags_for(self, compiler: str) -> str:
        """Compiler-dialect-aware flags: clang has no -flto=auto, its
        parallel LTO is ThinLTO. gcc output is byte-identical to the
        pre-clang-plumbing behavior (flags_hash stability for resumes)."""
        parts = [f"-march={self.march}", self.opt]
        if compiler == "clang":
            # Ubuntu's dwz (0.16) can't parse clang's DWARF5 sections
            # (.debug_str_offsets) and dh_dwz hard-fails on every
            # binary-producing package. DWARF4 keeps debug info and dwz
            # working. Found live; the Firefox/LLVM packages do the same.
            parts.append("-gdwarf-4")
        if self.lto != Lto.NONE:
            if compiler == "clang":
                parts.append("-flto" if self.lto == Lto.FULL else "-flto=thin")
            elif self.lto == Lto.AUTO:
                parts.append("-flto=auto")
            elif self.lto == Lto.FULL:
                parts.append("-flto")
            else:  # THIN on gcc: nearest equivalent is plain parallel LTO
                parts.append("-flto=auto")
        if self.hardening != HardeningTier.DISTRO_DEFAULT:
            parts.append("-D_FORTIFY_SOURCE=3")
        return " ".join(parts)

    def linker_for(self, compiler: str) -> str:
        """The linker to use. Explicit `linker` wins; else clang defaults
        to lld (plugin-capable, needed for ThinLTO), gcc to its own
        default (empty → bfd via collect2)."""
        if self.linker:
            return self.linker
        return "lld" if compiler == "clang" else ""

    def ldflags_for(self, compiler: str) -> str:
        """Link-stage appends. clang ThinLTO needs the LTO flag at link
        time and a plugin-capable linker; gcc handles both via collect2,
        so gcc gets nothing unless a linker is explicitly chosen.

        bfd/gold preserve `.symver` versioned symbols under ThinLTO where
        lld's --no-undefined-version default hard-errors (Phase 0
        finding, docs/experiment/DIARY.md)."""
        parts: list[str] = []
        if compiler == "clang" and self.lto != Lto.NONE:
            parts.append("-flto" if self.lto == Lto.FULL else "-flto=thin")
        linker = self.linker_for(compiler)
        if linker:
            parts.append(f"-fuse-ld={linker}")
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
    build_options: list[str] = Field(default_factory=list)
    """Extra DEB_BUILD_OPTIONS tokens for this package (e.g. nocheck)."""

    strip_build_options: list[str] = Field(default_factory=list)
    """Global DEB_BUILD_OPTIONS tokens to drop for this package — e.g.
    nodoc for packages whose debian/rules aren't nodoc-clean (found
    live: sed, bash)."""
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


# ── build records (W2) ──────────────────────────────────────────────────────


class AuditVerdict(str, Enum):
    OK = "ok"
    NO_COMPILER = "no-compiler"  # data/script package: nothing to audit
    MISSING_FLAGS = "missing-flags"  # our appends never reached a compiler


class FlagsAudit(_Frozen):
    """Did our flags actually shape the binaries? (docs/WORLD.md W0
    learning: blhc findings need a stock baseline to judge, so they are
    recorded informationally; the hard gate is our-flags-present.)"""

    verdict: AuditVerdict
    expected: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    blhc_finding_count: int = 0
    blhc_summary: list[str] = Field(default_factory=list)  # deduped finding kinds


class BuildOutcome(str, Enum):
    OK = "ok"
    FTBFS = "ftbfs"
    FETCH_FAILED = "fetch-failed"
    SKIPPED_STOCK = "skipped-stock"


class PackageBuildRecord(_Frozen):
    """Result of one build attempt; keyed on (source, version,
    flags_hash) for kill/restart resumability."""

    source: str
    archive_version: str
    local_version: str | None = None
    flags_hash: str
    outcome: BuildOutcome
    wave: int
    duration_s: float = 0.0
    audit: FlagsAudit | None = None
    debs: list[str] = Field(default_factory=list)
    log_path: str | None = None
    finished_at: datetime | None = None
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == BuildOutcome.OK


# ── FTBFS triage (W3) ───────────────────────────────────────────────────────


class FailureClass(str, Enum):
    LTO_INCOMPAT = "lto-incompat"
    NEEDS_GCC = "needs-gcc"  # clang-hostile: gcc extensions, configure rejects clang
    OPT_MISCOMPILE = "opt-miscompile"  # tests caught wrong behavior under -O3/march
    MARCH_ILLEGAL_INSN = "march-illegal-insn"
    TEST_FAILURE = "test-failure"  # deterministic, environment-caused
    TEST_FLAKE = "test-flake"  # nondeterministic; plain retry may pass
    DEP_SKEW = "dep-skew"  # build-dep version mismatch
    PACKAGING = "packaging"  # debian/ machinery, not the code
    UNKNOWN = "unknown"


class FtbfsVerdict(_Frozen):
    """LLM triage of one failed build. ``remedy=None`` means defer to a
    human (surfaced via status); a remedy is only persisted to the
    exceptions table after a real rebuild confirms it."""

    source: str
    failure_class: FailureClass
    remedy: PackageOverride | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    """Quoted build-log lines that justify the verdict."""


# ── package dimension decisions (W3) ────────────────────────────────────────


class Dimension(str, Enum):
    NECESSITY = "necessity"  # keep / trim / demote-to-optional
    FLAGS = "flags"  # proactive per-package flag hazards
    FEATURES = "features"  # build profiles / configure toggles
    RISK = "risk"  # blast radius for review routing / test depth


class PackageDecision(_Frozen):
    """One LLM judgment about one source package in one dimension —
    the package analogue of RemovalProposal. Advisory: policy + review
    decide what's applied."""

    source: str
    dimension: Dimension
    decision: str  # dimension-specific vocabulary, validated by the agent layer
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    params: dict[str, list[str]] = Field(default_factory=dict)
    """Structured extras: flags dimension → {'strip': [...], 'add': [...]};
    features → {'profiles': [...]}."""


# Packages whose removal can brick the system — the package-level
# analogue of the load-bearing CONFIG blocklist. Source-package names;
# necessity 'trim' decisions against these are forced to 'keep' at the
# policy layer regardless of confidence.
LOAD_BEARING_SOURCES = frozenset(
    {
        "apt",
        "base-files",
        "base-passwd",
        "bash",
        "coreutils",
        "dash",
        "debconf",
        "dpkg",
        "e2fsprogs",
        "glibc",
        "grep",
        "grub2",
        "gzip",
        "init-system-helpers",
        "linux",
        "pam",
        "sed",
        "shadow",
        "systemd",
        "sysvinit",
        "tar",
        "util-linux",
    }
)
