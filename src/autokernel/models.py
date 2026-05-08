"""Typed data model for hardware snapshots and config proposals.

A Snapshot is the canonical input to every downstream stage: deterministic
resolver, LLM agent, policy filter. Bash collectors produce raw files;
``autokernel.snapshot.load`` parses them into a Snapshot.

Proposals (RemovalProposal, ConfigDiff) are the canonical *output* of the
analysis pipeline.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ── snapshot inputs ─────────────────────────────────────────────────────────


class PciDevice(_Frozen):
    slot: str
    vendor_id: str
    device_id: str
    class_id: str | None = None
    subsystem_vendor: str | None = None
    subsystem_device: str | None = None
    driver: str | None = None
    modules: list[str] = Field(default_factory=list)
    description: str | None = None


class UsbDevice(_Frozen):
    bus: str
    device: str
    vendor_id: str
    product_id: str
    description: str | None = None


class Modalias(_Frozen):
    """A modalias string from /sys/devices/**/modalias.

    Format: ``<bus>:<encoded>``, e.g. ``pci:v00008086d000056A6sv...``,
    ``usb:v1D6Bp0003...``, ``acpi:PNP0103:``, ``platform:i8042``.
    """

    sysfs_path: str
    raw: str
    bus: str  # 'pci', 'usb', 'acpi', 'platform', 'hid', 'of', ...


class LoadedModule(_Frozen):
    name: str
    size: int
    used_by_count: int
    used_by: list[str] = Field(default_factory=list)


class CpuInfo(_Frozen):
    vendor_id: str  # e.g. 'GenuineIntel', 'AuthenticAMD'
    cpu_family: int | None = None
    model: int | None = None
    model_name: str | None = None
    flags: list[str] = Field(default_factory=list)
    cores: int = 1


class Mount(_Frozen):
    source: str
    target: str
    fstype: str
    options: str = ""


class BlockDevice(_Frozen):
    name: str
    fstype: str | None = None
    type: str | None = None  # 'disk', 'part', 'crypt', 'lvm', ...


class NetworkLink(_Frozen):
    name: str
    driver: str | None = None
    operstate: str | None = None
    is_active: bool = False  # has IP / link up


class FirmwareLoad(_Frozen):
    """A firmware blob the kernel asked for or loaded."""

    name: str  # filename relative to /lib/firmware
    source: str  # 'dmesg', 'sys_class_firmware', 'modinfo'


class DkmsModule(_Frozen):
    name: str
    version: str
    kernel: str
    status: str  # 'installed', 'built', etc.


class BootContext(_Frozen):
    cmdline: str
    cmdline_params: dict[str, str] = Field(default_factory=dict)
    """Parsed key=value pairs from /proc/cmdline. Bare flags get empty-string
    values. Multi-occurrence keys are joined with ',' (rare in cmdline).

    High-value fields used downstream:
        * ``root``, ``rootfstype``, ``resume`` — load-bearing identifiers
        * ``cryptdevice`` — confirms LUKS in boot chain
        * ``module_blacklist`` — comma-separated; these modules are
          authoritatively NOT load-bearing (the user has chosen to disable).
        * ``init`` — non-systemd hosts (``init=/bin/openrc-init`` etc.).
    """
    blacklisted_modules: list[str] = Field(default_factory=list)

    secure_boot: bool | None = None
    efi: bool = False
    root_fstype: str | None = None
    boot_fstype: str | None = None
    luks_in_chain: bool = False  # / or /boot is on LUKS


class KernelInfo(_Frozen):
    release: str  # uname -r
    version: str  # uname -v
    arch: str  # uname -m


class Snapshot(_Frozen):
    """Complete observation of one host at one point in time."""

    schema_version: int = 1
    collected_at: datetime
    host: str
    snapshot_dir: Path

    kernel: KernelInfo
    cpu: CpuInfo
    boot: BootContext

    pci: list[PciDevice] = Field(default_factory=list)
    usb: list[UsbDevice] = Field(default_factory=list)
    modaliases: list[Modalias] = Field(default_factory=list)
    bound_drivers: dict[str, str] = Field(
        default_factory=dict
    )  # sysfs_path -> driver name

    loaded_modules: list[LoadedModule] = Field(default_factory=list)
    mounts: list[Mount] = Field(default_factory=list)
    block_devices: list[BlockDevice] = Field(default_factory=list)
    network: list[NetworkLink] = Field(default_factory=list)

    firmware: list[FirmwareLoad] = Field(default_factory=list)
    dkms: list[DkmsModule] = Field(default_factory=list)

    # Modules and firmware files known to be in the initramfs — these are
    # load-bearing for early boot regardless of what's loaded at runtime.
    initramfs_modules: list[str] = Field(default_factory=list)
    initramfs_firmware: list[str] = Field(default_factory=list)

    running_config_path: Path | None = None  # path to extracted .config
    modules_alias_path: Path | None = None
    modules_dep_path: Path | None = None
    modules_builtin_path: Path | None = None
    modules_builtin_modinfo_path: Path | None = None


# ── proposal outputs ────────────────────────────────────────────────────────


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ProposalSource(str, Enum):
    DETERMINISTIC = "deterministic"  # hardcoded rule
    MICROARCH = "microarch"  # CPU microarchitecture tuning (a deterministic
    # rule, but distinguished so renderers can
    # surface it under a "tuning" heading)
    LLM = "llm"  # pydantic-ai agent (the existing tristate-trim path)
    USER = "user"  # explicit user override
    # ── v0.13: multi-dimensional optimization ─────────────────────────
    # Each new dimension records the LLM agent that decided it so the
    # CLI can group / filter / explain by dimension. They flow through
    # the same RemovalProposal struct but ``proposed_value`` is the
    # selected choice option (CHOICE), 'y'/'n' (TOGGLE), or numeric/
    # string literal (TUNABLE).
    CHOICE = "choice"  # a Kconfig choice-group selection
    TOGGLE = "toggle"  # a bool feature toggle (perf/security tradeoff)
    TUNABLE = "tunable"  # int/string Kconfig value


class ReviewDecision(str, Enum):
    """Reviewer's verdict on a single proposal.

    * ``ACCEPT`` — apply the change (move from ``needs_review`` toward
      ``auto_applied`` semantics for the build stage).
    * ``REJECT`` — keep the symbol at its current value.
    * ``DEFER`` — undecided; stays in ``needs_review`` for next round.
    """

    ACCEPT = "accept"
    REJECT = "reject"
    DEFER = "defer"


class Reviewer(str, Enum):
    USER = "user"
    CLAUDE = "claude"
    POLICY = "policy"  # bulk-flag automation


class RemovalProposal(_Frozen):
    """Proposal to disable / mark-as-module / remove a CONFIG_* symbol."""

    config: str  # e.g. 'CONFIG_INTEL_IDLE'
    current_value: str  # 'y', 'm', 'n', or string for non-bool
    proposed_value: str  # 'n' for disable, 'm' for tristate-to-module
    reason: str  # human-readable rationale
    risk: RiskLevel
    confidence: float = Field(ge=0.0, le=1.0)
    source: ProposalSource
    evidence: list[str] = Field(default_factory=list)  # snapshot fields cited


class ReviewedProposal(_Frozen):
    """A proposal paired with a reviewer's decision and rationale.

    Produced by ``autokernel review``. The decision is what the build stage
    will honor; the underlying ``RemovalProposal`` is preserved for audit.
    """

    proposal: RemovalProposal
    decision: ReviewDecision
    reviewer: Reviewer
    rule: str | None = None  # e.g. "accept-low-risk", "reject-pattern:CRYPTO_*"
    note: str | None = None


class ReviewSet(_Frozen):
    """Output of ``autokernel review``: a complete decision over the
    proposals that were on the input ``ConfigDiff``'s ``needs_review`` list.

    ``accepted``/``rejected``/``deferred`` are mutually exclusive. The
    union covers every proposal that came in. ``base_diff_path`` records
    where the input proposal lived so the build stage can re-load context.
    """

    base_diff_path: Path
    accepted: list[ReviewedProposal] = Field(default_factory=list)
    rejected: list[ReviewedProposal] = Field(default_factory=list)
    deferred: list[ReviewedProposal] = Field(default_factory=list)


class ConfigDiff(_Frozen):
    """The full proposed change set, partitioned by autonomy decision.

    The four buckets are MUTUALLY EXCLUSIVE and account for every symbol
    that came out of the analysis pipeline:

    * ``auto_applied`` — proposed change WILL be applied by the build stage.
    * ``needs_review`` — proposed change requires explicit human approval.
    * ``annotations`` — explanation-only at autonomy=EXPLAIN; not actionable.
    * ``blocked``     — would-be proposals vetoed by the load-bearing
                       blocklist; never applied at any level.
    * ``not_considered`` — symbols that were *candidate trims* but never
                       made it through the LLM pipeline (e.g. truncated by
                       --max-candidates, or LLM skipped). These are NOT
                       silently dropped — the consumer sees they were
                       skipped and can request a follow-up pass.
    """

    base_config_path: Path
    autonomy: str  # AutonomyLevel value
    auto_applied: list[RemovalProposal] = Field(default_factory=list)
    needs_review: list[RemovalProposal] = Field(default_factory=list)
    blocked: list[tuple[RemovalProposal, str]] = Field(default_factory=list)
    annotations: list[RemovalProposal] = Field(default_factory=list)
    not_considered: list[str] = Field(default_factory=list)
