"""Tests for the rollback module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autokernel import install as install_mod
from autokernel import rollback as rollback_mod
from autokernel.bootloader import Bootloader, BootloaderKind
from autokernel.distro import Family, parse_os_release
from autokernel.rollback import (
    _package_name_from_path,
    build_plan,
    execute,
    find_latest_install_record,
)


def _info(family: Family):
    return parse_os_release({
        Family.DEBIAN: "ID=ubuntu\nID_LIKE=debian\n",
        Family.FEDORA: "ID=fedora\n",
        Family.UNKNOWN: "ID=mystery\n",
    }[family])


_DEB_GRUB = Bootloader(
    kind=BootloaderKind.GRUB2,
    detected_via="x",
    config_dir=Path("/boot/grub"),
    grub_tool_prefix="",
)


def _seed_install_record(
    snapshot_dir: Path,
    package_paths: list[str],
    *,
    rolled_back: bool = False,
    timestamp: str = "20260508T120000Z",
) -> Path:
    log_dir = snapshot_dir / "install" / timestamp
    log_dir.mkdir(parents=True)
    record = {
        "schema": 1,
        "timestamp": timestamp,
        "distro_id": "ubuntu",
        "bootloader_kind": "grub2",
        "package_paths": package_paths,
        "steps": [],
        "ok": True,
    }
    if rolled_back:
        record["rolled_back"] = True
    record_path = log_dir / "record.json"
    record_path.write_text(json.dumps(record))
    return record_path


# ── package name extraction ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("linux-image-6.13.5_amd64.deb", "linux-image-6.13.5"),
        ("kernel-6.13.5-100.fc41.x86_64.rpm", "kernel-6.13.5-100.fc41.x86_64"),
        ("linux-6.13.5.pkg.tar.zst", "linux-6.13.5"),
        ("linux-headers-6.13.5_amd64.deb", "linux-headers-6.13.5"),
    ],
)
def test_package_name_from_path(filename: str, expected: str):
    assert _package_name_from_path(Path("/tmp") / filename) == expected


# ── find_latest_install_record ─────────────────────────────────────────────


def test_find_latest_returns_none_when_no_installs(tmp_path: Path):
    assert find_latest_install_record(tmp_path) is None


def test_find_latest_returns_only_record(tmp_path: Path):
    _seed_install_record(tmp_path, ["/x.deb"])
    found = find_latest_install_record(tmp_path)
    assert found is not None
    assert found.name == "record.json"


def test_find_latest_picks_newest_by_dirname(tmp_path: Path):
    _seed_install_record(tmp_path, ["/old.deb"], timestamp="20260101T000000Z")
    new_path = _seed_install_record(tmp_path, ["/new.deb"], timestamp="20260601T000000Z")
    found = find_latest_install_record(tmp_path)
    assert found == new_path


def test_find_latest_skips_already_rolled_back(tmp_path: Path):
    """If the newest record is already marked rolled_back, walk to the
    next-newest non-rolled-back one."""
    _seed_install_record(tmp_path, ["/old.deb"], timestamp="20260101T000000Z")
    _seed_install_record(
        tmp_path,
        ["/recent-rolled-back.deb"],
        rolled_back=True,
        timestamp="20260601T000000Z",
    )
    found = find_latest_install_record(tmp_path)
    assert found is not None
    # Must be the older, non-rolled-back one
    assert "20260101" in str(found)


def test_find_latest_returns_none_when_all_rolled_back(tmp_path: Path):
    _seed_install_record(tmp_path, ["/x.deb"], rolled_back=True)
    assert find_latest_install_record(tmp_path) is None


def test_find_latest_skips_malformed_records(tmp_path: Path):
    log_dir = tmp_path / "install" / "20260101T000000Z"
    log_dir.mkdir(parents=True)
    (log_dir / "record.json").write_text("not json{{{")
    assert find_latest_install_record(tmp_path) is None


# ── build_plan ─────────────────────────────────────────────────────────────


def test_build_plan_returns_rejected_when_no_install(tmp_path: Path):
    plan = build_plan(
        snapshot_dir=tmp_path,
        distro=_info(Family.DEBIAN),
        bootloader=_DEB_GRUB,
    )
    assert not plan.is_valid
    assert "no install record" in plan.rejected_reason


def test_build_plan_returns_rejected_for_unsupported_bootloader(tmp_path: Path):
    _seed_install_record(tmp_path, ["/x.deb"])
    plan = build_plan(
        snapshot_dir=tmp_path,
        distro=_info(Family.DEBIAN),
        bootloader=Bootloader(kind=BootloaderKind.SYSTEMD_BOOT, detected_via="x"),
    )
    assert not plan.is_valid
    assert "GRUB2" in plan.rejected_reason


def test_build_plan_returns_rejected_for_unknown_distro(tmp_path: Path):
    _seed_install_record(tmp_path, ["/x.deb"])
    plan = build_plan(
        snapshot_dir=tmp_path,
        distro=_info(Family.UNKNOWN),
        bootloader=_DEB_GRUB,
    )
    assert not plan.is_valid


def test_build_plan_emits_remove_then_regenerate(tmp_path: Path):
    _seed_install_record(tmp_path, ["/path/linux-image-6.13.5_amd64.deb"])
    plan = build_plan(
        snapshot_dir=tmp_path,
        distro=_info(Family.DEBIAN),
        bootloader=_DEB_GRUB,
    )
    assert plan.is_valid
    names = [s.name for s in plan.steps]
    assert names == ["remove_package", "regenerate_bootloader"]
    assert plan.steps[0].argv[:3] == ["apt", "remove", "-y"]
    assert "linux-image-6.13.5" in plan.steps[0].argv


def test_build_plan_fedora_uses_dnf_remove(tmp_path: Path):
    _seed_install_record(tmp_path, ["/path/kernel-6.13.5-100.fc41.x86_64.rpm"])
    fedora_grub = Bootloader(
        kind=BootloaderKind.GRUB2,
        detected_via="x",
        config_dir=Path("/boot/grub2"),
        grub_tool_prefix="grub2-",
    )
    plan = build_plan(
        snapshot_dir=tmp_path,
        distro=_info(Family.FEDORA),
        bootloader=fedora_grub,
    )
    assert plan.is_valid
    assert plan.steps[0].argv[0] == "dnf"
    assert plan.steps[1].argv[0] == "grub2-mkconfig"


# ── execute ────────────────────────────────────────────────────────────────


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


def test_execute_walks_steps_and_marks_record_rolled_back(tmp_path: Path, captured_runs):
    record_path = _seed_install_record(tmp_path, ["/path/linux-image-6.13.5_amd64.deb"])
    plan = build_plan(
        snapshot_dir=tmp_path,
        distro=_info(Family.DEBIAN),
        bootloader=_DEB_GRUB,
    )
    result = execute(plan, snapshot_dir=tmp_path)
    assert result.ok
    assert len(captured_runs) == len(plan.steps)
    # The install record should now be marked rolled_back.
    record = json.loads(record_path.read_text())
    assert record.get("rolled_back") is True
    assert "rolled_back_at" in record


def test_execute_does_not_mark_when_step_fails(tmp_path: Path, monkeypatch):
    record_path = _seed_install_record(tmp_path, ["/path/linux-image-6.13.5_amd64.deb"])
    plan = build_plan(
        snapshot_dir=tmp_path,
        distro=_info(Family.DEBIAN),
        bootloader=_DEB_GRUB,
    )

    class _R:
        def __init__(self, rc):
            self.returncode = rc

    def _fail(argv, **kwargs):
        for f in (kwargs.get("stdout"), kwargs.get("stderr")):
            if hasattr(f, "write"):
                f.write(b"")
        return _R(1)

    monkeypatch.setattr(install_mod.subprocess, "run", _fail)

    result = execute(plan, snapshot_dir=tmp_path)
    assert not result.ok
    record = json.loads(record_path.read_text())
    assert record.get("rolled_back") is None  # never set


def test_execute_refuses_invalid_plan(tmp_path: Path, captured_runs):
    plan = build_plan(
        snapshot_dir=tmp_path,
        distro=_info(Family.DEBIAN),
        bootloader=_DEB_GRUB,
    )
    assert not plan.is_valid
    with pytest.raises(RuntimeError):
        execute(plan, snapshot_dir=tmp_path)
    assert captured_runs == []


def test_idempotent_after_successful_rollback(tmp_path: Path, captured_runs):
    """A second build_plan call after a successful execute returns rejected
    (no remaining records to rollback)."""
    _seed_install_record(tmp_path, ["/x.deb"])
    plan1 = build_plan(
        snapshot_dir=tmp_path,
        distro=_info(Family.DEBIAN),
        bootloader=_DEB_GRUB,
    )
    execute(plan1, snapshot_dir=tmp_path)

    plan2 = build_plan(
        snapshot_dir=tmp_path,
        distro=_info(Family.DEBIAN),
        bootloader=_DEB_GRUB,
    )
    assert not plan2.is_valid
    assert "no install record" in plan2.rejected_reason
