"""Tests for install plan construction + execution. Subprocess is mocked."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autokernel import install as install_mod
from autokernel.bootloader import Bootloader, BootloaderKind
from autokernel.distro import Family, parse_os_release, spec_for
from autokernel.install import build_commit_plan, build_plan, execute


# ── helpers ─────────────────────────────────────────────────────────────────


def _info(family: Family):
    return parse_os_release(
        {
            Family.DEBIAN: "ID=ubuntu\nID_LIKE=debian\n",
            Family.FEDORA: "ID=fedora\n",
            Family.ARCH: "ID=arch\n",
            Family.SUSE: "ID=opensuse-tumbleweed\n",
            Family.UNKNOWN: "ID=mystery\n",
        }[family]
    )


_DEB_GRUB = Bootloader(
    kind=BootloaderKind.GRUB2,
    detected_via="/boot/grub/grub.cfg",
    config_dir=Path("/boot/grub"),
    grub_tool_prefix="",
)

_FEDORA_GRUB = Bootloader(
    kind=BootloaderKind.GRUB2,
    detected_via="/boot/grub2/grub.cfg",
    config_dir=Path("/boot/grub2"),
    grub_tool_prefix="grub2-",
)

_SYSTEMD_BOOT = Bootloader(
    kind=BootloaderKind.SYSTEMD_BOOT,
    detected_via="/boot/loader/loader.conf",
)


# ── plan construction ──────────────────────────────────────────────────────


def test_debian_plan_builds_apt_install_step(tmp_path: Path):
    deb = tmp_path / "linux-image-6.13.5_amd64.deb"
    deb.write_text("")
    info = _info(Family.DEBIAN)
    plan = build_plan(
        distro=info,
        spec=spec_for(info),
        bootloader=_DEB_GRUB,
        package_paths=[deb],
        kernel_entry="Linux 6.13.5",
    )
    assert plan.is_valid
    step_names = [s.name for s in plan.steps]
    assert step_names == [
        "capture_grub_state",
        "install_package",
        "regenerate_bootloader",
        "arm_one_shot_boot",
    ]
    install_step = plan.steps[1]
    assert install_step.argv[:3] == ["apt", "install", "-y"]
    assert str(deb) in install_step.argv


def test_fedora_plan_uses_dnf_and_grub2_tools(tmp_path: Path):
    rpm = tmp_path / "kernel-6.13.5-100.fc41.x86_64.rpm"
    rpm.write_text("")
    info = _info(Family.FEDORA)
    plan = build_plan(
        distro=info,
        spec=spec_for(info),
        bootloader=_FEDORA_GRUB,
        package_paths=[rpm],
        kernel_entry="Fedora Linux 6.13.5",
    )
    assert plan.is_valid
    install = plan.steps[1]
    assert install.argv[0] == "dnf"
    regen = plan.steps[2]
    assert regen.argv[0] == "grub2-mkconfig"
    arm = plan.steps[3]
    assert arm.argv[0] == "grub2-reboot"


def test_arch_plan_uses_pacman_U(tmp_path: Path):
    pkg = tmp_path / "linux-6.13.5.pkg.tar.zst"
    pkg.write_text("")
    info = _info(Family.ARCH)
    plan = build_plan(
        distro=info,
        spec=spec_for(info),
        bootloader=_DEB_GRUB,  # Arch typically uses GRUB or systemd-boot; here GRUB
        package_paths=[pkg],
        kernel_entry="Arch 6.13.5",
    )
    assert plan.is_valid
    assert plan.steps[1].argv[0] == "pacman"


def test_unsupported_bootloader_returns_rejected_plan(tmp_path: Path):
    deb = tmp_path / "x.deb"
    deb.write_text("")
    info = _info(Family.DEBIAN)
    plan = build_plan(
        distro=info,
        spec=spec_for(info),
        bootloader=_SYSTEMD_BOOT,
        package_paths=[deb],
    )
    assert not plan.is_valid
    assert plan.rejected_reason
    assert "GRUB2" in plan.rejected_reason


def test_unknown_distro_returns_rejected_plan(tmp_path: Path):
    deb = tmp_path / "x.deb"
    deb.write_text("")
    info = _info(Family.UNKNOWN)
    plan = build_plan(
        distro=info,
        spec=spec_for(info),
        bootloader=_DEB_GRUB,
        package_paths=[deb],
    )
    assert not plan.is_valid
    assert plan.rejected_reason is not None
    assert "no install recipe" in plan.rejected_reason


def test_disabling_probation_omits_arm_step(tmp_path: Path):
    deb = tmp_path / "x.deb"
    deb.write_text("")
    info = _info(Family.DEBIAN)
    plan = build_plan(
        distro=info,
        spec=spec_for(info),
        bootloader=_DEB_GRUB,
        package_paths=[deb],
        kernel_entry="Linux 6.13",
        enable_probation=False,
    )
    assert plan.is_valid
    assert "arm_one_shot_boot" not in [s.name for s in plan.steps]


def test_no_kernel_entry_omits_arm_step(tmp_path: Path):
    """When the caller doesn't know the new kernel's GRUB menu entry yet,
    the arm step is skipped — the user can re-arm later."""
    deb = tmp_path / "x.deb"
    deb.write_text("")
    info = _info(Family.DEBIAN)
    plan = build_plan(
        distro=info,
        spec=spec_for(info),
        bootloader=_DEB_GRUB,
        package_paths=[deb],
        kernel_entry=None,
    )
    assert plan.is_valid
    assert "arm_one_shot_boot" not in [s.name for s in plan.steps]


def test_install_steps_have_descriptions(tmp_path: Path):
    """Every step must have a non-empty human-readable description so
    dry-run output is meaningful."""
    deb = tmp_path / "x.deb"
    deb.write_text("")
    info = _info(Family.DEBIAN)
    plan = build_plan(
        distro=info,
        spec=spec_for(info),
        bootloader=_DEB_GRUB,
        package_paths=[deb],
        kernel_entry="Linux 6.13",
    )
    for step in plan.steps:
        assert step.description.strip(), f"{step.name} has empty description"


# ── commit plan ─────────────────────────────────────────────────────────────


def test_commit_plan_sets_default():
    info = _info(Family.DEBIAN)
    plan = build_commit_plan(
        distro=info,
        bootloader=_DEB_GRUB,
        kernel_entry="Linux 6.13.5",
    )
    assert plan.is_valid
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.argv[0] == "grub-set-default"
    assert step.argv[1] == "Linux 6.13.5"


def test_commit_plan_fedora_uses_grub2_set_default():
    info = _info(Family.FEDORA)
    plan = build_commit_plan(
        distro=info,
        bootloader=_FEDORA_GRUB,
        kernel_entry="Fedora 6.13",
    )
    assert plan.steps[0].argv[0] == "grub2-set-default"


def test_commit_plan_unsupported_bootloader_rejected():
    info = _info(Family.DEBIAN)
    plan = build_commit_plan(
        distro=info,
        bootloader=_SYSTEMD_BOOT,
        kernel_entry="x",
    )
    assert not plan.is_valid


# ── execution ──────────────────────────────────────────────────────────────


@pytest.fixture
def captured_runs(monkeypatch):
    """Replace subprocess.run inside install module."""
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


def test_execute_walks_all_steps_and_writes_record(tmp_path: Path, captured_runs):
    deb = tmp_path / "x.deb"
    deb.write_text("")
    info = _info(Family.DEBIAN)
    plan = build_plan(
        distro=info,
        spec=spec_for(info),
        bootloader=_DEB_GRUB,
        package_paths=[deb],
        kernel_entry="Linux 6.13",
    )
    snap = tmp_path / "snap"
    snap.mkdir()

    result = execute(plan, snapshot_dir=snap)
    assert result.ok
    assert len(captured_runs) == len(plan.steps)
    # Record file written
    assert result.record_path.exists()
    record = json.loads(result.record_path.read_text())
    assert record["distro_id"] == "ubuntu"
    assert record["ok"] is True
    assert len(record["steps"]) == len(plan.steps)


def test_execute_stops_on_first_failure(tmp_path: Path, monkeypatch):
    deb = tmp_path / "x.deb"
    deb.write_text("")
    info = _info(Family.DEBIAN)
    plan = build_plan(
        distro=info,
        spec=spec_for(info),
        bootloader=_DEB_GRUB,
        package_paths=[deb],
        kernel_entry="Linux 6.13",
    )
    snap = tmp_path / "snap"
    snap.mkdir()

    call_count = [0]

    class _R:
        def __init__(self, rc):
            self.returncode = rc

    def _fake(argv, **kwargs):
        call_count[0] += 1
        for f in (kwargs.get("stdout"), kwargs.get("stderr")):
            if hasattr(f, "write"):
                f.write(b"")
        # Second step (install_package) fails
        return _R(0 if call_count[0] != 2 else 1)

    monkeypatch.setattr(install_mod.subprocess, "run", _fake)

    result = execute(plan, snapshot_dir=snap)
    assert not result.ok
    # Should have stopped after step 2 (the failure); step 3+ skipped.
    assert call_count[0] == 2
    assert len(result.step_runs) == 2


def test_execute_refuses_invalid_plan(tmp_path: Path, captured_runs):
    snap = tmp_path / "snap"
    snap.mkdir()
    info = _info(Family.DEBIAN)
    plan = build_plan(
        distro=info,
        spec=spec_for(info),
        bootloader=_SYSTEMD_BOOT,  # unsupported
        package_paths=[tmp_path / "x.deb"],
    )
    with pytest.raises(RuntimeError, match="invalid plan"):
        execute(plan, snapshot_dir=snap)
    # Nothing should have been called.
    assert captured_runs == []
