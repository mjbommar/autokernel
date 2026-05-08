"""Per-workload Kconfig recipes.

Curated recommendations distilled from research across CachyOS, XanMod,
Liquorix, kernel.org admin-guide/, KSPP, RHEL low-latency tuning,
SUSE NUMA tuning, KVM guest config, Yocto, and Alpine. Each entry has a
structured fields the agent can paste into a prompt verbatim.

The recipes are intentionally **divergence-only** — they record where
a profile MEANINGFULLY differs from a stock Ubuntu/Fedora generic
kernel. The agents combine these with the live Kconfig surface (from
``autokernel.kconfig_walk``) to know what's available on the target.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WorkloadRecipe:
    """One recommendation in a workload's recipe.

    ``symbol`` is the bare CONFIG_NAME (no ``CONFIG_`` prefix — kept
    out so prompt rendering is consistent with the trim pipeline).
    ``value`` is the proposed assignment (``"y"``, ``"n"``, ``"m"``,
    ``"<integer>"``, ``"<string>"``, or for choices, the SELECTED
    option's symbol name).
    ``axis`` says what axis this recommendation targets (used when
    explaining tradeoffs).
    """

    symbol: str
    value: str
    axis: str  # 'perf' | 'surface' | 'security' | 'power' | 'compat'
    rationale: str
    source: str  # short citation


@dataclass(frozen=True)
class WorkloadProfileSpec:
    """The complete recipe for one workload profile."""

    profile: str  # one of: desktop, laptop, server, vm-guest, realtime, embedded
    description: str
    recipes: list[WorkloadRecipe] = field(default_factory=list)


# ── desktop ───────────────────────────────────────────────────────────────


_DESKTOP = WorkloadProfileSpec(
    profile="desktop",
    description=(
        "General-purpose interactive workstation. Optimizes for "
        "responsiveness — frame timing, audio latency, window-manager "
        "smoothness — at the cost of a few percent throughput. Hot-plug "
        "everything (GPU/audio/USB/Bluetooth)."
    ),
    recipes=[
        WorkloadRecipe(
            "PREEMPT",
            "y",
            "perf",
            "Full kernel preemption — minimizes scheduling latency for interactive apps. CachyOS/XanMod/Liquorix all default to this.",
            "CachyOS,XanMod,Liquorix",
        ),
        WorkloadRecipe(
            "HZ_1000",
            "y",
            "perf",
            "1000 Hz tick = ~1ms scheduling granularity. Liquorix/XanMod desktop default.",
            "XanMod,Liquorix",
        ),
        WorkloadRecipe(
            "NO_HZ_IDLE",
            "y",
            "power",
            "Skip ticks on idle CPUs; the desktop sweet spot vs NO_HZ_FULL (overhead).",
            "kernel.org/timers/no_hz",
        ),
        WorkloadRecipe(
            "HIGH_RES_TIMERS",
            "y",
            "perf",
            "Required for hrtimer-based audio and frame pacing.",
            "kernel.org",
        ),
        WorkloadRecipe(
            "SCHED_AUTOGROUP",
            "y",
            "perf",
            "Per-tty cgroup scheduling — keeps a runaway compile from killing your desktop.",
            "Ubuntu generic",
        ),
        WorkloadRecipe(
            "TRANSPARENT_HUGEPAGE_MADVISE",
            "y",
            "perf",
            "madvise mode is the safe default — opt-in app benefits without ALWAYS-mode latency spikes.",
            "RHEL,Percona",
        ),
        WorkloadRecipe(
            "ZSWAP",
            "y",
            "perf",
            "Compressed swap reduces swap-thrash stutter.",
            "CachyOS",
        ),
        WorkloadRecipe(
            "ZSWAP_DEFAULT_ON",
            "y",
            "perf",
            "Avoid needing zswap.enabled=1 on cmdline.",
            "CachyOS",
        ),
        WorkloadRecipe(
            "ZSWAP_COMPRESSOR_DEFAULT_ZSTD",
            "y",
            "perf",
            "Best ratio/speed tradeoff on modern x86.",
            "CachyOS",
        ),
        WorkloadRecipe(
            "LRU_GEN",
            "y",
            "perf",
            "MGLRU: -40% kswapd CPU, -85% LMK on Google ChromeOS/Android.",
            "kernel.org/admin-guide/mm/multigen_lru",
        ),
        WorkloadRecipe(
            "LRU_GEN_ENABLED",
            "y",
            "perf",
            "Make MGLRU on by default at boot.",
            "kernel.org",
        ),
        WorkloadRecipe(
            "TCP_CONG_BBR",
            "y",
            "perf",
            "Build BBR even if not default — needed for runtime sysctl switch.",
            "Cloudflare,Google",
        ),
        WorkloadRecipe(
            "NET_SCH_FQ",
            "y",
            "perf",
            "fq qdisc — required by BBR for pacing.",
            "kernel.org",
        ),
        WorkloadRecipe(
            "PSI",
            "y",
            "perf",
            "Pressure-Stall-Info — needed by systemd-oomd / desktop OOM-prevention.",
            "Fedora kernel.spec",
        ),
        WorkloadRecipe(
            "PREEMPT_DYNAMIC",
            "y",
            "perf",
            "Lets `preempt=` boot arg switch NONE/VOLUNTARY/FULL at runtime.",
            "LWN: lazy preemption",
        ),
        WorkloadRecipe(
            "RANDOM_TRUST_CPU",
            "y",
            "power",
            "Lets RDRAND seed CRNG instantly — eliminates boot-time entropy stalls.",
            "Red Hat 8.1 release notes",
        ),
        WorkloadRecipe(
            "RANDOM_TRUST_BOOTLOADER",
            "y",
            "power",
            "Same idea for bootloader-supplied seed.",
            "kernel.org",
        ),
        WorkloadRecipe(
            "DEBUG_INFO_NONE",
            "y",
            "surface",
            "Drop debug bloat — Ubuntu generic ships INFO_BTF=y by default; smaller image, faster build.",
            "Liquorix",
        ),
    ],
)


# ── laptop ────────────────────────────────────────────────────────────────


_LAPTOP = WorkloadProfileSpec(
    profile="laptop",
    description=(
        "Desktop's needs PLUS battery longevity, suspend/resume, thermal "
        "envelope management, lid/dock events, brightness, ACPI buttons. "
        "The kernel should aggressively idle and accept slightly higher "
        "latency to do so."
    ),
    recipes=[
        # Inherit desktop responsiveness…
        WorkloadRecipe(
            "PREEMPT",
            "y",
            "perf",
            "Same as desktop — interactive feel matters.",
            "XanMod",
        ),
        WorkloadRecipe(
            "HZ_1000",
            "y",
            "perf",
            "Negligible idle cost thanks to NO_HZ_IDLE.",
            "Ubuntu lowlatency",
        ),
        WorkloadRecipe(
            "NO_HZ_IDLE",
            "y",
            "power",
            "'Critically important to battery-powered devices' (kernel.org NO_HZ doc).",
            "kernel.org",
        ),
        # …then add laptop-specific power management.
        WorkloadRecipe(
            "PM_RUNTIME",
            "y",
            "power",
            "Per-device runtime PM; biggest single battery win for buses/devices.",
            "kernel.org/admin-guide/pm",
        ),
        WorkloadRecipe(
            "HIBERNATION",
            "y",
            "power",
            "Suspend-to-disk for laptops with limited battery.",
            "kernel.org",
        ),
        WorkloadRecipe("ACPI_AC", "y", "power", "AC-status events.", "Gentoo PM guide"),
        WorkloadRecipe(
            "ACPI_BATTERY", "y", "power", "Battery-status events.", "Gentoo PM guide"
        ),
        WorkloadRecipe(
            "ACPI_FAN", "y", "power", "Fan/thermal control.", "Gentoo PM guide"
        ),
        WorkloadRecipe(
            "ACPI_THERMAL",
            "y",
            "power",
            "Required for thermal management.",
            "kernel.org",
        ),
        WorkloadRecipe("ACPI_DOCK", "y", "power", "Dock/undock events.", "ThinkWiki"),
        WorkloadRecipe(
            "INTEL_PSTATE",
            "y",
            "power",
            "Native HWP driver for Skylake+ Intel; far better than acpi-cpufreq.",
            "kernel.org/admin-guide/pm/intel_pstate",
        ),
        WorkloadRecipe(
            "X86_AMD_PSTATE",
            "y",
            "power",
            "CPPC-based driver for Zen2+ AMD with EPP.",
            "kernel.org/admin-guide/pm/amd-pstate",
        ),
        WorkloadRecipe(
            "CPU_FREQ_DEFAULT_GOV_SCHEDUTIL",
            "y",
            "power",
            "schedutil is now the recommended default — sched-integrated, energy-aware.",
            "ArchWiki cpufreq",
        ),
        WorkloadRecipe(
            "INTEL_IDLE", "y", "power", "Deeper C-states than acpi_idle.", "kernel.org"
        ),
        WorkloadRecipe(
            "PCIEASPM",
            "y",
            "power",
            "Aggressive PCIe runtime PM — biggest single battery-life win.",
            "Ubuntu,Arch PM",
        ),
        WorkloadRecipe(
            "USB_AUTOSUSPEND",
            "y",
            "power",
            "USB device runtime PM.",
            "ArchWiki Power management",
        ),
        WorkloadRecipe(
            "ACPI_PLATFORM_PROFILE",
            "y",
            "power",
            "Modern Performance/Balanced/Quiet platform hooks.",
            "kernel.org",
        ),
        WorkloadRecipe(
            "BACKLIGHT_CLASS_DEVICE", "y", "compat", "Brightness keys.", "kernel.org"
        ),
        WorkloadRecipe(
            "EFI_RUNTIME_WRAPPERS",
            "y",
            "compat",
            "Modern UEFI laptop expects these.",
            "Fedora kernel.spec",
        ),
        WorkloadRecipe(
            "THINKPAD_ACPI",
            "m",
            "compat",
            "Vendor extras for hotkey/Fn/LED/fan — module-load by DMI.",
            "ThinkWiki",
        ),
    ],
)


# ── server ───────────────────────────────────────────────────────────────


_SERVER = WorkloadProfileSpec(
    profile="server",
    description=(
        "Datacenter / bare-metal throughput-first. No GUI, audio, "
        "Bluetooth, suspend, hibernate. NUMA-aware. Workloads are batch "
        "or steady-state network-bound. Module surface should be small."
    ),
    recipes=[
        WorkloadRecipe(
            "PREEMPT_NONE",
            "y",
            "perf",
            "'Traditional Linux preemption model, geared towards throughput' — recommended for server/scientific.",
            "LKDDB",
        ),
        WorkloadRecipe(
            "HZ_100",
            "y",
            "perf",
            "RHEL/SUSE server tick rate; minimum interrupt overhead.",
            "RHEL low-latency PDF",
        ),
        WorkloadRecipe(
            "NO_HZ_IDLE",
            "y",
            "perf",
            "Idle ticks gone — saves CPU and helps virt neighbors.",
            "kernel.org",
        ),
        WorkloadRecipe("NUMA", "y", "perf", "Required on multi-socket.", "RHEL"),
        WorkloadRecipe(
            "NUMA_BALANCING",
            "y",
            "perf",
            "Auto page-migration to local node — 'most important task' for multi-socket.",
            "SLES tuning",
        ),
        WorkloadRecipe(
            "NUMA_BALANCING_DEFAULT_ENABLED",
            "y",
            "perf",
            "Auto-on at boot.",
            "kernel.org",
        ),
        WorkloadRecipe(
            "SCHED_MC",
            "y",
            "perf",
            "Multi-core sched topology awareness.",
            "kernel.org",
        ),
        WorkloadRecipe("SCHED_SMT", "y", "perf", "SMT-aware sched.", "kernel.org"),
        WorkloadRecipe(
            "SCHED_CLUSTER",
            "y",
            "perf",
            "Cluster-aware (shared L2 on Zen5/Alder Lake/big.LITTLE).",
            "Phoronix cluster sched",
        ),
        WorkloadRecipe(
            "HUGETLBFS", "y", "perf", "Explicit huge pages for DBs / DPDK.", "RHEL"
        ),
        WorkloadRecipe(
            "TCP_CONG_BBR",
            "y",
            "perf",
            "Standard for cloud-edge workloads.",
            "Cloudflare,Google",
        ),
        WorkloadRecipe(
            "DEFAULT_BBR",
            "y",
            "perf",
            "Make BBR the default congestion control.",
            "arXiv 2510.22461",
        ),
        WorkloadRecipe(
            "NET_SCH_FQ", "y", "perf", "FQ qdisc for BBR pacing.", "kernel.org"
        ),
        WorkloadRecipe(
            "NET_SCH_FQ_CODEL", "y", "perf", "fq_codel default qdisc.", "kernel.org"
        ),
        WorkloadRecipe(
            "RPS", "y", "perf", "Receive packet steering across cores.", "Linux net"
        ),
        WorkloadRecipe(
            "RFS_ACCEL", "y", "perf", "RFS accelerator for steady flows.", "Linux net"
        ),
        WorkloadRecipe("XPS", "y", "perf", "Transmit packet steering.", "Linux net"),
        WorkloadRecipe(
            "BPF_SYSCALL",
            "y",
            "perf",
            "eBPF data plane (Cilium, Katran, etc.).",
            "Cilium",
        ),
        WorkloadRecipe(
            "BPF_JIT",
            "y",
            "perf",
            "JIT vs interpreter — large speedup for any BPF.",
            "iovisor",
        ),
        WorkloadRecipe(
            "BPF_JIT_DEFAULT_ON",
            "y",
            "perf",
            "Auto-JIT instead of needing sysctl.",
            "Cloudflare",
        ),
        WorkloadRecipe(
            "XDP_SOCKETS", "y", "perf", "AF_XDP for kernel-bypass dataplane.", "Cilium"
        ),
        WorkloadRecipe(
            "PCI_IOV",
            "y",
            "perf",
            "SR-IOV for accelerator/NIC pass-through.",
            "kernel.org",
        ),
        WorkloadRecipe(
            "VFIO",
            "m",
            "perf",
            "VFIO for device assignment to userspace/VMs.",
            "kernel.org",
        ),
        WorkloadRecipe(
            "IOMMU_DEFAULT_PASSTHROUGH",
            "y",
            "perf",
            "Skip IOMMU mappings for trusted DMA — measurable throughput win.",
            "LWN",
        ),
        WorkloadRecipe(
            "PSI", "y", "perf", "PSI metrics for autoscalers.", "kernel.org"
        ),
        WorkloadRecipe(
            "CGROUP_BPF", "y", "perf", "Containers / k8s.", "Ubuntu generic"
        ),
        WorkloadRecipe(
            "HIBERNATION",
            "n",
            "surface",
            "Servers don't sleep; trims attack surface and image size.",
            "RHEL",
        ),
        WorkloadRecipe("SUSPEND", "n", "surface", "Servers don't sleep.", "RHEL"),
        WorkloadRecipe(
            "SOUND", "n", "surface", "Drop hot-plug consumer surface.", "RHEL"
        ),
        WorkloadRecipe(
            "BT", "n", "surface", "No Bluetooth on a datacenter box.", "RHEL"
        ),
        WorkloadRecipe(
            "INPUT_JOYDEV", "n", "surface", "No game controllers in production.", "RHEL"
        ),
    ],
)


# ── vm-guest ─────────────────────────────────────────────────────────────


_VM_GUEST = WorkloadProfileSpec(
    profile="vm-guest",
    description=(
        "Lives entirely on virtio. No real hardware drivers, no thermal, "
        "no ACPI buttons, no hibernation, no Wi-Fi/BT/audio. Kernel "
        "image and boot time matter more than peak throughput; entropy "
        "is supplied by hypervisor."
    ),
    recipes=[
        WorkloadRecipe(
            "HYPERVISOR_GUEST",
            "y",
            "compat",
            "Master switch.",
            "linux/kernel/configs/kvm_guest.config",
        ),
        WorkloadRecipe(
            "PARAVIRT",
            "y",
            "perf",
            "Paravirt hooks; PV-spinlocks materially reduce lock-holder preemption stalls.",
            "kvm_guest.config",
        ),
        WorkloadRecipe(
            "PARAVIRT_SPINLOCKS",
            "y",
            "perf",
            "Required for KVM/Xen guests to avoid lock-holder preemption.",
            "Clovertrail KVM PV spinlocks",
        ),
        WorkloadRecipe(
            "KVM_GUEST",
            "y",
            "perf",
            "kvmclock, async PF, PV TLB flush, PV IPI — sizable wins inside KVM.",
            "LWN",
        ),
        WorkloadRecipe(
            "XEN", "y", "perf", "Xen guest support — PVH/PV.", "kvm_guest.config"
        ),
        WorkloadRecipe(
            "HYPERV",
            "y",
            "perf",
            "Hyper-V enlightenments give ~20% single-core / 11% multi-core.",
            "Red Hat optimizing Windows VMs",
        ),
        WorkloadRecipe(
            "VIRTIO_PCI", "y", "compat", "Virtio PCI bus.", "kvm_guest.config"
        ),
        WorkloadRecipe(
            "VIRTIO_BLK",
            "y",
            "compat",
            "Built-in (not modular) so initrd is small/optional.",
            "kvm_guest.config",
        ),
        WorkloadRecipe(
            "VIRTIO_NET",
            "y",
            "compat",
            "Built-in. Standard guest NIC.",
            "kvm_guest.config",
        ),
        WorkloadRecipe(
            "VIRTIO_CONSOLE",
            "y",
            "compat",
            "Console + agent transport.",
            "libvirt wiki",
        ),
        WorkloadRecipe(
            "VIRTIO_BALLOON",
            "y",
            "perf",
            "Ballooning for memory hot-plug.",
            "libvirt wiki",
        ),
        WorkloadRecipe(
            "VIRTIO_INPUT", "y", "compat", "QEMU virtio-tablet.", "libvirt wiki"
        ),
        WorkloadRecipe(
            "VIRTIO_FS", "m", "perf", "Lightweight host-shared FS.", "libvirt wiki"
        ),
        WorkloadRecipe(
            "VIRTIO_VSOCK", "m", "compat", "vsock for agents.", "kernel.org"
        ),
        WorkloadRecipe(
            "HW_RANDOM_VIRTIO",
            "y",
            "perf",
            "Avoids boot-time entropy starvation; cloud hosts expose this.",
            "Debian BoottimeEntropyStarvation",
        ),
        WorkloadRecipe(
            "RANDOM_TRUST_CPU",
            "y",
            "perf",
            "Trust RDRAND — gets crng_init done before userspace.",
            "kernel.org",
        ),
        WorkloadRecipe(
            "RANDOM_TRUST_BOOTLOADER",
            "y",
            "perf",
            "Trust hypervisor-supplied seed.",
            "kernel.org",
        ),
        WorkloadRecipe(
            "HZ_250",
            "y",
            "perf",
            "1000 Hz costs you on virt — every tick is a vmexit. Cloud images converge here.",
            "Passthrough POST CONFIG_HZ-KVM",
        ),
        WorkloadRecipe(
            "NO_HZ_IDLE",
            "y",
            "perf",
            "Skip ticks when idle — biggest hypervisor neighbor-friendliness win.",
            "kernel.org",
        ),
        WorkloadRecipe(
            "PREEMPT_VOLUNTARY",
            "y",
            "perf",
            "Cloud images converge here; full preempt has measurable PV overhead.",
            "Fedora cloud kernel",
        ),
        WorkloadRecipe(
            "HIBERNATION",
            "n",
            "surface",
            "A VM should be paused by hypervisor, not hibernated.",
            "Fedora cloud",
        ),
        WorkloadRecipe(
            "SUSPEND", "n", "surface", "Same as hibernation.", "Fedora cloud"
        ),
        WorkloadRecipe(
            "THERMAL", "n", "surface", "No thermal sensors in a VM.", "RHEL Atomic"
        ),
        WorkloadRecipe(
            "CPU_FREQ",
            "n",
            "surface",
            "Hypervisor scales frequency, not the guest.",
            "RHEL Atomic",
        ),
        WorkloadRecipe(
            "DRM",
            "n",
            "surface",
            "Pure VM: drop physical DRM — keep only DRM_VIRTIO_GPU/QXL/BOCHS.",
            "Alpine virt kernel",
        ),
        WorkloadRecipe("SOUND", "n", "surface", "Drop audio.", "Alpine virt"),
        WorkloadRecipe("BT", "n", "surface", "Drop Bluetooth.", "Alpine virt"),
        WorkloadRecipe(
            "X86_X2APIC",
            "y",
            "perf",
            "Required by KVM with > 255 vCPUs and faster IPIs anyway.",
            "KVM tuning",
        ),
        WorkloadRecipe(
            "TCP_CONG_BBR",
            "y",
            "perf",
            "BBR with FQ pacing wins for cloud egress.",
            "Google",
        ),
    ],
)


# ── realtime ─────────────────────────────────────────────────────────────


_REALTIME = WorkloadProfileSpec(
    profile="realtime",
    description=(
        "PREEMPT_RT. Bounded worst-case latency is the only metric; "
        "throughput is sacrificed. IRQ threading, RCU offload, full "
        "tickless on isolated cores, no debug. Industrial control, "
        "audio production, ROS 2 robotics, EtherCAT, telecom dataplane."
    ),
    recipes=[
        WorkloadRecipe(
            "PREEMPT_RT",
            "y",
            "perf",
            "The whole point — sleeping locks, PI-aware primitives.",
            "Ubuntu RT,realtime-linux.org",
        ),
        WorkloadRecipe(
            "HZ_1000",
            "y",
            "perf",
            "Highest standard tick — combined with hrtimers.",
            "Ubuntu lowlatency",
        ),
        WorkloadRecipe(
            "HIGH_RES_TIMERS", "y", "perf", "hrtimers are non-negotiable.", "kernel.org"
        ),
        WorkloadRecipe(
            "NO_HZ_FULL",
            "y",
            "perf",
            "Eliminate scheduling-clock interrupts on isolated CPUs.",
            "kernel.org",
        ),
        WorkloadRecipe(
            "RCU_NOCB_CPU",
            "y",
            "perf",
            "Offload RCU callbacks; required to actually exit ticks under nohz_full.",
            "linuxfoundation RT wiki",
        ),
        WorkloadRecipe(
            "RCU_NOCB_CPU_DEFAULT_ALL",
            "y",
            "perf",
            "Offload all CPUs' callbacks by default.",
            "linuxfoundation RT wiki",
        ),
        WorkloadRecipe(
            "RCU_BOOST",
            "y",
            "perf",
            "Avoid RCU-reader-induced grace-period blocking.",
            "linuxfoundation RT",
        ),
        WorkloadRecipe(
            "IRQ_FORCED_THREADING_DEFAULT",
            "y",
            "perf",
            "Threaded IRQs by default — schedulable, priority-able.",
            "bootlin RT slides",
        ),
        WorkloadRecipe(
            "DEBUG_PREEMPT",
            "n",
            "perf",
            "Critical: disable debug options that introduce latency.",
            "Ubuntu RT",
        ),
        WorkloadRecipe("DEBUG_LOCKDEP", "n", "perf", "Same.", "Ubuntu RT"),
        WorkloadRecipe("DEBUG_OBJECTS", "n", "perf", "Same.", "Ubuntu RT"),
        WorkloadRecipe("SLUB_DEBUG", "n", "perf", "Same.", "Ubuntu RT"),
        WorkloadRecipe(
            "CPU_FREQ_DEFAULT_GOV_PERFORMANCE",
            "y",
            "perf",
            "Avoid frequency-transition latency.",
            "Ubuntu RT",
        ),
        WorkloadRecipe(
            "TRANSPARENT_HUGEPAGE",
            "n",
            "perf",
            "THP coalescing/khugepaged causes stalls — RT can't tolerate.",
            "Ubuntu RT,RHEL",
        ),
        WorkloadRecipe(
            "HUGETLBFS",
            "y",
            "perf",
            "Explicit huge pages OK; transparent ones not.",
            "kernel.org",
        ),
        WorkloadRecipe(
            "NET_RX_BUSY_POLL", "y", "perf", "Low-latency NIC RX.", "Ubuntu RT"
        ),
        WorkloadRecipe(
            "BPF_JIT",
            "y",
            "perf",
            "Avoid interpreter latency for XDP/cgroup-bpf paths.",
            "Cilium",
        ),
        WorkloadRecipe(
            "BPF_JIT_ALWAYS_ON",
            "y",
            "perf",
            "Foreclose interpreter (Spectre v2 hardening + smaller text).",
            "LKDDB",
        ),
        WorkloadRecipe(
            "PCIEASPM", "n", "perf", "ASPM transition latency kills tail.", "Ubuntu RT"
        ),
        WorkloadRecipe(
            "VIRT_CPU_ACCOUNTING_GEN",
            "y",
            "perf",
            "Required by NO_HZ_FULL.",
            "kernel.org",
        ),
    ],
)


# ── embedded ─────────────────────────────────────────────────────────────


_EMBEDDED = WorkloadProfileSpec(
    profile="embedded",
    description=(
        "Fixed hardware, slow CPU, often slow flash. Smallest possible "
        "kernel. Often CONFIG_MODULES=n so initramfs disappears. No SMP, "
        "no NUMA, no virtio, no power management complexity."
    ),
    recipes=[
        WorkloadRecipe(
            "EMBEDDED",
            "y",
            "surface",
            "Unlocks the tiny-kernel knobs in Kconfig.",
            "Alpine custom kernel",
        ),
        WorkloadRecipe(
            "EXPERT",
            "y",
            "surface",
            "Required for many trim-down options.",
            "kernel.org",
        ),
        WorkloadRecipe(
            "CC_OPTIMIZE_FOR_SIZE",
            "y",
            "surface",
            "-Os; among the top-200 most-impactful options for image size.",
            "Inria HAL paper",
        ),
        WorkloadRecipe(
            "BASE_SMALL",
            "y",
            "surface",
            "Smaller hash tables / array sizes.",
            "LWN small-kernel",
        ),
        WorkloadRecipe(
            "SLUB_TINY",
            "y",
            "surface",
            "Tiny SLUB variant — saves ~4 KiB and a lot of code.",
            "kernel.org slub",
        ),
        WorkloadRecipe(
            "BLK_DEV_INITRD",
            "n",
            "surface",
            "Drop initramfs entirely if kernel can mount root directly.",
            "LKDDB BLK_DEV_INITRD",
        ),
        WorkloadRecipe(
            "KALLSYMS_BASE_RELATIVE",
            "y",
            "surface",
            "Smaller symbol table.",
            "LWN tinyconfig",
        ),
        WorkloadRecipe("DEBUG_FS", "n", "surface", "Drop debugfs.", "Yocto"),
        WorkloadRecipe(
            "PREEMPT_VOLUNTARY", "y", "perf", "Best size/latency compromise.", "bootlin"
        ),
        WorkloadRecipe(
            "HZ_100",
            "y",
            "perf",
            "Lowest tick rate — saves cycles on slow CPUs.",
            "RHEL low-lat",
        ),
        WorkloadRecipe(
            "NO_HZ_IDLE", "y", "power", "Battery-powered IoT cares too.", "kernel.org"
        ),
        WorkloadRecipe("HIBERNATION", "n", "surface", "Save space.", "Yocto"),
        WorkloadRecipe(
            "SUSPEND",
            "n",
            "surface",
            "Save space (unless battery-powered IoT).",
            "Yocto",
        ),
        WorkloadRecipe(
            "SQUASHFS", "y", "compat", "Standard read-only embedded rootfs.", "Yocto"
        ),
        WorkloadRecipe("SQUASHFS_XZ", "y", "compat", "XZ for squashfs.", "Yocto"),
        WorkloadRecipe(
            "KERNEL_ZSTD",
            "y",
            "perf",
            "Self-decompressing kernel, smaller flash footprint.",
            "Yocto kernel-size",
        ),
        WorkloadRecipe(
            "DEBUG_INFO_NONE", "y", "surface", "Don't ship debug bloat.", "Yocto"
        ),
        WorkloadRecipe("FTRACE", "n", "surface", "Drop ftrace surface.", "Yocto"),
    ],
)


# ── public API ────────────────────────────────────────────────────────────


workload_recipes: dict[str, WorkloadProfileSpec] = {
    spec.profile: spec
    for spec in (_DESKTOP, _LAPTOP, _SERVER, _VM_GUEST, _REALTIME, _EMBEDDED)
}
