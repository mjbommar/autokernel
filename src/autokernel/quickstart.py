"""``autokernel quickstart`` — guided walk-through for new users.

This verb is the gentle introduction: instead of asking the user to
remember verbs and flags, it runs the whole pipeline (preflight → scan
→ propose → review → apply → build prepare) interactively, prompting
before each step and giving great error messages when something goes
sideways.

Designed for someone who just ran the install one-liner and wants to
see what autokernel does without learning the CLI surface.

The implementation is intentionally **a thin orchestrator**: it imports
the same module functions the individual CLI verbs use, rather than
duplicating logic. That way every fix to the underlying verbs
automatically improves quickstart, and the quickstart can stay short.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

from autokernel import errors as err
from autokernel import preflight as preflight_mod
from autokernel import snapshot as snap_mod
from autokernel.distro import detect as detect_distro


@dataclass
class Step:
    """One stage of the walk-through."""

    name: str
    description: str
    why: str
    """Two-line case for why we run this step now. Shown to the user."""


_STEPS: list[Step] = [
    Step(
        name="preflight",
        description="Check the host has what it needs (~0.5s)",
        why=(
            "Catches missing build tools / dev libs before they bite you "
            "30 minutes into a build. Distro-aware fix hints."
        ),
    ),
    Step(
        name="scan",
        description="Snapshot hardware/system inventory",
        why=(
            "Walks /sys, lspci, lsusb, lsmod, /proc/cmdline, etc. Output is a "
            "typed JSON snapshot the next steps consume."
        ),
    ),
    Step(
        name="propose",
        description="LLM-judged trim proposal",
        why=(
            "Deterministic rules (CPU/GPU vendor mismatch) plus an LLM agent "
            "that reasons about which symbols are unused on this host. Per-batch "
            "cached, so re-runs are free if you change nothing."
        ),
    ),
    Step(
        name="review",
        description="Bulk-decision rules → Kconfig fragment",
        why=(
            "Turns the proposal into actionable accept/reject decisions. "
            "We default to --accept-recommended and reject crypto/security "
            "(opt in to trim those manually)."
        ),
    ),
    Step(
        name="apply",
        description="Merge kfrag into final.config (with safety check)",
        why=(
            "Refuses to write the final config if the merge would disable a "
            "load-bearing symbol that's currently working."
        ),
    ),
]


def run(
    snapshot_dir: Path,
    *,
    console: Console,
    err_console: Console,
    yes: bool = False,
    skip_llm: bool = False,
) -> None:
    """Walk the user through the pipeline.

    ``yes`` skips the per-step confirmation prompt (useful in CI or for
    a one-shot demo). ``skip_llm`` runs propose with --skip-llm — costs
    nothing, surfaces only the deterministic rules; useful when no API
    key is configured or the user is offline.
    """
    snapshot_dir = snapshot_dir.expanduser().resolve()
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    distro = detect_distro()
    console.print(Panel.fit(
        f"[bold]autokernel quickstart[/bold]\n"
        f"  host: {distro.pretty_name or distro.id} (family={distro.family.value})\n"
        f"  snapshot dir: {snapshot_dir}\n\n"
        f"[dim]This walks you through the pipeline step by step. "
        f"Each step prompts before it runs; Ctrl-C exits cleanly.[/dim]",
        title="Welcome",
    ))

    # ── 1. preflight ────────────────────────────────────────────────────
    if not _confirm(_STEPS[0], yes=yes, console=console):
        return
    pf_run = preflight_mod.run_checks(tags={"always", "build"}, distro=distro)
    from autokernel.cli import _render_preflight  # avoid circular at import time
    _render_preflight(pf_run, distro=distro, for_="build")
    if pf_run.has_failures:
        # Surface the one-shot fix verb instead of just "go install stuff yourself."
        console.print(
            "\n[bold]fix in one step:[/bold]\n"
            "  autokernel install-deps --for build --execute   "
            "[dim](installs the right packages for your distro)[/dim]"
        )
        if not _confirm_continue_after_warnings(console=console, yes=yes):
            console.print("[yellow]bailing — run install-deps and rerun.[/yellow]")
            return

    # ── 2. scan ────────────────────────────────────────────────────────
    if not _confirm(_STEPS[1], yes=yes, console=console):
        return
    _run_scan(snapshot_dir, console=console, err_console=err_console)

    # ── 3. propose ─────────────────────────────────────────────────────
    if not _confirm(_STEPS[2], yes=yes, console=console):
        return
    if not skip_llm:
        skip_llm = _maybe_skip_llm(console=console)
    _run_propose(snapshot_dir, skip_llm=skip_llm, console=console, err_console=err_console)

    # ── 4. review ──────────────────────────────────────────────────────
    if not _confirm(_STEPS[3], yes=yes, console=console):
        return
    _run_review(snapshot_dir, console=console, err_console=err_console)

    # ── 5. apply ───────────────────────────────────────────────────────
    if not _confirm(_STEPS[4], yes=yes, console=console):
        return
    _run_apply(snapshot_dir, console=console, err_console=err_console)

    # ── done ───────────────────────────────────────────────────────────
    final = snapshot_dir / "final.config"
    kfrag = snapshot_dir / "auto.kfrag"
    console.print(Panel.fit(
        f"[green]✓ done[/green]\n\n"
        f"  snapshot:   {snapshot_dir}\n"
        f"  proposal:   {snapshot_dir / 'proposal.json'}\n"
        f"  kfrag:      {kfrag}\n"
        f"  final cfg:  {final}\n\n"
        f"[bold]next:[/bold] to actually compile a kernel:\n"
        f"  autokernel fetch-source\n"
        f"  autokernel build {snapshot_dir} --kernel-source <path>            # prepare only\n"
        f"  autokernel build {snapshot_dir} --kernel-source <path> --execute  # ~30 min\n"
        f"  autokernel install {snapshot_dir} --execute   # one-shot probation boot\n",
        title="Quickstart complete",
    ))


# ── prompts ────────────────────────────────────────────────────────────────


def _confirm(step: Step, *, yes: bool, console: Console) -> bool:
    console.rule(f"step: {step.name}")
    console.print(f"[bold]{step.description}[/bold]")
    console.print(f"[dim]{step.why}[/dim]\n")
    if yes:
        return True
    try:
        return Confirm.ask("run this step?", default=True, console=console)
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]exiting on user request[/yellow]")
        return False


def _confirm_continue_after_warnings(*, console: Console, yes: bool) -> bool:
    console.print(
        "\n[yellow]some checks failed. continue anyway?[/yellow] "
        "[dim](some later steps may fail too)[/dim]"
    )
    if yes:
        return True
    try:
        return Confirm.ask("continue?", default=False, console=console)
    except (KeyboardInterrupt, EOFError):
        return False


def _maybe_skip_llm(*, console: Console) -> bool:
    """If no API key is set, ask whether to skip the LLM step rather than
    just failing in `propose`."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        return False
    console.print(
        "[yellow]no ANTHROPIC_API_KEY or OPENAI_API_KEY in environment.[/yellow]\n"
        "  [dim]The LLM stage proposes config trims based on hardware evidence "
        "and typically costs <$0.05 per run.[/dim]"
    )
    try:
        return not Confirm.ask(
            "configure a key first? (n = skip LLM and use only deterministic rules)",
            default=True,
            console=console,
        )
    except (KeyboardInterrupt, EOFError):
        return True


# ── step runners ───────────────────────────────────────────────────────────


def _run_scan(snapshot_dir: Path, *, console: Console, err_console: Console) -> None:
    """Reuse the same code path the scan verb uses, capturing errors."""
    from autokernel.cli import _SCRIPTS_DIR
    import subprocess

    collector = _SCRIPTS_DIR / "collect.sh"
    if not collector.exists():
        raise err.fail(
            "collector script missing",
            why=f"expected at {collector}",
            fix="reinstall via the install.sh one-liner, or git pull in your clone",
        )

    args = ["bash", str(collector), str(snapshot_dir)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise err.fail(
            "collector failed",
            why=result.stderr.splitlines()[-1] if result.stderr else "unknown",
            fix="see scripts/collect.sh; report the error if it's reproducible",
        )

    snap = snap_mod.load(snapshot_dir)
    (snapshot_dir / "snapshot.json").write_text(snap.model_dump_json(indent=2, exclude_none=True))
    console.print(
        f"[green]✓ scanned[/green]   "
        f"pci={len(snap.pci)}  usb={len(snap.usb)}  "
        f"modules loaded={len(snap.loaded_modules)}  modaliases={len(snap.modaliases)}"
    )


def _run_propose(
    snapshot_dir: Path, *, skip_llm: bool, console: Console, err_console: Console
) -> None:
    from autokernel.agent import deterministic_proposals, propose as llm_propose
    from autokernel.policy import (
        AutonomyLevel, apply_policy, compute_load_bearing, to_diff,
    )
    from autokernel.resolve import _running_config_symbols, candidate_trims, resolve

    snap = snap_mod.load(snapshot_dir)
    if not snap.running_config_path:
        raise err.hint_no_running_config(snapshot_dir)

    resolution = resolve(snap)
    candidates_syms = candidate_trims(snap, resolution)
    running = _running_config_symbols(snap.running_config_path)
    candidates = [(s, running.get(s, "y")) for s in candidates_syms]

    det = deterministic_proposals(snap, candidates)
    handled = {p.config for p in det}
    llm_pool = [(s, v) for s, v in candidates if s not in handled]

    not_considered: list[str] = []
    llm: list = []
    if skip_llm or not llm_pool:
        not_considered = [s for s, _ in llm_pool]
    else:
        cap = 200  # quickstart caps to keep cost predictable
        if len(llm_pool) > cap:
            console.print(f"[dim]capping LLM pool at {cap} symbols (the rest are deferred)[/dim]")
            not_considered = [s for s, _ in llm_pool[cap:]]
            llm_pool = llm_pool[:cap]
        cache_dir = snapshot_dir / "batches"
        with console.status(f"[cyan]asking LLM about {len(llm_pool)} candidates…[/cyan]"):
            try:
                llm = llm_propose(snap, llm_pool, cache_dir=cache_dir)
            except Exception as e:  # noqa: BLE001 — report any provider error gracefully
                console.print(f"[yellow]LLM call failed:[/yellow] {e}")
                console.print("[dim]continuing with deterministic-only proposals[/dim]")
                not_considered = [s for s, _ in llm_pool]
                llm = []

    all_proposals = det + llm
    load_bearing = compute_load_bearing(snap, resolution)
    pr = apply_policy(all_proposals, AutonomyLevel.ADVISE, load_bearing)
    diff = to_diff(snap.running_config_path, AutonomyLevel.ADVISE, pr, not_considered=not_considered)
    (snapshot_dir / "proposal.json").write_text(diff.model_dump_json(indent=2))

    console.print(
        f"[green]✓ proposed[/green]   "
        f"auto-applied={len(pr.auto_applied)}  "
        f"needs review={len(pr.needs_review)}  "
        f"blocked={len(pr.blocked)}  "
        f"not considered={len(not_considered)}"
    )


def _run_review(snapshot_dir: Path, *, console: Console, err_console: Console) -> None:
    import json

    from autokernel.kfrag import write_kfrag
    from autokernel.models import ConfigDiff, Reviewer
    from autokernel.review import (
        apply_rules,
        preset_accept_recommended,
        reject_subsystems_rule,
    )

    proposal_path = snapshot_dir / "proposal.json"
    if not proposal_path.exists():
        raise err.hint_missing_proposal(snapshot_dir, proposal_path)

    diff = ConfigDiff.model_validate(json.loads(proposal_path.read_text()))
    rules = [
        reject_subsystems_rule(["crypto", "security", "kasan"]),
        *preset_accept_recommended(),
    ]
    rs = apply_rules(
        diff.needs_review, rules,
        base_diff_path=proposal_path, reviewer=Reviewer.POLICY,
    )
    (snapshot_dir / "review.json").write_text(rs.model_dump_json(indent=2))
    header = write_kfrag(
        snapshot_dir / "auto.kfrag", rs,
        snapshot_dir=snapshot_dir, autonomy=diff.autonomy,
    )
    console.print(
        f"[green]✓ reviewed[/green]   "
        f"accepted={len(rs.accepted)}  rejected={len(rs.rejected)}  deferred={len(rs.deferred)}\n"
        f"  kfrag: disables={header.n_disable} demotions={header.n_demote}"
    )


def _run_apply(snapshot_dir: Path, *, console: Console, err_console: Console) -> None:
    from autokernel.merge import merge_kfrag, validate_load_bearing
    from autokernel.policy import compute_load_bearing
    from autokernel.resolve import resolve

    snap = snap_mod.load(snapshot_dir)
    kfrag_path = snapshot_dir / "auto.kfrag"
    if not kfrag_path.exists():
        raise err.hint_missing_kfrag(snapshot_dir, kfrag_path)

    merged_text, report = merge_kfrag(snap.running_config_path, kfrag_path)
    resolution = resolve(snap)
    lb = compute_load_bearing(snap, resolution)
    base_text = snap.running_config_path.read_text()
    findings = validate_load_bearing(merged_text, dict(lb.reasons), base_config_text=base_text)
    if findings:
        raise err.hint_load_bearing_brick([f.symbol for f in findings])

    (snapshot_dir / "final.config").write_text(merged_text)
    console.print(
        f"[green]✓ applied[/green]   "
        f"overrides={len(report.overrides)}  unchanged={report.base_only_count}"
    )
