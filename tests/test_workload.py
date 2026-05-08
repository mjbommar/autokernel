"""Tests for autokernel.workload.

Workload detection is pure-evidence: synthetic Snapshot + synthetic
``/sys`` filesystem layout → expected classification. Each classifier
gets coverage for both positive and negative paths; resolution-order
tests prove vm-guest wins over laptop, etc.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from autokernel.models import (
    BootContext,
    CpuInfo,
    KernelInfo,
    PciDevice,
    Snapshot,
)
from autokernel.workload import (
    WorkloadDetection,
    WorkloadProfile,
    detect,
)


def _bare_snap(
    *,
    vendor_id: str = "GenuineIntel",
    cores: int = 8,
    flags: list[str] | None = None,
    arch: str = "x86_64",
    pci: list[PciDevice] | None = None,
) -> Snapshot:
    return Snapshot(
        collected_at=datetime.now(timezone.utc),
        host="testhost",
        snapshot_dir=Path("/tmp/snap"),
        kernel=KernelInfo(release="6.19.0-test", version="#1 SMP", arch=arch),
        cpu=CpuInfo(
            vendor_id=vendor_id, cpu_family=6, model=170, cores=cores,
            flags=flags or [],
        ),
        boot=BootContext(cmdline="ro quiet"),
        pci=pci or [],
    )


def _make_sys(tmp_path: Path, **files: str) -> Path:
    """Build a fake /sys tree. Keys are relative paths; values are
    contents. Directories are created automatically."""
    root = tmp_path / "sys"
    root.mkdir()
    for relpath, content in files.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root


# ── explicit override ────────────────────────────────────────────────────


def test_detect_explicit_override_returns_user_choice(tmp_path):
    snap = _bare_snap()
    sys_root = _make_sys(tmp_path)
    res = detect(snap, sys_root=sys_root, explicit=WorkloadProfile.SERVER)
    assert res.profile == WorkloadProfile.SERVER
    assert res.confidence == 1.0
    assert any("user-supplied" in r for r in res.reasons)


# ── vm-guest classification (highest priority) ────────────────────────────


def test_detect_vm_guest_via_hypervisor_cpu_flag(tmp_path):
    snap = _bare_snap(flags=["fpu", "vme", "hypervisor"])
    sys_root = _make_sys(tmp_path)
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.VM_GUEST
    assert any("hypervisor" in r for r in res.reasons)


def test_detect_vm_guest_via_dmi_sys_vendor(tmp_path):
    snap = _bare_snap()
    sys_root = _make_sys(tmp_path, **{
        "class/dmi/id/sys_vendor": "QEMU\n",
    })
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.VM_GUEST


def test_detect_vm_guest_higher_confidence_with_two_signals(tmp_path):
    snap = _bare_snap(flags=["hypervisor"])
    sys_root = _make_sys(tmp_path, **{
        "class/dmi/id/sys_vendor": "Amazon EC2\n",
    })
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.VM_GUEST
    assert res.confidence >= 0.9


def test_detect_vm_guest_amazon_ec2_partial_match(tmp_path):
    snap = _bare_snap()
    sys_root = _make_sys(tmp_path, **{
        "class/dmi/id/sys_vendor": "Amazon EC2\n",
    })
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.VM_GUEST


def test_detect_vm_guest_wins_over_laptop_chassis(tmp_path):
    """Cloud images can claim portable chassis. VM-guest must win."""
    snap = _bare_snap(flags=["hypervisor"])
    sys_root = _make_sys(tmp_path, **{
        "class/dmi/id/chassis_type": "10\n",  # Notebook
        "class/dmi/id/sys_vendor": "QEMU\n",
    })
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.VM_GUEST


# ── embedded classification ─────────────────────────────────────────────


def test_detect_embedded_via_devicetree_and_arch(tmp_path):
    snap = _bare_snap(arch="aarch64")
    sys_root = _make_sys(tmp_path, **{
        "firmware/devicetree/base/.placeholder": "",
    })
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.EMBEDDED


def test_detect_arm_only_arch_is_embedded_signal(tmp_path):
    snap = _bare_snap(arch="armv7l")
    sys_root = _make_sys(tmp_path)
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.EMBEDDED


# ── laptop classification ───────────────────────────────────────────────


def test_detect_laptop_via_chassis_type(tmp_path):
    snap = _bare_snap()
    sys_root = _make_sys(tmp_path, **{
        "class/dmi/id/chassis_type": "10\n",  # Notebook
    })
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.LAPTOP


def test_detect_laptop_via_battery(tmp_path):
    snap = _bare_snap()
    root = _make_sys(tmp_path)
    (root / "class/power_supply/BAT0").mkdir(parents=True)
    res = detect(snap, sys_root=root)
    assert res.profile == WorkloadProfile.LAPTOP


def test_detect_laptop_high_confidence_with_both_signals(tmp_path):
    snap = _bare_snap()
    root = _make_sys(tmp_path, **{
        "class/dmi/id/chassis_type": "9\n",  # Laptop
    })
    (root / "class/power_supply/BAT0").mkdir(parents=True)
    res = detect(snap, sys_root=root)
    assert res.profile == WorkloadProfile.LAPTOP
    assert res.confidence >= 0.9


def test_detect_laptop_chassis_31_convertible(tmp_path):
    snap = _bare_snap()
    sys_root = _make_sys(tmp_path, **{
        "class/dmi/id/chassis_type": "31\n",  # Convertible
    })
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.LAPTOP


# ── server classification ───────────────────────────────────────────────


def test_detect_server_via_chassis_type(tmp_path):
    snap = _bare_snap(cores=64)
    sys_root = _make_sys(tmp_path, **{
        "class/dmi/id/chassis_type": "23\n",  # Rack Mount
    })
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.SERVER


def test_detect_server_via_product_family(tmp_path):
    snap = _bare_snap(cores=32)
    sys_root = _make_sys(tmp_path, **{
        "class/dmi/id/product_family": "PowerEdge R740\n",
    })
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.SERVER


def test_detect_server_via_headless_big_iron_heuristic(tmp_path):
    snap = _bare_snap(cores=32)  # no GPU, no battery
    sys_root = _make_sys(tmp_path)
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.SERVER


def test_detect_server_does_not_fire_with_only_8_cores(tmp_path):
    """Headless heuristic requires >=16 cores."""
    snap = _bare_snap(cores=8)
    sys_root = _make_sys(tmp_path)
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.DESKTOP


# ── desktop fallback ────────────────────────────────────────────────────


def test_detect_desktop_fallback(tmp_path):
    snap = _bare_snap(cores=8, pci=[
        PciDevice(slot="00:02.0", vendor_id="8086", device_id="56a6", class_id="0300"),
    ])
    sys_root = _make_sys(tmp_path)
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.DESKTOP


def test_detect_desktop_via_explicit_chassis_type(tmp_path):
    snap = _bare_snap(cores=8, pci=[
        PciDevice(slot="00:02.0", vendor_id="8086", device_id="56a6", class_id="0300"),
    ])
    sys_root = _make_sys(tmp_path, **{
        "class/dmi/id/chassis_type": "3\n",  # Desktop
    })
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.DESKTOP
    assert res.confidence > 0.8  # higher confidence with explicit chassis


# ── unknown handling ────────────────────────────────────────────────────


def test_detect_unknown_when_snap_lacks_cpu_info(tmp_path):
    snap = Snapshot(
        collected_at=datetime.now(timezone.utc),
        host="empty",
        snapshot_dir=Path("/tmp/snap"),
        kernel=KernelInfo(release="6.19.0", version="#1 SMP", arch="x86_64"),
        cpu=CpuInfo(vendor_id=""),  # empty vendor
        boot=BootContext(cmdline=""),
    )
    sys_root = _make_sys(tmp_path)
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.UNKNOWN


# ── resolution order ────────────────────────────────────────────────────


def test_resolution_order_embedded_beats_laptop(tmp_path):
    """A device-tree-booted ARM laptop is still embedded (Pinebook,
    etc.) — embedded classifier sits ahead of laptop intentionally."""
    snap = _bare_snap(arch="aarch64")
    root = _make_sys(tmp_path, **{
        "firmware/devicetree/base/.placeholder": "",
        "class/dmi/id/chassis_type": "10\n",
    })
    (root / "class/power_supply/BAT0").mkdir(parents=True)
    res = detect(snap, sys_root=root)
    assert res.profile == WorkloadProfile.EMBEDDED


def test_resolution_order_vm_beats_server_chassis(tmp_path):
    snap = _bare_snap(cores=32, flags=["hypervisor"])
    sys_root = _make_sys(tmp_path, **{
        "class/dmi/id/chassis_type": "23\n",
    })
    res = detect(snap, sys_root=sys_root)
    assert res.profile == WorkloadProfile.VM_GUEST


# ── unreadable /sys (permissions / containerized) ────────────────────────


def test_detect_handles_missing_sys_root(tmp_path):
    """When /sys probes return None across the board, fall back gracefully."""
    snap = _bare_snap(cores=4)
    sys_root = tmp_path / "nonexistent"
    res = detect(snap, sys_root=sys_root)
    assert res.profile in (WorkloadProfile.DESKTOP, WorkloadProfile.UNKNOWN)
