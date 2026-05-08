"""Tests for the boot-test module. QEMU/virtme subprocess calls are mocked."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from autokernel import boottest as boottest_mod
from autokernel.boottest import (
    BootTestPlan,
    Method,
    Verdict,
    analyze_serial,
    detect_method,
    execute,
    find_bzimage,
    plan,
    read_latest_record,
)


# ── method detection ───────────────────────────────────────────────────────


def test_detect_method_prefers_virtme(monkeypatch):
    monkeypatch.setattr(boottest_mod.shutil, "which", lambda c: f"/usr/bin/{c}")
    assert detect_method() == Method.VIRTME


def test_detect_method_falls_back_to_qemu(monkeypatch):
    def _which(c):
        if c.startswith("virtme"):
            return None
        if c == "qemu-system-x86_64":
            return "/usr/bin/qemu-system-x86_64"
        return None
    monkeypatch.setattr(boottest_mod.shutil, "which", _which)
    assert detect_method() == Method.QEMU


def test_detect_method_returns_auto_when_nothing_available(monkeypatch):
    monkeypatch.setattr(boottest_mod.shutil, "which", lambda c: None)
    assert detect_method() == Method.AUTO


# ── plan builders ──────────────────────────────────────────────────────────


def test_qemu_plan_includes_required_flags(tmp_path: Path):
    bz = tmp_path / "bzImage"
    bz.write_bytes(b"x")
    p = plan(method=Method.QEMU, bzimage_path=bz, kernel_release="6.13.0")
    assert p.method == Method.QEMU
    assert "qemu-system-x86_64" == p.argv[0]
    assert "-kernel" in p.argv and str(bz) in p.argv
    assert "-no-reboot" in p.argv
    # panic=1 in cmdline so kernel reboots immediately on panic and QEMU exits
    append_idx = p.argv.index("-append")
    assert "panic=1" in p.argv[append_idx + 1]


def test_virtme_plan_uses_virtme_ng_when_available(tmp_path: Path, monkeypatch):
    bz = tmp_path / "bzImage"
    bz.write_bytes(b"x")
    monkeypatch.setattr(boottest_mod.shutil, "which", lambda c: "/usr/bin/virtme-ng")
    p = plan(method=Method.VIRTME, bzimage_path=bz, kernel_release="6.13.0")
    assert p.argv[0] == "virtme-ng"
    assert "--kimg" in p.argv
    assert "AUTOKERNEL_BOOT_TEST_OK" in " ".join(p.argv)


def test_virtme_plan_falls_back_to_virtme_run(tmp_path: Path, monkeypatch):
    bz = tmp_path / "bzImage"
    bz.write_bytes(b"x")
    monkeypatch.setattr(
        boottest_mod.shutil,
        "which",
        lambda c: None if c == "virtme-ng" else f"/usr/bin/{c}",
    )
    p = plan(method=Method.VIRTME, bzimage_path=bz, kernel_release="6.13.0")
    assert p.argv[0] == "virtme-run"


def test_auto_method_resolves_at_plan_time(tmp_path: Path, monkeypatch):
    bz = tmp_path / "bzImage"
    bz.write_bytes(b"x")
    monkeypatch.setattr(boottest_mod.shutil, "which", lambda c: "/usr/bin/qemu-system-x86_64" if c == "qemu-system-x86_64" else None)
    p = plan(method=Method.AUTO, bzimage_path=bz, kernel_release="6.13.0")
    assert p.method == Method.QEMU


def test_auto_method_raises_when_nothing_available(tmp_path: Path, monkeypatch):
    bz = tmp_path / "bzImage"
    bz.write_bytes(b"x")
    monkeypatch.setattr(boottest_mod.shutil, "which", lambda c: None)
    with pytest.raises(RuntimeError, match="no boot-test runtime"):
        plan(method=Method.AUTO, bzimage_path=bz, kernel_release="6.13.0")


# ── analyze_serial ─────────────────────────────────────────────────────────


# A typical successful QEMU kernel-only run looks like this:
_QEMU_PASS_LOG = """
[    0.000000] Linux version 6.13.0-12-generic (root@host) (gcc 14.2)
[    0.000123] Command line: console=ttyS0 panic=1 quiet
[    0.234567] x86: Booting SMP configuration: 1 cores
[    1.234567] EXT4-fs (sda1): mounted filesystem
[    1.500000] VFS: Cannot open root device "(null)" or unknown-block(0,0): error -6
[    1.500001] VFS: Unable to mount root fs on unknown-block(0,0)
[    1.500002] Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)
[    1.500003] CPU: 0 PID: 1 Comm: swapper/0 Not tainted 6.13.0
"""


_QEMU_EARLY_PANIC_LOG = """
[    0.000000] Linux version 6.13.0-12-generic
[    0.001000] something something
[    0.002000] BUG: kernel NULL pointer dereference, address: 0000000000000000
[    0.002001] Kernel panic - not syncing: Fatal exception in interrupt
"""


_QEMU_NO_BOOT_LOG = """
SeaBIOS (version 1.0)
Booting from ROM...
"""


def test_analyze_qemu_pass_via_vfs_panic():
    v = analyze_serial(_QEMU_PASS_LOG, method=Method.QEMU)
    assert v.ok is True
    assert "VFS" in v.reason


def test_analyze_qemu_fail_on_early_panic():
    v = analyze_serial(_QEMU_EARLY_PANIC_LOG, method=Method.QEMU)
    assert v.ok is False
    assert "panic" in v.reason.lower()


def test_analyze_qemu_fail_when_kernel_didnt_boot():
    v = analyze_serial(_QEMU_NO_BOOT_LOG, method=Method.QEMU)
    assert v.ok is False
    assert "banner" in v.reason.lower()


def test_analyze_qemu_pass_when_no_panic_at_all():
    """If the kernel kept going somehow without panicking, that's still
    a pass — at least it didn't crash."""
    log = "Linux version 6.13.0\n[boot continues forever, no panic]\n"
    v = analyze_serial(log, method=Method.QEMU)
    assert v.ok is True


def test_analyze_qemu_uses_cannot_open_root_alternate():
    """Some kernels print 'Cannot open root device' before the canonical
    'VFS: Unable to mount root fs' line. Either is success."""
    log = (
        "Linux version 6.13\n"
        "Cannot open root device \"(null)\" or unknown-block(0,0)\n"
        "Kernel panic - not syncing: VFS\n"
    )
    v = analyze_serial(log, method=Method.QEMU)
    assert v.ok is True


def test_analyze_virtme_pass_on_sentinel():
    log = "Linux version 6.13\nfoo\nAUTOKERNEL_BOOT_TEST_OK\n"
    v = analyze_serial(log, method=Method.VIRTME)
    assert v.ok is True


def test_analyze_virtme_fail_on_panic():
    log = "Linux version 6.13\nKernel panic - not syncing: oops\n"
    v = analyze_serial(log, method=Method.VIRTME)
    assert v.ok is False
    assert "panic" in v.reason.lower()


def test_analyze_virtme_fail_on_no_sentinel():
    log = "Linux version 6.13\nbooted ok but no sentinel\n"
    v = analyze_serial(log, method=Method.VIRTME)
    assert v.ok is False


# ── execute ────────────────────────────────────────────────────────────────


@pytest.fixture
def captured_runs(monkeypatch):
    calls: list[dict[str, Any]] = []

    class _R:
        def __init__(self, rc=0):
            self.returncode = rc

    def _fake(argv, **kwargs):
        # Pretend QEMU printed a successful boot trace.
        f = kwargs.get("stdout")
        if hasattr(f, "write"):
            f.write(_QEMU_PASS_LOG.encode())
        calls.append({"argv": list(argv), **kwargs})
        return _R(0)

    monkeypatch.setattr(boottest_mod.subprocess, "run", _fake)
    return calls


def test_execute_writes_result_and_record(tmp_path: Path, captured_runs, monkeypatch):
    bz = tmp_path / "bzImage"
    bz.write_bytes(b"fake-kernel-bytes")
    monkeypatch.setattr(boottest_mod.shutil, "which", lambda c: "/usr/bin/qemu-system-x86_64" if c == "qemu-system-x86_64" else None)
    p = plan(method=Method.AUTO, bzimage_path=bz, kernel_release="6.13.0")
    snap = tmp_path / "snap"
    snap.mkdir()

    result = execute(p, snapshot_dir=snap)
    assert result.verdict.ok is True
    assert (snap / "boot-test.json").exists()
    record = json.loads((snap / "boot-test.json").read_text())
    assert record["verdict_ok"] is True
    assert record["bzimage_sha256"]
    # Same hash as our fake bzImage
    import hashlib
    assert record["bzimage_sha256"] == hashlib.sha256(b"fake-kernel-bytes").hexdigest()
    # serial log written
    assert Path(record["serial_log"]).exists()


def test_execute_propagates_failure_verdict(tmp_path: Path, monkeypatch):
    bz = tmp_path / "bzImage"
    bz.write_bytes(b"x")

    def _fake(argv, **kwargs):
        f = kwargs.get("stdout")
        if hasattr(f, "write"):
            f.write(_QEMU_EARLY_PANIC_LOG.encode())
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(boottest_mod.subprocess, "run", _fake)
    monkeypatch.setattr(boottest_mod.shutil, "which", lambda c: "/usr/bin/qemu-system-x86_64" if c == "qemu-system-x86_64" else None)

    p = plan(method=Method.AUTO, bzimage_path=bz, kernel_release="6.13.0")
    snap = tmp_path / "snap"
    snap.mkdir()
    result = execute(p, snapshot_dir=snap)
    assert result.verdict.ok is False


def test_execute_handles_timeout_gracefully(tmp_path: Path, monkeypatch):
    bz = tmp_path / "bzImage"
    bz.write_bytes(b"x")
    monkeypatch.setattr(boottest_mod.shutil, "which", lambda c: "/usr/bin/qemu-system-x86_64" if c == "qemu-system-x86_64" else None)

    def _fake(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 60))

    monkeypatch.setattr(boottest_mod.subprocess, "run", _fake)

    p = plan(method=Method.QEMU, bzimage_path=bz, kernel_release="6.13.0")
    snap = tmp_path / "snap"
    snap.mkdir()
    result = execute(p, snapshot_dir=snap)
    assert result.verdict.ok is False
    assert "timed out" in result.verdict.reason.lower()


def test_execute_missing_bzimage_raises(tmp_path: Path):
    p = BootTestPlan(
        method=Method.QEMU,
        bzimage_path=tmp_path / "nope",
        kernel_release="6.13.0",
        argv=["true"],
        timeout=5.0,
        description="x",
    )
    snap = tmp_path / "snap"
    snap.mkdir()
    with pytest.raises(FileNotFoundError):
        execute(p, snapshot_dir=snap)


# ── record reading ─────────────────────────────────────────────────────────


def test_read_latest_record_returns_none_when_missing(tmp_path: Path):
    assert read_latest_record(tmp_path) is None


def test_read_latest_record_returns_parsed_json(tmp_path: Path):
    (tmp_path / "boot-test.json").write_text(
        json.dumps({"verdict_ok": True, "bzimage_sha256": "abc"}),
    )
    record = read_latest_record(tmp_path)
    assert record is not None
    assert record["verdict_ok"] is True


def test_read_latest_record_returns_none_on_malformed(tmp_path: Path):
    (tmp_path / "boot-test.json").write_text("not json{{{")
    assert read_latest_record(tmp_path) is None


# ── find_bzimage ───────────────────────────────────────────────────────────


def test_find_bzimage_under_x86_boot(tmp_path: Path):
    bz = tmp_path / "arch" / "x86" / "boot" / "bzImage"
    bz.parent.mkdir(parents=True)
    bz.write_text("x")
    assert find_bzimage(tmp_path) == bz


def test_find_bzimage_falls_back_to_vmlinux(tmp_path: Path):
    vm = tmp_path / "vmlinux"
    vm.write_text("x")
    assert find_bzimage(tmp_path) == vm


def test_find_bzimage_returns_none_when_nothing(tmp_path: Path):
    assert find_bzimage(tmp_path) is None
