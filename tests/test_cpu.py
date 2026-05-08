"""Tests for CPU microarch detection.

Synthetic CpuInfo objects represent a curated cross-section of the
x86_64 line, from Sandy Bridge to Lunar Lake and from Bulldozer to
Zen 5. Anything in production that hits a model not covered here gets
:attr:`Microarch.GENERIC` — by design.
"""

from __future__ import annotations

import pytest

from autokernel.cpu import (
    Microarch,
    detect_microarch,
    kconfig_symbol_for,
    kernel_supports,
    recommend,
)
from autokernel.models import CpuInfo


def _ci(vendor: str, family: int, model: int, **kw) -> CpuInfo:
    return CpuInfo(
        vendor_id=vendor,
        cpu_family=family,
        model=model,
        model_name=kw.get("model_name", f"{vendor} fam{family} model{model}"),
        flags=kw.get("flags", []),
        cores=kw.get("cores", 8),
    )


# ── Intel ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fam,model,expected",
    [
        # Sandy Bridge — i5-2500K
        (6, 42, Microarch.SANDYBRIDGE),
        # Ivy Bridge
        (6, 58, Microarch.IVYBRIDGE),
        # Haswell — i7-4790K
        (6, 60, Microarch.HASWELL),
        # Broadwell
        (6, 61, Microarch.BROADWELL),
        # Skylake / Kaby Lake / Coffee Lake / Comet Lake (all → SKYLAKE)
        (6, 78, Microarch.SKYLAKE),
        (6, 94, Microarch.SKYLAKE),
        (6, 142, Microarch.SKYLAKE),
        (6, 158, Microarch.SKYLAKE),
        (6, 165, Microarch.SKYLAKE),
        # Skylake-X (Xeon)
        (6, 85, Microarch.SKYLAKEX),
        # Tiger Lake — i7-1165G7 mobile
        (6, 140, Microarch.TIGERLAKE),
        # Ice Lake
        (6, 125, Microarch.ICELAKE),
        (6, 106, Microarch.ICELAKE),  # Ice Lake-SP server
        # Rocket Lake
        (6, 167, Microarch.ROCKETLAKE),
        # Alder Lake — i9-12900K (model 151 desktop, 154 mobile)
        (6, 151, Microarch.ALDERLAKE),
        (6, 154, Microarch.ALDERLAKE),
        # Raptor Lake
        (6, 183, Microarch.RAPTORLAKE),
        # Sapphire Rapids
        (6, 143, Microarch.SAPPHIRERAPIDS),
        # Meteor Lake — Core Ultra 7 165U/165H (the box autokernel was built on)
        (6, 170, Microarch.METEORLAKE),
        # Lunar Lake — Core Ultra 200V series
        (6, 188, Microarch.LUNARLAKE),
        # Arrow Lake
        (6, 197, Microarch.ARROWLAKE),
    ],
)
def test_intel_family6_microarch(fam: int, model: int, expected: Microarch):
    cpu = _ci("GenuineIntel", fam, model)
    assert detect_microarch(cpu) == expected


def test_intel_unknown_model_falls_back_to_generic():
    cpu = _ci("GenuineIntel", 6, 250)
    assert detect_microarch(cpu) == Microarch.GENERIC


# ── AMD ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fam,model,expected",
    [
        # K8 (family 15)
        (15, 4, Microarch.K8),
        # K10 (Phenom II)
        (16, 5, Microarch.K10),
        # Bulldozer
        (21, 1, Microarch.BULLDOZER),
        # Piledriver — FX-8350
        (21, 0x10, Microarch.PILEDRIVER),
        # Steamroller
        (21, 0x30, Microarch.STEAMROLLER),
        # Excavator
        (21, 0x60, Microarch.EXCAVATOR),
        # Bobcat
        (20, 1, Microarch.BOBCAT),
        # Jaguar
        (22, 0, Microarch.JAGUAR),
        # Zen 1 — Ryzen 1700 (family 23, model 1)
        (23, 1, Microarch.ZEN),
        # Zen+ — Ryzen 2700X (family 23, model 8) — same Kconfig symbol
        (23, 8, Microarch.ZEN),
        # Zen 2 — Ryzen 3700X / 5700G (family 23, model 113)
        (23, 113, Microarch.ZEN2),
        # Zen 3 — Ryzen 5800X3D (family 25, model 33)
        (25, 33, Microarch.ZEN3),
        # Zen 3+ — Ryzen 6800U (family 25, model 68)
        (25, 68, Microarch.ZEN3),
        # Zen 4 — Ryzen 7950X (family 25, model 97)
        (25, 97, Microarch.ZEN4),
        # Zen 4 — EPYC Genoa
        (25, 144, Microarch.ZEN4),
        # Zen 5 — Strix Point (family 26, model 32)
        (26, 32, Microarch.ZEN5),
    ],
)
def test_amd_microarch_detection(fam: int, model: int, expected: Microarch):
    cpu = _ci("AuthenticAMD", fam, model)
    assert detect_microarch(cpu) == expected


def test_hygon_dhyana_treated_as_zen():
    cpu = _ci("HygonGenuine", 24, 0)
    assert detect_microarch(cpu) == Microarch.ZEN


# ── unknown / fallback ─────────────────────────────────────────────────────


def test_unknown_vendor_falls_back_to_generic():
    cpu = _ci("UnknownCorp", 6, 100)
    assert detect_microarch(cpu) == Microarch.GENERIC


def test_missing_family_or_model_returns_generic():
    cpu = CpuInfo(vendor_id="GenuineIntel", cpu_family=None, model=None)
    assert detect_microarch(cpu) == Microarch.GENERIC


# ── kconfig symbol mapping ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "arch,expected",
    [
        (Microarch.METEORLAKE, "CONFIG_MMETEORLAKE"),
        (Microarch.ZEN3, "CONFIG_MZEN3"),
        (Microarch.SKYLAKE, "CONFIG_MSKYLAKE"),
        (Microarch.GENERIC, "CONFIG_GENERIC_CPU"),
    ],
)
def test_kconfig_symbol_for(arch: Microarch, expected: str):
    assert kconfig_symbol_for(arch) == expected


# ── kernel version awareness ───────────────────────────────────────────────


def test_kernel_supports_returns_true_for_old_arch():
    """Skylake's CONFIG_MSKYLAKE has been around forever — any kernel
    we'd realistically run on supports it."""
    assert kernel_supports(Microarch.SKYLAKE, "5.15.0")
    assert kernel_supports(Microarch.SKYLAKE, "6.13.0-12-generic")


def test_kernel_supports_recognizes_too_old():
    """Meteor Lake's CONFIG_MMETEORLAKE was added in 6.7. A 6.5 kernel
    can't honour it."""
    assert not kernel_supports(Microarch.METEORLAKE, "6.5.0")
    assert kernel_supports(Microarch.METEORLAKE, "6.7.0")
    assert kernel_supports(Microarch.METEORLAKE, "6.13.0-12-generic")


def test_kernel_supports_assumes_yes_for_unparseable_release():
    assert kernel_supports(Microarch.METEORLAKE, "garbage release string")


# ── recommend(): combined entry point ─────────────────────────────────────


def test_recommend_returns_full_pair_for_zen3_on_modern_kernel():
    cpu = _ci("AuthenticAMD", 25, 33)
    rec = recommend(cpu, "6.13.0")
    assert rec == (Microarch.ZEN3, "CONFIG_MZEN3")


def test_recommend_returns_none_for_unknown_cpu():
    cpu = _ci("UnknownCorp", 6, 0)
    assert recommend(cpu, "6.13.0") is None


def test_recommend_returns_none_when_kernel_too_old():
    """If running an old kernel, we shouldn't recommend a symbol it
    doesn't have — it'd just downgrade to GENERIC_CPU silently."""
    cpu = _ci("GenuineIntel", 6, 170)
    assert recommend(cpu, "6.5.0") is None
    assert recommend(cpu, "6.13.0") == (Microarch.METEORLAKE, "CONFIG_MMETEORLAKE")
