"""Regression tests for Linux v7.0+ conditional Kconfig dependencies.

The kernel added ``depends on X if Y`` in v7.0 (commit 76df6815dab7). The
bundled pip ``kconfiglib`` (14.1.0, unmaintained) can't parse it and raises
``KconfigError`` mid-tree, which used to abort ``propose`` entirely. autokernel
patches the parser (``_ensure_conditional_depends_support``) to encode it the
way the kernel itself does: ``depends on X if Y`` ≡ ``X || (Y == n)``.

See ``autokernel.kconfig_walk`` for the patch and ``cli.propose`` for the
defense-in-depth guard that keeps completed module-trim work if any *other*
unparseable syntax still slips through.
"""

from __future__ import annotations

from pathlib import Path

import kconfiglib

from autokernel.kconfig_walk import (
    _ensure_conditional_depends_support,
    _installed_kconfiglib_handles_conditional_deps,
    walk,
)


def _make_conditional_dep_source(tmp_path: Path) -> Path:
    """A minimal walk()-compatible tree using conditional dependencies."""
    src = tmp_path / "linux"
    src.mkdir()
    (src / "Makefile").write_text("# fake kernel Makefile\n")

    arch_dir = src / "arch" / "x86"
    arch_dir.mkdir(parents=True)
    (arch_dir / "Kconfig").write_text("config X86\n\tdef_bool y\n")

    (src / "Kconfig").write_text(
        'mainmenu "conditional-dep kernel"\n'
        'source "arch/$(SRCARCH)/Kconfig"\n'
        'source "drivers/Kconfig"\n'
    )

    drv = src / "drivers"
    drv.mkdir()
    # Mirrors drivers/usb/cdns3/Kconfig: both the simple and the compound form.
    (drv / "Kconfig").write_text(
        'config USB\n\tbool "USB host"\n'
        'config USB_GADGET\n\tbool "USB gadget"\n'
        'config USB_CDNS_SUPPORT\n'
        '\tbool "Cadence USB"\n'
        "\tdepends on USB if !USB_GADGET\n"
        "\tdepends on (USB && USB_GADGET) if (USB || USB_GADGET)\n"
        "\thelp\n"
        "\t  Synthetic conditional-dependency stanza.\n"
    )
    return src


def test_walk_handles_conditional_depends(tmp_path: Path) -> None:
    """walk() must parse a tree with ``depends on X if Y`` without crashing
    and still surface the affected symbol."""
    src = _make_conditional_dep_source(tmp_path)

    surface = walk(src, arch="x86_64")  # must not raise KconfigError

    names = {t.name for t in surface.toggles}
    assert "USB_CDNS_SUPPORT" in names


def test_conditional_depends_encoding(tmp_path: Path, monkeypatch) -> None:
    """The patch must encode ``depends on A if B`` as ``A || (B == n)``."""
    _ensure_conditional_depends_support()

    (tmp_path / "Kconfig").write_text(
        'config A\n\tbool "A"\n'
        'config B\n\tbool "B"\n'
        'config C\n\tbool "C"\n\tdepends on A if B\n'
    )
    monkeypatch.chdir(tmp_path)
    kconf = kconfiglib.Kconfig("Kconfig", warn=False, warn_to_stderr=False)
    a, b, c = kconf.syms["A"], kconf.syms["B"], kconf.syms["C"]

    # B = n → the condition lifts, so C's dependency is satisfied regardless of A.
    a.set_value(0)
    b.set_value(0)
    assert kconfiglib.expr_value(c.direct_dep) == 2  # y

    # B = y → C genuinely depends on A.
    a.set_value(0)
    b.set_value(2)
    assert kconfiglib.expr_value(c.direct_dep) == 0  # n

    a.set_value(2)
    b.set_value(2)
    assert kconfiglib.expr_value(c.direct_dep) == 2  # y


def test_ensure_conditional_depends_support_idempotent() -> None:
    """Applying the patch repeatedly is safe and reports success."""
    assert _ensure_conditional_depends_support() is True
    assert _ensure_conditional_depends_support() is True
    # After the patch is active, the installed lib does parse the syntax.
    assert _installed_kconfiglib_handles_conditional_deps() is True
