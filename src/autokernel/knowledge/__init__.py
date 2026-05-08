"""Curated Kconfig knowledge for the LLM-driven optimization pipeline.

This package holds the **structured, source-cited recommendations**
that the propose agents inject as system-prompt context. The data is
distilled from four research passes (KSPP, kernel.org admin docs,
Phoronix benchmarks, CachyOS/XanMod/Liquorix recipes, distro
configs) and is intentionally curated — not exhaustive — so the LLM
prompts stay short and high-signal.

Modules:
    workload_recipes — per-profile (desktop/laptop/server/vm-guest/
        realtime/embedded) CONFIG_* recommendations.
    perf_recipes    — performance-axis (PREEMPT, HZ, sched, MM, net).
    security_recipes — KSPP-aligned hardening (per threat-model).
    hw_rules        — deterministic CONFIG_* rules keyed off Snapshot
        evidence (no LLM needed).

Each module exports plain Python data (lists of dataclasses or dicts)
so the agents can compose the relevant slice into a prompt at call
time. Tests in ``tests/test_knowledge_*`` smoke-check the data shape
to catch typos.
"""

from autokernel.knowledge.workload_recipes import (
    WorkloadRecipe,
    workload_recipes,
)

__all__ = ["WorkloadRecipe", "workload_recipes"]
