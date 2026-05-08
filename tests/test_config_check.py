"""Tests for autokernel.config_check."""

from __future__ import annotations

from pathlib import Path

import pytest

from autokernel.config_check import (
    CheckReport,
    Finding,
    FindingKind,
    check,
)
from autokernel.kconfig_walk import (
    BoolToggle,
    ChoiceGroup,
    ChoiceOption,
    KconfigSurface,
    NumericTunable,
    SymbolType,
)


def _surface(*, choices=None, toggles=None, tunables=None) -> KconfigSurface:
    return KconfigSurface(
        arch="x86_64",
        source_dir=Path("/tmp"),
        choices=choices or [],
        toggles=toggles or [],
        tunables=tunables or [],
    )


def _toggle(name: str, current: str = "n") -> BoolToggle:
    return BoolToggle(
        name=name, prompt=f"prompt {name}", help=None,
        current_value=current, location="x", direct_dep_str="",
    )


def _tunable(name: str, current: str = "0", *, ranges: list[tuple[str, str]] | None = None) -> NumericTunable:
    return NumericTunable(
        name=name, type=SymbolType.INT, prompt=f"prompt {name}", help=None,
        current_value=current, ranges=ranges or [], location="x",
    )


def _choice(prompt: str, options: list[tuple[str, bool]]) -> ChoiceGroup:
    """Build a choice with (option_name, is_current) tuples."""
    return ChoiceGroup(
        name=None, prompt=prompt, help=None,
        options=[
            ChoiceOption(name=n, prompt=n, help=None, is_current=is_current)
            for n, is_current in options
        ],
        location="x",
    )


# ── unknown symbols ──────────────────────────────────────────────────────


def test_unknown_symbol_is_error():
    surface = _surface(toggles=[_toggle("KNOWN")])
    proposed = "CONFIG_KNOWN=y\nCONFIG_HALLUCINATED=y\n"
    rep = check(proposed, surface)
    assert rep.has_errors
    syms = rep.error_symbols()
    assert "CONFIG_HALLUCINATED" in syms
    assert "CONFIG_KNOWN" not in syms
    # Detail mentions Kconfig.
    finding = next(f for f in rep.errors if f.symbol == "CONFIG_HALLUCINATED")
    assert finding.kind == FindingKind.UNKNOWN_SYMBOL


def test_known_symbol_no_error():
    surface = _surface(toggles=[_toggle("KNOWN")])
    rep = check("CONFIG_KNOWN=y\n", surface)
    assert not rep.has_errors


def test_disabled_symbols_recognized():
    """`# CONFIG_X is not set` lines should parse to value 'n'."""
    surface = _surface(toggles=[_toggle("KNOWN", current="y")])
    rep = check("# CONFIG_KNOWN is not set\n", surface)
    assert not rep.has_errors


# ── dead-letter choice options ───────────────────────────────────────────


def test_dead_letter_choice_when_parent_disabled():
    """Choice options where no sibling is current → parent feature is
    off; our =y is silently dropped by olddefconfig."""
    kasan = _choice(
        "KASAN mode",
        [("KASAN_GENERIC", False), ("KASAN_OUTLINE", False), ("KASAN_INLINE", False)],
    )
    surface = _surface(choices=[kasan])
    rep = check("CONFIG_KASAN_OUTLINE=y\n", surface)
    # Not an error (the proposal isn't actively wrong) — but a warning.
    assert not rep.has_errors
    assert rep.warnings
    finding = rep.warnings[0]
    assert finding.kind == FindingKind.DEAD_LETTER_CHOICE
    assert finding.symbol == "CONFIG_KASAN_OUTLINE"


def test_active_choice_no_warning():
    """If a sibling option is is_current, the parent IS active —
    proposing a different sibling is fine."""
    preempt = _choice(
        "Preemption",
        [("PREEMPT_NONE", False), ("PREEMPT_VOLUNTARY", True), ("PREEMPT", False)],
    )
    surface = _surface(choices=[preempt])
    rep = check("CONFIG_PREEMPT=y\n", surface)
    assert not rep.has_errors
    assert not rep.warnings


# ── out-of-range tunables ────────────────────────────────────────────────


def test_out_of_range_int_tunable_is_error():
    surface = _surface(tunables=[_tunable("NR_CPUS", "8192", ranges=[("2", "8192")])])
    rep = check("CONFIG_NR_CPUS=99999\n", surface)
    assert rep.has_errors
    finding = rep.errors[0]
    assert finding.kind == FindingKind.OUT_OF_RANGE_TUNABLE


def test_in_range_int_tunable_no_error():
    surface = _surface(tunables=[_tunable("NR_CPUS", "8192", ranges=[("2", "8192")])])
    rep = check("CONFIG_NR_CPUS=64\n", surface)
    assert not rep.has_errors


def test_no_ranges_no_check():
    """Tunable without declared ranges — accept any int."""
    surface = _surface(tunables=[_tunable("LOG_BUF_SHIFT", "17", ranges=[])])
    rep = check("CONFIG_LOG_BUF_SHIFT=99\n", surface)
    assert not rep.has_errors


# ── post-olddefconfig stripped check ─────────────────────────────────────


def test_olddefconfig_stripped_warning():
    """When proposed says =y but actual says =n (or vice versa),
    olddefconfig dropped our change due to dependency conflict."""
    surface = _surface(toggles=[_toggle("X")])
    proposed = "CONFIG_X=y\n"
    actual = "# CONFIG_X is not set\n"
    rep = check(proposed, surface, actual_config_text=actual)
    assert not rep.has_errors  # this is a warning, not error
    finding = next(
        (f for f in rep.warnings if f.kind == FindingKind.OLDDEFCONFIG_STRIPPED),
        None,
    )
    assert finding is not None
    assert finding.symbol == "CONFIG_X"


def test_olddefconfig_no_stripped_when_match():
    surface = _surface(toggles=[_toggle("X")])
    proposed = "CONFIG_X=y\n"
    actual = "CONFIG_X=y\n"
    rep = check(proposed, surface, actual_config_text=actual)
    assert not rep.has_errors
    assert not any(f.kind == FindingKind.OLDDEFCONFIG_STRIPPED for f in rep.warnings)


# ── full integration ─────────────────────────────────────────────────────


def test_full_check_combines_findings():
    surface = _surface(
        toggles=[_toggle("KNOWN", current="n")],
        tunables=[_tunable("NR_CPUS", "8192", ranges=[("2", "8192")])],
        choices=[_choice("KASAN", [("KASAN_GENERIC", False), ("KASAN_OUTLINE", False)])],
    )
    proposed = (
        "CONFIG_KNOWN=y\n"          # ok
        "CONFIG_HALLUCINATED=y\n"    # error: unknown
        "CONFIG_NR_CPUS=99999\n"     # error: out of range
        "CONFIG_KASAN_OUTLINE=y\n"   # warning: dead letter
    )
    rep = check(proposed, surface)
    assert {f.symbol for f in rep.errors} == {"CONFIG_HALLUCINATED", "CONFIG_NR_CPUS"}
    assert {f.symbol for f in rep.warnings} == {"CONFIG_KASAN_OUTLINE"}
    assert rep.has_errors
