"""FTBFS triage agent: classify a failed build, propose a remedy.

The core W3 design bet (docs/WORLD.md): archive rebuilds under
aggressive flags always have a failure tail; Gentoo handles it with 20
years of hand-curated package.env. Here an LLM reads the build log and
proposes a *constrained* remedy, `override_check` statically rejects
hallucinations before any slow rebuild, a real rebuild validates the
remedy, and only then does it persist to ``exceptions.json``
(provenance=LLM_TRIAGE) — the accumulated package.env analogue.

Honesty rules baked into the prompt: a test failure that smells like a
miscompile (crypto/data-integrity tests, wrong-result assertions) must
NOT be silenced with nocheck — back off the optimization flags
instead, or defer. nocheck is only for environment-caused failures
(userns/chroot-sensitive tests) and known-flaky suites.

Caching follows agent.py: content-addressed on (model, prompt version,
source, flags hash, log digest) under ``<world_dir>/batches/triage/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from autokernel.world.models import (
    FailureClass,
    FtbfsVerdict,
    GlobalFlags,
    OverrideSource,
    PackageBuildRecord,
    PackageOverride,
)

DEFAULT_MODEL = os.environ.get("AUTOKERNEL_MODEL", "anthropic:claude-sonnet-4-6")
SYSTEM_PROMPT_VERSION = "v2"  # v2: force-gcc action + clang rule 5b
LOG_TAIL_LINES = 250


class _RemedyDraft(BaseModel):
    """Constrained remedy vocabulary — the LLM picks an action and
    parameters; it never writes a raw PackageOverride."""

    action: str = Field(
        description=(
            "'retry' (flake, no change) | 'nocheck' (skip tests: ONLY for "
            "environment-caused or flaky test failures) | 'strip-flags' "
            "(remove offending optimization flags) | 'force-gcc' (clang "
            "world only: build this package with gcc) | 'use-stock' (give "
            "up, keep distro binary) | 'defer' (needs human)"
        )
    )
    strip_flags: list[str] = Field(
        default_factory=list,
        description="For strip-flags: exact tokens to remove from the appended flags",
    )
    add_flags: list[str] = Field(
        default_factory=list,
        description="For strip-flags: replacement tokens (e.g. -O2 when stripping -O3)",
    )
    reason: str = Field(description="One sentence: why this remedy fits the evidence")


class _TriageDraft(BaseModel):
    failure_class: FailureClass
    remedy: _RemedyDraft
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(
        default_factory=list,
        description="2-5 lines quoted verbatim from the build log that justify the verdict",
    )


_SYSTEM_PROMPT = """\
You triage failed Debian package rebuilds. Context: packages are rebuilt
from source in clean sbuild chroots with appended compiler flags (shown
per case). A failure means the package built fine for the distro but
fails with OUR flags or in OUR unshare-chroot environment.

Classify the failure and pick ONE remedy. Decision rules, in order:

1. If the log shows a test asserting WRONG COMPUTED RESULTS (crypto,
   compression round-trips, checksums, numeric comparisons), treat it as
   a possible optimization miscompile (opt-miscompile). NEVER silence it
   with nocheck — the test is doing its job. Remedy: strip-flags (drop
   the most aggressive flag first: -flto* before -O3→-O2 before
   -march=native), or defer if you cannot tell which flag.

2. If tests fail on environment operations that unshare chroots can't do
   (mount, chown to arbitrary uids, setuid, /proc oddities, missing
   /dev nodes, xattr/ACLs on the build filesystem), classify
   test-failure and remedy nocheck — the code is fine, the sandbox is
   the problem. Quote the failing test's error as evidence.

3. If the same suite is known timing/parallelism sensitive (sleeps,
   races, port collisions) classify test-flake; remedy retry.

4. SIGILL / 'illegal instruction' → march-illegal-insn: strip-flags
   removing -march=native (the build machine's ISA leaked into
   binaries that run during the build/tests).

5. LTO symbol/section errors (lto1, ld plugin, symbol versioning) →
   lto-incompat: strip-flags removing the -flto token.

5b. When the world compiler is clang and the failure is clang-specific
   (error: unknown argument/warning option, gcc extensions like nested
   functions or computed gotos rejected, configure hard-coding gcc) →
   needs-gcc; remedy force-gcc. Don't use this for plain warnings
   promoted to errors that strip-flags could fix.

6. Unsatisfiable build-deps, version conflicts → dep-skew; remedy
   retry (the repo may have caught up) or defer.

7. debian/rules or packaging machinery errors unrelated to our flags →
   packaging; remedy defer.

8. When genuinely unsure, defer. A wrong nocheck ships broken binaries;
   a defer just leaves the stock package in place. Bias to safety.

Confidence: 0.9+ only when the log line directly names the cause.
Evidence MUST be verbatim quotes from the provided log.
"""

_agent: Agent[None, _TriageDraft] | None = None
_agent_model: str | None = None


def _get_agent(model: str) -> Agent[None, _TriageDraft]:
    global _agent, _agent_model
    if _agent is not None and _agent_model == model:
        return _agent
    _agent = cast(
        Agent[None, _TriageDraft],
        Agent(model, system_prompt=_SYSTEM_PROMPT, output_type=_TriageDraft),
    )
    _agent_model = model
    return _agent


# ── prompt assembly ─────────────────────────────────────────────────────────


def extract_log_tail(log_text: str, *, lines: int = LOG_TAIL_LINES) -> str:
    """Tail of the .build log with the noise stripped: apt fetch lines
    and sbuild banners drown the signal in the last N lines."""
    keep = [
        line
        for line in log_text.splitlines()
        if not re.match(r"^(Get:|Ign:|Hit:|Fetched |Reading |Building dep)", line)
    ]
    return "\n".join(keep[-lines:])


def build_prompt(
    record: PackageBuildRecord,
    log_tail: str,
    flags: GlobalFlags,
    effective_cflags: str,
    prior_exceptions: list[PackageOverride],
) -> str:
    prior = ""
    if prior_exceptions:
        prior_lines = [
            f"  {o.source_pkg}: action history → strip={o.strip_flags} "
            f"options={o.build_options} ({o.reason})"
            for o in prior_exceptions[:10]
        ]
        prior = "# Prior confirmed remedies for similar packages:\n" + "\n".join(
            prior_lines
        )
    return (
        f"# Package: {record.source} {record.archive_version}\n"
        f"# Appended flags: {effective_cflags}\n"
        f"# DEB_BUILD_OPTIONS extras: {' '.join(flags.build_options) or '(none)'}\n"
        f"# Builder note: {record.note or '-'}\n"
        f"{prior}\n"
        f"# Build log tail:\n{log_tail}"
    )


# ── static validation (the config_check analogue) ───────────────────────────

_FLAG_RE = re.compile(r"^-[A-Za-z][A-Za-z0-9=_+,.-]*$")
_VALID_ACTIONS = {"retry", "nocheck", "strip-flags", "force-gcc", "use-stock", "defer"}


def override_check(draft: _TriageDraft, *, effective_tokens: list[str]) -> list[str]:
    """Reject hallucinated remedies before any slow rebuild. Returns a
    list of problems; empty = sane."""
    problems: list[str] = []
    remedy = draft.remedy
    if remedy.action not in _VALID_ACTIONS:
        problems.append(f"unknown action {remedy.action!r}")
    if not remedy.reason.strip():
        problems.append("empty reason")
    if remedy.action == "strip-flags":
        if not remedy.strip_flags:
            problems.append("strip-flags action with empty strip_flags")
        for tok in remedy.strip_flags:
            if tok not in effective_tokens:
                problems.append(f"strip_flags token {tok!r} not in effective flags")
        for tok in remedy.add_flags:
            if not _FLAG_RE.match(tok):
                problems.append(f"add_flags token {tok!r} doesn't look like a flag")
    elif remedy.strip_flags or remedy.add_flags:
        problems.append(f"flags given but action is {remedy.action!r}")
    if (
        remedy.action == "nocheck"
        and draft.failure_class == FailureClass.OPT_MISCOMPILE
    ):
        problems.append("refusing nocheck for opt-miscompile (tests caught it)")
    return problems


def draft_to_verdict(source: str, draft: _TriageDraft) -> FtbfsVerdict:
    remedy: PackageOverride | None = None
    action = draft.remedy.action
    if action == "nocheck":
        remedy = PackageOverride(
            source_pkg=source,
            build_options=["nocheck"],
            reason=draft.remedy.reason,
            provenance=OverrideSource.LLM_TRIAGE,
        )
    elif action == "strip-flags":
        remedy = PackageOverride(
            source_pkg=source,
            strip_flags=draft.remedy.strip_flags,
            add_flags=draft.remedy.add_flags,
            reason=draft.remedy.reason,
            provenance=OverrideSource.LLM_TRIAGE,
        )
    elif action == "force-gcc":
        remedy = PackageOverride(
            source_pkg=source,
            force_compiler="gcc",
            reason=draft.remedy.reason,
            provenance=OverrideSource.LLM_TRIAGE,
        )
    elif action == "use-stock":
        remedy = PackageOverride(
            source_pkg=source,
            use_stock=True,
            reason=draft.remedy.reason,
            provenance=OverrideSource.LLM_TRIAGE,
        )
    elif action == "retry":
        # Empty override: same flags, the retry itself is the remedy.
        remedy = PackageOverride(
            source_pkg=source,
            reason=draft.remedy.reason,
            provenance=OverrideSource.LLM_TRIAGE,
        )
    # 'defer' → remedy stays None
    return FtbfsVerdict(
        source=source,
        failure_class=draft.failure_class,
        remedy=remedy,
        confidence=draft.confidence,
        evidence=draft.evidence,
    )


# ── exceptions table ────────────────────────────────────────────────────────


def exceptions_path(world_dir: Path) -> Path:
    return world_dir / "exceptions.json"


def load_exceptions(world_dir: Path) -> list[PackageOverride]:
    path = exceptions_path(world_dir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [PackageOverride.model_validate(o) for o in raw]
    except (ValueError, OSError):
        return []


def save_exception(world_dir: Path, override: PackageOverride) -> None:
    current = [
        o for o in load_exceptions(world_dir) if o.source_pkg != override.source_pkg
    ]
    current.append(override)
    exceptions_path(world_dir).write_text(
        json.dumps(
            [
                o.model_dump(mode="json")
                for o in sorted(current, key=lambda o: o.source_pkg)
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# ── cached triage call ──────────────────────────────────────────────────────


def _cache_key(model: str, record: PackageBuildRecord, log_digest: str) -> str:
    payload = json.dumps(
        {
            "model": model,
            "prompt_version": SYSTEM_PROMPT_VERSION,
            "source": record.source,
            "version": record.archive_version,
            "flags_hash": record.flags_hash,
            "log": log_digest,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def triage_record(
    record: PackageBuildRecord,
    *,
    log_text: str,
    flags: GlobalFlags,
    effective_cflags: str,
    world_dir: Path,
    model: str = DEFAULT_MODEL,
) -> tuple[FtbfsVerdict, list[str]]:
    """Triage one FTBFS record. Returns (verdict, override_check
    problems). Cached; a cache hit pays nothing."""
    log_tail = extract_log_tail(log_text)
    digest = hashlib.sha256(log_tail.encode()).hexdigest()[:16]
    cache_dir = world_dir / "batches" / "triage"
    cache_path = cache_dir / f"{_cache_key(model, record, digest)}.json"

    draft: _TriageDraft | None = None
    if cache_path.exists():
        try:
            draft = _TriageDraft.model_validate(
                json.loads(cache_path.read_text(encoding="utf-8"))
            )
        except (ValueError, OSError):
            draft = None
    if draft is None:
        prompt = build_prompt(
            record, log_tail, flags, effective_cflags, load_exceptions(world_dir)
        )
        draft = _get_agent(model).run_sync(prompt).output
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(draft.model_dump_json(indent=2) + "\n", encoding="utf-8")

    problems = override_check(draft, effective_tokens=effective_cflags.split())
    verdict = draft_to_verdict(record.source, draft)
    if problems:
        # Failed override_check → treat as defer, keep the classification.
        verdict = verdict.model_copy(update={"remedy": None})
    return verdict, problems
