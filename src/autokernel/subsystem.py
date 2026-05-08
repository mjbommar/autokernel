"""Classify any ``CONFIG_*`` symbol into a coarse subsystem bucket.

Used by :mod:`autokernel.review` to group ``needs_review`` proposals so a
human (or Claude) can make decisions group-at-a-time instead of seeing
hundreds of unrelated rows.

The classifier is **deliberately approximate**. The kernel tree's Kconfig
hierarchy is the ground truth, but we can't parse it without sources.
Instead we use ordered regex/prefix rules tuned to high-traffic symbols.
A misclassification is a UX paper-cut, not a correctness bug — every
proposal still receives the same policy treatment regardless of bucket.

Buckets are intentionally coarse — fewer than 50 — so the reviewer's
mental model stays small.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Subsystem name → ordered list of regex patterns. First matching subsystem
# wins. Patterns are anchored to CONFIG_ implicitly (callers strip prefix).
_RULES: list[tuple[str, list[re.Pattern[str]]]] = []


def _r(*pats: str) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in pats]


# Ordered most-specific → most-general. Each tuple: (subsystem, patterns).
# Patterns match against the part AFTER the ``CONFIG_`` prefix.
#
# IMPORTANT: subsystems whose patterns overlap a generic prefix (e.g. arch's
# ``^X86($|_)`` would otherwise swallow ``X86_INTEL_PSTATE``) must declare
# their specific rules BEFORE the generic arch fallback. Same applies for
# microcode (``^MICROCODE`` would otherwise be caught by debug if patterns
# expanded). When in doubt, add a test in ``tests/test_subsystem.py``.
_RULES_DEF: list[tuple[str, list[str]]] = [
    # ── architecture-specific power / cpu (must precede arch) ────────────
    (
        "cpufreq",
        [
            r"^CPU_FREQ",
            r"^X86_INTEL_PSTATE",
            r"^X86_AMD_PSTATE",
            r"^X86_AMD_FREQ",
            r"^X86_AMD_PLATFORM_DEVICE$",
            r"^X86_POWERNOW",
        ],
    ),
    ("cpuidle", [r"^CPU_IDLE", r"^INTEL_IDLE", r"^ACPI_PROCESSOR_IDLE"]),
    ("microcode", [r"^MICROCODE"]),
    # ── arch fundamentals ────────────────────────────────────────────────
    (
        "arch",
        [
            r"^X86($|_)",
            r"^64BIT$",
            r"^SMP$",
            r"^ARM($|64$|_)",
            r"^RISCV",
            r"^PPC",
            r"^MIPS",
        ],
    ),
    ("arch", [r"^IA32_EMULATION$", r"^COMPAT_", r"^X32_ABI$"]),
    # ── boot / init ──────────────────────────────────────────────────────
    (
        "boot",
        [
            r"^EFI($|_)",
            r"^BLK_DEV_INITRD$",
            r"^RD_(GZIP|LZ4|ZSTD|XZ|LZMA|LZO|BZIP2)$",
            r"^BOOT",
            r"^CMDLINE",
            r"^DMI",
        ],
    ),
    # ── core kernel infrastructure ───────────────────────────────────────
    (
        "core",
        [
            r"^PRINTK",
            r"^TTY$",
            r"^VT($|_)",
            r"^UNIX98_PTYS$",
            r"^DEVTMPFS",
            r"^TMPFS",
            r"^PROC_FS$",
            r"^SYSFS$",
            r"^FUTEX",
            r"^EPOLL",
            r"^SIGNALFD",
            r"^TIMERFD",
            r"^EVENTFD",
            r"^BLOCK$",
            r"^MULTIUSER$",
            r"^MODULES$",
            r"^MODULE_",
            r"^MMU$",
            r"^SWAP$",
            r"^SHMEM$",
            r"^HW_RANDOM($|_)",
            r"^SYSVIPC",
            r"^POSIX_MQUEUE$",
            r"^NAMESPACES$",
            r"^USER_NS$",
            r"^PID_NS$",
            r"^IPC_NS$",
            r"^UTS_NS$",
            r"^NET_NS$",
            r"^TIME_NS$",
            r"^CHECKPOINT_RESTORE$",
        ],
    ),
    # ── cgroups (their own bucket; systemd-critical) ─────────────────────
    ("cgroups", [r"^CGROUP", r"^MEMCG"]),
    # ── memory management ────────────────────────────────────────────────
    (
        "mm",
        [
            r"^TRANSPARENT_HUGEPAGE",
            r"^HUGETLB",
            r"^KSM$",
            r"^MEMORY_HOTPLUG",
            r"^ZSWAP",
            r"^ZRAM",
            r"^SPARSEMEM",
            r"^FLATMEM",
            r"^NUMA",
            r"^MEMORY_FAILURE",
            r"^CMA",
        ],
    ),
    # ── crypto / security infrastructure ─────────────────────────────────
    ("crypto", [r"^CRYPTO"]),
    (
        "security",
        [
            r"^SECURITY",
            r"^SELINUX",
            r"^APPARMOR",
            r"^SMACK",
            r"^LSM",
            r"^INTEGRITY",
            r"^IMA($|_)",
            r"^EVM",
            r"^AUDIT",
            r"^FORTIFY_SOURCE",
            r"^STACKPROTECTOR",
        ],
    ),
    ("kasan", [r"^KASAN", r"^KMSAN", r"^UBSAN", r"^KFENCE", r"^KCSAN"]),
    # ── debug / tracing ──────────────────────────────────────────────────
    (
        "debug",
        [
            r"^DEBUG",
            r"^FTRACE",
            r"^TRACE",
            r"^DYNAMIC_DEBUG",
            r"^GDB_SCRIPTS",
            r"^KPROBES",
            r"^FUNCTION_TRACER",
            r"^STACKTRACE",
            r"^LOCK_STAT",
            r"^LOCKDEP",
            r"^PROVE_",
            r"^FAULT_INJECTION",
            r"^MAGIC_SYSRQ",
        ],
    ),
    ("bpf", [r"^BPF", r"^XDP_SOCKETS"]),
    # ── virtualization ───────────────────────────────────────────────────
    ("kvm", [r"^KVM", r"^VHOST"]),
    ("virtio", [r"^VIRTIO"]),
    ("xen", [r"^XEN", r"^PARAVIRT"]),
    # ── PM / ACPI (cpufreq/cpuidle/microcode declared at top of table) ───
    (
        "pm",
        [
            r"^PM($|_)",
            r"^SUSPEND",
            r"^HIBERNATION",
            r"^PM_AUTOSLEEP$",
            r"^PM_WAKELOCKS$",
        ],
    ),
    ("acpi", [r"^ACPI"]),
    # ── graphics / display ───────────────────────────────────────────────
    (
        "gpu",
        [
            r"^DRM",
            r"^FB($|_)",
            r"^AGP",
            r"^I915$",
            r"^VGA_ARB",
            r"^FRAMEBUFFER_CONSOLE",
            r"^BACKLIGHT_CLASS_DEVICE$",
            r"^LCD_CLASS_DEVICE$",
        ],
    ),
    # ── sound ────────────────────────────────────────────────────────────
    ("sound", [r"^SND", r"^SOUND$"]),
    # ── input / hid ──────────────────────────────────────────────────────
    (
        "input",
        [
            r"^INPUT",
            r"^KEYBOARD",
            r"^MOUSE",
            r"^TOUCHSCREEN",
            r"^JOYSTICK",
            r"^HID",
            r"^SERIO",
            r"^EVDEV",
            r"^GAMEPORT",
        ],
    ),
    # ── networking ───────────────────────────────────────────────────────
    ("bluetooth", [r"^BT($|_)"]),
    (
        "wireless",
        [r"^CFG80211", r"^MAC80211", r"^IEEE80211", r"^RFKILL", r"^WIRELESS_EXT"],
    ),
    (
        "wifi-driver",
        [
            r"^IWLWIFI",
            r"^IWLMVM",
            r"^IWLDVM",
            r"^ATH(5|6|9|10|11|12)K",
            r"^RTL8",
            r"^RT2X",
            r"^BRCM",
            r"^MWIFIEX",
            r"^MT76",
            r"^MT(79)?21",
        ],
    ),
    (
        "eth-driver",
        [
            r"^R8169$",
            r"^R8152$",
            r"^E1000",
            r"^IGB($|_)",
            r"^IGC",
            r"^IXGBE",
            r"^TG3",
            r"^BNX2",
            r"^MLX",
            r"^I40E",
            r"^ICE($|_)",
            r"^ENA$",
            r"^FM10K",
        ],
    ),
    (
        "network",
        [
            r"^NET($|_)",
            r"^INET$",
            r"^INET6$",
            r"^IP_",
            r"^IPV6",
            r"^TCP_",
            r"^UDP_",
            r"^NETFILTER",
            r"^NF_",
            r"^NETLINK",
            r"^BRIDGE",
            r"^VLAN_",
            r"^BONDING",
            r"^VETH",
            r"^TUN$",
            r"^WIREGUARD",
            r"^OPENVSWITCH",
            r"^GENEVE",
            r"^VXLAN",
            r"^MACVLAN",
            r"^MACVTAP",
            r"^IPVLAN",
            r"^MACSEC",
            r"^L2TP",
            r"^PPP",
            r"^SLIP",
            r"^MAC_PARTITION$",
            r"^PHYLIB$",
            r"^MDIO",
            r"^MII$",
            r"^DUMMY$",
        ],
    ),
    # ── filesystems ──────────────────────────────────────────────────────
    (
        "fs",
        [
            r"_FS$",
            r"^FUSE",
            r"^OVERLAY",
            r"^SQUASHFS",
            r"^NFS",
            r"^CIFS",
            r"^CEPH",
            r"^BTRFS",
            r"^EXT[234]",
            r"^XFS",
            r"^F2FS",
            r"^NTFS",
            r"^EXFAT",
            r"^ISO9660",
            r"^UDF",
            r"^MSDOS",
            r"^VFAT",
            r"^FAT_FS$",
            r"^AUTOFS",
            r"^9P_FS",
            r"^ZFS",
            r"^EROFS",
            r"^BCACHEFS",
            r"^GFS2",
            r"^OCFS2",
            r"^DLM$",
            r"^QUOTA",
            r"^FANOTIFY",
            r"^INOTIFY",
            r"^DNOTIFY",
            r"^FSCACHE",
            r"^CACHEFILES",
        ],
    ),
    ("binfmt", [r"^BINFMT"]),
    # ── storage / block ──────────────────────────────────────────────────
    ("nvme", [r"^NVME", r"^BLK_DEV_NVME"]),
    ("scsi", [r"^SCSI", r"^SATA", r"^PATA", r"^ATA($|_)"]),
    (
        "block",
        [r"^BLK_DEV", r"^DM_", r"^MD_", r"^BCACHE($|_)", r"^IOSCHED", r"^MQ_IOSCHED"],
    ),
    ("mmc", [r"^MMC"]),
    ("mtd", [r"^MTD"]),
    # ── usb (subsystem; specific drivers may already be reclassified) ────
    ("usb", [r"^USB"]),
    # ── buses / lowlevel ─────────────────────────────────────────────────
    ("i2c", [r"^I2C"]),
    ("spi", [r"^SPI"]),
    ("gpio", [r"^GPIO"]),
    ("pinctrl", [r"^PINCTRL"]),
    ("clk", [r"^COMMON_CLK", r"^CLK_"]),
    ("iommu", [r"^INTEL_IOMMU", r"^AMD_IOMMU", r"^IOMMU", r"_IOMMU$"]),
    ("dma", [r"^DMA(DEVICES)?$", r"^DMAENGINE", r"_DMAC$", r"^INTEL_IDXD"]),
    ("pci", [r"^PCI($|_)", r"^PCIE", r"^PCIEPORTBUS", r"^PCIE(AER|PME|ASPM)"]),
    ("serial", [r"^SERIAL"]),
    # ── platform-specific ────────────────────────────────────────────────
    (
        "platform-x86",
        [
            r"^DELL",
            r"^X86_PLATFORM",
            r"^THINKPAD",
            r"^IDEAPAD",
            r"^ASUS",
            r"^ACER",
            r"^HP_WMI",
            r"^HP_ACCEL",
            r"^MSI_LAPTOP",
            r"^TOSHIBA",
            r"^FUJITSU",
            r"^INTEL_(MENU|HID|VBTN|WMI|RST|ATOM|PUNIT|TURBO|UNCORE|TELEMETRY|PMC|PMT|ISH|IPS|SCU|SOC|VSEC|SPEED|SDSI)",
            r"^AMD_PMC",
            r"^AMD_HSMP",
            r"^AMD_PMF",
        ],
    ),
    # ── thermal / power-supply / regulator / leds / watchdog / rtc ───────
    (
        "thermal",
        [
            r"^THERMAL",
            r"^INT3400_THERMAL",
            r"^INT340X_THERMAL",
            r"^X86_PKG_TEMP_THERMAL",
        ],
    ),
    ("power-supply", [r"^POWER_SUPPLY", r"^CHARGER_", r"^BATTERY_"]),
    ("regulator", [r"^REGULATOR"]),
    ("leds", [r"^LEDS"]),
    ("watchdog", [r"^WATCHDOG", r"_WDT($|_)"]),
    ("rtc", [r"^RTC"]),
    # ── media / iio / infiniband ─────────────────────────────────────────
    ("media", [r"^MEDIA", r"^VIDEO_", r"^DVB", r"^RC_", r"^CEC", r"^V4L2_"]),
    ("iio", [r"^IIO"]),
    ("infiniband", [r"^INFINIBAND", r"^RDMA", r"^MLX(4|5)_(IB|EN|CORE)"]),
    # ── perf / tracing extras ────────────────────────────────────────────
    ("perf", [r"^PERF_EVENTS", r"^PERF_USE", r"^OPROFILE"]),
    # ── firmware / hwmon / mei / cxl ─────────────────────────────────────
    ("hwmon", [r"^HWMON$", r"^SENSORS"]),
    (
        "firmware",
        [
            r"^FW_LOADER",
            r"^EXTRA_FIRMWARE",
            r"^FIRMWARE_",
            r"^EFI_VARS_PSTORE",
            r"^TCG_",
            r"^TPM_",
        ],
    ),
    ("mei", [r"^INTEL_MEI"]),
    ("cxl", [r"^CXL"]),
    ("edac", [r"^EDAC"]),
    ("extcon", [r"^EXTCON"]),
    ("auxdisplay", [r"^AUXDISPLAY"]),
]


for _subsystem, _pats in _RULES_DEF:
    _RULES.append((_subsystem, _r(*_pats)))


def classify(symbol: str) -> str:
    """Return the subsystem bucket for ``CONFIG_<NAME>`` (or ``<NAME>``).

    Returns ``'misc'`` for any symbol that doesn't match a known rule.
    """
    name = symbol[7:] if symbol.startswith("CONFIG_") else symbol
    for subsystem, patterns in _RULES:
        for p in patterns:
            if p.search(name):
                return subsystem
    return "misc"


def group_by_subsystem(symbols: Iterable[str]) -> dict[str, list[str]]:
    """Bucket an iterable of CONFIG_ symbols by subsystem.

    Returns a stable dict (insertion order matches first-encountered order).
    """
    out: dict[str, list[str]] = {}
    for s in symbols:
        out.setdefault(classify(s), []).append(s)
    return out
