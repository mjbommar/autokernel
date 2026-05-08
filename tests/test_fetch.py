"""Tests for the kernel source fetch module — purely planning + mocks."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from autokernel import fetch as fetch_mod
from autokernel.distro import DistroInfo, Family, parse_os_release, spec_for
from autokernel.fetch import (
    FetchPlan,
    Method,
    fetch_source,
    normalize_kernel_version,
    plan,
    select_method,
)


def _info(family: Family) -> DistroInfo:
    fixture = {
        Family.DEBIAN: "ID=ubuntu\nID_LIKE=debian\n",
        Family.FEDORA: "ID=fedora\n",
        Family.ARCH: "ID=arch\n",
        Family.SUSE: "ID=opensuse-tumbleweed\nID_LIKE=opensuse suse\n",
        Family.GENTOO: "ID=gentoo\n",
        Family.UNKNOWN: "ID=mystery\n",
    }[family]
    return parse_os_release(fixture)


# ── version parsing ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "release,expected",
    [
        ("6.13.0", (6, 13, 0)),
        ("6.13", (6, 13, 0)),
        ("6.13.5", (6, 13, 5)),
        ("6.13.0-12-generic", (6, 13, 0)),
        ("6.13.5-100.fc41.x86_64", (6, 13, 5)),
        ("5.15.167", (5, 15, 167)),
    ],
)
def test_normalize_kernel_version(release: str, expected: tuple[int, int, int]):
    assert normalize_kernel_version(release) == expected


def test_normalize_kernel_version_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_kernel_version("notaversion")


# ── method selection ────────────────────────────────────────────────────────


def test_select_method_debian():
    assert select_method(Family.DEBIAN) == Method.APT_GET_SOURCE


def test_select_method_fedora_uses_tarball():
    assert select_method(Family.FEDORA) == Method.TARBALL


def test_select_method_arch_uses_tarball():
    assert select_method(Family.ARCH) == Method.TARBALL


def test_select_method_unknown_falls_back_to_tarball():
    assert select_method(Family.UNKNOWN) == Method.TARBALL


# ── plan construction ──────────────────────────────────────────────────────


def test_plan_debian_apt_get_source(tmp_path: Path):
    info = _info(Family.DEBIAN)
    p = plan(
        distro=info,
        spec=spec_for(info),
        release="6.13.0",
        working_dir=tmp_path,
        method=Method.AUTO,
    )
    assert p.method == Method.APT_GET_SOURCE
    assert any("apt-get" in cmd[0] for cmd in p.commands)
    assert not p.needs_root


def test_plan_apt_install_source_includes_install_cmd(tmp_path: Path):
    info = _info(Family.DEBIAN)
    p = plan(
        distro=info,
        spec=spec_for(info),
        release="6.13.0",
        working_dir=tmp_path,
        method=Method.APT_INSTALL_SOURCE,
    )
    flat = " ".join(c for cmd in p.commands for c in cmd)
    assert "linux-source-6.13" in flat
    assert "apt" in flat
    assert p.needs_root


def test_plan_tarball_uses_kernelorg_url(tmp_path: Path):
    info = _info(Family.FEDORA)
    p = plan(
        distro=info,
        spec=spec_for(info),
        release="6.13.0",
        working_dir=tmp_path,
        method=Method.TARBALL,
    )
    flat = " ".join(c for cmd in p.commands for c in cmd)
    assert "cdn.kernel.org" in flat
    assert "linux-6.13.tar.xz" in flat


def test_plan_tarball_includes_patch_in_url(tmp_path: Path):
    info = _info(Family.UNKNOWN)
    p = plan(
        distro=info,
        spec=spec_for(info),
        release="6.13.5",
        working_dir=tmp_path,
        method=Method.TARBALL,
    )
    flat = " ".join(c for cmd in p.commands for c in cmd)
    assert "linux-6.13.5.tar.xz" in flat


def test_plan_arch_auto_falls_through_to_tarball(tmp_path: Path):
    info = _info(Family.ARCH)
    p = plan(
        distro=info,
        spec=spec_for(info),
        release="6.13.0",
        working_dir=tmp_path,
    )
    assert p.method == Method.TARBALL


def test_plan_gentoo_uses_emerge(tmp_path: Path):
    info = _info(Family.GENTOO)
    p = plan(
        distro=info,
        spec=spec_for(info),
        release="6.13.0",
        working_dir=tmp_path,
    )
    assert p.method == Method.EMERGE_GENTOO_SOURCES
    assert p.needs_root
    assert p.target_dir == Path("/usr/src/linux")


# ── fetch_source orchestration ──────────────────────────────────────────────


def test_fetch_source_dry_run_does_not_invoke_subprocess(monkeypatch, tmp_path: Path):
    called: list = []
    monkeypatch.setattr(
        fetch_mod.subprocess,
        "run",
        lambda *a, **k: called.append(a) or pytest.fail("subprocess.run called in dry_run"),
    )
    info = _info(Family.DEBIAN)
    result = fetch_source(
        distro=info,
        spec=spec_for(info),
        release="6.13.0",
        working_dir=tmp_path,
        dry_run=True,
    )
    assert called == []
    assert result.plan.method == Method.APT_GET_SOURCE
    assert not result.cached


def test_fetch_source_returns_cached_when_target_exists(tmp_path: Path):
    info = _info(Family.DEBIAN)
    p = plan(
        distro=info,
        spec=spec_for(info),
        release="6.13.0",
        working_dir=tmp_path,
        method=Method.APT_GET_SOURCE,
    )
    p.target_dir.mkdir(parents=True)
    (p.target_dir / "Makefile").write_text("# fake kernel\n")

    result = fetch_source(
        distro=info,
        spec=spec_for(info),
        release="6.13.0",
        working_dir=tmp_path,
        method=Method.APT_GET_SOURCE,
    )
    assert result.cached
    assert result.target_dir == p.target_dir


def test_fetch_source_executes_commands_when_not_cached(monkeypatch, tmp_path: Path):
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        # Don't actually run; pretend success.
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(fetch_mod.subprocess, "run", _fake_run)
    info = _info(Family.UNKNOWN)
    result = fetch_source(
        distro=info,
        spec=spec_for(info),
        release="6.13.0",
        working_dir=tmp_path / "wd",
        method=Method.TARBALL,
    )
    # mkdir + curl + tar
    assert len(calls) == 3
    assert "curl" in calls[1][0]
    assert "tar" in calls[2][0]
    assert not result.cached


def test_fetch_source_failure_raises(monkeypatch, tmp_path: Path):
    def _fake_run(argv, **kwargs):
        raise subprocess.CalledProcessError(returncode=1, cmd=argv)

    monkeypatch.setattr(fetch_mod.subprocess, "run", _fake_run)
    info = _info(Family.UNKNOWN)
    with pytest.raises(RuntimeError, match="rc=1"):
        fetch_source(
            distro=info,
            spec=spec_for(info),
            release="6.13.0",
            working_dir=tmp_path,
            method=Method.TARBALL,
        )


def test_fetch_source_missing_tool_raises(monkeypatch, tmp_path: Path):
    def _fake_run(argv, **kwargs):
        raise FileNotFoundError("no curl")

    monkeypatch.setattr(fetch_mod.subprocess, "run", _fake_run)
    info = _info(Family.UNKNOWN)
    with pytest.raises(RuntimeError, match="missing command"):
        fetch_source(
            distro=info,
            spec=spec_for(info),
            release="6.13.0",
            working_dir=tmp_path,
            method=Method.TARBALL,
        )
