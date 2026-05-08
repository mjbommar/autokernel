"""Per-module-strategy guidance.

Module strategy isn't a list of CONFIG_* values like workload/threat
recipes. It's *meta-policy* the LLM applies to tristate symbols:

* ``distro``     — keep current y/m balance (Ubuntu/Fedora-like).
* ``monolithic`` — when a tristate is load-bearing, prefer =y.
                    Drop unused subsystems entirely (=n).
* ``modular``    — when a tristate is =y but not boot-path, demote to =m.
                    Modules can be loaded on demand; smaller kernel image.

This module exports two pieces:

1. :data:`module_strategies` — short prompt-block per strategy
   describing the policy the LLM should apply to tristates.
2. :data:`STRATEGY_HINTS` — symbol-level hints for symbols where the
   "right" answer is unambiguous regardless of workload, e.g.
   ``CONFIG_MODULES`` itself (always =y for distro/modular, optional
   =n for hardened-monolithic appliance).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleStrategySpec:
    strategy: str  # 'distro' | 'monolithic' | 'modular'
    description: str
    policy: str  # prompt-ready guidance for the LLM


_DISTRO = ModuleStrategySpec(
    strategy="distro",
    description="Distro-default composition (Ubuntu/Fedora-like).",
    policy=(
        "Keep the current y/m balance. Most things should remain =m so "
        "modules can be loaded on demand and unused ones never use RAM. "
        "Only propose =m → =y when a symbol is on the boot path AND "
        "would require initramfs work to load early. Only propose "
        "=y → =m when a symbol is clearly not boot-critical and is "
        "rarely-used."
    ),
)


_MONOLITHIC = ModuleStrategySpec(
    strategy="monolithic",
    description="Monolithic build: load-bearing =m → =y, unused → =n.",
    policy=(
        "Prefer =y over =m for everything load-bearing on this host. "
        "The goal is to skip initramfs entirely — every driver needed "
        "to mount root must be built-in. For non-load-bearing symbols, "
        "prefer =n (drop the surface) over =m (keep the module loadable). "
        "When in doubt about =y vs =m for a symbol that IS used: "
        "prefer =y. The image grows, but boot is faster and the "
        "module-loading attack surface shrinks."
    ),
)


_MODULAR = ModuleStrategySpec(
    strategy="modular",
    description="Modular composition: kernel image is minimal.",
    policy=(
        "Prefer =m over =y for everything not strictly boot-path. The "
        "goal is the smallest possible kernel image, with everything "
        "else in modules loaded by initramfs/udev. Demote =y → =m "
        "where possible. For unused symbols, =n (drop entirely) is "
        "still preferred over =m. Useful for net-boot, embedded, "
        "or squashfs-rootfs setups."
    ),
)


module_strategies: dict[str, ModuleStrategySpec] = {
    spec.strategy: spec
    for spec in (_DISTRO, _MONOLITHIC, _MODULAR)
}


# ── symbol-level overrides ────────────────────────────────────────────────


# Hints the LLM can apply regardless of workload — these are universal
# given a strategy. Format: (symbol → {strategy: value}).
STRATEGY_HINTS: dict[str, dict[str, str]] = {
    # MODULES itself: monolithic appliances may want this off entirely
    # (eg embedded with all-builtin), but for desktop/server keep it on.
    "MODULES": {
        "distro": "y",
        "monolithic": "y",
        "modular": "y",
    },
    # initramfs-related: monolithic kernels can drop initramfs.
    "BLK_DEV_INITRD": {
        "monolithic": "n",  # if the kernel has everything built in
    },
    # Module signing: enabled regardless under non-permissive threat,
    # but the strategy decides FORCE.
    "MODULE_SIG_FORCE": {
        # Monolithic with no out-of-tree modules can SIG_FORCE
        # without compat issues.
        "monolithic": "y",
    },
}
