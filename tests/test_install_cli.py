"""End-to-end tests for `autokernel install` and `autokernel rollback` CLI.

The destructive subprocess calls are mocked. Bootloader detection and
distro detection are monkey-patched to deterministic values so tests are
host-agnostic.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from autokernel import cli, install as install_mod
from autokernel.bootloader import Bootloader, BootloaderKind
from autokernel.cli import app
from autokernel.distro import parse_os_release

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


_DEB_GRUB = Bootloader(
    kind=BootloaderKind.GRUB2,
    detected_via="/boot/grub/grub.cfg",
    config_dir=Path("/boot/grub"),
    grub_tool_prefix="",
)


@pytest.fixture
def patched_env(monkeypatch):
    """Pin distro=Ubuntu and bootloader=GRUB2 (Debian-flavoured)."""
    monkeypatch.setattr(
        cli,
        "detect_distro",
        lambda: parse_os_release(
            'ID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu Test"\n'
        ),
    )
    monkeypatch.setattr(cli.bootloader_mod, "detect", lambda: _DEB_GRUB)


@pytest.fixture
def captured_runs(monkeypatch):
    calls: list[dict[str, Any]] = []

    class _R:
        def __init__(self, rc=0):
            self.returncode = rc

    def _fake(argv, **kwargs):
        for f in (kwargs.get("stdout"), kwargs.get("stderr")):
            if hasattr(f, "write"):
                f.write(b"")
        calls.append({"argv": list(argv), **kwargs})
        return _R(0)

    monkeypatch.setattr(install_mod.subprocess, "run", _fake)
    return calls


def _seed_snapshot_with_deb(tmp_path: Path) -> tuple[Path, Path]:
    snap = tmp_path / "snap"
    shutil.copytree(FIXTURES / "intel_laptop", snap)
    deb = snap / "linux-image-6.13.5_amd64.deb"
    deb.write_text("")  # contents don't matter; we mock dpkg/apt
    return snap, deb


# ── install: dry-run ─────────────────────────────────────────────────────


def test_install_dry_run_renders_plan_no_subprocess(
    tmp_path: Path, patched_env, captured_runs
):
    snap, deb = _seed_snapshot_with_deb(tmp_path)
    result = runner.invoke(
        app,
        ["install", str(snap), "--package", str(deb), "--skip-preflight"],
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()
    # apt install should appear in the rendered plan
    assert "apt install" in result.output
    # No subprocess calls in dry-run mode
    assert captured_runs == []


def test_install_finds_packages_in_snapshot_dir(
    tmp_path: Path, patched_env, captured_runs
):
    """When --package isn't passed, autokernel should discover .deb files
    under the snapshot dir."""
    snap, deb = _seed_snapshot_with_deb(tmp_path)
    result = runner.invoke(app, ["install", str(snap), "--skip-preflight"])
    assert result.exit_code == 0, result.output
    # Rich console wraps long paths; check for an unmistakable substring.
    rendered_no_ws = "".join(result.output.split())
    assert "linux-image-6.13.5_amd64.deb" in rendered_no_ws


def test_install_no_package_and_none_found_exits_2(tmp_path: Path, patched_env):
    snap = tmp_path / "snap"
    shutil.copytree(FIXTURES / "intel_laptop", snap)
    result = runner.invoke(app, ["install", str(snap), "--skip-preflight"])
    assert result.exit_code == 2
    assert "no packages found" in result.output.lower()
    assert "autokernel build" in result.output  # the fix hint


def test_install_unsupported_bootloader_emits_clear_error(tmp_path: Path, monkeypatch):
    snap, deb = _seed_snapshot_with_deb(tmp_path)
    monkeypatch.setattr(
        cli,
        "detect_distro",
        lambda: parse_os_release("ID=ubuntu\nID_LIKE=debian\n"),
    )
    monkeypatch.setattr(
        cli.bootloader_mod,
        "detect",
        lambda: Bootloader(
            kind=BootloaderKind.SYSTEMD_BOOT, detected_via="/boot/loader"
        ),
    )
    result = runner.invoke(
        app,
        ["install", str(snap), "--package", str(deb), "--skip-preflight"],
    )
    assert result.exit_code == 4
    assert "systemd-boot" in result.output
    assert "GRUB2" in result.output  # fix hint mentions what IS supported


def test_install_execute_without_root_exits_5(
    tmp_path: Path, patched_env, captured_runs, monkeypatch
):
    """`--execute` without root should refuse and tell the user to sudo."""
    snap, deb = _seed_snapshot_with_deb(tmp_path)
    monkeypatch.setattr("os.geteuid", lambda: 1000)  # not root
    result = runner.invoke(
        app,
        [
            "install",
            str(snap),
            "--package",
            str(deb),
            "--skip-preflight",
            "--skip-boot-test",
            "--execute",
        ],
    )
    assert result.exit_code == 5
    assert "requires root" in result.output.lower()
    # Subprocess never invoked
    assert captured_runs == []


def test_install_execute_as_root_runs_steps(
    tmp_path: Path, patched_env, captured_runs, monkeypatch
):
    snap, deb = _seed_snapshot_with_deb(tmp_path)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    result = runner.invoke(
        app,
        [
            "install",
            str(snap),
            "--package",
            str(deb),
            "--skip-preflight",
            "--skip-boot-test",
            "--execute",
            "--kernel-entry",
            "Linux 6.13.5",
        ],
    )
    assert result.exit_code == 0, result.output
    # Should have called: capture_grub_state, apt install, update-grub or grub-mkconfig, grub-reboot
    assert len(captured_runs) >= 3
    # Record file written
    record_files = list((snap / "install").rglob("record.json"))
    assert len(record_files) == 1


def test_install_execute_blocks_without_boot_test_record(
    tmp_path: Path, patched_env, captured_runs, monkeypatch
):
    """Without --skip-boot-test, install --execute refuses to proceed
    when no boot-test record exists for this snapshot."""
    snap, deb = _seed_snapshot_with_deb(tmp_path)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    result = runner.invoke(
        app,
        [
            "install",
            str(snap),
            "--package",
            str(deb),
            "--skip-preflight",  # but NOT skip-boot-test
            "--execute",
        ],
    )
    assert result.exit_code == 1
    assert "boot-test" in result.output.lower()
    assert "skip-boot-test" in result.output
    assert captured_runs == []


def test_install_execute_passes_with_passing_boot_test_record(
    tmp_path: Path, patched_env, captured_runs, monkeypatch
):
    """Once a passing boot-test.json exists, install --execute proceeds."""
    snap, deb = _seed_snapshot_with_deb(tmp_path)
    # Synthesize a passing boot-test record.
    import json

    (snap / "boot-test.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "verdict_ok": True,
                "verdict_reason": "passed",
                "kernel_release": "6.13.5",
                "method": "qemu",
                "timestamp": "2026-05-08T12:00:00+00:00",
            }
        )
    )
    monkeypatch.setattr("os.geteuid", lambda: 0)
    result = runner.invoke(
        app,
        [
            "install",
            str(snap),
            "--package",
            str(deb),
            "--skip-preflight",
            "--execute",
            "--kernel-entry",
            "Linux 6.13.5",
        ],
    )
    assert result.exit_code == 0, result.output
    # Output should mention the boot-test record was found
    assert "boot-test on record" in result.output


def test_install_execute_refuses_with_failing_boot_test_record(
    tmp_path: Path, patched_env, captured_runs, monkeypatch
):
    """A FAILED boot-test record blocks install --execute."""
    snap, deb = _seed_snapshot_with_deb(tmp_path)
    import json

    (snap / "boot-test.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "verdict_ok": False,
                "verdict_reason": "kernel panic before VFS stage",
            }
        )
    )
    monkeypatch.setattr("os.geteuid", lambda: 0)
    result = runner.invoke(
        app,
        [
            "install",
            str(snap),
            "--package",
            str(deb),
            "--skip-preflight",
            "--execute",
        ],
    )
    assert result.exit_code == 1
    assert "failed" in result.output.lower()
    assert captured_runs == []


# ── install --commit ─────────────────────────────────────────────────────


def test_install_commit_dry_run(tmp_path: Path, patched_env, captured_runs):
    snap, _ = _seed_snapshot_with_deb(tmp_path)
    result = runner.invoke(
        app,
        ["install", str(snap), "--commit", "--kernel-entry", "Linux 6.13.5"],
    )
    assert result.exit_code == 0, result.output
    assert "grub-set-default" in result.output
    assert captured_runs == []


def test_install_commit_execute_requires_root(
    tmp_path: Path, patched_env, captured_runs, monkeypatch
):
    snap, _ = _seed_snapshot_with_deb(tmp_path)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    result = runner.invoke(
        app,
        [
            "install",
            str(snap),
            "--commit",
            "--execute",
            "--kernel-entry",
            "Linux 6.13.5",
        ],
    )
    assert result.exit_code == 5


# ── rollback ───────────────────────────────────────────────────────────────


def _seed_install_record(
    snap: Path, deb_name: str = "linux-image-6.13.5_amd64.deb"
) -> Path:
    log_dir = snap / "install" / "20260508T120000Z"
    log_dir.mkdir(parents=True)
    record_path = log_dir / "record.json"
    record_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "timestamp": "20260508T120000Z",
                "distro_id": "ubuntu",
                "bootloader_kind": "grub2",
                "package_paths": [str(snap / deb_name)],
                "steps": [],
                "ok": True,
            }
        )
    )
    return record_path


def test_rollback_dry_run_renders_plan(tmp_path: Path, patched_env, captured_runs):
    snap, _ = _seed_snapshot_with_deb(tmp_path)
    _seed_install_record(snap)
    result = runner.invoke(app, ["rollback", str(snap)])
    assert result.exit_code == 0, result.output
    assert "apt remove" in result.output
    assert captured_runs == []


def test_rollback_no_install_record_exits_2(tmp_path: Path, patched_env, captured_runs):
    snap, _ = _seed_snapshot_with_deb(tmp_path)
    result = runner.invoke(app, ["rollback", str(snap)])
    assert result.exit_code == 2
    assert "nothing to rollback" in result.output.lower()


def test_rollback_execute_without_root_exits_5(
    tmp_path: Path, patched_env, captured_runs, monkeypatch
):
    snap, _ = _seed_snapshot_with_deb(tmp_path)
    _seed_install_record(snap)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    result = runner.invoke(app, ["rollback", str(snap), "--execute"])
    assert result.exit_code == 5
    assert captured_runs == []


def test_rollback_execute_as_root_marks_record(
    tmp_path: Path, patched_env, captured_runs, monkeypatch
):
    snap, _ = _seed_snapshot_with_deb(tmp_path)
    record_path = _seed_install_record(snap)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    result = runner.invoke(app, ["rollback", str(snap), "--execute"])
    assert result.exit_code == 0, result.output
    assert len(captured_runs) >= 1
    record = json.loads(record_path.read_text())
    assert record.get("rolled_back") is True
