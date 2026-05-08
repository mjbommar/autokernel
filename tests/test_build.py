"""Tests for the build module — subprocess invocations are mocked.

We can't run a real kernel build in CI / a unit-test loop. Instead we
verify the *contract*: argv shape, CWD, environment variables, log file
locations, and result objects.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from autokernel import build as build_mod
from autokernel.build import (
    BuildResult,
    PrepareResult,
    StepResult,
    build,
    prepare,
)


def _make_fake_kernel_source(tmp_path: Path) -> Path:
    src = tmp_path / "linux-source"
    src.mkdir()
    (src / "Makefile").write_text("# fake kernel Makefile\n")
    return src


def _make_final_config(tmp_path: Path) -> Path:
    p = tmp_path / "final.config"
    p.write_text("CONFIG_FOO=y\n# CONFIG_BAR is not set\n")
    return p


class _FakeProcess:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode


@pytest.fixture
def captured_runs(monkeypatch):
    """Replace subprocess.run with a recorder; return the list of calls."""
    calls: list[dict[str, Any]] = []

    def _fake(argv, **kwargs):
        # Drain stdout/stderr like the real subprocess does so the
        # caller doesn't notice the difference.
        for f in (kwargs.get("stdout"), kwargs.get("stderr")):
            if hasattr(f, "write"):
                f.write(b"")
        calls.append({"argv": argv, **kwargs})
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(build_mod.subprocess, "run", _fake)
    return calls


# ── prepare ─────────────────────────────────────────────────────────────────


def test_prepare_copies_config_and_runs_olddefconfig(
    tmp_path: Path, captured_runs: list[dict[str, Any]]
):
    src = _make_fake_kernel_source(tmp_path)
    cfg = _make_final_config(tmp_path)
    snap = tmp_path / "snap"
    snap.mkdir()

    result = prepare(source_dir=src, config_path=cfg, snapshot_dir=snap)

    assert isinstance(result, PrepareResult)
    assert (src / ".config").exists()
    assert (src / ".config").read_text() == cfg.read_text()

    # exactly one subprocess call: make olddefconfig
    assert len(captured_runs) == 1
    call = captured_runs[0]
    assert call["argv"] == ["make", "olddefconfig"]
    assert Path(call["cwd"]) == src.resolve()


def test_prepare_sets_reproducibility_env(tmp_path: Path, captured_runs):
    src = _make_fake_kernel_source(tmp_path)
    cfg = _make_final_config(tmp_path)
    snap = tmp_path / "snap"
    snap.mkdir()

    prepare(source_dir=src, config_path=cfg, snapshot_dir=snap)
    env = captured_runs[0]["env"]
    assert env["KBUILD_BUILD_TIMESTAMP"] == build_mod.REPRO_TIMESTAMP_DEFAULT
    assert env["KBUILD_BUILD_USER"] == build_mod.REPRO_USER_DEFAULT
    assert env["KBUILD_BUILD_HOST"] == build_mod.REPRO_HOST_DEFAULT


def test_prepare_env_overrides(tmp_path: Path, captured_runs):
    src = _make_fake_kernel_source(tmp_path)
    cfg = _make_final_config(tmp_path)
    snap = tmp_path / "snap"
    snap.mkdir()

    prepare(
        source_dir=src,
        config_path=cfg,
        snapshot_dir=snap,
        env_overrides={"KBUILD_BUILD_USER": "alice"},
    )
    assert captured_runs[0]["env"]["KBUILD_BUILD_USER"] == "alice"


def test_prepare_writes_log_files(tmp_path: Path, captured_runs):
    src = _make_fake_kernel_source(tmp_path)
    cfg = _make_final_config(tmp_path)
    snap = tmp_path / "snap"
    snap.mkdir()

    result = prepare(source_dir=src, config_path=cfg, snapshot_dir=snap)
    assert result.log_dir.exists()
    assert (result.log_dir / "olddefconfig.argv.log").exists()
    assert (result.log_dir / "olddefconfig.env.log").exists()
    argv_log = (result.log_dir / "olddefconfig.argv.log").read_text()
    assert "make" in argv_log
    assert "olddefconfig" in argv_log


def test_prepare_rejects_non_kernel_dir(tmp_path: Path):
    not_a_kernel = tmp_path / "notkernel"
    not_a_kernel.mkdir()
    cfg = _make_final_config(tmp_path)
    snap = tmp_path / "snap"
    snap.mkdir()
    with pytest.raises(FileNotFoundError, match="Makefile"):
        prepare(source_dir=not_a_kernel, config_path=cfg, snapshot_dir=snap)


def test_prepare_rejects_missing_config(tmp_path: Path):
    src = _make_fake_kernel_source(tmp_path)
    snap = tmp_path / "snap"
    snap.mkdir()
    with pytest.raises(FileNotFoundError, match="final.config"):
        prepare(source_dir=src, config_path=tmp_path / "missing", snapshot_dir=snap)


# ── build ───────────────────────────────────────────────────────────────────


def test_build_invokes_bindeb_pkg(tmp_path: Path, captured_runs):
    src = _make_fake_kernel_source(tmp_path)
    (src / ".config").write_text("CONFIG_X=y\n")
    snap = tmp_path / "snap"
    snap.mkdir()

    result = build(source_dir=src, snapshot_dir=snap, jobs=8)

    assert len(captured_runs) == 1
    assert captured_runs[0]["argv"] == ["make", "-j8", "bindeb-pkg"]
    assert isinstance(result, BuildResult)
    assert result.steps[0].ok is True


def test_build_default_jobs_uses_cpu_count(tmp_path: Path, captured_runs, monkeypatch):
    src = _make_fake_kernel_source(tmp_path)
    (src / ".config").write_text("CONFIG_X=y\n")
    snap = tmp_path / "snap"
    snap.mkdir()
    monkeypatch.setattr(build_mod.os, "cpu_count", lambda: 16)

    build(source_dir=src, snapshot_dir=snap)
    assert captured_runs[0]["argv"] == ["make", "-j16", "bindeb-pkg"]


def test_build_ccache_wraps_cc_when_available(tmp_path: Path, captured_runs, monkeypatch):
    src = _make_fake_kernel_source(tmp_path)
    (src / ".config").write_text("CONFIG_X=y\n")
    snap = tmp_path / "snap"
    snap.mkdir()
    monkeypatch.setattr(build_mod.shutil, "which", lambda c: "/usr/bin/ccache" if c == "ccache" else None)

    build(source_dir=src, snapshot_dir=snap, jobs=2)
    env = captured_runs[0]["env"]
    assert env["CC"].startswith("ccache ")
    assert env["HOSTCC"].startswith("ccache ")


def test_build_no_ccache_when_disabled(tmp_path: Path, captured_runs, monkeypatch):
    src = _make_fake_kernel_source(tmp_path)
    (src / ".config").write_text("CONFIG_X=y\n")
    snap = tmp_path / "snap"
    snap.mkdir()
    monkeypatch.setattr(build_mod.shutil, "which", lambda c: "/usr/bin/ccache" if c == "ccache" else None)

    build(source_dir=src, snapshot_dir=snap, jobs=2, use_ccache=False)
    env = captured_runs[0]["env"]
    assert "ccache" not in env.get("CC", "cc")


def test_build_picks_up_deb_artifacts(tmp_path: Path, captured_runs):
    src = _make_fake_kernel_source(tmp_path)
    (src / ".config").write_text("CONFIG_X=y\n")
    # Simulate make output: .debs land in the parent of source_dir
    deb1 = tmp_path / "linux-image-6.13.0-12-generic_amd64.deb"
    deb2 = tmp_path / "linux-headers-6.13.0-12-generic_amd64.deb"
    deb1.write_text("")
    deb2.write_text("")
    snap = tmp_path / "snap"
    snap.mkdir()

    result = build(source_dir=src, snapshot_dir=snap, jobs=1)
    assert deb1 in result.deb_paths
    assert deb2 in result.deb_paths
    assert result.ok


def test_build_rejects_unprepared_source(tmp_path: Path):
    src = _make_fake_kernel_source(tmp_path)
    snap = tmp_path / "snap"
    snap.mkdir()
    with pytest.raises(FileNotFoundError, match=".config"):
        build(source_dir=src, snapshot_dir=snap, jobs=1)


def test_build_failure_recorded(tmp_path: Path, monkeypatch):
    src = _make_fake_kernel_source(tmp_path)
    (src / ".config").write_text("CONFIG_X=y\n")
    snap = tmp_path / "snap"
    snap.mkdir()

    def _fail(argv, **kwargs):
        for f in (kwargs.get("stdout"), kwargs.get("stderr")):
            if hasattr(f, "write"):
                f.write(b"build broke\n")
        return _FakeProcess(returncode=2)

    monkeypatch.setattr(build_mod.subprocess, "run", _fail)
    result = build(source_dir=src, snapshot_dir=snap, jobs=1)
    assert not result.ok
    assert result.steps[0].exit_code == 2


# ── distro cert-path stripping (build.prepare auto-fix) ───────────────────


def test_prepare_strips_missing_distro_cert_paths(tmp_path, monkeypatch):
    """Ubuntu's running config bakes in
    `CONFIG_SYSTEM_TRUSTED_KEYS="debian/canonical-certs.pem"`; that path
    only exists inside Ubuntu's own kernel source. Building from
    kernel.org or apt-get-source dies with `No rule to make target`.
    Auto-strip when the .pem isn't there."""
    src = _make_fake_kernel_source(tmp_path)
    cfg = tmp_path / "final.config"
    cfg.write_text(
        'CONFIG_FOO=y\n'
        'CONFIG_SYSTEM_TRUSTED_KEYS="debian/canonical-certs.pem"\n'
        'CONFIG_SYSTEM_REVOCATION_KEYS="debian/canonical-revoked-certs.pem"\n'
    )
    snap = tmp_path / "snap"
    snap.mkdir()

    def _ok(argv, **kwargs):
        for f in (kwargs.get("stdout"), kwargs.get("stderr")):
            if hasattr(f, "write"):
                f.write(b"")
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(build_mod.subprocess, "run", _ok)
    result = prepare(source_dir=src, config_path=cfg, snapshot_dir=snap)
    assert result.ok
    final = (src / ".config").read_text()
    assert 'CONFIG_SYSTEM_TRUSTED_KEYS=""' in final
    assert 'CONFIG_SYSTEM_REVOCATION_KEYS=""' in final
    # The "real" path string should be gone.
    assert "canonical-certs.pem" not in final
    assert "canonical-revoked-certs.pem" not in final


def test_prepare_keeps_existing_cert_paths(tmp_path, monkeypatch):
    """If the user has a real signing key inside the source tree,
    leave it alone."""
    src = _make_fake_kernel_source(tmp_path)
    real_key = src / "certs" / "my-signing.pem"
    real_key.parent.mkdir(parents=True)
    real_key.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
    cfg = tmp_path / "final.config"
    cfg.write_text(
        'CONFIG_FOO=y\n'
        'CONFIG_SYSTEM_TRUSTED_KEYS="certs/my-signing.pem"\n'
    )
    snap = tmp_path / "snap"
    snap.mkdir()

    def _ok(argv, **kwargs):
        for f in (kwargs.get("stdout"), kwargs.get("stderr")):
            if hasattr(f, "write"):
                f.write(b"")
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(build_mod.subprocess, "run", _ok)
    prepare(source_dir=src, config_path=cfg, snapshot_dir=snap)
    final = (src / ".config").read_text()
    assert 'CONFIG_SYSTEM_TRUSTED_KEYS="certs/my-signing.pem"' in final


def test_prepare_leaves_already_empty_cert_paths(tmp_path, monkeypatch):
    src = _make_fake_kernel_source(tmp_path)
    cfg = tmp_path / "final.config"
    cfg.write_text(
        'CONFIG_FOO=y\nCONFIG_SYSTEM_TRUSTED_KEYS=""\n'
    )
    snap = tmp_path / "snap"
    snap.mkdir()

    def _ok(argv, **kwargs):
        for f in (kwargs.get("stdout"), kwargs.get("stderr")):
            if hasattr(f, "write"):
                f.write(b"")
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(build_mod.subprocess, "run", _ok)
    prepare(source_dir=src, config_path=cfg, snapshot_dir=snap)
    final = (src / ".config").read_text()
    assert 'CONFIG_SYSTEM_TRUSTED_KEYS=""' in final


# ── localmodconfig integration (build.prepare --localmodconfig) ─────────


def test_prepare_with_localmodconfig_runs_extra_steps(tmp_path, monkeypatch):
    """When localmodconfig=True, prepare runs olddefconfig →
    `yes '' | make LSMOD=<lsmod> localmodconfig` → olddefconfig again."""
    src = _make_fake_kernel_source(tmp_path)
    cfg = _make_final_config(tmp_path)
    snap = tmp_path / "snap"
    snap.mkdir()
    lsmod = snap / "lsmod"
    lsmod.write_text("Module Size Used by\nfoo 1024 0\n")

    calls = []

    def _ok(argv, **kwargs):
        calls.append(list(argv))
        for f in (kwargs.get("stdout"), kwargs.get("stderr")):
            if hasattr(f, "write"):
                f.write(b"")
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(build_mod.subprocess, "run", _ok)
    result = prepare(
        source_dir=src,
        config_path=cfg,
        snapshot_dir=snap,
        localmodconfig=True,
        lsmod_path=lsmod,
    )
    assert result.ok
    # Three steps: initial olddefconfig, localmodconfig, post-trim olddefconfig
    assert len(result.steps) == 3
    step_names = [s.name for s in result.steps]
    assert step_names == ["olddefconfig", "localmodconfig", "olddefconfig-after-localmodconfig"]
    # The localmodconfig step must invoke `yes '' | make LSMOD=...`
    lmc_argv = calls[1]
    assert lmc_argv[:2] == ["sh", "-c"]
    assert "localmodconfig" in lmc_argv[2]
    assert f"LSMOD={lsmod}" in lmc_argv[2]


def test_prepare_without_localmodconfig_runs_only_olddefconfig(tmp_path, monkeypatch):
    src = _make_fake_kernel_source(tmp_path)
    cfg = _make_final_config(tmp_path)
    snap = tmp_path / "snap"
    snap.mkdir()

    def _ok(argv, **kwargs):
        for f in (kwargs.get("stdout"), kwargs.get("stderr")):
            if hasattr(f, "write"):
                f.write(b"")
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(build_mod.subprocess, "run", _ok)
    result = prepare(source_dir=src, config_path=cfg, snapshot_dir=snap)
    assert len(result.steps) == 1
    assert result.steps[0].name == "olddefconfig"


# ── compiler + LTO env (v0.15) ────────────────────────────────────────────


def test_build_env_clang_default(tmp_path):
    from autokernel.build import _build_env
    env = _build_env(use_ccache=False, env_overrides=None)
    # Default is clang.
    assert env.get("CC") == "clang"
    assert env.get("HOSTCC") == "clang"
    assert "LLVM" not in env


def test_build_env_llvm_sets_llvm_flag():
    from autokernel.build import _build_env
    env = _build_env(use_ccache=False, env_overrides=None, compiler="llvm")
    assert env["LLVM"] == "1"


def test_build_env_gcc_explicit():
    from autokernel.build import _build_env
    env = _build_env(use_ccache=False, env_overrides=None, compiler="gcc")
    assert env.get("CC") == "gcc"
    assert env.get("HOSTCC") == "gcc"
    assert "LLVM" not in env


def test_build_env_unknown_compiler_raises():
    from autokernel.build import _build_env
    with pytest.raises(ValueError, match="unknown compiler"):
        _build_env(use_ccache=False, env_overrides=None, compiler="msvc")


def test_build_env_lto_thin_adds_kcflags():
    from autokernel.build import _build_env
    env = _build_env(use_ccache=False, env_overrides=None, lto="thin")
    assert "-flto=thin" in env.get("KCFLAGS", "")


def test_build_env_lto_full():
    from autokernel.build import _build_env
    env = _build_env(use_ccache=False, env_overrides=None, lto="full")
    kcflags = env.get("KCFLAGS", "")
    assert "-flto" in kcflags and "-flto=thin" not in kcflags


def test_build_env_lto_none_no_kcflags_change():
    from autokernel.build import _build_env
    env = _build_env(use_ccache=False, env_overrides={"KCFLAGS": "existing"}, lto="none")
    # env_overrides win; we don't add anything for lto=none.
    assert env["KCFLAGS"] == "existing"


def test_build_env_ccache_wraps_clang(tmp_path, monkeypatch):
    from autokernel.build import _build_env
    monkeypatch.setattr("shutil.which", lambda c: "/usr/bin/ccache" if c == "ccache" else None)
    env = _build_env(use_ccache=True, env_overrides=None, compiler="clang")
    # ccache wraps the active CC.
    assert "ccache" in env["CC"]
    assert "clang" in env["CC"]


def test_prepare_passes_compiler_to_env(tmp_path, monkeypatch):
    """`prepare(compiler='gcc')` should propagate to the make env."""
    src = _make_fake_kernel_source(tmp_path)
    cfg = _make_final_config(tmp_path)
    snap = tmp_path / "snap"
    snap.mkdir()

    captured_envs: list[dict[str, str]] = []

    def _ok(argv, **kwargs):
        captured_envs.append(dict(kwargs.get("env") or {}))
        for f in (kwargs.get("stdout"), kwargs.get("stderr")):
            if hasattr(f, "write"):
                f.write(b"")
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(build_mod.subprocess, "run", _ok)
    prepare(source_dir=src, config_path=cfg, snapshot_dir=snap, compiler="gcc")
    assert captured_envs
    assert captured_envs[0].get("CC") == "gcc"
    assert captured_envs[0].get("HOSTCC") == "gcc"
