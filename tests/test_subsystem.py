"""Tests for the CONFIG_* → subsystem classifier."""

from __future__ import annotations

import pytest

from autokernel.subsystem import classify, group_by_subsystem


@pytest.mark.parametrize(
    "symbol,expected",
    [
        # arch
        ("CONFIG_X86_64", "arch"),
        ("CONFIG_64BIT", "arch"),
        ("CONFIG_SMP", "arch"),
        ("CONFIG_ARM64", "arch"),
        # core
        ("CONFIG_PRINTK", "core"),
        ("CONFIG_TTY", "core"),
        ("CONFIG_DEVTMPFS", "core"),
        ("CONFIG_FUTEX", "core"),
        ("CONFIG_MODULES", "core"),
        # cgroups
        ("CONFIG_CGROUPS", "cgroups"),
        ("CONFIG_CGROUP_PIDS", "cgroups"),
        ("CONFIG_MEMCG", "cgroups"),
        # graphics
        ("CONFIG_DRM_I915", "gpu"),
        ("CONFIG_DRM_AMDGPU", "gpu"),
        ("CONFIG_DRM_NOUVEAU", "gpu"),
        ("CONFIG_FB_EFI", "gpu"),
        ("CONFIG_AGP_AMD64", "gpu"),
        # sound
        ("CONFIG_SND_HDA_INTEL", "sound"),
        ("CONFIG_SND_SOC_SOF_INTEL_PCI_MTL", "sound"),
        # bluetooth
        ("CONFIG_BT", "bluetooth"),
        ("CONFIG_BT_HCIBTUSB", "bluetooth"),
        # wireless / wifi
        ("CONFIG_CFG80211", "wireless"),
        ("CONFIG_MAC80211", "wireless"),
        ("CONFIG_IWLWIFI", "wifi-driver"),
        ("CONFIG_ATH10K", "wifi-driver"),
        # ethernet
        ("CONFIG_R8169", "eth-driver"),
        ("CONFIG_E1000E", "eth-driver"),
        ("CONFIG_IGB", "eth-driver"),
        # network
        ("CONFIG_NET", "network"),
        ("CONFIG_BRIDGE", "network"),
        ("CONFIG_NETFILTER", "network"),
        ("CONFIG_WIREGUARD", "network"),
        ("CONFIG_TUN", "network"),
        # filesystems
        ("CONFIG_EXT4_FS", "fs"),
        ("CONFIG_BTRFS_FS", "fs"),
        ("CONFIG_NTFS3_FS", "fs"),
        ("CONFIG_FUSE_FS", "fs"),
        ("CONFIG_OVERLAY_FS", "fs"),
        ("CONFIG_NFS_FS", "fs"),
        ("CONFIG_9P_FS", "fs"),
        # crypto / security
        ("CONFIG_CRYPTO_AES", "crypto"),
        ("CONFIG_CRYPTO_USER_API", "crypto"),
        ("CONFIG_SELINUX", "security"),
        ("CONFIG_AUDIT", "security"),
        ("CONFIG_KASAN", "kasan"),
        ("CONFIG_UBSAN", "kasan"),
        # storage
        ("CONFIG_BLK_DEV_NVME", "nvme"),
        ("CONFIG_NVME_CORE", "nvme"),
        ("CONFIG_SCSI", "scsi"),
        ("CONFIG_SATA_AHCI", "scsi"),
        ("CONFIG_DM_CRYPT", "block"),
        ("CONFIG_BLK_DEV_LOOP", "block"),
        # USB
        ("CONFIG_USB_XHCI_PCI", "usb"),
        ("CONFIG_USB_STORAGE", "usb"),
        # CPU power
        ("CONFIG_INTEL_IDLE", "cpuidle"),
        ("CONFIG_X86_INTEL_PSTATE", "cpufreq"),
        ("CONFIG_X86_AMD_PSTATE", "cpufreq"),
        ("CONFIG_MICROCODE_INTEL", "microcode"),
        # ACPI / boot
        ("CONFIG_ACPI", "acpi"),
        ("CONFIG_ACPI_BATTERY", "acpi"),
        ("CONFIG_EFI", "boot"),
        ("CONFIG_EFI_STUB", "boot"),
        ("CONFIG_BLK_DEV_INITRD", "boot"),
        ("CONFIG_RD_ZSTD", "boot"),
        # virtualization
        ("CONFIG_KVM", "kvm"),
        ("CONFIG_VHOST_NET", "kvm"),
        ("CONFIG_VIRTIO_NET", "virtio"),
        # tracing / debug
        ("CONFIG_DEBUG_KERNEL", "debug"),
        ("CONFIG_LOCKDEP", "debug"),
        ("CONFIG_DYNAMIC_DEBUG", "debug"),
        ("CONFIG_FTRACE", "debug"),
        ("CONFIG_BPF_SYSCALL", "bpf"),
        # platform
        ("CONFIG_DELL_LAPTOP", "platform-x86"),
        ("CONFIG_THINKPAD_ACPI", "platform-x86"),
        ("CONFIG_INTEL_HID", "platform-x86"),
        # binfmt / hwmon / firmware
        ("CONFIG_BINFMT_MISC", "binfmt"),
        ("CONFIG_SENSORS_CORETEMP", "hwmon"),
        ("CONFIG_FW_LOADER", "firmware"),
        ("CONFIG_TPM_TIS", "firmware"),
        # input
        ("CONFIG_KEYBOARD_ATKBD", "input"),
        ("CONFIG_MOUSE_PS2", "input"),
        ("CONFIG_HID_LOGITECH", "input"),
        # iommu
        ("CONFIG_INTEL_IOMMU", "iommu"),
        ("CONFIG_AMD_IOMMU", "iommu"),
        # fallback
        ("CONFIG_TOTALLY_UNKNOWN_THING", "misc"),
        ("CONFIG_FOO_BAR", "misc"),
    ],
)
def test_classify_known_symbols(symbol: str, expected: str):
    assert classify(symbol) == expected, (
        f"{symbol} → expected {expected}, got {classify(symbol)}"
    )


def test_classify_accepts_bare_name():
    assert classify("DRM_I915") == classify("CONFIG_DRM_I915")


def test_group_by_subsystem_basic():
    grouped = group_by_subsystem(
        [
            "CONFIG_DRM_I915",
            "CONFIG_DRM_AMDGPU",
            "CONFIG_BTRFS_FS",
            "CONFIG_KVM",
            "CONFIG_TOTALLY_UNKNOWN",
        ]
    )
    assert "gpu" in grouped
    assert set(grouped["gpu"]) == {"CONFIG_DRM_I915", "CONFIG_DRM_AMDGPU"}
    assert grouped["fs"] == ["CONFIG_BTRFS_FS"]
    assert grouped["kvm"] == ["CONFIG_KVM"]
    assert grouped["misc"] == ["CONFIG_TOTALLY_UNKNOWN"]


def test_group_preserves_input_order_within_bucket():
    """Ordering within each bucket is the input order — not sorted."""
    grouped = group_by_subsystem(
        ["CONFIG_DRM_NOUVEAU", "CONFIG_DRM_I915", "CONFIG_DRM_AMDGPU"]
    )
    assert grouped["gpu"] == [
        "CONFIG_DRM_NOUVEAU",
        "CONFIG_DRM_I915",
        "CONFIG_DRM_AMDGPU",
    ]
