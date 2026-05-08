"""Multi-axis optimization context for the v0.13+ propose pipeline.

The original v0.13 dimension agents took a single ``WorkloadProfile``
(desktop / laptop / server / vm-guest / realtime / embedded). That
answers *what kind of machine*, but not *how paranoid*, *how is the
kernel composed*, or *how willing to change defaults*. Real-world
intent factors along four orthogonal axes:

* :class:`WorkloadProfile`  — what the machine is for *(re-exported
  from autokernel.workload to keep imports small)*
* :class:`ThreatModel`      — how paranoid: permissive / balanced /
                              paranoid (KSPP-aligned)
* :class:`ModuleStrategy`   — how the kernel is composed: distro /
                              monolithic / modular
* :class:`Aggression`       — confidence threshold for LLM proposals:
                              conservative / balanced / aggressive

These are independent — a *paranoid + monolithic + aggressive desktop*
is meaningfully different from a *balanced + distro + conservative
desktop*.

:class:`OptimizationContext` bundles all four for a single propose
run. Presets (the ``PRESETS`` table) collapse common combinations
into single-name shorthand: ``--preset=hardened-server``,
``--preset=lean-static``, ``--preset=hyperoptimize``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from autokernel.workload import WorkloadProfile


class ThreatModel(str, Enum):
    """Security stance, traded against perf cost.

    * ``PERMISSIVE`` — max perf. Mitigations OFF where allowed, no
      INIT_ON_FREE, no UBSAN, no LOCKDOWN. Suitable for hosts where
      the operator already controls every workload (HPC clusters,
      hand-tuned single-tenant servers).

    * ``BALANCED`` *(default)* — KSPP minimum. PTI, RETPOLINE,
      FORTIFY_SOURCE, STACKPROTECTOR_STRONG, INIT_ON_ALLOC,
      hardened-usercopy, INIT_ON_FREE, lockdown=integrity if Secure
      Boot. Distro-friendly default.

    * ``PARANOID`` — KSPP+. RANDSTRUCT_FULL, ZERO_CALL_USED_REGS,
      lockdown=confidentiality, IMA appraise, MODULE_SIG_FORCE,
      BPF_UNPRIV_DEFAULT_OFF, drop X32_ABI / IA32_EMULATION /
      USERFAULTFD. Accepts measurable perf cost for raised
      exploit-development bar.
    """

    PERMISSIVE = "permissive"
    BALANCED = "balanced"
    PARANOID = "paranoid"


class ModuleStrategy(str, Enum):
    """How the kernel is composed (=y vs =m).

    * ``DISTRO`` *(default)* — like Ubuntu/Fedora. Most things =m.
      Smallest kernel image, biggest initramfs, slowest boot, easiest
      to load random hardware later.

    * ``MONOLITHIC`` — every load-bearing =m → =y. Bigger kernel
      image, no initramfs needed for root, fastest boot, smallest
      attack surface (fewer module-load paths). Good for VM guests,
      appliances, and hardened desktops.

    * ``MODULAR`` — every =y we can demote → =m. Smallest kernel
      image, biggest initramfs. Good for embedded with squashfs root
      or net-boot.
    """

    DISTRO = "distro"
    MONOLITHIC = "monolithic"
    MODULAR = "modular"


class Aggression(str, Enum):
    """Confidence threshold for LLM proposals.

    * ``CONSERVATIVE`` — propose a change only at confidence ≥ 0.85.
      Keep current value unless slam-dunk. Good for reviewing what
      autokernel *would* recommend without committing to broad change.

    * ``BALANCED`` *(default)* — confidence ≥ 0.65. The middle path:
      propose a change when the LLM has a clear reason but not when
      it's reaching.

    * ``AGGRESSIVE`` — confidence ≥ 0.40. Sweep every defensible
      change. Good when iterating with the closed-loop ``iterate``
      verb that auto-reverts regressions.
    """

    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"

    @property
    def confidence_floor(self) -> float:
        """Minimum confidence the LLM must report for a proposal to
        flow into the kfrag. Below the floor, the proposal is dropped."""
        return _AGGRESSION_FLOOR[self]


_AGGRESSION_FLOOR: dict[Aggression, float] = {
    Aggression.CONSERVATIVE: 0.85,
    Aggression.BALANCED: 0.65,
    Aggression.AGGRESSIVE: 0.40,
}


# ── the bundle ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OptimizationContext:
    """Four-axis configuration intent for one propose run.

    Construct via :func:`from_preset` for shortcuts, or directly with
    the four enums. The CLI exposes both: ``--preset=<name>`` for the
    common combinations, plus per-axis override flags
    (``--threat`` / ``--modules`` / ``--aggression``) that take
    precedence over preset values.
    """

    workload: WorkloadProfile
    threat: ThreatModel = ThreatModel.BALANCED
    modules: ModuleStrategy = ModuleStrategy.DISTRO
    aggression: Aggression = Aggression.BALANCED

    def render_for_prompt(self) -> str:
        """One-block summary the agents paste into their LLM prompt."""
        return (
            f"# OptimizationContext:\n"
            f"#   workload:   {self.workload.value}\n"
            f"#   threat:     {self.threat.value}\n"
            f"#   modules:    {self.modules.value}\n"
            f"#   aggression: {self.aggression.value}\n"
            f"#\n"
            f"# Conflict resolution: when axes disagree, threat wins for\n"
            f"# security symbols, workload wins for perf symbols, modules\n"
            f"# wins for tristate composition (=y vs =m), aggression sets\n"
            f"# the confidence floor (drop proposals below "
            f"{self.aggression.confidence_floor:.2f})."
        )


# ── presets ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Preset:
    """A named four-axis combination."""

    name: str
    description: str
    workload: WorkloadProfile
    threat: ThreatModel
    modules: ModuleStrategy
    aggression: Aggression

    def to_context(self) -> OptimizationContext:
        return OptimizationContext(
            workload=self.workload,
            threat=self.threat,
            modules=self.modules,
            aggression=self.aggression,
        )


PRESETS: dict[str, Preset] = {
    p.name: p
    for p in (
        Preset(
            "desktop",
            "General desktop, KSPP-balanced, distro composition.",
            WorkloadProfile.DESKTOP,
            ThreatModel.BALANCED,
            ModuleStrategy.DISTRO,
            Aggression.BALANCED,
        ),
        Preset(
            "gaming-desktop",
            "Performance-first desktop. Mitigations relaxed for max perf, "
            "monolithic to skip initramfs, aggressive proposals.",
            WorkloadProfile.DESKTOP,
            ThreatModel.PERMISSIVE,
            ModuleStrategy.MONOLITHIC,
            Aggression.AGGRESSIVE,
        ),
        Preset(
            "paranoid-desktop",
            "Hardened desktop: KSPP+ mitigations, distro modules for "
            "vendor compat, balanced aggression.",
            WorkloadProfile.DESKTOP,
            ThreatModel.PARANOID,
            ModuleStrategy.DISTRO,
            Aggression.BALANCED,
        ),
        Preset(
            "laptop",
            "Battery-aware laptop. KSPP-balanced, distro modules.",
            WorkloadProfile.LAPTOP,
            ThreatModel.BALANCED,
            ModuleStrategy.DISTRO,
            Aggression.BALANCED,
        ),
        Preset(
            "paranoid-laptop",
            "Travel laptop: lockdown=confidentiality, MODULE_SIG_FORCE, "
            "USERFAULTFD off, etc.",
            WorkloadProfile.LAPTOP,
            ThreatModel.PARANOID,
            ModuleStrategy.DISTRO,
            Aggression.BALANCED,
        ),
        Preset(
            "server",
            "Datacenter server: throughput-first, KSPP-balanced, distro modules.",
            WorkloadProfile.SERVER,
            ThreatModel.BALANCED,
            ModuleStrategy.DISTRO,
            Aggression.BALANCED,
        ),
        Preset(
            "hardened-server",
            "Server with KSPP+ hardening, monolithic for tight surface.",
            WorkloadProfile.SERVER,
            ThreatModel.PARANOID,
            ModuleStrategy.MONOLITHIC,
            Aggression.BALANCED,
        ),
        Preset(
            "cloud-vm",
            "Cloud VM guest. virtio-everything, no thermal/power, monolithic, "
            "aggressive trims.",
            WorkloadProfile.VM_GUEST,
            ThreatModel.BALANCED,
            ModuleStrategy.MONOLITHIC,
            Aggression.AGGRESSIVE,
        ),
        Preset(
            "realtime",
            "PREEMPT_RT, no_hz_full, no debug, performance governor.",
            WorkloadProfile.REALTIME,
            ThreatModel.BALANCED,
            ModuleStrategy.MONOLITHIC,
            Aggression.BALANCED,
        ),
        Preset(
            "embedded",
            "Smallest kernel, monolithic, fixed hardware. squashfs/UBI root.",
            WorkloadProfile.EMBEDDED,
            ThreatModel.BALANCED,
            ModuleStrategy.MONOLITHIC,
            Aggression.AGGRESSIVE,
        ),
        Preset(
            "lean-static",
            "Workload-agnostic monolithic build. KSPP-balanced, aggressive "
            "trims. Pair with --workload to set the perf axis.",
            WorkloadProfile.DESKTOP,
            ThreatModel.BALANCED,
            ModuleStrategy.MONOLITHIC,
            Aggression.AGGRESSIVE,
        ),
        Preset(
            "lean-module",
            "Workload-agnostic modular build. Smallest kernel image; "
            "biggest initramfs. Good for net-boot or squashfs root.",
            WorkloadProfile.DESKTOP,
            ThreatModel.BALANCED,
            ModuleStrategy.MODULAR,
            Aggression.AGGRESSIVE,
        ),
        Preset(
            "hyperoptimize",
            "Permissive + monolithic + aggressive desktop. The 'I know "
            "what I'm doing' preset — every defensible perf change "
            "applied, no security headroom.",
            WorkloadProfile.DESKTOP,
            ThreatModel.PERMISSIVE,
            ModuleStrategy.MONOLITHIC,
            Aggression.AGGRESSIVE,
        ),
    )
}


def from_preset(name: str) -> OptimizationContext:
    """Resolve a preset name to an OptimizationContext.

    Raises :class:`KeyError` if the name isn't in :data:`PRESETS`.
    """
    return PRESETS[name].to_context()


def context_from_flags(
    *,
    preset: str | None,
    workload: str | None,
    threat: str | None,
    modules: str | None,
    aggression: str | None,
    detected_workload: WorkloadProfile | None = None,
) -> OptimizationContext:
    """Compose an OptimizationContext from CLI flags.

    Per-axis flags override preset values. ``detected_workload`` is the
    auto-detected workload when nothing explicit was passed; it's only
    used if neither ``preset`` nor ``workload`` was specified.

    Raises :class:`ValueError` for unknown enum values; lets
    :class:`KeyError` propagate for unknown presets so the CLI can
    surface a list of valid names.
    """
    base = (
        from_preset(preset)
        if preset is not None
        else OptimizationContext(
            workload=detected_workload or WorkloadProfile.DESKTOP,
        )
    )

    return OptimizationContext(
        workload=WorkloadProfile(workload) if workload else base.workload,
        threat=ThreatModel(threat) if threat else base.threat,
        modules=ModuleStrategy(modules) if modules else base.modules,
        aggression=Aggression(aggression) if aggression else base.aggression,
    )
