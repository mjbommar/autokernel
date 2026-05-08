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

from autokernel import build as build_mod
from autokernel import fetch as fetch_mod
from autokernel import preflight as preflight_mod
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
    if not snapshot_dir.is_dir():
        err_console.print(f"[red]not a directory:[/red] {snapshot_dir}")
        raise typer.Exit(2)
    manifest = snapshot_dir / "manifest"
    if not manifest.exists():
        err_console.print(
            f"[red]not an autokernel snapshot:[/red] {snapshot_dir} has no manifest. "
            f"Run `autokernel scan {snapshot_dir}` first."
        )
        raise typer.Exit(2)


@app.command()
def propose(
    snapshot_dir: Annotated[Path, typer.Argument(help="Directory from `autokernel scan`")],
    autonomy: Annotated[AutonomyLevel, typer.Option(help="How aggressive to be")] = AutonomyLevel.ADVISE,
    skip_llm: Annotated[bool, typer.Option(help="Skip the LLM stage; only run deterministic rules")] = False,
    max_candidates: Annotated[int, typer.Option(help="Cap the number of candidates passed to the LLM")] = 600,
    model: Annotated[str | None, typer.Option(help="pydantic-ai model id (overrides AUTOKERNEL_MODEL)")] = None,
    service_tier: Annotated[str | None, typer.Option(help="OpenAI service_tier: 'flex' | 'priority' | 'auto' (overrides AUTOKERNEL_SERVICE_TIER)")] = None,
    out: Annotated[Path | None, typer.Option(help="Write proposal JSON to this path")] = None,
    force_dkms: Annotated[bool, typer.Option(help="Allow auto-* autonomy even when DKMS modules are present (use only if you understand the rebuild risk)")] = False,
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
        cache_dir = snapshot_dir / "batches"
        with console.status(f"[cyan]asking LLM about {len(llm_pool)} candidates…[/cyan]"):
            def _progress(i: int, n: int, sz: int, *, cached: bool = False) -> None:
                tag = "[dim](cached)[/dim]" if cached else ""
                console.log(f"  batch {i}/{n} ({sz} symbols) {tag}")
            kwargs: dict = {}
            if model:
                kwargs["model"] = model
            if service_tier:
                kwargs["service_tier"] = service_tier
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
    out: Annotated[Path | None, typer.Option(help="Where to write review.json (default: SNAPSHOT_DIR/review.json)")] = None,
    kfrag: Annotated[Path | None, typer.Option(help="Where to write the kfrag (default: SNAPSHOT_DIR/auto.kfrag)")] = None,
    reviewer: Annotated[Reviewer, typer.Option(help="Identity to record on each decision")] = Reviewer.POLICY,
) -> None:
    """Apply bulk decision rules to a proposal and emit a kfrag."""
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

    # ── render summary ──────────────────────────────────────────────────
    _render_review(review_set, rule_labels)

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
        err_console.print("[red]no running_config in snapshot[/red]")
        raise typer.Exit(1)

    kfrag_path = kfrag or snapshot_dir / "auto.kfrag"
    if not kfrag_path.exists():
        err_console.print(
            f"[red]kfrag not found:[/red] {kfrag_path}\n"
            f"  Run `autokernel review {snapshot_dir} --accept-recommended` first."
        )
        raise typer.Exit(2)

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


def _main() -> None:
    app()


if __name__ == "__main__":
    _main()
