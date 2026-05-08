"""Per-threat-model Kconfig recipes.

Distilled from KSPP (Kernel Self-Protection Project), kernel.org
Documentation/security/, the kernel-hardening-checker rule database,
and Lockheed/CIS hardening guidance. Each entry includes the perf
cost so the LLM can reason about the perf-vs-security trade.

Threat levels:

* ``permissive`` — perf-first; mitigations OFF where allowed.
* ``balanced``  — KSPP minimum; the consumer-grade default.
* ``paranoid``  — KSPP+; raised exploit-development bar at measurable
                  perf cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ThreatRecipe:
    symbol: str  # bare CONFIG_NAME (no prefix)
    value: str
    rationale: str  # short — perf cost stated when relevant
    source: str  # short citation


@dataclass(frozen=True)
class ThreatProfileSpec:
    threat: str  # 'permissive' | 'balanced' | 'paranoid'
    description: str
    recipes: list[ThreatRecipe] = field(default_factory=list)


# ── permissive ───────────────────────────────────────────────────────────


_PERMISSIVE = ThreatProfileSpec(
    threat="permissive",
    description=(
        "Performance-first. Mitigations off where allowed; no INIT_ON_FREE; "
        "no UBSAN; no LOCKDOWN. Suitable for hosts where the operator "
        "already controls every workload (HPC, hand-tuned single-tenant "
        "servers, sandboxed labs)."
    ),
    recipes=[
        ThreatRecipe("INIT_ON_FREE_DEFAULT_ON",  "n",
            "3-5% typical, ~40% on zfs send. Drop on perf-sensitive.",
            "LWN init_on_alloc/free"),
        ThreatRecipe("STACKLEAK",                "n",
            "Stack wipe on syscall return: ~1-2% syscall overhead. Skip for max perf.",
            "outflux v5.3"),
        ThreatRecipe("UBSAN_SANITIZE_ALL",       "n",
            "Whole-kernel UBSAN; perceptible overhead. Skip.",
            "KSPP"),
        ThreatRecipe("ZERO_CALL_USED_REGS",      "n",
            "~1% function-call overhead. Skip for perf.",
            "KSPP"),
        ThreatRecipe("RANDSTRUCT_FULL",          "n",
            "Compile-only cost; runtime neutral. Drop for distro compat.",
            "KSPP"),
        ThreatRecipe("SECURITY_LOCKDOWN_LSM",    "n",
            "Lockdown blocks BPF tracepoints, kprobes — hostile to ops/dev.",
            "kernel_lockdown(7)"),
        ThreatRecipe("MITIGATION_PAGE_TABLE_ISOLATION", "n",
            "Meltdown KPTI: 5-30% syscall hit (worse without PCID).",
            "Brendan Gregg KPTI"),
        ThreatRecipe("MITIGATION_RETHUNK",       "n",
            "Retbleed return-spec mitigation: 14-39% in some workloads.",
            "Phoronix Retbleed"),
    ],
)


# ── balanced (KSPP minimum) ──────────────────────────────────────────────


_BALANCED = ThreatProfileSpec(
    threat="balanced",
    description=(
        "KSPP minimum. PTI, RETPOLINE, FORTIFY_SOURCE, STACKPROTECTOR_STRONG, "
        "INIT_ON_ALLOC, hardened-usercopy, INIT_ON_FREE, lockdown=integrity "
        "if Secure Boot. The distro default."
    ),
    recipes=[
        # Speculation/side-channel
        ThreatRecipe("MITIGATION_PAGE_TABLE_ISOLATION", "y",
            "Meltdown KPTI; cost 5-30% syscalls. Mandatory.", "kernel.org PTI"),
        ThreatRecipe("MITIGATION_RETPOLINE",     "y",
            "Spectre v2; mostly absorbed by IBRS/eIBRS on modern CPUs.",
            "KSPP"),
        ThreatRecipe("MITIGATION_RETHUNK",       "y",
            "Retbleed; required on Skylake-family / Zen 1-2.",
            "LWN Retbleed"),
        ThreatRecipe("RANDOMIZE_BASE",           "y",
            "KASLR. Negligible runtime cost.", "KSPP"),
        ThreatRecipe("RANDOMIZE_MEMORY",         "y",
            "physmap/vmalloc/vmemmap base randomization.", "KSPP"),
        # Heap/stack
        ThreatRecipe("FORTIFY_SOURCE",           "y",
            "str/mem overflow checks. ~zero cost.", "KSPP"),
        ThreatRecipe("STACKPROTECTOR_STRONG",    "y",
            "Stack canaries; <1% overhead.", "KSPP"),
        ThreatRecipe("VMAP_STACK",               "y",
            "Guard pages + vmap'd stacks; defangs stack overruns.", "KSPP"),
        ThreatRecipe("RANDOMIZE_KSTACK_OFFSET_DEFAULT", "y",
            "Per-syscall stack-offset rand. <1%.", "KSPP"),
        ThreatRecipe("INIT_ON_ALLOC_DEFAULT_ON", "y",
            "Zero on alloc. <1% typical, up to 7% synthetic.",
            "LWN init_on_alloc"),
        ThreatRecipe("INIT_ON_FREE_DEFAULT_ON",  "y",
            "Zero on free. 3-5%; 40% synthetic. Tradeoff. KSPP recommends.",
            "KSPP"),
        ThreatRecipe("INIT_STACK_ALL_ZERO",      "y",
            "-ftrivial-auto-var-init=zero. Negligible.", "KSPP"),
        # Slab
        ThreatRecipe("SLAB_FREELIST_RANDOM",     "y",
            "Per-slab freelist randomization.", "KSPP"),
        ThreatRecipe("SLAB_FREELIST_HARDENED",   "y",
            "XOR-obfuscated freelist pointers.", "KSPP"),
        ThreatRecipe("RANDOM_KMALLOC_CACHES",    "y",
            "16 caller-keyed kmalloc caches.", "KSPP"),
        ThreatRecipe("SHUFFLE_PAGE_ALLOCATOR",   "y",
            "Buddy allocator randomization.", "KSPP"),
        ThreatRecipe("HARDENED_USERCOPY",        "y",
            "Bounds-check copy_to/from_user.", "KSPP"),
        ThreatRecipe("BUG_ON_DATA_CORRUPTION",   "y",
            "Panic on detected list/refcount corruption.", "KSPP"),
        # LSMs
        ThreatRecipe("SECURITY_YAMA",            "y",
            "ptrace_scope; near-free.", "KSPP"),
        ThreatRecipe("SECURITY_LANDLOCK",        "y",
            "Unprivileged sandboxing API.", "KSPP"),
        ThreatRecipe("SECURITY_LOCKDOWN_LSM",    "y",
            "Confidentiality/integrity boundary.", "kernel_lockdown(7)"),
        ThreatRecipe("SECURITY_LOCKDOWN_LSM_EARLY", "y",
            "Apply lockdown before other LSMs init.", "KSPP"),
        ThreatRecipe("SECURITY_DMESG_RESTRICT",  "y",
            "Default kernel.dmesg_restrict=1; hides kptrs.", "KSPP"),
        # Modules
        ThreatRecipe("MODULE_SIG",               "y",
            "Signed-module support.", "KSPP"),
        ThreatRecipe("STRICT_MODULE_RWX",        "y",
            "Enforce W^X on module text.", "KSPP"),
        ThreatRecipe("MODULE_SIG_SHA512",        "y",
            "Avoid SHA-1 for module signing.", "KSPP"),
        # Surface
        ThreatRecipe("STRICT_DEVMEM",            "y",
            "Restrict /dev/mem to devmem regions.", "KSPP"),
        ThreatRecipe("IO_STRICT_DEVMEM",         "y",
            "Block ioport overlap.", "KSPP"),
        ThreatRecipe("LEGACY_TIOCSTI",           "n",
            "Block TTY-injection escapes.", "KSPP"),
        ThreatRecipe("BPF_UNPRIV_DEFAULT_OFF",   "y",
            "Closes Spectre-v1-via-BPF and many UAF CVEs.",
            "KSPP / Ubuntu"),
        # Numeric
        ThreatRecipe("DEFAULT_MMAP_MIN_ADDR",    "65536",
            "Block NULL-deref-to-mmap.", "KSPP"),
    ],
)


# ── paranoid (KSPP+) ─────────────────────────────────────────────────────


_PARANOID = ThreatProfileSpec(
    threat="paranoid",
    description=(
        "KSPP+. RANDSTRUCT_FULL, ZERO_CALL_USED_REGS, lockdown="
        "confidentiality, IMA appraise, MODULE_SIG_FORCE, BPF_UNPRIV_OFF, "
        "drop X32_ABI/IA32_EMULATION/USERFAULTFD. Accepts measurable perf "
        "cost for raised exploit-development bar."
    ),
    recipes=[
        # All of BALANCED, plus extras:
        ThreatRecipe("RANDSTRUCT_FULL",          "y",
            "Compile-time struct field randomization. Build cost only; "
            "raises exploit-dev cost significantly.", "KSPP"),
        ThreatRecipe("ZERO_CALL_USED_REGS",      "y",
            "Wipe regs on function exit; ~1% overhead, blunts ROP.",
            "KSPP"),
        ThreatRecipe("STACKLEAK",                "y",
            "Wipe kernel stack on syscall return. ~1-2% syscall overhead.",
            "outflux v5.3"),
        ThreatRecipe("UBSAN_BOUNDS",             "y",
            "Array bounds checks; trap on violation.", "KSPP"),
        ThreatRecipe("UBSAN_TRAP",               "y",
            "Hardware trap on UBSAN finding (not just log).", "KSPP"),
        ThreatRecipe("KFENCE",                   "y",
            "Sampling UAF/OOB detector. ~free at sample=100ms.",
            "KSPP"),
        ThreatRecipe("LOCK_DOWN_KERNEL_FORCE_CONFIDENTIALITY", "y",
            "Hard lockdown; blocks BPF tracepoints, kprobes. Hostile to ops.",
            "kernel_lockdown(7)"),
        ThreatRecipe("MODULE_SIG_FORCE",         "y",
            "Refuse unsigned modules.", "KSPP"),
        ThreatRecipe("MODULE_SIG_ALL",           "y",
            "Sign during build.", "KSPP"),
        ThreatRecipe("IMA",                      "y",
            "File hashing on access; per-open hashing cost.", "Red Hat IMA"),
        ThreatRecipe("IMA_APPRAISE",             "y",
            "Enforce signatures against measurements.", "Gentoo IMA"),
        ThreatRecipe("EVM",                      "y",
            "Protects xattrs (security.ima); pairs with IMA.",
            "Gentoo EVM"),
        ThreatRecipe("KEXEC_SIG_FORCE",          "y",
            "If kexec on, require signed kernels.", "KSPP"),
        # Surface reduction (paranoid only)
        ThreatRecipe("X86_X32_ABI",              "n",
            "Frequent CVE source; rarely used. Disable.", "KSPP"),
        ThreatRecipe("IA32_EMULATION",           "n",
            "32-bit syscall surface; large CVE history. Server may need.",
            "KSPP"),
        ThreatRecipe("MODIFY_LDT_SYSCALL",       "n",
            "Wine/dosemu only.", "KSPP"),
        ThreatRecipe("X86_MSR",                  "n",
            "/dev/cpu/*/msr.", "KSPP"),
        ThreatRecipe("PROC_KCORE",               "n",
            "Crash-dump utility; otherwise leak source.", "KSPP"),
        ThreatRecipe("DEVMEM",                   "n",
            "/dev/mem; physmem read/write.", "KSPP"),
        ThreatRecipe("LEGACY_VSYSCALL_NONE",     "y",
            "Kill vsyscall entirely (modern glibc).", "KSPP"),
        ThreatRecipe("KEXEC",                    "n",
            "Replace running kernel; disable on desktop.", "KSPP"),
        ThreatRecipe("HIBERNATION",              "n",
            "Image-write attack vector.", "KSPP"),
        ThreatRecipe("ACPI_CUSTOM_METHOD",       "n",
            "Direct physmem write via AML.", "KSPP"),
        ThreatRecipe("COMPAT_BRK",               "n",
            "Disables brk-ASLR.", "KSPP"),
        ThreatRecipe("COMPAT_VDSO",              "n",
            "Defeats vDSO ASLR.", "KSPP"),
    ],
)


# ── public API ────────────────────────────────────────────────────────────


threat_recipes: dict[str, ThreatProfileSpec] = {
    spec.threat: spec
    for spec in (_PERMISSIVE, _BALANCED, _PARANOID)
}
