"""Detect the host CPU's microarchitecture and propose the right kernel
``CONFIG_M*`` symbol for it.

The kernel's ``arch/x86/Kconfig.cpu`` exposes one symbol per supported
microarch (``CONFIG_MZEN3``, ``CONFIG_MMETEORLAKE``, …). Setting the
right one cascades into the build's ``-march=`` flag and unlocks
microarch-specific code paths and instruction-set assumptions
(``RDSEED``, ``CLWB``, ``AVX-VNNI``, …).

This module is **pure logic**:

* :func:`detect_microarch` takes a :class:`autokernel.models.CpuInfo`
  (the snapshot's parsed ``/proc/cpuinfo``) and returns a
  :class:`Microarch`.
* :func:`kconfig_symbol_for` maps a Microarch to its ``CONFIG_M*``
  string.
* :func:`kernel_supports` answers "does the running kernel know this
  symbol?" via the symbol's "added in kernel X.Y" metadata; the caller
  can fall back to ``GENERIC_CPU`` for older kernels.

Lookup tables cover the high-traffic Intel Family-6 models (Nehalem
through Lunar Lake) and AMD Families 23/25/26 (Zen 1 through Zen 5).
Anything not in the table maps to :attr:`Microarch.GENERIC` and the
caller emits no tuning proposal — better to leave ``GENERIC_CPU=y``
than guess wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from autokernel.models import CpuInfo


class Microarch(str, Enum):
    """Recognized x86_64 microarchitectures, named to match the kernel's
    ``CONFIG_M*`` symbol suffix (uppercase). ``GENERIC`` is the
    catch-all when detection fails."""

    GENERIC = "GENERIC"

    # ── AMD ────────────────────────────────────────────────────────────
    K8 = "K8"            # Athlon 64 / Opteron (family 15)
    K10 = "K10"          # Phenom / Athlon II (family 16)
    BARCELONA = "BARCELONA"
    BOBCAT = "BOBCAT"
    JAGUAR = "JAGUAR"
    BULLDOZER = "BULLDOZER"
    PILEDRIVER = "PILEDRIVER"
    STEAMROLLER = "STEAMROLLER"
    EXCAVATOR = "EXCAVATOR"
    ZEN = "ZEN"          # family 23 model 0x00-0x0F
    ZEN2 = "ZEN2"        # family 23 model 0x30+
    ZEN3 = "ZEN3"        # family 25 model 0x00-0x4F (Vermeer, Cezanne, …)
    ZEN4 = "ZEN4"        # family 25 model 0x60+ (Raphael, Phoenix, Genoa)
    ZEN5 = "ZEN5"        # family 26 (Strix Point, Granite Ridge)

    # ── Intel — Core / server ──────────────────────────────────────────
    NEHALEM = "NEHALEM"
    WESTMERE = "WESTMERE"
    SANDYBRIDGE = "SANDYBRIDGE"
    IVYBRIDGE = "IVYBRIDGE"
    HASWELL = "HASWELL"
    BROADWELL = "BROADWELL"
    SKYLAKE = "SKYLAKE"
    SKYLAKEX = "SKYLAKEX"        # Skylake-X / Cascade Lake server
    CANNONLAKE = "CANNONLAKE"
    ICELAKE = "ICELAKE"
    CASCADELAKE = "CASCADELAKE"
    COOPERLAKE = "COOPERLAKE"
    TIGERLAKE = "TIGERLAKE"
    SAPPHIRERAPIDS = "SAPPHIRERAPIDS"
    ROCKETLAKE = "ROCKETLAKE"
    ALDERLAKE = "ALDERLAKE"
    RAPTORLAKE = "RAPTORLAKE"
    METEORLAKE = "METEORLAKE"
    EMERALDRAPIDS = "EMERALDRAPIDS"
    GRANITERAPIDS = "GRANITERAPIDS"
    ARROWLAKE = "ARROWLAKE"
    LUNARLAKE = "LUNARLAKE"

    # ── Intel — Atom line ──────────────────────────────────────────────
    SILVERMONT = "SILVERMONT"
    GOLDMONT = "GOLDMONT"
    GOLDMONTPLUS = "GOLDMONTPLUS"
    TREMONT = "TREMONT"
    GRACEMONT = "GRACEMONT"


# ── lookup tables ──────────────────────────────────────────────────────────


# Intel Family-6 model → microarch. Models are decimal as printed by
# /proc/cpuinfo. Sources: arch/x86/include/asm/intel-family.h, public
# Intel ARK, Wikichip.
_INTEL_FAM6: dict[int, Microarch] = {
    # Nehalem / Westmere
    26: Microarch.NEHALEM, 30: Microarch.NEHALEM, 46: Microarch.NEHALEM,
    37: Microarch.WESTMERE, 44: Microarch.WESTMERE, 47: Microarch.WESTMERE,
    # Sandy Bridge / Ivy Bridge
    42: Microarch.SANDYBRIDGE, 45: Microarch.SANDYBRIDGE,
    58: Microarch.IVYBRIDGE, 62: Microarch.IVYBRIDGE,
    # Haswell / Broadwell
    60: Microarch.HASWELL, 63: Microarch.HASWELL, 69: Microarch.HASWELL,
    70: Microarch.HASWELL, 71: Microarch.HASWELL,
    61: Microarch.BROADWELL, 79: Microarch.BROADWELL, 86: Microarch.BROADWELL,
    87: Microarch.BROADWELL,
    # Skylake / Kaby / Coffee / Comet (all called Skylake by Kconfig)
    78: Microarch.SKYLAKE, 94: Microarch.SKYLAKE,
    142: Microarch.SKYLAKE, 158: Microarch.SKYLAKE, 165: Microarch.SKYLAKE,
    166: Microarch.SKYLAKE,
    # Skylake-X / Cascade Lake-X
    85: Microarch.SKYLAKEX,
    # Cannon Lake (rare)
    102: Microarch.CANNONLAKE,
    # Ice Lake (client + server)
    125: Microarch.ICELAKE, 126: Microarch.ICELAKE,
    106: Microarch.ICELAKE, 108: Microarch.ICELAKE,
    # Tiger Lake
    140: Microarch.TIGERLAKE, 141: Microarch.TIGERLAKE,
    # Rocket Lake
    167: Microarch.ROCKETLAKE,
    # Alder Lake (model 151 desktop, 154 mobile)
    151: Microarch.ALDERLAKE, 154: Microarch.ALDERLAKE,
    # Raptor Lake (refresh of Alder Lake)
    183: Microarch.RAPTORLAKE,
    # Sapphire Rapids / Emerald Rapids server
    143: Microarch.SAPPHIRERAPIDS,
    207: Microarch.EMERALDRAPIDS,
    # Granite Rapids
    173: Microarch.GRANITERAPIDS,
    # Meteor Lake (host CPU at 170/171/172)
    170: Microarch.METEORLAKE, 171: Microarch.METEORLAKE,
    172: Microarch.METEORLAKE,
    # Arrow Lake / Lunar Lake (newest as of mid-2025)
    197: Microarch.ARROWLAKE, 198: Microarch.ARROWLAKE,
    188: Microarch.LUNARLAKE, 189: Microarch.LUNARLAKE,

    # Atom-line — small cores
    55: Microarch.SILVERMONT, 76: Microarch.SILVERMONT, 77: Microarch.SILVERMONT,
    74: Microarch.SILVERMONT, 90: Microarch.SILVERMONT, 93: Microarch.SILVERMONT,
    92: Microarch.GOLDMONT, 95: Microarch.GOLDMONT,
    122: Microarch.GOLDMONTPLUS,
    134: Microarch.TREMONT, 138: Microarch.TREMONT, 150: Microarch.TREMONT,
    156: Microarch.TREMONT,
    190: Microarch.GRACEMONT,
}


# AMD: family + model range → microarch. The model byte is split into
# "extended model" (top 4 bits of 12) + "model" (low 4 bits) by the
# kernel; /proc/cpuinfo's `model:` field already presents the combined
# value, so we use that.
@dataclass(frozen=True)
class _AmdRule:
    family: int
    model_min: int
    model_max: int
    arch: Microarch


_AMD_RULES: tuple[_AmdRule, ...] = (
    # Family 0x10 (16) — K10 / Barcelona
    _AmdRule(16, 0, 255, Microarch.K10),
    # Family 0x11/0x12 — Turion II / Llano
    _AmdRule(17, 0, 255, Microarch.K10),
    _AmdRule(18, 0, 255, Microarch.K10),
    # Family 0x14 (Bobcat APU)
    _AmdRule(20, 0, 255, Microarch.BOBCAT),
    # Family 0x15 — Bulldozer / Piledriver / Steamroller / Excavator
    _AmdRule(21, 0, 0x0F, Microarch.BULLDOZER),
    _AmdRule(21, 0x10, 0x1F, Microarch.PILEDRIVER),
    _AmdRule(21, 0x30, 0x3F, Microarch.STEAMROLLER),
    _AmdRule(21, 0x60, 0x6F, Microarch.EXCAVATOR),
    _AmdRule(21, 0x70, 0x7F, Microarch.EXCAVATOR),
    # Family 0x16 — Jaguar / Puma
    _AmdRule(22, 0, 255, Microarch.JAGUAR),
    # Family 0x17 — Zen / Zen+ (model 0x00-0x0F, 0x10-0x2F) and Zen2 (0x30+)
    _AmdRule(23, 0x00, 0x0F, Microarch.ZEN),
    _AmdRule(23, 0x10, 0x2F, Microarch.ZEN),  # Zen+ counted as ZEN by Kconfig
    _AmdRule(23, 0x30, 0xFF, Microarch.ZEN2),
    # Family 0x18 — Hygon Dhyana (Zen-clone, treat as ZEN)
    _AmdRule(24, 0, 255, Microarch.ZEN),
    # Family 0x19 (25) — Zen 3 (0x00-0x5F) and Zen 4 (0x60+)
    _AmdRule(25, 0x00, 0x5F, Microarch.ZEN3),
    _AmdRule(25, 0x60, 0xFF, Microarch.ZEN4),
    # Family 0x1A (26) — Zen 5
    _AmdRule(26, 0, 255, Microarch.ZEN5),
)


# Kernel version a given Microarch's CONFIG_M* symbol was added in.
# Used by :func:`kernel_supports` to fall back gracefully on older
# running kernels. Not exhaustive — when in doubt, omit the entry and
# the caller assumes the symbol is "old enough" (true for Skylake-era
# and earlier).
_ADDED_IN: dict[Microarch, tuple[int, int]] = {
    Microarch.ZEN: (4, 19),
    Microarch.ZEN2: (5, 7),
    Microarch.ZEN3: (5, 14),
    Microarch.ZEN4: (6, 4),
    Microarch.ZEN5: (6, 11),
    Microarch.SAPPHIRERAPIDS: (5, 18),
    Microarch.ROCKETLAKE: (5, 14),
    Microarch.ALDERLAKE: (5, 18),
    Microarch.RAPTORLAKE: (6, 4),
    Microarch.METEORLAKE: (6, 7),
    Microarch.EMERALDRAPIDS: (6, 5),
    Microarch.GRANITERAPIDS: (6, 6),
    Microarch.ARROWLAKE: (6, 8),
    Microarch.LUNARLAKE: (6, 8),
    Microarch.GRACEMONT: (6, 1),
    Microarch.TREMONT: (5, 8),
    Microarch.GOLDMONT: (4, 13),
    Microarch.GOLDMONTPLUS: (4, 16),
}


# ── public API ─────────────────────────────────────────────────────────────


def detect_microarch(cpu: CpuInfo) -> Microarch:
    """Map a parsed ``/proc/cpuinfo`` block to a :class:`Microarch`.

    Returns :attr:`Microarch.GENERIC` when the vendor/family/model
    combination isn't recognized — better to leave ``GENERIC_CPU=y``
    than guess wrong.
    """
    if cpu.vendor_id == "GenuineIntel" and cpu.cpu_family == 6 and cpu.model is not None:
        return _INTEL_FAM6.get(cpu.model, Microarch.GENERIC)

    if cpu.vendor_id == "AuthenticAMD" and cpu.cpu_family is not None and cpu.model is not None:
        for rule in _AMD_RULES:
            if rule.family == cpu.cpu_family and rule.model_min <= cpu.model <= rule.model_max:
                return rule.arch
        # AMD family 15 — pre-Bulldozer K8 era
        if cpu.cpu_family == 15:
            return Microarch.K8

    if cpu.vendor_id == "HygonGenuine":
        return Microarch.ZEN  # Hygon Dhyana == Zen-clone

    return Microarch.GENERIC


def kconfig_symbol_for(arch: Microarch) -> str:
    """``Microarch.METEORLAKE`` → ``'CONFIG_MMETEORLAKE'``.

    For :attr:`Microarch.GENERIC` returns ``'CONFIG_GENERIC_CPU'``.
    """
    if arch == Microarch.GENERIC:
        return "CONFIG_GENERIC_CPU"
    return f"CONFIG_M{arch.value}"


def kernel_supports(arch: Microarch, kernel_release: str) -> bool:
    """Does the running kernel ship a CONFIG_M* for this microarch?

    Falls back to *True* when we don't have an "added in" entry — the
    running kernel is presumed recent enough (Skylake and older symbols
    have been around since the 4.x series).
    """
    needed = _ADDED_IN.get(arch)
    if needed is None:
        return True
    parsed = _parse_kernel_release(kernel_release)
    if parsed is None:
        return True  # can't tell — assume yes
    return parsed >= needed


def _parse_kernel_release(release: str) -> tuple[int, int] | None:
    """Parse ``'6.13.0-12-generic'`` → ``(6, 13)``."""
    head = release.split("-", 1)[0]
    parts = head.split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def recommend(cpu: CpuInfo, kernel_release: str) -> tuple[Microarch, str] | None:
    """Combined entry point: detect microarch, return ``(arch, symbol)``
    if a real recommendation exists for this kernel.

    Returns ``None`` when:

    * the CPU isn't recognized (Microarch.GENERIC), or
    * the running kernel is too old for the detected microarch's symbol
      (and we'd just be downgrading to GENERIC_CPU anyway).
    """
    arch = detect_microarch(cpu)
    if arch == Microarch.GENERIC:
        return None
    if not kernel_supports(arch, kernel_release):
        return None
    return arch, kconfig_symbol_for(arch)
