"""autokernel CLI — verb-by-verb pipeline.

    autokernel preflight [SNAPSHOT_DIR] [--for=...]
        Pre-flight checks: distro, build tools, libs, disk, RAM, etc.

    autokernel scan [OUTDIR]
        Run the bash collector, parse into a Snapshot, save snapshot.json.

    autokernel propose SNAPSHOT_DIR
        Run resolver + deterministic + LLM agent → proposal report.

    autokernel review SNAPSHOT_DIR
        Apply bulk decision rules to the proposal; emit review.json + kfrag.

    autokernel apply SNAPSHOT_DIR
        Merge the kfrag into the snapshot's running .config; emit final.config.

    autokernel build SNAPSHOT_DIR --kernel-source PATH
        Drop final.config into a kernel source tree; run olddefconfig;
        optionally build a package with `make <target>` (--execute).

    autokernel install SNAPSHOT_DIR [--package PATH] [--execute] [--commit]
        Distro-aware kernel package install with one-shot probation.

    autokernel rollback SNAPSHOT_DIR [--execute]
        Undo the most recent install: remove the package, regenerate config.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from autokernel import bootloader as bootloader_mod
from autokernel import boottest as boottest_mod
from autokernel import build as build_mod
from autokernel import errors as err
from autokernel import fetch as fetch_mod
from autokernel import install as install_mod
from autokernel import llm as llm_mod
from autokernel import preflight as preflight_mod
from autokernel import rollback as rollback_mod
from autokernel import snapshot as snap_mod
from autokernel.agent import deterministic_proposals, propose as llm_propose
from autokernel.distro import detect as detect_distro, spec_for
from autokernel.fetch import Method as FetchMethod
from autokernel.kfrag import write_kfrag
from autokernel.merge import merge_kfrag, validate_load_bearing
from autokernel.models import (
    ConfigDiff,
    RemovalProposal,
    Reviewer,
    ReviewSet,
)
from autokernel.policy import (
    AutonomyLevel,
    apply_policy,
    compute_load_bearing,
    to_diff,
)
from autokernel.resolve import candidate_trims, resolve
from autokernel.review import (
    AcceptRule,
    DeferRule,
    RejectRule,
    apply_rules,
    preset_accept_deterministic,
    preset_accept_low_risk,
    preset_accept_recommended,
    reject_pattern_rule,
    reject_subsystems_rule,
)
from autokernel.subsystem import group_by_subsystem

load_dotenv()

app = typer.Typer(
    add_completion=False,
    help="LLM-assisted minimal Linux kernel builder.",
    no_args_is_help=True,
)

# `autokernel config <show|test>` is a sub-app so the verb namespacing
# stays clean. The Typer add_typer call binds it under app at "config".
config_app = typer.Typer(
    add_completion=False,
    help="Inspect / test the LLM configuration.",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")

console = Console()
err_console = Console(stderr=True)


_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

# Autonomy levels that auto-apply proposals.
_AUTO_LEVELS = {AutonomyLevel.AUTO_SAFE, AutonomyLevel.AUTO_BOLD}


@app.command()
def scan(
    outdir: Annotated[Path | None, typer.Argument(help="Where to write the snapshot")] = None,
) -> None:
    """Collect hardware/system inventory into SNAPSHOT_DIR."""
    collector = _SCRIPTS_DIR / "collect.sh"
    if not collector.exists():
        err_console.print(f"[red]collector script missing:[/red] {collector}")
        raise typer.Exit(1)

    args = ["bash", str(collector)]
    if outdir:
        args.append(str(outdir))

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        err_console.print("[red]collector timed out after 5 min[/red]")
        raise typer.Exit(1) from None
    if result.returncode != 0:
        err_console.print(f"[red]collector failed:[/red]\n{result.stderr}")
        raise typer.Exit(result.returncode)

    snapdir = Path(result.stdout.strip())
    snap = snap_mod.load(snapdir)

    # Persist parsed JSON beside raw files for human inspection
    snapshot_json = snapdir / "snapshot.json"
    snapshot_json.write_text(snap.model_dump_json(indent=2, exclude_none=True))

    console.print(Panel.fit(
        f"[green]✓ snapshot saved[/green]\n"
        f"  dir:     {snapdir}\n"
        f"  pci:     {len(snap.pci)}\n"
        f"  usb:     {len(snap.usb)}\n"
        f"  modaliases: {len(snap.modaliases)}\n"
        f"  loaded modules: {len(snap.loaded_modules)}\n"
        f"  mounts:  {len(snap.mounts)}\n"
        f"  dkms:    {len(snap.dkms)}\n"
        f"  initramfs modules: {len(snap.initramfs_modules)}\n"
        f"  firmware loads: {len(snap.firmware)}\n"
        f"  running config: {snap.running_config_path}\n"
        f"\n[dim]next: autokernel propose {snapdir}[/dim]",
        title="autokernel scan",
    ))


def _validate_snapshot_dir(snapshot_dir: Path) -> None:
    if not snapshot_dir.is_dir() or not (snapshot_dir / "manifest").exists():
        raise err.hint_not_a_snapshot(snapshot_dir)


@app.command()
def propose(
    snapshot_dir: Annotated[Path, typer.Argument(help="Directory from `autokernel scan`")],
    autonomy: Annotated[AutonomyLevel, typer.Option(help="How aggressive to be")] = AutonomyLevel.ADVISE,
    skip_llm: Annotated[bool, typer.Option(help="Skip the LLM stage; only run deterministic rules")] = False,
    max_candidates: Annotated[int, typer.Option(help="Cap the number of candidates passed to the LLM")] = 600,
    llm_mode: Annotated[str, typer.Option("--llm-mode", help="Mode preset: auto|cheap|fast|quality. Picks the best model from a provider you have credentials for. Overridden by --model.")] = "auto",
    model: Annotated[str | None, typer.Option(help="Literal pydantic-ai model id (e.g. 'anthropic:claude-opus-4-7'). Overrides --llm-mode.")] = None,
    service_tier: Annotated[str | None, typer.Option(help="OpenAI service_tier: 'flex' | 'priority' | 'auto'")] = None,
    out: Annotated[Path | None, typer.Option(help="Write proposal JSON to this path")] = None,
    force_dkms: Annotated[bool, typer.Option(help="Allow auto-* autonomy even when DKMS modules are present (use only if you understand the rebuild risk)")] = False,
    no_cpu_tune: Annotated[bool, typer.Option("--no-cpu-tune", help="Don't propose CPU microarch tuning (CONFIG_M<arch>=y).")] = False,
) -> None:
    """Generate a proposed kernel config trim from a snapshot."""
    _validate_snapshot_dir(snapshot_dir)
    snap = snap_mod.load(snapshot_dir)

    if not snap.running_config_path:
        err_console.print(
            "[red]no running .config found in snapshot.[/red] "
            "Need /proc/config.gz or /boot/config-$(uname -r) at scan time."
        )
        raise typer.Exit(1)

    # ── DKMS gate ────────────────────────────────────────────────────────
    if snap.dkms:
        _render_dkms_panel(snap.dkms)
        if autonomy in _AUTO_LEVELS and not force_dkms:
            err_console.print(
                f"[red]DKMS modules detected[/red] — auto-* autonomy is unsafe without "
                f"verifying they rebuild against the new kernel. Refusing.\n"
                f"  Re-run with --autonomy=advise to review proposals manually, or "
                f"--force-dkms to override (you'll need to test the rebuild yourself)."
            )
            raise typer.Exit(3)

    # ── deterministic resolution ────────────────────────────────────────
    console.print("[dim]resolving deterministic keep-set…[/dim]")
    resolution = resolve(snap)
    console.print(
        f"  required modules: {len(resolution.required_modules)}  "
        f"required configs: {len(resolution.required_configs)}  "
        f"unresolved modules: {len(resolution.unresolved_modules)}  "
        f"unresolved modaliases: {len(resolution.unresolved_modaliases)}"
    )

    candidate_syms = candidate_trims(snap, resolution)
    console.print(f"  candidate trims: {len(candidate_syms)}")

    from autokernel.resolve import _running_config_symbols
    running = _running_config_symbols(snap.running_config_path)
    candidates = [(s, running.get(s, "y")) for s in candidate_syms]

    # ── deterministic proposals ─────────────────────────────────────────
    det = deterministic_proposals(snap, candidates)
    if no_cpu_tune:
        from autokernel.models import ProposalSource as _PS
        det = [p for p in det if p.source != _PS.MICROARCH]

    # Surface CPU tune separately so the user sees what we recommended for
    # their host (and can opt out with --no-cpu-tune if they don't want it).
    from autokernel.cpu import recommend as _cpu_recommend
    cpu_rec = _cpu_recommend(snap.cpu, snap.kernel.release)
    if cpu_rec is not None and not no_cpu_tune:
        arch, sym = cpu_rec
        console.print(
            f"[cyan]CPU tune:[/cyan] {snap.cpu.model_name or snap.cpu.vendor_id} → "
            f"[bold]{sym}=y[/bold] [dim](microarch: {arch.value})[/dim]"
        )
    console.print(f"[dim]deterministic proposals: {len(det)}[/dim]")

    # Remove deterministic-handled symbols from the LLM pile.
    handled = {p.config for p in det}
    llm_pool = [(s, v) for s, v in candidates if s not in handled]

    # Track everything we drop from consideration so the consumer sees it.
    not_considered: list[str] = []

    if skip_llm:
        not_considered = [s for s, _ in llm_pool]
    elif max_candidates and len(llm_pool) > max_candidates:
        console.print(
            f"[yellow]capping LLM pool at {max_candidates} "
            f"(full pool: {len(llm_pool)}; "
            f"{len(llm_pool) - max_candidates} symbols deferred)[/yellow]"
        )
        not_considered = [s for s, _ in llm_pool[max_candidates:]]
        llm_pool = llm_pool[:max_candidates]

    # ── LLM proposals ───────────────────────────────────────────────────
    llm: list = []
    if not skip_llm and llm_pool:
        # Resolve model + service tier via the LLM-config module so the user
        # gets a clear error when no provider is configured (instead of a
        # cryptic auth failure mid-batch).
        spec = model if model else llm_mode
        try:
            cfg = llm_mod.resolve(spec=spec, service_tier=service_tier)
        except llm_mod.NoProviderConfigured as e:
            raise err.fail(
                "no LLM provider configured",
                why=str(e),
                fix="set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env (see .env.example), or pass --skip-llm to use only deterministic rules",
                exit_code=1,
            )
        except llm_mod.ProviderNotAvailable as e:
            raise err.fail(
                f"the model {spec!r} requires {e.provider.value!r} but its API key is not set",
                fix=f"set one of: {', '.join(e.env_vars)}; or pick a different --llm-mode/--model",
                exit_code=1,
            )
        console.print(
            f"[dim]LLM: {cfg.model}[/dim]"
            + (f"  [dim]tier={cfg.service_tier}[/dim]" if cfg.service_tier else "")
        )

        cache_dir = snapshot_dir / "batches"
        with console.status(f"[cyan]asking LLM about {len(llm_pool)} candidates…[/cyan]"):
            def _progress(i: int, n: int, sz: int, *, cached: bool = False) -> None:
                tag = "[dim](cached)[/dim]" if cached else ""
                console.log(f"  batch {i}/{n} ({sz} symbols) {tag}")
            kwargs: dict = {"model": cfg.model}
            if cfg.service_tier:
                kwargs["service_tier"] = cfg.service_tier
            llm = llm_propose(
                snap, llm_pool, progress=_progress, cache_dir=cache_dir, **kwargs
            )
        console.print(f"[dim]LLM proposals: {len(llm)}  cache: {cache_dir}[/dim]")

    # ── policy filter ───────────────────────────────────────────────────
    all_proposals = det + llm
    load_bearing = compute_load_bearing(snap, resolution)
    pr = apply_policy(all_proposals, autonomy, load_bearing)
    diff = to_diff(snap.running_config_path, autonomy, pr, not_considered=not_considered)

    # ── render + persist ────────────────────────────────────────────────
    _render_diff(diff)

    out_path = out or snapshot_dir / "proposal.json"
    out_path.write_text(diff.model_dump_json(indent=2))
    console.print(f"\n[green]wrote {out_path}[/green]")


# ── rendering ──────────────────────────────────────────────────────────


def _render_dkms_panel(dkms: list) -> None:
    body = "\n".join(f"  {d.name}/{d.version}  for {d.kernel}  ({d.status})" for d in dkms)
    console.print(Panel(
        f"[bold]DKMS modules detected[/bold]\n{body}\n\n"
        f"[dim]These out-of-tree modules must rebuild against any new kernel "
        f"or the kernel will fail to boot/run them. Verify rebuilds before "
        f"installing a custom kernel.[/dim]",
        title="DKMS",
        border_style="yellow",
    ))


def _render_diff(diff) -> None:
    console.rule(f"proposal — autonomy={diff.autonomy}")

    def tbl(title: str, items, color: str) -> None:
        if not items:
            console.print(f"[dim]{title}: (none)[/dim]")
            return
        t = Table(title=title, show_lines=False, header_style=color)
        t.add_column("symbol", style=color, no_wrap=True)
        t.add_column("from", justify="center")
        t.add_column("to", justify="center")
        t.add_column("risk")
        t.add_column("conf", justify="right")
        t.add_column("src", justify="center")
        t.add_column("reason", overflow="fold")
        for p in items:
            t.add_row(
                p.config,
                p.current_value,
                p.proposed_value,
                p.risk.value,
                f"{p.confidence:.2f}",
                p.source.value[:3],
                p.reason,
            )
        console.print(t)

    tbl(f"auto-applied ({len(diff.auto_applied)})", diff.auto_applied, "green")
    tbl(f"needs review ({len(diff.needs_review)})", diff.needs_review, "yellow")
    if diff.annotations:
        tbl(f"annotations ({len(diff.annotations)})", diff.annotations, "cyan")

    if diff.blocked:
        t = Table(title=f"blocked by load-bearing policy ({len(diff.blocked)})", header_style="red")
        t.add_column("symbol", style="red")
        t.add_column("would-be reason")
        t.add_column("blocked because")
        for p, why in diff.blocked:
            t.add_row(p.config, p.reason, why)
        console.print(t)

    if diff.not_considered:
        console.print(
            f"\n[yellow]{len(diff.not_considered)} candidate symbol(s) were not considered[/yellow] "
            f"(LLM skipped or pool truncated). Re-run without --skip-llm or with a higher "
            f"--max-candidates to address them. Sample: "
            f"{', '.join(diff.not_considered[:5])}…"
        )


@app.command()
def review(
    snapshot_dir: Annotated[Path, typer.Argument(help="Directory from `autokernel scan` (must contain proposal.json)")],
    proposal: Annotated[Path | None, typer.Option(help="Override path to proposal.json")] = None,
    accept_recommended: Annotated[bool, typer.Option(help="Bulk-accept everything that isn't risk=high")] = False,
    accept_low_risk: Annotated[bool, typer.Option(help="Bulk-accept only risk=low")] = False,
    accept_deterministic: Annotated[bool, typer.Option(help="Bulk-accept only deterministic-source proposals")] = False,
    reject_subsystem: Annotated[list[str] | None, typer.Option("--reject-subsystem", help="Veto a whole subsystem (repeatable). Examples: crypto security kasan debug")] = None,
    reject_pattern: Annotated[list[str] | None, typer.Option("--reject-pattern", help="Veto symbols matching glob (repeatable). Example: 'CONFIG_DEBUG_*'")] = None,
    interactive: Annotated[bool, typer.Option("--interactive/--no-interactive", help="After bulk rules apply, open a TUI to step through remaining deferred items")] = False,
    out: Annotated[Path | None, typer.Option(help="Where to write review.json (default: SNAPSHOT_DIR/review.json)")] = None,
    kfrag: Annotated[Path | None, typer.Option(help="Where to write the kfrag (default: SNAPSHOT_DIR/auto.kfrag)")] = None,
    reviewer: Annotated[Reviewer, typer.Option(help="Identity to record on each decision")] = Reviewer.POLICY,
) -> None:
    """Apply bulk decision rules to a proposal and emit a kfrag.

    With --interactive, after the bulk rules apply, opens a Textual TUI
    to step through items still in `deferred`. The TUI uses single-key
    bindings (a/r/d to decide, j/k to navigate, w to save+exit, q to
    quit without saving). User decisions overwrite any existing rule
    label and are recorded with reviewer=USER.
    """
    _validate_snapshot_dir(snapshot_dir)

    proposal_path = proposal or snapshot_dir / "proposal.json"
    if not proposal_path.exists():
        err_console.print(
            f"[red]proposal not found:[/red] {proposal_path}\n"
            f"  Run `autokernel propose {snapshot_dir}` first."
        )
        raise typer.Exit(2)

    diff = ConfigDiff.model_validate(json.loads(proposal_path.read_text()))

    # ── compose rules in order ──────────────────────────────────────────
    rules: list = []
    rule_labels: list[str] = []

    if reject_subsystem:
        rules.append(reject_subsystems_rule(reject_subsystem))
        rule_labels.append(f"reject-subsystem={','.join(reject_subsystem)}")
    if reject_pattern:
        rules.append(reject_pattern_rule(reject_pattern))
        rule_labels.append(f"reject-pattern={','.join(reject_pattern)}")
    if accept_deterministic:
        rules.extend(preset_accept_deterministic())
        rule_labels.append("accept-deterministic")
    if accept_low_risk:
        rules.extend(preset_accept_low_risk())
        rule_labels.append("accept-low-risk")
    if accept_recommended:
        rules.extend(preset_accept_recommended())
        rule_labels.append("accept-recommended")

    if not rules:
        err_console.print(
            "[yellow]no rules supplied[/yellow] — every proposal will be deferred. "
            "Pass at least one of --accept-recommended / --accept-low-risk / "
            "--accept-deterministic / --reject-subsystem / --reject-pattern."
        )

    review_set = apply_rules(
        diff.needs_review,
        rules,
        base_diff_path=proposal_path,
        reviewer=reviewer,
    )

    # ── interactive review (TUI) ────────────────────────────────────────
    if interactive:
        from autokernel.tui import run_review

        edited = run_review(review_set, snapshot_dir=snapshot_dir)
        if edited is None:
            err_console.print(
                "[yellow]TUI exited without saving — no artifacts written.[/yellow]"
            )
            raise typer.Exit(130)  # SIGINT-style "user cancelled"
        review_set = edited

    # ── render summary ──────────────────────────────────────────────────
    _render_review(review_set, rule_labels + (["interactive"] if interactive else []))

    # ── persist artifacts ───────────────────────────────────────────────
    out_path = out or snapshot_dir / "review.json"
    out_path.write_text(review_set.model_dump_json(indent=2))

    kfrag_path = kfrag or snapshot_dir / "auto.kfrag"
    header = write_kfrag(
        kfrag_path,
        review_set,
        snapshot_dir=snapshot_dir,
        autonomy=diff.autonomy,
    )

    console.print(f"[green]wrote {out_path}[/green]")
    console.print(
        f"[green]wrote {kfrag_path}[/green]  "
        f"[dim]disables={header.n_disable} demotions={header.n_demote}[/dim]"
    )


def _render_review(rs: ReviewSet, rule_labels: list[str]) -> None:
    console.rule("review")
    if rule_labels:
        console.print(f"[dim]rules applied (in order): {', '.join(rule_labels)}[/dim]")

    counts = (
        f"[green]accepted: {len(rs.accepted)}[/green]  "
        f"[red]rejected: {len(rs.rejected)}[/red]  "
        f"[yellow]deferred: {len(rs.deferred)}[/yellow]"
    )
    console.print(counts)

    # Group accepted/deferred by subsystem so the user sees what got cut
    # and what's still pending.
    def _group_summary(label: str, items: list, color: str) -> None:
        if not items:
            return
        groups = group_by_subsystem(rp.proposal.config for rp in items)
        t = Table(title=f"{label} by subsystem ({len(items)})", header_style=color)
        t.add_column("subsystem", style=color)
        t.add_column("count", justify="right")
        t.add_column("examples", overflow="fold")
        for ss in sorted(groups, key=lambda s: -len(groups[s])):
            syms = groups[ss]
            t.add_row(ss, str(len(syms)), ", ".join(syms[:5]) + ("…" if len(syms) > 5 else ""))
        console.print(t)

    _group_summary("accepted", rs.accepted, "green")
    _group_summary("rejected", rs.rejected, "red")
    _group_summary("deferred (still need review)", rs.deferred, "yellow")


@app.command()
def apply(
    snapshot_dir: Annotated[Path, typer.Argument(help="Snapshot directory containing running_config + auto.kfrag")],
    kfrag: Annotated[Path | None, typer.Option(help="Override path to the kfrag (default: SNAPSHOT_DIR/auto.kfrag)")] = None,
    out: Annotated[Path | None, typer.Option(help="Where to write the merged config (default: SNAPSHOT_DIR/final.config)")] = None,
    no_validate: Annotated[bool, typer.Option(help="Skip the load-bearing validation pass (don't use unless you know what you're doing)")] = False,
) -> None:
    """Merge the snapshot's kfrag into the running .config, validate, write final.config."""
    _validate_snapshot_dir(snapshot_dir)
    snap = snap_mod.load(snapshot_dir)

    if not snap.running_config_path:
        raise err.hint_no_running_config(snapshot_dir)

    kfrag_path = kfrag or snapshot_dir / "auto.kfrag"
    if not kfrag_path.exists():
        raise err.hint_missing_kfrag(snapshot_dir, kfrag_path)

    merged_text, report = merge_kfrag(snap.running_config_path, kfrag_path)

    # ── validate load-bearing survived ───────────────────────────────────
    findings: list = []
    if not no_validate:
        resolution = resolve(snap)
        load_bearing = compute_load_bearing(snap, resolution)
        base_text = snap.running_config_path.read_text()
        findings = validate_load_bearing(
            merged_text, dict(load_bearing.reasons), base_config_text=base_text
        )
        if findings:
            _render_validation_failures(findings)
            err_console.print(
                f"[red]merge would brick the box[/red]: {len(findings)} load-bearing "
                f"symbol(s) end up disabled. Refusing to write final.config. "
                f"Re-run with --no-validate to override (not recommended)."
            )
            raise typer.Exit(4)

    out_path = out or snapshot_dir / "final.config"
    out_path.write_text(merged_text)

    _render_merge_report(report, out_path, kfrag_path)


def _render_merge_report(report, out_path: Path, kfrag_path: Path) -> None:
    console.rule("apply")
    console.print(
        f"  base:  {report.base_only_count} unchanged base symbols\n"
        f"  override: [yellow]{len(report.overrides)}[/yellow] symbols\n"
        f"  no-op:    [dim]{len(report.no_ops)}[/dim] (kfrag matched base)\n"
        f"  new:      [green]{len(report.fragment_only)}[/green] introduced by kfrag"
    )
    if report.overrides:
        t = Table(title="overrides (kfrag wins)", header_style="yellow")
        t.add_column("symbol", style="yellow")
        t.add_column("base", justify="center")
        t.add_column("→", justify="center")
        t.add_column("kfrag", justify="center")
        for sym, base_v, frag_v in report.overrides[:30]:
            t.add_row(sym, base_v, "→", frag_v)
        if len(report.overrides) > 30:
            t.add_row("…", "", "", f"({len(report.overrides) - 30} more)")
        console.print(t)
    console.print(f"\n[green]wrote {out_path}[/green]  [dim]from {kfrag_path}[/dim]")


def _render_validation_failures(findings) -> None:
    t = Table(title=f"load-bearing violations ({len(findings)})", header_style="red")
    t.add_column("symbol", style="red")
    t.add_column("status", justify="center")
    t.add_column("reason")
    for f in findings:
        t.add_row(f.symbol, f.actual_value, f.reason)
    console.print(t)


@app.command()
def build(
    snapshot_dir: Annotated[Path, typer.Argument(help="Snapshot directory containing final.config")],
    kernel_source: Annotated[Path, typer.Option("--kernel-source", help="Path to a kernel source tree (must contain a top-level Makefile)")],
    execute: Annotated[bool, typer.Option(help="Run the actual `make <target>` (default is prepare-only: drop config + olddefconfig)")] = False,
    jobs: Annotated[int | None, typer.Option(help="Parallel make jobs (default: $(nproc))")] = None,
    no_ccache: Annotated[bool, typer.Option(help="Disable ccache wrapping even when available")] = False,
    target: Annotated[str, typer.Option(help="Make target for --execute. Default 'auto' picks per distro: bindeb-pkg (Debian/Ubuntu), rpm-pkg (Fedora/SUSE), targz-pkg (Arch/Gentoo/other).")] = "auto",
    force_dkms: Annotated[bool, typer.Option(help="Allow --execute even with DKMS modules present")] = False,
) -> None:
    """Drop final.config into a kernel source tree, run olddefconfig, optionally build."""
    _validate_snapshot_dir(snapshot_dir)
    snap = snap_mod.load(snapshot_dir)

    final_config = snapshot_dir / "final.config"
    if not final_config.exists():
        err_console.print(
            f"[red]final.config not found:[/red] {final_config}\n"
            f"  Run `autokernel apply {snapshot_dir}` first."
        )
        raise typer.Exit(2)

    if execute and snap.dkms and not force_dkms:
        _render_dkms_panel(snap.dkms)
        err_console.print(
            f"[red]DKMS modules detected[/red] — refusing --execute without "
            f"--force-dkms. Verify they rebuild against the new kernel first."
        )
        raise typer.Exit(3)

    # ── prepare ──────────────────────────────────────────────────────────
    console.print(f"[dim]preparing {kernel_source} with {final_config}…[/dim]")
    try:
        prep = build_mod.prepare(
            source_dir=kernel_source,
            config_path=final_config,
            snapshot_dir=snapshot_dir,
        )
    except FileNotFoundError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None

    _render_step_results("prepare", prep.steps, prep.log_dir)

    if not prep.ok:
        err_console.print("[red]olddefconfig failed — see log dir.[/red]")
        raise typer.Exit(prep.steps[-1].exit_code)

    if not execute:
        console.print(Panel.fit(
            f"[green]✓ prepared[/green]\n"
            f"  source:  {prep.source_dir}\n"
            f"  config:  {prep.config_path}\n"
            f"  logs:    {prep.log_dir}\n"
            f"\n[dim]run with --execute to invoke `make {target}` "
            f"(this is the slow step; ~15-60 min).[/dim]",
            title="autokernel build (prepare-only)",
        ))
        return

    # ── execute ──────────────────────────────────────────────────────────
    if target == "auto":
        distro = detect_distro()
        target = spec_for(distro).build_target_default
        console.print(
            f"[dim]auto-selected target '{target}' for distro family '{distro.family.value}'[/dim]"
        )

    console.print(f"[cyan]running make {target}… (this is the slow step)[/cyan]")
    bres = build_mod.build(
        source_dir=prep.source_dir,
        snapshot_dir=snapshot_dir,
        jobs=jobs,
        use_ccache=not no_ccache,
        target=target,
        log_dir=prep.log_dir,
    )

    _render_step_results("build", bres.steps, bres.log_dir)

    if not bres.ok:
        err_console.print("[red]build failed — see log dir.[/red]")
        raise typer.Exit(bres.steps[-1].exit_code)

    if bres.deb_paths:
        body = "\n".join(f"  {p}" for p in bres.deb_paths)
        console.print(Panel.fit(
            f"[green]✓ built[/green]\n"
            f"  source:  {bres.source_dir}\n"
            f"  logs:    {bres.log_dir}\n\n"
            f"[bold]artifacts[/bold]\n{body}",
            title="autokernel build",
        ))
    else:
        err_console.print(
            "[yellow]build returned 0 but no linux-*.deb found in source parent[/yellow]"
        )


def _render_step_results(label: str, steps: list, log_dir: Path) -> None:
    t = Table(title=f"{label} steps", header_style="cyan")
    t.add_column("step")
    t.add_column("rc", justify="right")
    t.add_column("dur (s)", justify="right")
    t.add_column("logs")
    for s in steps:
        rc_color = "green" if s.ok else "red"
        t.add_row(
            s.name,
            f"[{rc_color}]{s.exit_code}[/{rc_color}]",
            f"{s.duration_s:.1f}",
            f"{s.stdout_path.name}, {s.stderr_path.name}",
        )
    console.print(t)
    console.print(f"[dim]log dir: {log_dir}[/dim]")


@app.command()
def preflight(
    snapshot_dir: Annotated[Path | None, typer.Argument(help="Optional snapshot dir; enables snapshot-aware checks")] = None,
    for_: Annotated[str, typer.Option("--for", help="Which verb's checks to run: all|scan|propose|apply|build|install")] = "all",
    strict: Annotated[bool, typer.Option(help="Treat WARN as failure for the exit code")] = False,
) -> None:
    """Run pre-flight system checks before invoking other verbs.

    Exit codes:
        0  no FAIL (and no WARN if --strict)
        1  FAIL present (or WARN with --strict)
    """
    snap = None
    if snapshot_dir is not None:
        if not (snapshot_dir / "manifest").exists():
            err_console.print(f"[red]not an autokernel snapshot:[/red] {snapshot_dir}")
            raise typer.Exit(2)
        snap = snap_mod.load(snapshot_dir)

    tag_map = {
        "all":      None,
        "scan":     {"always", "scan"},
        "propose":  {"always", "propose"},
        "apply":    {"always", "apply"},
        "build":    {"always", "build"},
        "install":  {"always", "install"},
        "boot-test": {"always", "boot-test"},
    }
    tags = tag_map.get(for_)
    if for_ not in tag_map:
        err_console.print(f"[red]unknown --for value:[/red] {for_!r}; use one of {list(tag_map)}")
        raise typer.Exit(2)

    distro = detect_distro()
    run = preflight_mod.run_checks(tags=tags, snapshot=snap, distro=distro)

    _render_preflight(run, distro=distro, for_=for_)

    if run.has_failures:
        raise typer.Exit(1)
    if strict and run.has_warnings:
        raise typer.Exit(1)


def _render_preflight(run, *, distro, for_: str) -> None:
    console.rule(f"preflight — for={for_}")
    console.print(f"[dim]host: {distro.pretty_name or distro.id} (family={distro.family.value})[/dim]")

    sev_color = {
        preflight_mod.Severity.PASS: "green",
        preflight_mod.Severity.WARN: "yellow",
        preflight_mod.Severity.FAIL: "red",
        preflight_mod.Severity.SKIP: "dim",
    }
    sev_glyph = {
        preflight_mod.Severity.PASS: "✓",
        preflight_mod.Severity.WARN: "!",
        preflight_mod.Severity.FAIL: "✗",
        preflight_mod.Severity.SKIP: "·",
    }

    t = Table(show_header=True, header_style="bold")
    t.add_column("", width=1)
    t.add_column("check", style="cyan")
    t.add_column("status")
    t.add_column("detail", overflow="fold")
    for r in run.results:
        col = sev_color[r.severity]
        msg = r.message
        if r.fix_hint:
            msg += f"\n[dim]→ {r.fix_hint}[/dim]"
        t.add_row(
            f"[{col}]{sev_glyph[r.severity]}[/{col}]",
            r.name,
            f"[{col}]{r.severity.value}[/{col}]",
            msg,
        )
    console.print(t)

    counts = {s: len(run.by_severity(s)) for s in preflight_mod.Severity}
    summary = (
        f"[green]{counts[preflight_mod.Severity.PASS]} pass[/green]  "
        f"[yellow]{counts[preflight_mod.Severity.WARN]} warn[/yellow]  "
        f"[red]{counts[preflight_mod.Severity.FAIL]} fail[/red]  "
        f"[dim]{counts[preflight_mod.Severity.SKIP]} skip[/dim]"
    )
    console.print(f"\n{summary}")


@app.command("fetch-source")
def fetch_source_cmd(
    kernel_version: Annotated[str | None, typer.Option("--kernel-version", help="Kernel version (e.g. 6.13.0). Defaults to running uname -r")] = None,
    method: Annotated[FetchMethod, typer.Option(help="Acquisition method")] = FetchMethod.AUTO,
    out: Annotated[Path, typer.Option("--out", help="Working directory for downloads + extraction")] = Path.home() / ".cache" / "autokernel" / "kernels",
    dry_run: Annotated[bool, typer.Option(help="Print the plan without executing")] = False,
) -> None:
    """Acquire a kernel source tree distro-aware (Debian apt-get source, Fedora SRPM, kernel.org tarball, …)."""
    import os

    distro = detect_distro()
    spec = spec_for(distro)
    release = kernel_version or os.uname().release

    out = out.expanduser()

    try:
        plan = fetch_mod.plan(
            distro=distro,
            spec=spec,
            release=release,
            working_dir=out,
            method=method,
        )
    except ValueError as e:
        err_console.print(f"[red]invalid plan:[/red] {e}")
        raise typer.Exit(2) from None

    _render_fetch_plan(plan, distro, dry_run)

    if dry_run:
        return

    if plan.needs_root and os.geteuid() != 0:
        err_console.print(
            f"[yellow]plan requires root for some steps; "
            f"prepend `sudo` to the commands above or rerun with sudo[/yellow]"
        )

    try:
        result = fetch_mod.fetch_source(
            distro=distro,
            spec=spec,
            release=release,
            working_dir=out,
            method=method,
        )
    except RuntimeError as e:
        err_console.print(f"[red]fetch failed:[/red] {e}")
        raise typer.Exit(1) from None

    if result.cached:
        console.print(f"[green]✓ source already at[/green] {result.target_dir}")
    else:
        console.print(f"[green]✓ source ready at[/green] {result.target_dir}")
    console.print(
        f"\n[dim]next: autokernel build SNAPSHOT_DIR --kernel-source {result.target_dir}[/dim]"
    )


def _render_fetch_plan(plan, distro, dry_run: bool) -> None:
    title = "fetch-source plan (dry-run)" if dry_run else "fetch-source plan"
    body_lines = [
        f"[bold]distro[/bold]: {distro.pretty_name or distro.id} (family={distro.family.value})",
        f"[bold]method[/bold]: {plan.method.value}",
        f"[bold]target[/bold]: {plan.target_dir}",
        f"[bold]needs root[/bold]: {plan.needs_root}",
        f"[dim]{plan.description}[/dim]",
        "",
        "[bold]commands[/bold]:",
    ]
    for cmd in plan.commands:
        body_lines.append(f"  $ {' '.join(cmd)}")
    console.print(Panel.fit("\n".join(body_lines), title=title))


# ── quickstart ─────────────────────────────────────────────────────────────


@app.command()
def quickstart(
    snapshot_dir: Annotated[Path, typer.Argument(help="Where to put the snapshot + artifacts")] = Path.home() / ".local" / "share" / "autokernel" / "quickstart",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't prompt; run all steps")] = False,
    skip_llm: Annotated[bool, typer.Option(help="Skip the LLM step (free; deterministic-only proposal)")] = False,
) -> None:
    """Guided walk-through: preflight → scan → propose → review → apply."""
    from autokernel import quickstart as quickstart_mod

    quickstart_mod.run(
        snapshot_dir,
        console=console,
        err_console=err_console,
        yes=yes,
        skip_llm=skip_llm,
    )


# ── install + rollback ─────────────────────────────────────────────────────


@app.command()
def install(
    snapshot_dir: Annotated[Path, typer.Argument(help="Snapshot directory containing built package(s)")],
    package: Annotated[list[Path] | None, typer.Option("--package", help="Path to a built package (.deb/.rpm/.pkg.tar.zst). Repeatable. If omitted, autokernel scans the snapshot dir for the most recent package.")] = None,
    kernel_entry: Annotated[str | None, typer.Option(help="GRUB menu entry name to arm for one-shot boot. If omitted, the arm step is skipped (run --commit later instead).")] = None,
    execute: Annotated[bool, typer.Option(help="Actually run the install. Default: dry-run.")] = False,
    commit: Annotated[bool, typer.Option(help="Promote the running kernel to permanent default (after a successful probation boot).")] = False,
    no_probation: Annotated[bool, typer.Option("--no-probation", help="Skip the one-shot grub-reboot step (NOT RECOMMENDED).")] = False,
    skip_preflight: Annotated[bool, typer.Option(help="Skip pre-flight checks (use only if you've verified them yourself).")] = False,
    skip_boot_test: Annotated[bool, typer.Option(help="Don't require a recent successful `boot-test` for this snapshot. Override only when you know what you're doing.")] = False,
) -> None:
    """Install a built kernel package with one-shot probation, or commit a successful boot."""
    import os

    _validate_snapshot_dir(snapshot_dir)
    distro = detect_distro()
    spec = spec_for(distro)
    bootloader = bootloader_mod.detect()

    # ── commit path ─────────────────────────────────────────────────────
    if commit:
        if kernel_entry is None:
            kernel_entry = os.uname().release
            console.print(f"[dim]commit: defaulting to running kernel '{kernel_entry}'[/dim]")
        plan = install_mod.build_commit_plan(
            distro=distro, bootloader=bootloader, kernel_entry=kernel_entry,
        )
        _render_install_plan(plan, distro=distro, bootloader=bootloader, mode="commit")
        if not plan.is_valid:
            raise err.hint_unsupported_bootloader(bootloader.kind.value)
        if not execute:
            console.print("\n[dim]dry-run; pass --execute to actually run the commands[/dim]")
            return
        if os.geteuid() != 0:
            raise err.hint_not_root("`autokernel install --commit --execute`")
        result = install_mod.execute(plan, snapshot_dir=snapshot_dir)
        _render_install_result(result)
        if not result.ok:
            raise typer.Exit(result.step_runs[-1].exit_code)
        return

    # ── install path: locate package(s) ─────────────────────────────────
    if not package:
        package = _find_latest_built_packages(snapshot_dir)
        if not package:
            raise err.fail(
                "no packages found and none provided",
                why=f"no built kernel packages under {snapshot_dir}",
                fix=(
                    f"run `autokernel build {snapshot_dir} --kernel-source PATH --execute` "
                    f"first, or pass --package PATH explicitly"
                ),
                exit_code=2,
            )

    # ── pre-flight (optional skip) ──────────────────────────────────────
    if not skip_preflight:
        snap = snap_mod.load(snapshot_dir)
        run = preflight_mod.run_checks(
            tags={"always", "install"}, snapshot=snap, distro=distro,
        )
        _render_preflight(run, distro=distro, for_="install")
        if run.has_failures:
            raise err.fail(
                "preflight check failures — refusing to proceed",
                fix="address the FAILed items above, or rerun with --skip-preflight",
                exit_code=1,
            )

    # ── boot-test gate ──────────────────────────────────────────────────
    # Only enforced when the user requests --execute. In dry-run we just
    # mention the lack of a boot-test as a friendly nudge.
    bt_record = boottest_mod.read_latest_record(snapshot_dir)

    if execute and not skip_boot_test:
        if bt_record is None:
            raise err.fail(
                "no boot-test on record for this snapshot",
                why=(
                    "installing an untested kernel risks an unbootable system. "
                    f"a successful `autokernel boot-test {snapshot_dir} --kernel-source PATH` "
                    f"writes the all-clear at {snapshot_dir}/boot-test.json."
                ),
                fix=(
                    f"run `autokernel boot-test {snapshot_dir} --kernel-source PATH` "
                    f"to verify, or pass --skip-boot-test to override (not recommended)"
                ),
                exit_code=1,
            )
        if not bt_record.get("verdict_ok"):
            raise err.fail(
                "the most recent boot-test for this snapshot FAILED",
                why=str(bt_record.get("verdict_reason", "")),
                fix=(
                    "fix the kernel build, re-run boot-test, or pass --skip-boot-test "
                    "if you've verified the kernel some other way"
                ),
                exit_code=1,
            )

    # Always surface the boot-test record state to the user — whether
    # we're enforcing it or not.
    if bt_record is None and not skip_boot_test:
        console.print(
            "[yellow]· no boot-test on record;[/yellow] "
            f"run `autokernel boot-test {snapshot_dir} --kernel-source PATH` "
            "before --execute"
        )
    elif bt_record is not None:
        ok = "✓" if bt_record.get("verdict_ok") else "✗"
        color = "green" if bt_record.get("verdict_ok") else "red"
        console.print(
            f"[{color}]{ok}[/{color}] boot-test on record "
            f"[dim]({bt_record.get('timestamp', 'unknown')}, "
            f"method={bt_record.get('method', '?')})[/dim]"
        )

    # ── plan + render + (maybe) execute ─────────────────────────────────
    plan = install_mod.build_plan(
        distro=distro,
        spec=spec,
        bootloader=bootloader,
        package_paths=package,
        kernel_entry=kernel_entry,
        enable_probation=not no_probation,
    )
    _render_install_plan(plan, distro=distro, bootloader=bootloader, mode="install")

    if not plan.is_valid:
        if bootloader.kind != bootloader_mod.BootloaderKind.GRUB2:
            raise err.hint_unsupported_bootloader(bootloader.kind.value)
        raise err.fail(
            "refusing to install — plan is invalid",
            why=plan.rejected_reason,
            exit_code=4,
        )

    if not execute:
        console.print(
            "\n[dim]dry-run; pass --execute to actually run the commands above. "
            "Re-read the plan first.[/dim]"
        )
        return

    if os.geteuid() != 0:
        raise err.hint_not_root("`autokernel install --execute`")

    console.print("\n[cyan]running install plan…[/cyan]")
    result = install_mod.execute(plan, snapshot_dir=snapshot_dir)
    _render_install_result(result)
    if not result.ok:
        raise typer.Exit(result.step_runs[-1].exit_code)
    console.print(Panel.fit(
        f"[green]✓ kernel installed[/green]\n"
        f"  log dir: {result.log_dir}\n"
        f"  record:  {result.record_path}\n\n"
        f"[bold]next:[/bold]\n"
        f"  reboot — the new kernel will boot ONCE (one-shot probation).\n"
        f"  if it boots successfully:\n"
        f"    autokernel install {snapshot_dir} --commit --execute\n"
        f"  if it fails to boot, GRUB falls back automatically; then:\n"
        f"    autokernel rollback {snapshot_dir} --execute",
        title="autokernel install",
    ))


@app.command()
def rollback(
    snapshot_dir: Annotated[Path, typer.Argument(help="Snapshot directory")],
    execute: Annotated[bool, typer.Option(help="Actually run the rollback. Default: dry-run.")] = False,
) -> None:
    """Undo the most recent autokernel install for SNAPSHOT_DIR."""
    import os

    _validate_snapshot_dir(snapshot_dir)
    distro = detect_distro()
    bootloader = bootloader_mod.detect()

    plan = rollback_mod.build_plan(
        snapshot_dir=snapshot_dir, distro=distro, bootloader=bootloader,
    )
    _render_rollback_plan(plan, distro=distro, bootloader=bootloader)

    if not plan.is_valid:
        if bootloader.kind != bootloader_mod.BootloaderKind.GRUB2:
            raise err.hint_unsupported_bootloader(bootloader.kind.value)
        raise err.fail(
            "nothing to rollback",
            why=plan.rejected_reason,
            fix="run `autokernel install --execute` first if you actually meant to install",
            exit_code=2,
        )

    if not execute:
        console.print(
            "\n[dim]dry-run; pass --execute to actually run the commands above[/dim]"
        )
        return

    if os.geteuid() != 0:
        raise err.hint_not_root("`autokernel rollback --execute`")

    console.print("\n[cyan]running rollback plan…[/cyan]")
    result = rollback_mod.execute(plan, snapshot_dir=snapshot_dir)
    _render_install_result(result)
    if not result.ok:
        raise typer.Exit(result.step_runs[-1].exit_code)
    console.print(f"[green]✓ rolled back; record marked at {plan.record_path}[/green]")


# ── install/rollback rendering ─────────────────────────────────────────────


def _find_latest_built_packages(snapshot_dir: Path) -> list[Path]:
    """Locate built kernel packages under the snapshot dir.

    The build verb writes packages to the source tree's parent dir, so
    they don't usually land here automatically; users either pass
    --package explicitly or symlink the .deb into the snapshot dir.
    """
    candidates: list[Path] = []
    for pat in ("linux-image-*.deb", "kernel-*.rpm", "linux-*.pkg.tar.zst"):
        candidates.extend(snapshot_dir.glob(pat))
    return sorted(candidates)


def _render_install_plan(plan, *, distro, bootloader, mode: str) -> None:
    console.rule(f"install: {mode}")
    console.print(
        f"[dim]distro: {distro.pretty_name or distro.id} (family={distro.family.value})[/dim]\n"
        f"[dim]bootloader: {bootloader.kind.value} ({bootloader.detected_via})[/dim]"
    )
    if not plan.is_valid:
        console.print(f"\n[red]✗ plan rejected:[/red] {plan.rejected_reason}")
        return
    if plan.package_paths:
        console.print("\n[bold]packages:[/bold]")
        for p in plan.package_paths:
            console.print(f"  · {p}")
    console.print(f"\n[bold]steps ({len(plan.steps)}):[/bold]")
    for i, step in enumerate(plan.steps, 1):
        crown = "[red]root[/red]" if step.needs_root else "[dim]user[/dim]"
        console.print(f"  [bold]{i}. {step.name}[/bold] ({crown})")
        console.print(f"     [dim]{step.description}[/dim]")
        console.print(f"     $ {' '.join(step.argv)}")


def _render_rollback_plan(plan, *, distro, bootloader) -> None:
    console.rule("rollback")
    console.print(
        f"[dim]distro: {distro.pretty_name or distro.id} (family={distro.family.value})[/dim]\n"
        f"[dim]bootloader: {bootloader.kind.value} ({bootloader.detected_via})[/dim]"
    )
    if not plan.is_valid:
        console.print(f"\n[yellow]· nothing to do:[/yellow] {plan.rejected_reason}")
        return
    console.print(f"\n[dim]targeting record: {plan.record_path}[/dim]")
    console.print(f"\n[bold]steps ({len(plan.steps)}):[/bold]")
    for i, step in enumerate(plan.steps, 1):
        console.print(f"  [bold]{i}. {step.name}[/bold]  $ {' '.join(step.argv)}")
        console.print(f"     [dim]{step.description}[/dim]")


def _render_install_result(result) -> None:
    t = Table(title="results", header_style="cyan")
    t.add_column("step")
    t.add_column("rc", justify="right")
    t.add_column("dur (s)", justify="right")
    for run in result.step_runs:
        rc_color = "green" if run.ok else "red"
        t.add_row(
            run.step.name,
            f"[{rc_color}]{run.exit_code}[/{rc_color}]",
            f"{run.duration_s:.1f}",
        )
    console.print(t)


# ── config sub-app: show / test ────────────────────────────────────────────


@config_app.command("show")
def config_show(
    spec: Annotated[str, typer.Option("--mode", help="Pretend the user passed this --llm-mode / --model and show what it'd resolve to. Default: 'auto'.")] = "auto",
) -> None:
    """Show the resolved LLM configuration + per-provider availability."""
    rep = llm_mod.status_report()
    available = [s.provider for s in rep if s.available]

    # Resolution attempt — show what `propose --llm-mode=<spec>` would use.
    cfg: llm_mod.LLMConfig | None = None
    err_msg: str | None = None
    try:
        cfg = llm_mod.resolve(spec=spec, available=available)
    except (llm_mod.NoProviderConfigured, llm_mod.ProviderNotAvailable) as e:
        err_msg = str(e)

    # Header panel: which provider would run and which env var holds the key.
    if cfg is not None:
        body = (
            f"[green]✓ resolved[/green]\n"
            f"  spec:        [bold]{spec}[/bold]\n"
            f"  model:       [bold]{cfg.model}[/bold]\n"
            f"  provider:    {cfg.provider.value}\n"
            f"  api_key_var: [dim]{cfg.api_key_var}[/dim]"
        )
        if cfg.service_tier:
            body += f"\n  service_tier: {cfg.service_tier}"
        if cfg.mode is not None:
            body += f"\n  mode preset: {cfg.mode.value}"
    else:
        body = (
            f"[red]✗ cannot resolve {spec!r}[/red]\n"
            f"  [dim]{err_msg}[/dim]"
        )
    console.print(Panel.fit(body, title="autokernel config (resolved)"))

    # Per-provider availability table.
    t = Table(title="provider availability", header_style="cyan")
    t.add_column("provider")
    t.add_column("env var")
    t.add_column("set?")
    t.add_column("default model (auto)")
    for s in rep:
        defaults = llm_mod.model_options_for(s.provider)
        default_auto = defaults.get(llm_mod.LLMMode.AUTO, "—")
        env_label = ", ".join(s.env_vars)
        if s.available:
            t.add_row(s.provider.value, env_label, f"[green]✓ {s.api_key_var}[/green]", default_auto)
        else:
            t.add_row(s.provider.value, env_label, "[dim]·[/dim]", default_auto)
    console.print(t)

    # Per-mode model menu for the active provider so the user knows what
    # --llm-mode={cheap,fast,quality} would pick today.
    if cfg is not None:
        opts = llm_mod.model_options_for(cfg.provider)
        m = Table(title=f"mode presets for {cfg.provider.value}", header_style="cyan")
        m.add_column("mode")
        m.add_column("model")
        for mode, model in opts.items():
            highlight = " ← current" if cfg.mode == mode else ""
            m.add_row(mode.value, model + highlight)
        console.print(m)


@config_app.command("test")
def config_test(
    spec: Annotated[str, typer.Option("--mode", help="Mode preset or literal model id to test (default: 'auto').")] = "auto",
    service_tier: Annotated[str | None, typer.Option("--service-tier", help="OpenAI service_tier override")] = None,
) -> None:
    """Send a tiny prompt to the configured model to verify credentials.

    Cost is approximately $0.001 — far cheaper than a real propose run.
    """
    available = [s.provider for s in llm_mod.status_report() if s.available]
    try:
        cfg = llm_mod.resolve(spec=spec, service_tier=service_tier, available=available)
    except llm_mod.NoProviderConfigured as e:
        raise err.fail(
            "no LLM provider configured",
            why=str(e),
            fix="copy .env.example to .env and set ANTHROPIC_API_KEY or OPENAI_API_KEY (or run `autokernel config show` to see all options)",
            exit_code=1,
        )
    except llm_mod.ProviderNotAvailable as e:
        raise err.fail(
            f"the model {spec!r} requires {e.provider.value!r} but its API key is not set",
            fix=f"set one of: {', '.join(e.env_vars)}; or pick a different --mode/--model",
            exit_code=1,
        )

    console.print(f"[dim]testing {cfg.model}…[/dim]")
    result = llm_mod.test_connection(cfg)
    if result.ok:
        console.print(f"[green]✓ {cfg.model}[/green]   {result.message}")
    else:
        raise err.fail(
            f"connection test failed for {cfg.model}",
            why=result.message,
            fix=(
                f"verify {cfg.api_key_var} is set correctly, the model id is "
                f"available in your account, and there are no network issues"
            ),
            exit_code=1,
        )


# ── boot-test ─────────────────────────────────────────────────────────────


@app.command("boot-test")
def boot_test(
    snapshot_dir: Annotated[Path, typer.Argument(help="Snapshot directory")],
    kernel_source: Annotated[Path, typer.Option("--kernel-source", help="Path to the kernel source tree containing the freshly-built bzImage")],
    method: Annotated[boottest_mod.Method, typer.Option(help="virtme | qemu | auto")] = boottest_mod.Method.AUTO,
    timeout: Annotated[float, typer.Option(help="Hard timeout in seconds")] = 60.0,
    dry_run: Annotated[bool, typer.Option(help="Print the plan without executing")] = False,
) -> None:
    """Boot the freshly-built kernel in a VM to verify it works.

    Two methods, both fast (5-15 sec) and non-destructive:

    * **virtme-ng** (preferred when installed) — boots the kernel against
      the host's read-only / via virtio-fs.
    * **QEMU kernel-only** — boots the kernel with no rootfs; success
      means the kernel reached the VFS-mount stage without an earlier
      panic. Universal fallback.

    Saves the verdict to <snapshot>/boot-test.json so a future
    `autokernel install --execute` can verify a recent passing test.
    """
    _validate_snapshot_dir(snapshot_dir)
    bzimage = boottest_mod.find_bzimage(kernel_source)
    if bzimage is None:
        raise err.fail(
            f"no bzImage found under {kernel_source}",
            why="expected arch/x86/boot/bzImage or vmlinux",
            fix=(
                f"run `autokernel build {snapshot_dir} --kernel-source {kernel_source} --execute` "
                f"first to compile the kernel"
            ),
            exit_code=2,
        )

    snap = snap_mod.load(snapshot_dir)
    try:
        plan_obj = boottest_mod.plan(
            method=method,
            bzimage_path=bzimage,
            kernel_release=snap.kernel.release,
            timeout=timeout,
        )
    except RuntimeError as e:
        raise err.fail(
            "no boot-test runtime available",
            why=str(e),
            fix=(
                "install qemu-system-x86 (sudo apt install qemu-system-x86), "
                "or pip install virtme-ng. "
                "Run `autokernel preflight --for boot-test` for distro-specific hints."
            ),
            exit_code=1,
        )

    _render_boot_test_plan(plan_obj)
    if dry_run:
        console.print("\n[dim]dry-run; pass without --dry-run to actually boot[/dim]")
        return

    console.print(f"\n[cyan]booting kernel {plan_obj.kernel_release} via {plan_obj.method.value}…[/cyan]")
    result = boottest_mod.execute(plan_obj, snapshot_dir=snapshot_dir)
    _render_boot_test_result(result)
    if not result.verdict.ok:
        raise typer.Exit(1)


def _render_boot_test_plan(plan_obj) -> None:
    console.rule("boot-test")
    console.print(
        f"[dim]method:        {plan_obj.method.value}\n"
        f"bzImage:       {plan_obj.bzimage_path}\n"
        f"kernel:        {plan_obj.kernel_release}\n"
        f"timeout:       {plan_obj.timeout}s[/dim]\n"
        f"\n[bold]description[/bold]\n{plan_obj.description}\n"
        f"\n[bold]command[/bold]\n  $ {' '.join(plan_obj.argv)}"
    )


def _render_boot_test_result(result) -> None:
    if result.verdict.ok:
        console.print(Panel.fit(
            f"[green]✓ PASS[/green]\n"
            f"  reason:   {result.verdict.reason}\n"
            f"  duration: {result.duration_s:.1f}s\n"
            f"  bzimage:  sha256:{result.bzimage_sha256[:16]}…\n"
            f"  log:      {result.serial_log_path}\n"
            f"  record:   {result.record_path}",
            title="boot-test",
            border_style="green",
        ))
    else:
        console.print(Panel.fit(
            f"[red]✗ FAIL[/red]\n"
            f"  reason:   {result.verdict.reason}\n"
            f"  exit:     {result.exit_code}\n"
            f"  duration: {result.duration_s:.1f}s\n"
            f"  log:      {result.serial_log_path}\n"
            f"  [yellow]inspect the serial log to see what went wrong[/yellow]",
            title="boot-test",
            border_style="red",
        ))


def _main() -> None:
    app()


if __name__ == "__main__":
    _main()
