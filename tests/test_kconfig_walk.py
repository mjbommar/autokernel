"""Tests for autokernel.kconfig_walk.

We don't ship the entire Linux source as a fixture — too heavy. Instead
we build a minimal synthetic kernel-source skeleton (Makefile + a
handful of Kconfig files) that exercises every shape of symbol the
walker has to handle: choice groups, bool toggles, ints, strings,
tristates (which should be SKIPPED — handled by the existing trim
pipeline), and the post-6.19 ``transitional``/``modules`` keywords
that crash kconfiglib until we patch them out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autokernel.kconfig_walk import (
    KconfigSurface,
    SymbolType,
    walk,
)


def _make_synthetic_source(tmp_path: Path) -> Path:
    """Build a tiny kernel-source-shaped tree."""
    src = tmp_path / "linux"
    src.mkdir()
    (src / "Makefile").write_text("# fake kernel Makefile\n")

    # Required arch overlay so kconfiglib can resolve `arch/$(SRCARCH)/Kconfig`.
    arch_dir = src / "arch" / "x86"
    arch_dir.mkdir(parents=True)
    (arch_dir / "Kconfig").write_text(
        # An x86-shaped Kconfig with a few real-feeling symbols.
        "config X86\n"
        "\tdef_bool y\n"
        "\nconfig 64BIT\n"
        '\tbool "64-bit kernel"\n'
        "\tdefault y\n"
    )

    # Top-level Kconfig that sources arch + a few feature pages.
    (src / "Kconfig").write_text(
        'mainmenu "synthetic kernel"\n'
        '\nsource "arch/$(SRCARCH)/Kconfig"\n'
        'source "kernel/Kconfig.preempt"\n'
        'source "kernel/Kconfig.hz"\n'
        'source "kernel/module/Kconfig"\n'
        'source "mm/Kconfig"\n'
    )

    # PREEMPT choice (4 options)
    pre_dir = src / "kernel"
    pre_dir.mkdir()
    (pre_dir / "Kconfig.preempt").write_text(
        "choice\n"
        '\tprompt "Preemption Model"\n'
        "\tdefault PREEMPT_VOLUNTARY\n"
        "\nconfig PREEMPT_NONE\n"
        '\tbool "No Forced Preemption (Server)"\n'
        "\thelp\n"
        "\t  Best throughput, server-style.\n"
        "\nconfig PREEMPT_VOLUNTARY\n"
        '\tbool "Voluntary Kernel Preemption (Desktop)"\n'
        "\thelp\n"
        "\t  Generic distro default.\n"
        "\nconfig PREEMPT\n"
        '\tbool "Preemptible Kernel (Low-Latency Desktop)"\n'
        "\nconfig PREEMPT_RT\n"
        '\tbool "Fully Preemptible Kernel (Real-Time)"\n'
        "\nendchoice\n"
    )

    # HZ choice (4 options) — exposes the int tunable form too.
    (pre_dir / "Kconfig.hz").write_text(
        "choice\n"
        '\tprompt "Timer frequency"\n'
        "\tdefault HZ_250\n"
        "\nconfig HZ_100\n"
        '\tbool "100 HZ"\n'
        "\nconfig HZ_250\n"
        '\tbool "250 HZ"\n'
        "\nconfig HZ_300\n"
        '\tbool "300 HZ"\n'
        "\nconfig HZ_1000\n"
        '\tbool "1000 HZ"\n'
        "\nendchoice\n"
        "\nconfig HZ\n"
        "\tint\n"
        "\tdefault 100 if HZ_100\n"
        "\tdefault 250 if HZ_250\n"
        "\tdefault 300 if HZ_300\n"
        "\tdefault 1000 if HZ_1000\n"
    )

    # MODULES — exercises the unsupported `modules` keyword pre-patching.
    mod_dir = src / "kernel" / "module"
    mod_dir.mkdir()
    (mod_dir / "Kconfig").write_text(
        "menuconfig MODULES\n"
        '\tbool "Enable loadable module support"\n'
        "\tmodules\n"
        "\thelp\n"
        "\t  Synthetic.\n"
    )

    # mm/Kconfig — bool toggles + int tunables + a tristate (must skip).
    mm_dir = src / "mm"
    mm_dir.mkdir()
    (mm_dir / "Kconfig").write_text(
        # Bool toggle (user-visible — should appear).
        "config TRANSPARENT_HUGEPAGE\n"
        '\tbool "Transparent Hugepage Support"\n'
        "\tdefault y\n"
        "\thelp\n"
        "\t  Coalesce 4K pages into 2M ones.\n"
        # Bool toggle without prompt (must NOT appear).
        "\nconfig SECRET_INTERNAL\n"
        "\tbool\n"
        "\tdefault y\n"
        # Int tunable (should appear in tunables, not toggles).
        "\nconfig NR_CPUS\n"
        '\tint "Maximum number of CPUs"\n'
        "\trange 2 8192\n"
        "\tdefault 64\n"
        "\thelp\n"
        "\t  Cap; >64 incurs off-stack cpumask.\n"
        # String tunable.
        "\nconfig LOCALVERSION\n"
        '\tstring "Local version - append to kernel release"\n'
        '\tdefault ""\n'
        # Tristate — must NOT appear in toggles or tunables (handled by trim).
        "\nconfig FOOBAR\n"
        '\ttristate "FooBar driver"\n'
        "\tdefault m\n"
        # Symbol marked transitional — must be silently stripped.
        "\nconfig OLD_NAME\n"
        "\tbool\n"
        "\ttransitional\n"
    )

    return src


# ── walk integration ─────────────────────────────────────────────────────


def test_walk_returns_surface_with_all_three_dimensions(tmp_path):
    src = _make_synthetic_source(tmp_path)
    surface = walk(src, arch="x86_64")
    assert isinstance(surface, KconfigSurface)
    assert surface.arch == "x86_64"
    assert len(surface.choices) >= 2  # PREEMPT + HZ
    assert len(surface.toggles) >= 2  # TRANSPARENT_HUGEPAGE, MODULES
    assert len(surface.tunables) >= 2  # NR_CPUS, LOCALVERSION


def test_walk_resolves_x86_64_to_arch_x86(tmp_path):
    src = _make_synthetic_source(tmp_path)
    surface = walk(src, arch="x86_64")
    # If the arch resolution were broken, walk() would have raised.
    assert any(
        c.name == "PREEMPT_VOLUNTARY" or c.name == "PREEMPT_NONE"
        for ch in surface.choices
        for c in ch.options
    )


def test_walk_strips_transitional_keyword(tmp_path):
    """The synthetic source has `transitional` on a symbol that
    kconfiglib 14.1.0 trips over. After walk(), the source files
    must be byte-identical to what we wrote."""
    src = _make_synthetic_source(tmp_path)
    before = (src / "mm" / "Kconfig").read_text()
    walk(src, arch="x86_64")
    after = (src / "mm" / "Kconfig").read_text()
    assert before == after, "walk() left the source modified!"


def test_walk_strips_modules_keyword(tmp_path):
    """Same restoration guarantee for the `modules` keyword."""
    src = _make_synthetic_source(tmp_path)
    before = (src / "kernel" / "module" / "Kconfig").read_text()
    walk(src, arch="x86_64")
    after = (src / "kernel" / "module" / "Kconfig").read_text()
    assert before == after


# ── choice extraction ───────────────────────────────────────────────────


def test_walk_extracts_preempt_choice(tmp_path):
    src = _make_synthetic_source(tmp_path)
    surface = walk(src, arch="x86_64")
    preempt = next(
        (c for c in surface.choices if c.prompt and "Preemption" in c.prompt),
        None,
    )
    assert preempt is not None
    names = {o.name for o in preempt.options}
    assert names == {"PREEMPT_NONE", "PREEMPT_VOLUNTARY", "PREEMPT", "PREEMPT_RT"}


def test_walk_marks_current_choice_option(tmp_path):
    src = _make_synthetic_source(tmp_path)
    surface = walk(src, arch="x86_64")
    preempt = next(c for c in surface.choices if c.prompt and "Preemption" in c.prompt)
    current = [o.name for o in preempt.options if o.is_current]
    assert current == ["PREEMPT_VOLUNTARY"]


def test_walk_extracts_hz_choice_with_help(tmp_path):
    src = _make_synthetic_source(tmp_path)
    surface = walk(src, arch="x86_64")
    hz = next(
        (c for c in surface.choices if c.prompt and "Timer" in c.prompt),
        None,
    )
    assert hz is not None
    assert {o.name for o in hz.options} == {"HZ_100", "HZ_250", "HZ_300", "HZ_1000"}


# ── toggle extraction ───────────────────────────────────────────────────


def test_walk_extracts_visible_bool_toggle(tmp_path):
    src = _make_synthetic_source(tmp_path)
    surface = walk(src, arch="x86_64")
    thp = next((t for t in surface.toggles if t.name == "TRANSPARENT_HUGEPAGE"), None)
    assert thp is not None
    assert thp.current_value == "y"
    assert thp.prompt == "Transparent Hugepage Support"
    assert thp.help and "Coalesce" in thp.help


def test_walk_skips_promptless_bool_symbols(tmp_path):
    """SECRET_INTERNAL has `bool` without a prompt — internal symbol,
    never user-visible. Must NOT appear in toggles."""
    src = _make_synthetic_source(tmp_path)
    surface = walk(src, arch="x86_64")
    assert all(t.name != "SECRET_INTERNAL" for t in surface.toggles)


def test_walk_skips_tristate_in_toggles_and_tunables(tmp_path):
    """Tristates are handled by the existing trim pipeline; they must
    not leak into the new dimensions."""
    src = _make_synthetic_source(tmp_path)
    surface = walk(src, arch="x86_64")
    assert all(t.name != "FOOBAR" for t in surface.toggles)
    assert all(t.name != "FOOBAR" for t in surface.tunables)


# ── tunable extraction ──────────────────────────────────────────────────


def test_walk_extracts_int_tunable_with_range(tmp_path):
    src = _make_synthetic_source(tmp_path)
    surface = walk(src, arch="x86_64")
    nr = next((t for t in surface.tunables if t.name == "NR_CPUS"), None)
    assert nr is not None
    assert nr.type == SymbolType.INT
    assert nr.current_value == "64"
    assert nr.ranges == [("2", "8192")]


def test_walk_extracts_string_tunable(tmp_path):
    src = _make_synthetic_source(tmp_path)
    surface = walk(src, arch="x86_64")
    lv = next((t for t in surface.tunables if t.name == "LOCALVERSION"), None)
    assert lv is not None
    assert lv.type == SymbolType.STRING
    assert lv.current_value == ""


# ── error paths ─────────────────────────────────────────────────────────


def test_walk_raises_on_missing_kconfig(tmp_path):
    src = tmp_path / "empty"
    src.mkdir()
    (src / "Makefile").write_text("# fake\n")
    with pytest.raises(FileNotFoundError, match="Kconfig"):
        walk(src, arch="x86_64")


# ── arch mapping ────────────────────────────────────────────────────────


def test_walk_maps_aarch64_to_arm64_srcarch(tmp_path):
    """``arch="aarch64"`` should map to ``SRCARCH=arm64``."""
    src = _make_synthetic_source(tmp_path)
    # Replace the x86 arch dir with an arm64 one.
    (src / "arch" / "x86" / "Kconfig").rename(src / "arch" / "x86" / "_unused")
    arm_dir = src / "arch" / "arm64"
    arm_dir.mkdir()
    (arm_dir / "Kconfig").write_text("config ARM64\n\tdef_bool y\n")
    surface = walk(src, arch="aarch64")
    assert surface.arch == "aarch64"


# ── config-loading ──────────────────────────────────────────────────────


def test_walk_with_config_path_reflects_actual_assignments(tmp_path):
    """When a .config is loaded, current_value reflects it (not the
    Kconfig-default)."""
    src = _make_synthetic_source(tmp_path)
    cfg = tmp_path / "test.config"
    cfg.write_text(
        "# x86 platform\n"
        "CONFIG_X86=y\n"
        "CONFIG_64BIT=y\n"
        "CONFIG_HZ_1000=y\n"
        "CONFIG_HZ=1000\n"
        "CONFIG_NR_CPUS=32\n"
        'CONFIG_LOCALVERSION="-test"\n'
        "# CONFIG_TRANSPARENT_HUGEPAGE is not set\n"
    )
    surface = walk(src, arch="x86_64", config_path=cfg)
    nr = next(t for t in surface.tunables if t.name == "NR_CPUS")
    assert nr.current_value == "32"
    lv = next(t for t in surface.tunables if t.name == "LOCALVERSION")
    assert lv.current_value == "-test"
    thp = next(t for t in surface.toggles if t.name == "TRANSPARENT_HUGEPAGE")
    assert thp.current_value == "n"
    hz = next(c for c in surface.choices if c.prompt and "Timer" in c.prompt)
    current = [o.name for o in hz.options if o.is_current]
    assert current == ["HZ_1000"]
