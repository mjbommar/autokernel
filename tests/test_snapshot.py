"""Snapshot parsing — exercised against synthetic fixtures, not the host."""

from __future__ import annotations

from autokernel.models import Snapshot


def test_intel_laptop_basic_shape(intel_laptop: Snapshot):
    assert intel_laptop.host == "intel-laptop-fixture"
    assert intel_laptop.kernel.release == "6.13.0-12-generic"
    assert intel_laptop.kernel.arch == "x86_64"
    assert intel_laptop.cpu.vendor_id == "GenuineIntel"
    assert intel_laptop.cpu.cpu_family == 6
    assert intel_laptop.cpu.model == 170
    assert intel_laptop.cpu.cores == 4
    assert "avx2" in intel_laptop.cpu.flags


def test_intel_laptop_boot_context(intel_laptop: Snapshot):
    b = intel_laptop.boot
    assert b.efi is True
    assert b.secure_boot is True
    assert b.luks_in_chain is True
    assert b.root_fstype == "btrfs"
    assert b.boot_fstype == "ext4"
    assert "BOOT_IMAGE" in b.cmdline


def test_amd_desktop_boot_context(amd_desktop: Snapshot):
    b = amd_desktop.boot
    assert b.efi is True
    assert b.secure_boot is False
    assert b.luks_in_chain is False
    assert b.root_fstype == "ext4"


def test_pci_class_and_driver_parsing(intel_laptop: Snapshot):
    by_slot = {p.slot: p for p in intel_laptop.pci}
    igpu = by_slot["00:02.0"]
    assert igpu.vendor_id == "8086"
    assert igpu.class_id == "0300"
    assert igpu.driver == "i915"
    assert "i915" in igpu.modules

    nvme = by_slot["01:00.0"]
    assert nvme.driver == "nvme"
    assert nvme.class_id == "0108"


def test_amd_desktop_pci_has_nvidia_dgpu(amd_desktop: Snapshot):
    nvidia = next(
        p
        for p in amd_desktop.pci
        if p.vendor_id == "10de" and (p.class_id or "").startswith("03")
    )
    assert nvidia.driver == "nvidia"


def test_loaded_modules_parsed_with_used_by(intel_laptop: Snapshot):
    by_name = {m.name: m for m in intel_laptop.loaded_modules}
    iwlmvm = by_name["iwlmvm"]
    assert iwlmvm.used_by_count == 0
    cfg80211 = by_name["cfg80211"]
    assert cfg80211.used_by_count == 3
    assert "iwlwifi" in cfg80211.used_by


def test_dkms_parsed_when_present(amd_desktop: Snapshot):
    assert len(amd_desktop.dkms) == 1
    nv = amd_desktop.dkms[0]
    assert nv.name == "nvidia"
    assert nv.version == "550.120"
    assert nv.kernel == "6.13.0-12-generic"
    assert nv.status == "installed"


def test_dkms_empty_when_absent(intel_laptop: Snapshot):
    assert intel_laptop.dkms == []


def test_active_network_iface_detected(intel_laptop: Snapshot):
    wifi = next(n for n in intel_laptop.network if n.name == "wlp0s20f3")
    assert wifi.is_active is True
    assert wifi.driver == "iwlwifi"
    lo = next(n for n in intel_laptop.network if n.name == "lo")
    assert lo.is_active is False


def test_modaliases_bus_classified(intel_laptop: Snapshot):
    pci_only = [m for m in intel_laptop.modaliases if m.bus == "pci"]
    assert len(pci_only) >= 5
    serio_present = any(m.bus == "serio" for m in intel_laptop.modaliases)
    assert serio_present


def test_running_config_path_set(intel_laptop: Snapshot):
    assert intel_laptop.running_config_path is not None
    assert intel_laptop.running_config_path.exists()
