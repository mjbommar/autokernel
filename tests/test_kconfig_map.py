"""Tests for the module → CONFIG candidate generator.

These are the highest-leverage correctness tests in the suite: a regression
here is a brick-the-box vector.
"""

from __future__ import annotations

import pytest

from autokernel.kconfig_map import candidate_configs, resolve_module_to_config


# ── candidate_configs: ordered, contains expected symbol ─────────────────────


@pytest.mark.parametrize(
    "name,path,expected",
    [
        # GPU drivers — DRM_ prefix, name suffix
        ("i915", "drivers/gpu/drm/i915/i915", "CONFIG_DRM_I915"),
        ("nouveau", "drivers/gpu/drm/nouveau/nouveau", "CONFIG_DRM_NOUVEAU"),
        ("amdgpu", "drivers/gpu/drm/amd/amdgpu/amdgpu", "CONFIG_DRM_AMDGPU"),
        # USB
        ("xhci_pci", "drivers/usb/host/xhci-pci", "CONFIG_USB_XHCI_PCI"),
        ("usb-storage", "drivers/usb/storage/usb-storage", "CONFIG_USB_STORAGE"),
        ("dwc3", "drivers/usb/dwc3/dwc3", "CONFIG_USB_DWC3"),
        # NVMe — special-cased to BLK_DEV_NVME
        ("nvme", "drivers/nvme/host/nvme", "CONFIG_BLK_DEV_NVME"),
        # Wireless — direct names
        ("iwlwifi", "drivers/net/wireless/intel/iwlwifi/iwlwifi", "CONFIG_IWLWIFI"),
        # Ethernet — direct
        ("r8169", "drivers/net/ethernet/realtek/r8169", "CONFIG_R8169"),
        # Sound
        ("snd_sof_pci_intel_mtl", "sound/soc/sof/intel/snd-sof-pci-intel-mtl", "CONFIG_SND_SOC_SOF_INTEL_SND_SOF_PCI_INTEL_MTL"),
        # Filesystems — fsdir-derived name
        ("ext4", "fs/ext4/ext4", "CONFIG_EXT4_FS"),
        ("btrfs", "fs/btrfs/btrfs", "CONFIG_BTRFS_FS"),
        # Crypto
        ("aes_generic", "crypto/aes_generic", "CONFIG_CRYPTO_AES_GENERIC"),
        # Bluetooth
        ("bluetooth", "net/bluetooth/bluetooth", "CONFIG_BT"),
        # Arch X86
        ("kvm", "arch/x86/kvm/kvm", "CONFIG_KVM"),
    ],
)
def test_candidate_includes_expected(name: str, path: str, expected: str):
    cands = candidate_configs(name, path)
    assert expected in cands, f"expected {expected} in {cands}"


def test_naive_only_when_no_path():
    cands = candidate_configs("foo_bar", None)
    assert cands[0] == "CONFIG_FOO_BAR"


def test_candidates_are_deduped():
    cands = candidate_configs("foo", "drivers/usb/foo/foo")
    assert len(cands) == len(set(cands))


def test_path_specificity_wins():
    """A `sound/soc/sof/intel/` path should not produce candidates derived
    from `sound/soc/` first."""
    cands = candidate_configs("snd_sof_pci_intel_mtl", "sound/soc/sof/intel/snd-sof-pci-intel-mtl")
    # The first candidate must come from the SOF_INTEL prefix, not bare SND_SOC.
    assert cands[0].startswith("CONFIG_SND_SOC_SOF_INTEL_")


# ── resolve_module_to_config picks first matching candidate ──────────────────


def test_resolve_picks_path_candidate_when_present():
    running = {"CONFIG_DRM_I915": "m"}
    assert resolve_module_to_config("i915", "drivers/gpu/drm/i915/i915", running) == "CONFIG_DRM_I915"


def test_resolve_returns_none_when_no_match():
    running = {"CONFIG_UNRELATED": "y"}
    assert resolve_module_to_config("i915", "drivers/gpu/drm/i915/i915", running) is None


def test_resolve_skips_disabled_symbols():
    """Even if CONFIG_DRM_I915 exists, =n means 'not enabled' — skip it."""
    running = {"CONFIG_DRM_I915": "n", "CONFIG_I915": "m"}
    # Only CONFIG_I915=m is enabled (a bare-name fallback). That should win.
    assert resolve_module_to_config("i915", "drivers/gpu/drm/i915/i915", running) == "CONFIG_I915"


def test_resolve_handles_unknown_path():
    """A path not in PATH_TABLE falls back to bare name."""
    running = {"CONFIG_FOO_BAR": "m"}
    assert resolve_module_to_config("foo_bar", "drivers/exotic/foo_bar", running) == "CONFIG_FOO_BAR"


def test_resolve_filesystem_with_fsdir():
    running = {"CONFIG_EXT4_FS": "y"}
    assert resolve_module_to_config("ext4", "fs/ext4/ext4", running) == "CONFIG_EXT4_FS"


def test_resolve_special_case_9p():
    running = {"CONFIG_9P_FS": "m"}
    assert resolve_module_to_config("9p", "fs/9p/9p", running) == "CONFIG_9P_FS"


def test_resolve_special_case_cifs():
    running = {"CONFIG_CIFS": "m"}
    assert resolve_module_to_config("cifs", "fs/cifs/cifs", running) == "CONFIG_CIFS"
