"""Integration tests for the `autokernel build` CLI verb.

The actual ``make`` calls are mocked at the autokernel.build level so we
verify orchestration without compiling a kernel.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from autokernel import build as build_mod
from autokernel.cli import app

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def compiler_on_path(monkeypatch):
    """CLI build tests mock make, so they should not depend on host clang."""
    real_which = shutil.which

    def _which(cmd: str):
        if cmd in {"clang", "gcc"}:
            return f"/usr/bin/{cmd}"
        return real_which(cmd)

    monkeypatch.setattr(shutil, "which", _which)


def _seed_apply_done(tmp_path: Path, *, with_dkms: bool = False) -> tuple[Path, Path]:
    """Create a snapshot with final.config and a fake kernel source dir."""
    snap_src = "amd_desktop" if with_dkms else "intel_laptop"
    snap = tmp_path / "snap"
    shutil.copytree(FIXTURES / snap_src, snap)
    (snap / "final.config").write_text("CONFIG_FOO=y\n# CONFIG_BAR is not set\n")

    kernel_src = tmp_path / "linux-source"
    kernel_src.mkdir()
    (kernel_src / "Makefile").write_text("# fake kernel\n")
    return snap, kernel_src


@pytest.fixture
def captured_runs(monkeypatch):
    """Replace subprocess.run inside autokernel.build with a recorder."""
    calls: list[dict[str, Any]] = []

    class _Result:
        def __init__(self, rc=0):
            self.returncode = rc

    def _fake(argv, **kwargs):
        for f in (kwargs.get("stdout"), kwargs.get("stderr")):
            if hasattr(f, "write"):
                f.write(b"")
        calls.append({"argv": argv, **kwargs})
        return _Result(0)

    monkeypatch.setattr(build_mod.subprocess, "run", _fake)
    return calls


def test_build_prepare_only_default(tmp_path: Path, captured_runs):
    snap, src = _seed_apply_done(tmp_path)
    result = runner.invoke(app, ["build", str(snap), "--kernel-source", str(src)])
    assert result.exit_code == 0, result.output
    assert "prepared" in result.output.lower()
    # one call: make olddefconfig
    assert len(captured_runs) == 1
    assert captured_runs[0]["argv"] == [
        "make",
        "CC=clang",
        "HOSTCC=clang",
        "olddefconfig",
    ]
    # final.config dropped into source
    assert (src / ".config").exists()
    assert "CONFIG_FOO=y" in (src / ".config").read_text()


def test_build_execute_runs_make_bindeb_pkg(tmp_path: Path, captured_runs):
    snap, src = _seed_apply_done(tmp_path)
    result = runner.invoke(
        app,
        [
            "build",
            str(snap),
            "--kernel-source",
            str(src),
            "--execute",
            "--jobs",
            "4",
            "--target",
            "bindeb-pkg",
        ],
    )
    assert result.exit_code == 0, result.output
    # two calls: olddefconfig, then make -j4 bindeb-pkg
    assert len(captured_runs) == 2
    assert captured_runs[1]["argv"] == [
        "make",
        "-j4",
        "CC=clang",
        "HOSTCC=clang",
        "bindeb-pkg",
    ]


def test_build_auto_target_picks_family_default(
    tmp_path: Path, captured_runs, monkeypatch
):
    """--target auto should resolve via detect_distro → spec_for → build_target_default.

    We mock detect_distro to a known family so the test is host-agnostic.
    """
    from autokernel import cli
    from autokernel.distro import parse_os_release

    monkeypatch.setattr(
        cli,
        "detect_distro",
        lambda: parse_os_release("ID=fedora\n"),
    )
    snap, src = _seed_apply_done(tmp_path)
    result = runner.invoke(
        app,
        ["build", str(snap), "--kernel-source", str(src), "--execute", "--jobs", "1"],
    )
    assert result.exit_code == 0, result.output
    # Fedora's family default is rpm-pkg
    assert captured_runs[1]["argv"] == [
        "make",
        "-j1",
        "CC=clang",
        "HOSTCC=clang",
        "rpm-pkg",
    ]


def test_build_refuses_execute_when_dkms_present(tmp_path: Path, captured_runs):
    snap, src = _seed_apply_done(tmp_path, with_dkms=True)
    result = runner.invoke(
        app,
        ["build", str(snap), "--kernel-source", str(src), "--execute"],
    )
    assert result.exit_code == 3, result.output
    assert "dkms" in result.output.lower()
    # No make calls should have happened.
    assert captured_runs == []


def test_build_force_dkms_proceeds(tmp_path: Path, captured_runs):
    snap, src = _seed_apply_done(tmp_path, with_dkms=True)
    result = runner.invoke(
        app,
        [
            "build",
            str(snap),
            "--kernel-source",
            str(src),
            "--execute",
            "--force-dkms",
            "--target",
            "bindeb-pkg",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(captured_runs) == 2  # olddefconfig + bindeb-pkg


def test_build_missing_final_config_exits_2(tmp_path: Path, captured_runs):
    snap = tmp_path / "snap"
    shutil.copytree(FIXTURES / "intel_laptop", snap)
    # no final.config
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text("\n")
    result = runner.invoke(app, ["build", str(snap), "--kernel-source", str(src)])
    assert result.exit_code == 2
    assert "final.config not found" in result.output.lower()


def test_build_missing_makefile_exits_1(tmp_path: Path, captured_runs):
    snap, _ = _seed_apply_done(tmp_path)
    not_a_kernel = tmp_path / "notkernel"
    not_a_kernel.mkdir()
    result = runner.invoke(
        app, ["build", str(snap), "--kernel-source", str(not_a_kernel)]
    )
    assert result.exit_code == 1
    assert "makefile" in result.output.lower()


def test_build_olddefconfig_failure_propagates(tmp_path: Path, monkeypatch):
    snap, src = _seed_apply_done(tmp_path)

    class _Result:
        def __init__(self, rc=2):
            self.returncode = rc

    def _fake(argv, **kwargs):
        for f in (kwargs.get("stdout"), kwargs.get("stderr")):
            if hasattr(f, "write"):
                f.write(b"oops\n")
        return _Result(2)

    monkeypatch.setattr(build_mod.subprocess, "run", _fake)
    result = runner.invoke(app, ["build", str(snap), "--kernel-source", str(src)])
    assert result.exit_code == 2
    assert "olddefconfig failed" in result.output.lower()


def test_build_target_override(tmp_path: Path, captured_runs):
    snap, src = _seed_apply_done(tmp_path)
    result = runner.invoke(
        app,
        [
            "build",
            str(snap),
            "--kernel-source",
            str(src),
            "--execute",
            "--jobs",
            "2",
            "--target",
            "deb-pkg",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured_runs[1]["argv"] == [
        "make",
        "-j2",
        "CC=clang",
        "HOSTCC=clang",
        "deb-pkg",
    ]


# ── --lto / --patches ──────────────────────────────────────────────────────


def test_build_lto_without_llvm_fails_fast(tmp_path: Path, captured_runs):
    snap, src = _seed_apply_done(tmp_path)
    result = runner.invoke(
        app, ["build", str(snap), "--kernel-source", str(src), "--lto", "thin"]
    )
    assert result.exit_code == 2
    assert "--compiler=llvm" in result.output
    assert captured_runs == []


def _git_kernel_tree(tmp_path: Path) -> Path:
    import subprocess

    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    src = tmp_path / "linux-git"
    src.mkdir()
    (src / "Makefile").write_text(
        "VERSION = 7\nPATCHLEVEL = 3\nSUBLEVEL = 0\nEXTRAVERSION = -rc6\n"
    )
    (src / "a.c").write_text("int x = 1;\n")
    subprocess.run([*git, "init", "-q", str(src)], check=True)
    subprocess.run([*git, "-C", str(src), "add", "."], check=True)
    subprocess.run([*git, "-C", str(src), "commit", "-qm", "base"], check=True)
    (src / "a.c").write_text("int x = 2;\n")
    subprocess.run([*git, "-C", str(src), "commit", "-qam", "fix x"], check=True)
    pdir = tmp_path / "patches" / "fix-x"
    pdir.mkdir(parents=True)
    subprocess.run(
        [*git, "-C", str(src), "format-patch", "-q", "-1", "-o", str(pdir)], check=True
    )
    subprocess.run(
        [*git, "-C", str(src), "reset", "-q", "--hard", "HEAD~1"], check=True
    )
    (pdir / "series").write_text(
        "".join(p.name + "\n" for p in sorted(pdir.glob("*.patch")))
    )
    (pdir / "meta.toml").write_text(
        'kind = "upstream-pending"\nmin_version = "7.3-rc1"\n'
    )
    return src


def test_build_patches_applied_and_recorded(tmp_path: Path, monkeypatch):
    import json

    snap, _ = _seed_apply_done(tmp_path)
    src = _git_kernel_tree(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    def _fake_prepare(**kw):
        return build_mod.PrepareResult(
            source_dir=kw["source_dir"],
            config_path=kw["source_dir"] / ".config",
            log_dir=log_dir,
            steps=[],
        )

    monkeypatch.setattr(build_mod, "prepare", _fake_prepare)
    result = runner.invoke(
        app,
        [
            "build",
            str(snap),
            "--kernel-source",
            str(src),
            "--patches",
            "fix-x",
            "--patches-dir",
            str(tmp_path / "patches"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (src / "a.c").read_text() == "int x = 2;\n"
    record = json.loads((log_dir / "build.json").read_text())
    assert record["kernel_version"] == "7.3-rc6"
    assert record["patches"]["patches"][0]["result"] == "applied"
    assert record["patches"]["base_commit"] != record["source_commit"]


def test_build_unknown_patch_set(tmp_path: Path, captured_runs):
    snap, src = _seed_apply_done(tmp_path)
    (tmp_path / "patches").mkdir()
    result = runner.invoke(
        app,
        [
            "build",
            str(snap),
            "--kernel-source",
            str(src),
            "--patches",
            "nope",
            "--patches-dir",
            str(tmp_path / "patches"),
        ],
    )
    assert result.exit_code == 2
    assert "unknown patch set" in result.output
    assert captured_runs == []
