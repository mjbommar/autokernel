"""Tests for the centralized error hint module."""

from __future__ import annotations

import typer
from rich.console import Console

import pytest

from autokernel import errors
from autokernel.distro import Family, parse_os_release, spec_for


def _capture(monkeypatch) -> list[str]:
    buf: list[str] = []
    fake = Console(file=None, record=True, width=120)

    def _print(*args, **kwargs):
        buf.append(" ".join(str(a) for a in args))

    fake.print = _print
    monkeypatch.setattr(errors, "err_console", fake)
    return buf


def test_fail_raises_typer_exit_with_code(monkeypatch):
    _capture(monkeypatch)
    exc = errors.fail("oops", fix="do X", exit_code=7)
    assert isinstance(exc, typer.Exit)
    assert exc.exit_code == 7
    with pytest.raises(typer.Exit) as e:
        raise exc
    assert e.value.exit_code == 7


def test_fail_renders_summary_why_and_fix(monkeypatch):
    buf = _capture(monkeypatch)
    errors.fail("snapshot not found", why="no manifest", fix="run scan first")
    rendered = "\n".join(buf)
    assert "snapshot not found" in rendered
    assert "no manifest" in rendered
    assert "run scan first" in rendered


def test_hint_not_a_snapshot_includes_path(monkeypatch):
    buf = _capture(monkeypatch)
    errors.hint_not_a_snapshot("/tmp/nope")
    rendered = "\n".join(buf)
    assert "/tmp/nope" in rendered
    assert "autokernel scan /tmp/nope" in rendered


def test_hint_missing_proposal_points_at_propose(monkeypatch):
    buf = _capture(monkeypatch)
    errors.hint_missing_proposal("/tmp/snap", "/tmp/snap/proposal.json")
    rendered = "\n".join(buf)
    assert "autokernel propose /tmp/snap" in rendered


def test_hint_missing_build_deps_uses_debian_install_cmd(monkeypatch):
    buf = _capture(monkeypatch)
    info = parse_os_release("ID=ubuntu\nID_LIKE=debian\n")
    errors.hint_missing_build_deps(spec_for(info), ["flex", "bison"])
    rendered = "\n".join(buf)
    assert "flex" in rendered and "bison" in rendered
    assert "apt install" in rendered  # Debian-family
    assert "sudo" in rendered


def test_hint_missing_build_deps_uses_fedora_install_cmd(monkeypatch):
    buf = _capture(monkeypatch)
    info = parse_os_release("ID=fedora\n")
    errors.hint_missing_build_deps(spec_for(info), ["flex"])
    rendered = "\n".join(buf)
    assert "dnf install" in rendered


def test_hint_missing_build_deps_unknown_distro_falls_back(monkeypatch):
    buf = _capture(monkeypatch)
    info = parse_os_release("ID=mystery\n")
    errors.hint_missing_build_deps(spec_for(info), ["flex"])
    rendered = "\n".join(buf)
    assert "package manager" in rendered.lower()


def test_hint_unsupported_bootloader_names_alternatives(monkeypatch):
    buf = _capture(monkeypatch)
    errors.hint_unsupported_bootloader("systemd-boot")
    rendered = "\n".join(buf)
    assert "systemd-boot" in rendered
    assert "GRUB2" in rendered  # tells user what IS supported
    assert "bootctl" in rendered  # gives manual recipe


def test_hint_dkms_blocks_auto_lists_modules(monkeypatch):
    buf = _capture(monkeypatch)
    errors.hint_dkms_blocks_auto(["nvidia", "vboxhost"])
    rendered = "\n".join(buf)
    assert "nvidia" in rendered
    assert "vboxhost" in rendered
    assert "--force-dkms" in rendered


def test_hint_load_bearing_brick_truncates_long_lists(monkeypatch):
    buf = _capture(monkeypatch)
    errors.hint_load_bearing_brick(["A", "B", "C", "D", "E"])
    rendered = "\n".join(buf)
    assert "5 load-bearing" in rendered
    assert "A, B, C…" in rendered  # ellipsis after 3


def test_hint_only_kernel_installed_warns_about_recovery(monkeypatch):
    buf = _capture(monkeypatch)
    errors.hint_only_kernel_installed()
    rendered = "\n".join(buf)
    assert "fallback" in rendered.lower()
    assert "--no-probation" in rendered
    assert "NOT RECOMMENDED" in rendered  # loud warning
