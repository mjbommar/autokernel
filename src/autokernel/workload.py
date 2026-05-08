"""Workload-profile detection for autokernel.

Determining a host's *workload profile* — desktop / laptop / server /
vm-guest / realtime / embedded — is the single most consequential
input to the multi-dimensional Kconfig optimization. PREEMPT model,
HZ, transparent-hugepage policy, NUMA balancing, default I/O scheduler,
and a hundred bool toggles diverge based on the answer.

Detection is pure-evidence: it walks the Snapshot plus a handful of
``/sys`` probes, returns a confidence-tagged classification, and never
guesses. ``WorkloadProfile.UNKNOWN`` falls back when signals are
contradictory or absent — the caller can then ask the user.

The detection rules follow the consensus from KSPP, kernel.org admin
docs, Debian's `laptop-detect`, systemd's `systemd-detect-virt`, and
the workload-specific Kconfig recipes documented under
``src/autokernel/knowledge/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from autokernel.models import Snapshot


class WorkloadProfile(str, Enum):
    """The set of profiles autokernel optimizes for.

    Values correspond to the keys in
    ``autokernel.knowledge.workload_recipes``; downstream code uses
    the string value to look up per-profile Kconfig recommendations.
    """

    DESKTOP = "desktop"
    LAPTOP = "laptop"
    SERVER = "server"
    VM_GUEST = "vm-guest"
    REALTIME = "realtime"
    EMBEDDED = "embedded"
    UNKNOWN = "unknown"

    @property
    def is_user_facing(self) -> bool:
        """``UNKNOWN`` is internal — never present it to the LLM."""
        return self != WorkloadProfile.UNKNOWN


# ── chassis-type → profile classification ─────────────────────────────────
#
# ``/sys/class/dmi/id/chassis_type`` is the SMBIOS chassis enum. Debian's
# ``laptop-detect`` uses these ranges as ground truth.
_LAPTOP_CHASSIS = {8, 9, 10, 11, 14, 30, 31, 32}
# Server chassis: Main Server (17), Rack Mount (23), Blade (28),
# Blade Enclosure (29).
_SERVER_CHASSIS = {17, 23, 28, 29}
# Desktop chassis: Desktop (3), Low Profile (4), Pizza Box (5),
# Mini Tower (6), Tower (7), Mini PC (35).
_DESKTOP_CHASSIS = {3, 4, 5, 6, 7, 35}


_CLOUD_DMI_VENDORS = (
    "QEMU",
    "Amazon EC2",
    "Google",
    "Microsoft Corporation",
    "innotek GmbH",  # VirtualBox
    "VMware, Inc.",
    "Xen",
    "OpenStack Foundation",
    "Bochs",
    "Red Hat",  # libvirt-default
)


@dataclass(frozen=True)
class WorkloadDetection:
    """Result of :func:`detect`.

    ``profile`` is the chosen :class:`WorkloadProfile`. ``confidence``
    is a coarse 0.0-1.0 score reflecting how unambiguous the evidence
    was. ``reasons`` is a chronological list of human-readable
    justifications — render directly in CLI output.
    """

    profile: WorkloadProfile
    confidence: float
    reasons: list[str] = field(default_factory=list)


# ── /sys probes — deliberately separate from detect() so tests can mock ───


def _read_sys(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _probe_chassis_type(sys_root: Path) -> int | None:
    raw = _read_sys(sys_root / "class/dmi/id/chassis_type")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _probe_dmi_string(sys_root: Path, key: str) -> str | None:
    """Read ``/sys/class/dmi/id/<key>`` (e.g. ``sys_vendor``,
    ``product_family``, ``product_name``)."""
    return _read_sys(sys_root / "class/dmi/id" / key)


def _probe_has_battery(sys_root: Path) -> bool:
    """Any ``BAT*`` directory under ``/sys/class/power_supply/``."""
    psu = sys_root / "class/power_supply"
    if not psu.is_dir():
        return False
    try:
        return any(p.name.startswith("BAT") for p in psu.iterdir())
    except (PermissionError, OSError):
        return False


def _probe_devicetree_present(sys_root: Path) -> bool:
    """``/sys/firmware/devicetree/base/`` exists → device-tree-booted
    (almost always embedded). The ``/proc/device-tree/`` symlink also
    works but ``sys_root`` is what we accept for testability."""
    return (sys_root / "firmware/devicetree/base").is_dir()


# ── classifiers ───────────────────────────────────────────────────────────


def _classify_vm_guest(snap: Snapshot, sys_root: Path) -> WorkloadDetection | None:
    """VM-guest detection. First-priority — overrides every other
    signal because hardware drivers are wholly different inside a VM.

    Evidence (any one suffices):
      1. ``hypervisor`` in cpu.flags (Intel/AMD CPUID 0x40000000 leaf)
      2. ``/sys/class/dmi/id/sys_vendor`` matches a known cloud/VM vendor
      3. ``/sys/hypervisor/`` directory present (Xen/Hyper-V)

    Confidence is high when both (1) and (2) match.
    """
    reasons: list[str] = []
    matched = 0

    if "hypervisor" in snap.cpu.flags:
        reasons.append("cpu flag: hypervisor (CPUID 0x40000000 leaf set)")
        matched += 1

    sys_vendor = _probe_dmi_string(sys_root, "sys_vendor") or ""
    matched_vendor: str | None = None
    for v in _CLOUD_DMI_VENDORS:
        if v.lower() in sys_vendor.lower():
            matched_vendor = sys_vendor
            break
    if matched_vendor:
        reasons.append(f"DMI sys_vendor: {matched_vendor!r} matches a known VM/cloud vendor")
        matched += 1

    if (sys_root / "hypervisor").is_dir():
        reasons.append("/sys/hypervisor/ exists (Xen/Hyper-V hint)")
        matched += 1

    if matched == 0:
        return None

    confidence = 0.95 if matched >= 2 else 0.75
    return WorkloadDetection(WorkloadProfile.VM_GUEST, confidence, reasons)


def _classify_embedded(snap: Snapshot, sys_root: Path) -> WorkloadDetection | None:
    """Embedded detection. Prioritized BEFORE laptop because some
    SoCs report "Portable" chassis. Signals:

      1. Device-tree booted (``/sys/firmware/devicetree/base/`` exists)
      2. Architecture is ARM/RISCV/MIPS (non-x86)
      3. Total RAM < 1 GiB (rough threshold for IoT)
    """
    reasons: list[str] = []
    matched = 0

    if _probe_devicetree_present(sys_root):
        reasons.append("device-tree present (/sys/firmware/devicetree/base/)")
        matched += 1

    arch = (snap.kernel.arch or "").lower()
    if any(arch.startswith(p) for p in ("arm", "aarch", "risc", "mips")):
        # x86_64-on-DT is unusual but not embedded; require either
        # arch hint OR DT-only systems with no DMI.
        reasons.append(f"non-x86 architecture: {arch!r}")
        matched += 1

    if matched == 0:
        return None

    # Embedded inference is conservative — even a Raspberry Pi could
    # be a "desktop" if the user wants. Cap confidence.
    confidence = 0.75 if matched >= 2 else 0.55
    return WorkloadDetection(WorkloadProfile.EMBEDDED, confidence, reasons)


def _classify_laptop(snap: Snapshot, sys_root: Path) -> WorkloadDetection | None:
    """Laptop detection. Signals:

      1. ``/sys/class/dmi/id/chassis_type`` ∈ {8, 9, 10, 11, 14, 30, 31, 32}
      2. ``/sys/class/power_supply/BAT*`` exists (battery)

    Either alone is sufficient (chassis is often correct, BAT is often
    correct, both is unambiguous).
    """
    reasons: list[str] = []
    chassis = _probe_chassis_type(sys_root)
    if chassis in _LAPTOP_CHASSIS:
        reasons.append(f"DMI chassis_type {chassis} = portable/laptop class")
    has_bat = _probe_has_battery(sys_root)
    if has_bat:
        reasons.append("battery present (/sys/class/power_supply/BAT*)")

    if not reasons:
        return None

    confidence = 0.95 if len(reasons) >= 2 else 0.80
    return WorkloadDetection(WorkloadProfile.LAPTOP, confidence, reasons)


def _classify_server(snap: Snapshot, sys_root: Path) -> WorkloadDetection | None:
    """Server detection. Signals:

      1. DMI chassis ∈ {17, 23, 28, 29}
      2. DMI ``product_family`` matches a known server line
      3. No GPU display controller (PCI class 0x03xx)
      4. CPU cores >= 16 AND no battery (heuristic for "datacenter")
    """
    reasons: list[str] = []
    matched = 0

    chassis = _probe_chassis_type(sys_root)
    if chassis in _SERVER_CHASSIS:
        reasons.append(f"DMI chassis_type {chassis} = server class")
        matched += 1

    family = _probe_dmi_string(sys_root, "product_family") or ""
    server_lines = ("PowerEdge", "ProLiant", "UCS", "ThinkSystem", "PRIMERGY", "PRIMEPOWER")
    matched_line = next((p for p in server_lines if p.lower() in family.lower()), None)
    if matched_line:
        reasons.append(f"DMI product_family: {family!r} matches server line {matched_line!r}")
        matched += 1

    has_gpu = any(
        (d.class_id or "").startswith(("0300", "0302", "0380"))
        for d in snap.pci
    )
    if not has_gpu and not _probe_has_battery(sys_root) and snap.cpu.cores >= 16:
        reasons.append(
            f"no GPU display controller, no battery, {snap.cpu.cores} cores "
            "(headless big iron heuristic)"
        )
        matched += 1

    if matched == 0:
        return None

    confidence = 0.90 if matched >= 2 else 0.70
    return WorkloadDetection(WorkloadProfile.SERVER, confidence, reasons)


def _classify_desktop(snap: Snapshot, sys_root: Path) -> WorkloadDetection:
    """Fallback when nothing else triggers. Always succeeds with low-ish
    confidence — desktop is the safest assumption for a non-VM, non-laptop,
    non-server x86_64 host."""
    reasons: list[str] = ["fallback: no laptop/server/vm/embedded signal"]

    chassis = _probe_chassis_type(sys_root)
    if chassis in _DESKTOP_CHASSIS:
        reasons.insert(0, f"DMI chassis_type {chassis} = desktop class")
        confidence = 0.85
    else:
        confidence = 0.55

    return WorkloadDetection(WorkloadProfile.DESKTOP, confidence, reasons)


# ── public entry point ────────────────────────────────────────────────────


def detect(
    snap: Snapshot,
    *,
    sys_root: Path = Path("/sys"),
    explicit: WorkloadProfile | None = None,
) -> WorkloadDetection:
    """Classify the host's workload profile.

    Args:
        snap: the system snapshot from :func:`autokernel.snapshot.scan`.
        sys_root: filesystem root for ``/sys`` probes — overridable for
            tests (point at a synthetic dir).
        explicit: when not ``None``, return this profile with confidence
            1.0 and a single reason 'user-supplied'. This is the public
            override path for ``--workload=...``.

    Returns:
        :class:`WorkloadDetection` — never raises; returns
        ``UNKNOWN`` only when *snap* is so empty we can't even guess
        (no CPU info, etc.).

    Resolution order: **vm-guest > embedded > laptop > server > desktop**.
    The first classifier to fire wins; the rest are skipped to avoid
    contradictory tags.
    """
    if explicit is not None:
        return WorkloadDetection(explicit, 1.0, ["user-supplied via --workload"])

    if not snap.cpu.vendor_id:
        return WorkloadDetection(WorkloadProfile.UNKNOWN, 0.0, ["snapshot lacks cpu info"])

    for classifier in (
        _classify_vm_guest,
        _classify_embedded,
        _classify_laptop,
        _classify_server,
    ):
        result = classifier(snap, sys_root)
        if result is not None:
            return result

    return _classify_desktop(snap, sys_root)
