"""Tier-3 remedy: a headless coding agent generates a source patch.

When flag surgery can't fix an FTBFS (a genuine source incompatibility
with clang/ThinLTO/-O3), hand the failing source tree + build log to a
headless `claude` or `codex` and capture the minimal patch it makes —
validated by a real rebuild like every other remedy
(docs/CLANG_PGO_EXPERIMENT.md Phase 4, remedy escalation ladder in
WORLD.md).

The pattern (from the research): run the agent in a disposable
git-init'd copy of the unpacked source, let it edit in place, capture
the patch as `git diff` (don't ask the model to hand-format quilt —
the orchestrator serializes the diff to debian/patches/ via
builder.apply_patches). Bound it with the CLI's own turn/budget caps
plus an OS timeout; nothing persists without a green rebuild.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Discipline shared by both backends — the same honesty the triage agent
# uses: minimal change, keep the optimization, stay in the tree, then stop.
_SYSTEM_RULES = (
    "Make the MINIMAL source change that fixes the build failure. Do NOT "
    "disable the optimization (-O3 / LTO / -march). Do NOT edit files "
    "outside this directory. Do NOT run the build. When the edit is "
    "complete, stop and print one line summarizing the change."
)


@dataclass
class AgentResult:
    backend: str
    ok: bool  # produced a non-empty diff and didn't error
    gave_up: bool  # ran clean but produced no diff
    patch: str  # `git diff` of the tree — the quilt source
    summary: str  # final agent message
    transcript: str  # raw json/jsonl, for provenance
    cost_usd: float | None
    exit_code: int
    timed_out: bool = False


def build_fix_prompt(
    *, source: str, version: str, flags_desc: str, log_tail: str
) -> str:
    return (
        f"The Debian source package {source} {version} fails to build from "
        f"source with these appended compiler flags:\n  {flags_desc}\n\n"
        "It builds fine for the distro with stock flags, so the failure is a "
        "source incompatibility with the optimization, not a distro bug. "
        "Below is the tail of the build log. Make the minimal source fix.\n\n"
        f"--- build log tail ---\n{log_tail}\n"
    )


def _claude_argv(
    prompt: str, *, model: str, max_turns: int, budget: float
) -> list[str]:
    return [
        "claude",
        "--bare",
        "-p",
        prompt,
        "--append-system-prompt",
        _SYSTEM_RULES,
        "--model",
        model,
        "--output-format",
        "json",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        "Read Edit Bash(grep *) Bash(rg *) Bash(sed -n *) Bash(cat *)",
        "--max-turns",
        str(max_turns),
        "--max-budget-usd",
        f"{budget:.2f}",
        "--no-session-persistence",
    ]


def _claude_parse(stdout: str, stderr: str) -> tuple[str, float | None]:
    try:
        obj = json.loads(stdout)
        return str(obj.get("result", "")), obj.get("total_cost_usd")
    except (json.JSONDecodeError, AttributeError):
        return stdout.strip()[:500], None


def _codex_argv(
    prompt: str, tree: Path, *, model: str, summary_file: Path, **_: object
) -> list[str]:
    return [
        "codex",
        "exec",
        prompt,
        "-C",
        str(tree),
        "--sandbox",
        "workspace-write",
        "--model",
        model,
        "--json",
        "-o",
        str(summary_file),
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
    ]


def _codex_parse(stdout: str, stderr: str) -> tuple[str, float | None]:
    # JSONL events; final agent_message + usage on turn.completed.
    summary, cost = "", None
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "item.completed":
            item = ev.get("item", {})
            if item.get("type") == "agent_message":
                summary = str(item.get("text", summary))
        # token usage is reported; cost computed by the caller's rate card
    return summary or stdout.strip()[:500], cost


_DEFAULT_MODELS = {"claude": "sonnet", "codex": "gpt-5.4"}


def _ensure_git(tree: Path) -> None:
    """git-init + baseline commit so `git diff` captures the agent's edit."""
    if (tree / ".git").exists():
        return
    env = {**os.environ, "GIT_AUTHOR_NAME": "autokernel", "GIT_AUTHOR_EMAIL": "a@b",
           "GIT_COMMITTER_NAME": "autokernel", "GIT_COMMITTER_EMAIL": "a@b"}  # fmt: skip
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "baseline", "--no-verify"],
    ):
        subprocess.run(argv, cwd=tree, env=env, capture_output=True, check=False)


def run_coding_agent(
    backend: str,
    tree: Path,
    prompt: str,
    *,
    model: str | None = None,
    max_turns: int = 25,
    max_budget_usd: float = 3.0,
    timeout_s: int = 900,
    runner=subprocess.run,
) -> AgentResult:
    """Run a headless coding agent in ``tree`` and capture its patch.

    ``runner`` is injectable for testing (defaults to subprocess.run)."""
    if backend not in ("claude", "codex"):
        raise ValueError(f"unknown agent backend {backend!r}")
    if shutil.which(backend) is None:
        return AgentResult(
            backend, False, False, "", f"{backend} CLI not found", "", None, 127
        )
    model = model or _DEFAULT_MODELS[backend]
    _ensure_git(tree)
    summary_file = tree / ".ak-agent-summary.txt"
    if backend == "claude":
        argv = _claude_argv(
            prompt, model=model, max_turns=max_turns, budget=max_budget_usd
        )
        parse = _claude_parse
    else:
        argv = _codex_argv(prompt, tree, model=model, summary_file=summary_file)
        parse = _codex_parse

    timed_out = False
    try:
        proc = runner(
            argv, cwd=str(tree), capture_output=True, text=True, timeout=timeout_s
        )
        stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = (
            (exc.stdout or b"").decode(errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = "timed out"
        rc, timed_out = 124, True

    summary, cost = parse(stdout, stderr)
    # The patch is whatever changed in the tree (excluding our scratch file).
    diff = subprocess.run(
        ["git", "diff", "--", ".", f":(exclude){summary_file.name}"],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    patch = diff.stdout
    ok = rc == 0 and not timed_out and bool(patch.strip())
    gave_up = rc == 0 and not patch.strip()
    return AgentResult(
        backend=backend,
        ok=ok,
        gave_up=gave_up,
        patch=patch,
        summary=summary,
        transcript=stdout,
        cost_usd=cost,
        exit_code=rc,
        timed_out=timed_out,
    )


def save_patch(patch: str, dest_dir: Path, source: str) -> Path:
    """Persist a generated patch under the world patches/ dir, keyed by
    source name, for reuse + provenance."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{source}.patch"
    path.write_text(patch, encoding="utf-8")
    return path
