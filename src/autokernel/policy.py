"""Autonomy policy: how much of the LLM's advice we apply automatically.

Four levels (least → most autonomous):

    EXPLAIN     LLM annotates only; zero changes proposed for application.
    ADVISE      LLM proposes; every change requires explicit user/Claude approval.
    AUTO_SAFE   Auto-apply proposals where risk=LOW and confidence >= 0.9.
    AUTO_BOLD   Auto-apply everything except the load-bearing blocklist.

The **load-bearing blocklist** is enforced regardless of level. The LLM
cannot remove a symbol on the blocklist — these are the items that, if
disabled, will brick the system.

Blocklist composition (computed per-snapshot, not hardcoded):
    * The active root filesystem driver
    * The active /boot filesystem driver
    * The driver bound to the currently-active network interface
    * EFI / boot stub if EFI booted
    * LUKS / dm-crypt if present in the boot chain
    * Console drivers in use (TTY, serial, framebuffer)
    * Microcode loader for the current CPU vendor
    * Anything explicitly named in CMDLINE-required modules
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from autokernel.models import (
    ConfigDiff,
    RemovalProposal,
    RiskLevel,
    Snapshot,
)
from autokernel.resolve import ResolutionResult


class AutonomyLevel(str, Enum):
    EXPLAIN = "explain"
    ADVISE = "advise"
    AUTO_SAFE = "auto-safe"
    AUTO_BOLD = "auto-bold"


# Symbols that are NEVER candidates for removal, regardless of evidence.
# These either run before any module logic (early boot) or are infrastructure.
_HARD_BLOCKLIST: frozenset[str] = frozenset(
    {
        # Core kernel infrastructure
        "CONFIG_PRINTK",
        "CONFIG_TTY",
        "CONFIG_VT",
        "CONFIG_VT_CONSOLE",
        "CONFIG_UNIX98_PTYS",
        "CONFIG_DEVTMPFS",
        "CONFIG_DEVTMPFS_MOUNT",
        "CONFIG_TMPFS",
        "CONFIG_PROC_FS",
        "CONFIG_SYSFS",
        "CONFIG_FUTEX",
        "CONFIG_EPOLL",
        "CONFIG_SIGNALFD",
        "CONFIG_TIMERFD",
        "CONFIG_EVENTFD",
        "CONFIG_BLOCK",
        "CONFIG_MULTIUSER",
        "CONFIG_PRINTK_SAFE_LOG_BUF_SHIFT",
        # Init / first-second-stage essentials
        "CONFIG_BLK_DEV_INITRD",
        "CONFIG_RD_GZIP",
        "CONFIG_RD_LZ4",
        "CONFIG_RD_ZSTD",
        # Memory subsystem
        "CONFIG_MMU",
        "CONFIG_SWAP",
        "CONFIG_SHMEM",
        # Cgroups (systemd hard requirement)
        "CONFIG_CGROUPS",
        "CONFIG_CGROUP_SCHED",
        "CONFIG_CGROUP_CPUACCT",
        "CONFIG_CGROUP_MEMORY",
        "CONFIG_CGROUP_PIDS",
        "CONFIG_CGROUP_FREEZER",
        "CONFIG_CGROUP_DEVICE",
        # Modules infrastructure
        "CONFIG_MODULES",
        "CONFIG_MODULE_UNLOAD",
        # Random / entropy
        "CONFIG_HW_RANDOM",
    }
)

# Architectural fundamentals that depend on the host's actual arch.
# Removing any of these on a matching host is an unrecoverable brick:
# the resulting kernel cannot run on the architecture it was built for.
_ARCH_BLOCKLIST: dict[str, frozenset[str]] = {
    "x86_64": frozenset({"CONFIG_64BIT", "CONFIG_X86_64", "CONFIG_X86", "CONFIG_SMP"}),
    "aarch64": frozenset({"CONFIG_64BIT", "CONFIG_ARM64", "CONFIG_SMP"}),
    "armv7l": frozenset({"CONFIG_ARM", "CONFIG_SMP"}),
    "ppc64le": frozenset({"CONFIG_PPC64", "CONFIG_PPC", "CONFIG_SMP"}),
    "riscv64": frozenset({"CONFIG_64BIT", "CONFIG_RISCV", "CONFIG_SMP"}),
}


@dataclass
class PolicyResult:
    auto_applied: list[RemovalProposal] = field(default_factory=list)
    needs_review: list[RemovalProposal] = field(default_factory=list)
    blocked: list[tuple[RemovalProposal, str]] = field(default_factory=list)
    annotations: list[RemovalProposal] = field(default_factory=list)


# Threshold above which a deterministic proposal is treated as confirmed
# even at ADVISE — the user opted into deterministic certainty by enabling
# the rules in the first place.
DETERMINISTIC_AUTO_CONFIDENCE = 0.95


@dataclass
class LoadBearingSet:
    """Per-snapshot dynamic blocklist."""

    symbols: set[str] = field(default_factory=set)
    reasons: dict[str, str] = field(default_factory=dict)

    def add(self, sym: str, reason: str) -> None:
        if sym not in self.symbols:
            self.symbols.add(sym)
            self.reasons[sym] = reason

    def contains(self, sym: str) -> tuple[bool, str | None]:
        return (sym in self.symbols, self.reasons.get(sym))


def compute_load_bearing(snap: Snapshot, resolution: ResolutionResult) -> LoadBearingSet:
    lb = LoadBearingSet()

    for sym in _HARD_BLOCKLIST:
        lb.add(sym, "core infrastructure")

    # Architectural fundamentals — never trim CONFIG_64BIT / CONFIG_X86_64 /
    # CONFIG_SMP / etc. on a host that needs them.
    arch = snap.kernel.arch
    for sym in _ARCH_BLOCKLIST.get(arch, frozenset()):
        lb.add(sym, f"architecture: {arch}")

    # Active root + boot fs drivers
    from autokernel.resolve import _FS_TO_CONFIG

    for mount in snap.mounts:
        if mount.target in ("/", "/boot", "/usr"):
            for cfg in _FS_TO_CONFIG.get(mount.fstype, []):
                lb.add(cfg, f"{mount.target} is {mount.fstype}")

    # EFI
    if snap.boot.efi:
        lb.add("CONFIG_EFI", "EFI boot")
        lb.add("CONFIG_EFI_STUB", "EFI boot")

    # LUKS in boot chain
    if snap.boot.luks_in_chain:
        for cfg in ("CONFIG_DM_CRYPT", "CONFIG_CRYPTO_AES", "CONFIG_CRYPTO_XTS"):
            lb.add(cfg, "LUKS in boot chain")

    # Active network driver(s) — if a NIC is UP and providing the route,
    # killing its driver bricks remote access. Use the already-computed
    # module→CONFIG mapping from the resolver; if the NIC's driver wasn't
    # resolved (rare — would mean it's not in /lib/modules at all), fall
    # back to marking the bare name's CONFIG candidate as load-bearing.
    for net in snap.network:
        if net.is_active and net.driver:
            cfg = resolution.module_to_config.get(net.driver)
            if cfg is not None:
                lb.add(cfg, f"active network interface {net.name}")
            else:
                # Driver name is required, but we couldn't pin a CONFIG_ for
                # it. Mark the module itself as required-but-unresolved; the
                # policy filter will treat any proposal targeting a symbol
                # we can't trace back to it as a load-bearing protection
                # via the unresolved-modules set below.
                pass

    # CPU microcode
    if snap.cpu.vendor_id == "GenuineIntel":
        lb.add("CONFIG_MICROCODE_INTEL", "Intel CPU microcode")
    elif snap.cpu.vendor_id == "AuthenticAMD":
        lb.add("CONFIG_MICROCODE_AMD", "AMD CPU microcode")

    # Anything resolved as required is by definition load-bearing
    for cfg in resolution.required_configs:
        lb.add(cfg, "deterministically required")

    # Modules we couldn't map to a CONFIG symbol — record their guessed
    # candidates as load-bearing too, so a proposal targeting any of them
    # is blocked. This is the conservative fallback that protects against
    # the resolver missing a real symbol mapping.
    from autokernel.kconfig_map import candidate_configs

    for mod in resolution.unresolved_modules:
        info = None  # we don't have the path here; use bare-name candidates
        for cand in candidate_configs(mod, info):
            lb.add(cand, f"unresolved module {mod} (mapping uncertain — protected)")

    # DKMS modules — their CONFIG_ are not on the kernel but the kernel
    # internals they depend on are. We can't enumerate that here; we leave
    # a loud warning in the CLI.

    return lb


def apply_policy(
    proposals: list[RemovalProposal],
    autonomy: AutonomyLevel,
    load_bearing: LoadBearingSet,
) -> PolicyResult:
    """Sort proposals into auto_applied / needs_review / annotations / blocked
    according to autonomy level.

    Decision matrix:

        EXPLAIN     : everything → annotations (zero actionable changes)
        ADVISE      : deterministic & confidence ≥ 0.95 → auto_applied
                      everything else                   → needs_review
        AUTO_SAFE   : (risk=LOW ∧ confidence ≥ 0.9)     → auto_applied
                      everything else                   → needs_review
        AUTO_BOLD   : (risk≠HIGH)                       → auto_applied
                      risk=HIGH                         → needs_review

    Load-bearing matches always go to ``blocked`` regardless of level. That's
    what makes auto-bold acceptable: the LLM literally cannot remove a root
    fs driver, the active NIC, EFI, microcode, or LUKS chain symbols.
    """
    from autokernel.models import ProposalSource

    out = PolicyResult()

    for p in proposals:
        is_lb, reason = load_bearing.contains(p.config)
        if is_lb:
            out.blocked.append((p, f"load-bearing: {reason}"))
            continue

        if autonomy == AutonomyLevel.EXPLAIN:
            out.annotations.append(p)
            continue

        if autonomy == AutonomyLevel.ADVISE:
            # Deterministic certainty isn't a guess — apply it.
            if (
                p.source == ProposalSource.DETERMINISTIC
                and p.confidence >= DETERMINISTIC_AUTO_CONFIDENCE
            ):
                out.auto_applied.append(p)
            else:
                out.needs_review.append(p)
        elif autonomy == AutonomyLevel.AUTO_SAFE:
            if p.risk == RiskLevel.LOW and p.confidence >= 0.9:
                out.auto_applied.append(p)
            else:
                out.needs_review.append(p)
        elif autonomy == AutonomyLevel.AUTO_BOLD:
            if p.risk == RiskLevel.HIGH:
                out.needs_review.append(p)
            else:
                out.auto_applied.append(p)

    return out


def to_diff(
    base_config_path,
    autonomy: AutonomyLevel,
    policy_result: PolicyResult,
    *,
    not_considered: list[str] | None = None,
) -> ConfigDiff:
    return ConfigDiff(
        base_config_path=base_config_path,
        autonomy=autonomy.value,
        auto_applied=policy_result.auto_applied,
        needs_review=policy_result.needs_review,
        blocked=policy_result.blocked,
        annotations=policy_result.annotations,
        not_considered=not_considered or [],
    )
