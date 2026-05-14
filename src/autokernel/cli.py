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
import os
import shutil
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
from autokernel import installdeps as installdeps_mod
from autokernel import llm as llm_mod
from autokernel import nvidia as nvidia_mod
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
    Reviewer,
    ReviewSet,
)
from autokernel.policy import (
    AutonomyLevel,
    apply_policy,
    compute_load_bearing,
    to_diff,
)
from autokernel.resolve import candidate_trims, focused_candidate_trims, resolve
from autokernel.review import (
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

inventory_app = typer.Typer(
    add_completion=False,
    help="Build, search, and enrich source-derived Kconfig inventories.",
    no_args_is_help=True,
)
app.add_typer(inventory_app, name="inventory")

console = Console()
err_console = Console(stderr=True)


_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

# Autonomy levels that auto-apply proposals.
_AUTO_LEVELS = {AutonomyLevel.AUTO_SAFE, AutonomyLevel.AUTO_BOLD}


# ── v0.13 dimension dispatch helpers ──────────────────────────────────────


_VALID_DIMENSIONS = {"modules", "choices", "toggles", "tunables", "all"}


def _parse_dimensions(spec: str) -> set[str]:
    """``"all"`` → every dimension; ``"modules,toggles"`` → those two;
    raises if any token is unknown."""
    raw = {tok.strip() for tok in spec.split(",") if tok.strip()}
    unknown = raw - _VALID_DIMENSIONS
    if unknown:
        raise typer.BadParameter(
            f"unknown --dimension(s): {sorted(unknown)}. "
            f"Valid: {sorted(_VALID_DIMENSIONS)}"
        )
    if "all" in raw:
        return {"modules", "choices", "toggles", "tunables"}
    return raw


def _run_dimension_passes(
    *,
    snap,
    snapshot_dir: Path,
    requested: set[str],
    workload_override: str | None,
    threat: str | None = None,
    modules_strategy: str | None = None,
    aggression: str | None = None,
    preset: str | None = None,
    kernel_source: Path | None,
    llm_spec: str,
    service_tier: str | None,
    skip_llm: bool,
    history_text: str | None = None,
    base_config_path: Path | None = None,
) -> list:
    """Run propose_choices / propose_toggles / propose_tunables as
    requested. Returns the merged list of RemovalProposals; each
    sub-pass writes its cache under ``<snapshot_dir>/batches/dim-<n>/``.
    """
    from autokernel.agent_dims import (
        propose_choices,
        propose_toggles,
        propose_tunables,
    )
    from autokernel.kconfig_walk import walk as walk_kconfig
    from autokernel.workload import (
        WorkloadProfile,
        detect as detect_workload,
    )

    if skip_llm:
        console.print(
            "[yellow]--skip-llm with non-modules dimensions skips ALL LLM passes; "
            "no choice/toggle/tunable proposals will be generated.[/yellow]"
        )
        return []

    if kernel_source is None:
        raise err.fail(
            f"--dimension={','.join(sorted(requested))} requires --kernel-source",
            why="we need to walk the target kernel's Kconfig to know what's available",
            fix="pass --kernel-source=<path-to-kernel-source> (e.g. ~/build/sources/linux-X.Y)",
            exit_code=2,
        )

    # Workload — explicit override or detected from snapshot + /sys.
    if workload_override is not None:
        try:
            workload = WorkloadProfile(workload_override)
        except ValueError:
            raise typer.BadParameter(
                f"unknown --workload {workload_override!r}. "
                f"Valid: {sorted(p.value for p in WorkloadProfile if p.is_user_facing)}"
            )
        detection = detect_workload(snap, explicit=workload)
    else:
        detection = detect_workload(snap)
        console.print(
            f"[cyan]detected workload:[/cyan] [bold]{detection.profile.value}[/bold] "
            f"(conf={detection.confidence:.2f})"
        )
        for r in detection.reasons[:3]:
            console.print(f"  [dim]{r}[/dim]")

    # Compose the four-axis context.
    from autokernel.optimize_context import context_from_flags

    try:
        ctx = context_from_flags(
            preset=preset,
            workload=workload_override,
            threat=threat,
            modules=modules_strategy,
            aggression=aggression,
            detected_workload=detection.profile,
        )
    except KeyError as e:
        from autokernel.optimize_context import PRESETS

        raise typer.BadParameter(
            f"unknown --preset {e.args[0]!r}. Valid: {sorted(PRESETS)}"
        )
    except ValueError as e:
        raise typer.BadParameter(str(e))
    console.print(
        f"[cyan]context:[/cyan] workload=[bold]{ctx.workload.value}[/bold] "
        f"threat=[bold]{ctx.threat.value}[/bold] "
        f"modules=[bold]{ctx.modules.value}[/bold] "
        f"aggression=[bold]{ctx.aggression.value}[/bold]"
        + (f"  [dim](preset={preset})[/dim]" if preset else "")
    )

    # Resolve LLM model (same as the trim path).
    try:
        cfg = llm_mod.resolve(spec=llm_spec, service_tier=service_tier)
    except (llm_mod.NoProviderConfigured, llm_mod.ProviderNotAvailable) as e:
        raise err.fail(
            "no LLM provider configured for v0.13 dimension passes",
            why=str(e),
            fix="set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GOOGLE_API_KEY in .env, or omit --dimension",
            exit_code=1,
        )

    # Walk the kernel's Kconfig surface — slow on first call but cached
    # by Python's import. When --base-config was passed, we walk against
    # THAT instead of the original snapshot's running config so each
    # iteration's propose builds on top of the previous round.
    console.print(f"[dim]walking Kconfig under {kernel_source}…[/dim]")
    config_for_surface = base_config_path or snap.running_config_path
    surface = walk_kconfig(
        kernel_source,
        arch=snap.kernel.arch,
        config_path=config_for_surface,
    )
    console.print(
        f"  choices: {len(surface.choices)}  "
        f"toggles: {len(surface.toggles)}  "
        f"tunables: {len(surface.tunables)}"
    )

    cache_root = snapshot_dir / "batches"
    out: list = []

    if "choices" in requested:
        with console.status("[cyan]choice groups…[/cyan]"):

            def _p(i, n, sz, *, cached=False):
                tag = "[dim](cached)[/dim]" if cached else ""
                console.log(f"  choice batch {i}/{n} ({sz}) {tag}")

            ch = propose_choices(
                snap,
                surface,
                ctx,
                history_text=history_text,
                cache_dir=cache_root / "dim-choices",
                progress=_p,
                model=cfg.model,
                service_tier=cfg.service_tier,
            )
        console.print(f"[dim]choice proposals: {len(ch)}[/dim]")
        out.extend(ch)

    if "toggles" in requested:
        with console.status("[cyan]bool toggles…[/cyan]"):

            def _p(i, n, sz, *, cached=False):
                tag = "[dim](cached)[/dim]" if cached else ""
                console.log(f"  toggle batch {i}/{n} ({sz}) {tag}")

            tg = propose_toggles(
                snap,
                surface,
                ctx,
                history_text=history_text,
                cache_dir=cache_root / "dim-toggles",
                progress=_p,
                model=cfg.model,
                service_tier=cfg.service_tier,
            )
        console.print(f"[dim]toggle proposals: {len(tg)}[/dim]")
        out.extend(tg)

    if "tunables" in requested:
        with console.status("[cyan]numeric tunables…[/cyan]"):

            def _p(i, n, sz, *, cached=False):
                tag = "[dim](cached)[/dim]" if cached else ""
                console.log(f"  tunable batch {i}/{n} ({sz}) {tag}")

            tn = propose_tunables(
                snap,
                surface,
                ctx,
                history_text=history_text,
                cache_dir=cache_root / "dim-tunables",
                progress=_p,
                model=cfg.model,
                service_tier=cfg.service_tier,
            )
        console.print(f"[dim]tunable proposals: {len(tn)}[/dim]")
        out.extend(tn)

    return out


@app.command()
def scan(
    outdir: Annotated[
        Path | None, typer.Argument(help="Where to write the snapshot")
    ] = None,
    sudo_probes: Annotated[
        bool,
        typer.Option(
            "--sudo-probes/--no-sudo-probes",
            help=(
                "Prompt once for sudo and use it only for read-only privileged "
                "probes such as dmesg, dmidecode, lshw, and initramfs listing."
            ),
        ),
    ] = True,
) -> None:
    """Collect hardware/system inventory into SNAPSHOT_DIR."""
    collector = _SCRIPTS_DIR / "collect.sh"
    if not collector.exists():
        err_console.print(f"[red]collector script missing:[/red] {collector}")
        raise typer.Exit(1)

    args = ["bash", str(collector)]
    if outdir:
        args.append(str(outdir))

    env = _scan_subprocess_env(sudo_probes=sudo_probes)

    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=300, env=env
        )
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

    console.print(
        Panel.fit(
            f"[green]✓ snapshot saved[/green]\n"
            f"  dir:     {snapdir}\n"
            f"  pci:     {len(snap.pci)}\n"
            f"  usb:     {len(snap.usb)}\n"
            f"  modaliases: {len(snap.modaliases)}\n"
            f"  loaded modules: {len(snap.loaded_modules)}\n"
            f"  mounts:  {len(snap.mounts)}\n"
            f"  dkms:    {len(snap.dkms)}\n"
            f"  software signals: {len(snap.software_features)}\n"
            f"  audio:   {'useful' if snap.audio.useful else 'unused'}"
            f" ({snap.audio.role}, conf={snap.audio.confidence:.2f})\n"
            f"  initramfs modules: {len(snap.initramfs_modules)}\n"
            f"  firmware loads: {len(snap.firmware)}\n"
            f"  running config: {snap.running_config_path}\n"
            f"\n[dim]next: autokernel propose {snapdir}[/dim]",
            title="autokernel scan",
        )
    )


def _scan_subprocess_env(*, sudo_probes: bool = True) -> dict[str, str]:
    env = os.environ.copy()
    if not sudo_probes:
        env["AUTOKERNEL_SCAN_SUDO"] = "0"
        return env

    if os.geteuid() == 0:
        env["AUTOKERNEL_SCAN_SUDO"] = "1"
        return env

    if shutil.which("sudo") is None:
        console.print(
            "[yellow]sudo not found; scan will use unprivileged probes only[/yellow]"
        )
        env["AUTOKERNEL_SCAN_SUDO"] = "0"
        return env

    console.print("[cyan]requesting sudo for read-only scan probes[/cyan]")
    rc = subprocess.run(["sudo", "-v"], check=False).returncode
    if rc == 0:
        env["AUTOKERNEL_SCAN_SUDO"] = "1"
    else:
        console.print(
            "[yellow]sudo authentication failed; scan will use unprivileged probes only[/yellow]"
        )
        env["AUTOKERNEL_SCAN_SUDO"] = "0"
    return env


def _validate_snapshot_dir(snapshot_dir: Path) -> None:
    if not snapshot_dir.is_dir() or not (snapshot_dir / "manifest").exists():
        raise err.hint_not_a_snapshot(snapshot_dir)


@app.command()
def propose(
    snapshot_dir: Annotated[
        Path, typer.Argument(help="Directory from `autokernel scan`")
    ],
    autonomy: Annotated[
        AutonomyLevel, typer.Option(help="How aggressive to be")
    ] = AutonomyLevel.ADVISE,
    skip_llm: Annotated[
        bool, typer.Option(help="Skip the LLM stage; only run deterministic rules")
    ] = False,
    max_candidates: Annotated[
        int,
        typer.Option(
            help="Cost guard for module-trim LLM candidates; 0 disables the cap"
        ),
    ] = 0,
    candidate_scope: Annotated[
        str,
        typer.Option(
            "--candidate-scope",
            help="Module-trim candidate pool: focused (modules.dep-backed, prioritized) or all (legacy broad complement).",
        ),
    ] = "focused",
    llm_mode: Annotated[
        str,
        typer.Option(
            "--llm-mode",
            help="Mode preset: auto|cheap|fast|quality. Picks the best model from a provider you have credentials for. Overridden by --model.",
        ),
    ] = "auto",
    model: Annotated[
        str | None,
        typer.Option(
            help="Literal pydantic-ai model id (e.g. 'anthropic:claude-opus-4-7'). Overrides --llm-mode."
        ),
    ] = None,
    service_tier: Annotated[
        str | None,
        typer.Option(help="OpenAI service_tier: 'flex' | 'priority' | 'auto'"),
    ] = None,
    out: Annotated[
        Path | None, typer.Option(help="Write proposal JSON to this path")
    ] = None,
    force_dkms: Annotated[
        bool,
        typer.Option(
            help="Allow auto-* autonomy even when DKMS modules are present (use only if you understand the rebuild risk)"
        ),
    ] = False,
    no_cpu_tune: Annotated[
        bool,
        typer.Option(
            "--no-cpu-tune",
            help="Don't propose CPU microarch tuning (CONFIG_M<arch>=y).",
        ),
    ] = False,
    # ── v0.13: multi-dimensional optimization ─────────────────────────────
    dimension: Annotated[
        str,
        typer.Option(
            "--dimension",
            help="Which optimization dimensions to run. 'modules' = the existing trim path. 'choices' = pick PREEMPT/HZ/IOSCHED/etc. 'toggles' = bool perf/security knobs. 'tunables' = NR_CPUS, LOG_BUF_SHIFT etc. 'all' = run every dimension. Comma-separate to pick a subset (e.g. 'modules,toggles').",
        ),
    ] = "modules",
    workload: Annotated[
        str | None,
        typer.Option(
            "--workload",
            help="Override the auto-detected workload profile. One of: desktop, laptop, server, vm-guest, realtime, embedded.",
        ),
    ] = None,
    threat: Annotated[
        str | None,
        typer.Option(
            "--threat",
            help="Security threat model. One of: permissive, balanced, paranoid. Default: balanced.",
        ),
    ] = None,
    modules_strategy: Annotated[
        str | None,
        typer.Option(
            "--modules",
            help="Module composition strategy. One of: distro, monolithic, modular. Default: distro.",
        ),
    ] = None,
    aggression: Annotated[
        str | None,
        typer.Option(
            "--aggression",
            help="Confidence threshold for proposals. One of: conservative (≥0.85), balanced (≥0.65, default), aggressive (≥0.40).",
        ),
    ] = None,
    preset: Annotated[
        str | None,
        typer.Option(
            "--preset",
            help="Named four-axis combination shortcut (e.g. 'gaming-desktop', 'paranoid-laptop', 'hardened-server', 'cloud-vm', 'lean-static', 'hyperoptimize'). Per-axis flags override.",
        ),
    ] = None,
    kernel_source: Annotated[
        Path | None,
        typer.Option(
            "--kernel-source",
            help="Path to a kernel source tree. Required for --dimension={choices,toggles,tunables,all} so we can walk Kconfig and see what's available on the target kernel.",
        ),
    ] = None,
    history_from: Annotated[
        Path | None,
        typer.Option(
            "--history-from",
            help="(closed-loop) Path to a prompt-ready iteration-history block. Prepended to dim-agent prompts so they reason about prior rounds.",
        ),
    ] = None,
    base_config: Annotated[
        Path | None,
        typer.Option(
            "--base-config",
            help="(closed-loop) Override the .config we compare against. Default: snapshot's running_config. Pass iterations/i<N-1>/final.config to chain rounds.",
        ),
    ] = None,
) -> None:
    """Generate a proposed kernel config trim from a snapshot.

    By default runs only the module-trim dimension (existing behavior).
    Pass ``--dimension=all --kernel-source=PATH`` to also run the v0.13
    LLM passes for choice groups (PREEMPT, HZ, IOSCHED, …),
    bool feature toggles (TRANSPARENT_HUGEPAGE, BPF_JIT_ALWAYS_ON, …),
    and numeric/string tunables (NR_CPUS, LOG_BUF_SHIFT, …).
    """
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
                "[red]DKMS modules detected[/red] — auto-* autonomy is unsafe without "
                "verifying they rebuild against the new kernel. Refusing.\n"
                "  Re-run with --autonomy=advise to review proposals manually, or "
                "--force-dkms to override (you'll need to test the rebuild yourself)."
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
    if snap.audio.useful:
        console.print(
            f"  audio keep-set: {snap.audio.role} "
            f"(conf={snap.audio.confidence:.2f}; "
            "protecting HDA/SOF/SoundWire/codec/USB audio)"
        )

    requested_dims = _parse_dimensions(dimension)
    broad_candidate_syms = candidate_trims(snap, resolution)
    if "modules" in requested_dims:
        if candidate_scope == "focused":
            candidate_syms = focused_candidate_trims(
                snap, resolution, broad_candidates=broad_candidate_syms
            )
            console.print(
                f"  candidate trims: {len(broad_candidate_syms)} broad; "
                f"{len(candidate_syms)} focused for module LLM"
            )
        elif candidate_scope == "all":
            candidate_syms = broad_candidate_syms
            console.print(f"  candidate trims: {len(candidate_syms)}")
        else:
            raise typer.BadParameter("--candidate-scope must be 'focused' or 'all'")
    else:
        candidate_syms = []
        console.print(
            f"  broad module-trim candidates available: {len(broad_candidate_syms)} "
            "[dim](module LLM dimension not requested)[/dim]"
        )

    from autokernel.resolve import _running_config_symbols

    # Closed-loop: when --base-config is given, we compare against THAT
    # so iteration N proposes on top of iteration N-1's accepted changes
    # rather than the original snapshot's running config.
    running_cfg_path = base_config or snap.running_config_path
    running = _running_config_symbols(running_cfg_path)
    candidates = [(s, running.get(s, "y")) for s in candidate_syms]
    broad_candidates = [(s, running.get(s, "y")) for s in broad_candidate_syms]

    # ── deterministic proposals ─────────────────────────────────────────
    det = deterministic_proposals(snap, broad_candidates)
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

    if "modules" not in requested_dims:
        llm_pool = []
    elif skip_llm:
        not_considered = [s for s, _ in llm_pool]
    elif max_candidates and len(llm_pool) > max_candidates:
        console.print(
            f"[yellow]capping LLM pool at {max_candidates} "
            f"(full pool: {len(llm_pool)}; "
            f"{len(llm_pool) - max_candidates} symbols deferred)[/yellow]"
        )
        not_considered = [s for s, _ in llm_pool[max_candidates:]]
        llm_pool = llm_pool[:max_candidates]

    # ── LLM proposals (modules dimension) ──────────────────────────────
    llm: list = []
    if "modules" in requested_dims and not skip_llm and llm_pool:
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
                fix="set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GOOGLE_API_KEY in .env (see .env.example), or pass --skip-llm to use only deterministic rules",
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
        with console.status(
            f"[cyan]asking LLM about {len(llm_pool)} candidates…[/cyan]"
        ):

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

    # ── v0.13 multi-dimensional passes ─────────────────────────────────
    extra_proposals: list = []
    extra_dims = requested_dims - {"modules"}
    # Read closed-loop history from disk once if provided.
    history_text: str | None = None
    if history_from is not None:
        if not history_from.exists():
            raise typer.BadParameter(
                f"--history-from path does not exist: {history_from}"
            )
        history_text = history_from.read_text()
        console.print(
            f"[dim]closed-loop: history-from={history_from} "
            f"({len(history_text)} chars)[/dim]"
        )
    if base_config is not None:
        if not base_config.exists():
            raise typer.BadParameter(
                f"--base-config path does not exist: {base_config}"
            )
        console.print(f"[dim]closed-loop: base-config={base_config}[/dim]")

    if extra_dims:
        extra_proposals = _run_dimension_passes(
            snap=snap,
            snapshot_dir=snapshot_dir,
            requested=extra_dims,
            workload_override=workload,
            threat=threat,
            modules_strategy=modules_strategy,
            aggression=aggression,
            preset=preset,
            kernel_source=kernel_source,
            llm_spec=model if model else llm_mode,
            service_tier=service_tier,
            skip_llm=skip_llm,
            history_text=history_text,
            base_config_path=base_config,
        )

    # ── policy filter ───────────────────────────────────────────────────
    all_proposals = det + llm + extra_proposals
    load_bearing = compute_load_bearing(snap, resolution)
    pr = apply_policy(all_proposals, autonomy, load_bearing)
    diff = to_diff(
        snap.running_config_path, autonomy, pr, not_considered=not_considered
    )

    # ── render + persist ────────────────────────────────────────────────
    _render_diff(diff)

    out_path = out or snapshot_dir / "proposal.json"
    out_path.write_text(diff.model_dump_json(indent=2))
    console.print(f"\n[green]wrote {out_path}[/green]")


# ── rendering ──────────────────────────────────────────────────────────


def _render_dkms_panel(dkms: list) -> None:
    body = "\n".join(
        f"  {d.name}/{d.version}  for {d.kernel}  ({d.status})" for d in dkms
    )
    console.print(
        Panel(
            f"[bold]DKMS modules detected[/bold]\n{body}\n\n"
            f"[dim]These out-of-tree modules must rebuild against any new kernel "
            f"or the kernel will fail to boot/run them. Verify rebuilds before "
            f"installing a custom kernel.[/dim]",
            title="DKMS",
            border_style="yellow",
        )
    )


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
        t = Table(
            title=f"blocked by load-bearing policy ({len(diff.blocked)})",
            header_style="red",
        )
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
            f"--max-candidates, or --max-candidates 0, to address them. Sample: "
            f"{', '.join(diff.not_considered[:5])}…"
        )


@app.command()
def review(
    snapshot_dir: Annotated[
        Path,
        typer.Argument(
            help="Directory from `autokernel scan` (must contain proposal.json)"
        ),
    ],
    proposal: Annotated[
        Path | None, typer.Option(help="Override path to proposal.json")
    ] = None,
    accept_recommended: Annotated[
        bool, typer.Option(help="Bulk-accept everything that isn't risk=high")
    ] = False,
    accept_low_risk: Annotated[
        bool, typer.Option(help="Bulk-accept only risk=low")
    ] = False,
    accept_deterministic: Annotated[
        bool, typer.Option(help="Bulk-accept only deterministic-source proposals")
    ] = False,
    reject_subsystem: Annotated[
        list[str] | None,
        typer.Option(
            "--reject-subsystem",
            help="Veto a whole subsystem (repeatable). Examples: crypto security kasan debug",
        ),
    ] = None,
    reject_pattern: Annotated[
        list[str] | None,
        typer.Option(
            "--reject-pattern",
            help="Veto symbols matching glob (repeatable). Example: 'CONFIG_DEBUG_*'",
        ),
    ] = None,
    interactive: Annotated[
        bool,
        typer.Option(
            "--interactive/--no-interactive",
            help="After bulk rules apply, open a TUI to step through remaining deferred items",
        ),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option(
            help="Where to write review.json (default: SNAPSHOT_DIR/review.json)"
        ),
    ] = None,
    kfrag: Annotated[
        Path | None,
        typer.Option(
            help="Where to write the kfrag (default: SNAPSHOT_DIR/auto.kfrag)"
        ),
    ] = None,
    reviewer: Annotated[
        Reviewer, typer.Option(help="Identity to record on each decision")
    ] = Reviewer.POLICY,
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

    # Items that propose() already auto-applied (high-confidence
    # deterministic + microarch tuning) bypass the review bulk-rules but
    # MUST still flow into the kfrag — otherwise the CPU-tune and
    # vendor-mismatch trims get silently dropped from final.config.
    # Tag them with rule='propose-auto' so the audit trail distinguishes
    # them from review-time decisions.
    if diff.auto_applied:
        from autokernel.models import ReviewDecision, ReviewedProposal

        pre_accepted = [
            ReviewedProposal(
                proposal=p,
                decision=ReviewDecision.ACCEPT,
                reviewer=reviewer,
                rule="propose-auto",
            )
            for p in diff.auto_applied
        ]
        # ReviewSet is frozen; rebuild with accepted prepended (so their
        # provenance is visible first when the user inspects review.json).
        review_set = ReviewSet(
            base_diff_path=review_set.base_diff_path,
            accepted=pre_accepted + list(review_set.accepted),
            rejected=list(review_set.rejected),
            deferred=list(review_set.deferred),
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
            t.add_row(
                ss, str(len(syms)), ", ".join(syms[:5]) + ("…" if len(syms) > 5 else "")
            )
        console.print(t)

    _group_summary("accepted", rs.accepted, "green")
    _group_summary("rejected", rs.rejected, "red")
    _group_summary("deferred (still need review)", rs.deferred, "yellow")


@app.command()
def apply(
    snapshot_dir: Annotated[
        Path,
        typer.Argument(
            help="Snapshot directory containing running_config + auto.kfrag"
        ),
    ],
    kfrag: Annotated[
        Path | None,
        typer.Option(
            help="Override path to the kfrag (default: SNAPSHOT_DIR/auto.kfrag)"
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            help="Where to write the merged config (default: SNAPSHOT_DIR/final.config)"
        ),
    ] = None,
    no_validate: Annotated[
        bool,
        typer.Option(
            help="Skip the load-bearing validation pass (don't use unless you know what you're doing)"
        ),
    ] = False,
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
    snapshot_dir: Annotated[
        Path, typer.Argument(help="Snapshot directory containing final.config")
    ],
    kernel_source: Annotated[
        Path,
        typer.Option(
            "--kernel-source",
            help="Path to a kernel source tree (must contain a top-level Makefile)",
        ),
    ],
    execute: Annotated[
        bool,
        typer.Option(
            help="Run the actual `make <target>` (default is prepare-only: drop config + olddefconfig)"
        ),
    ] = False,
    jobs: Annotated[
        int | None, typer.Option(help="Parallel make jobs (default: $(nproc))")
    ] = None,
    no_ccache: Annotated[
        bool, typer.Option(help="Disable ccache wrapping even when available")
    ] = False,
    target: Annotated[
        str,
        typer.Option(
            help="Make target for --execute. Default 'auto' picks per distro: bindeb-pkg (Debian/Ubuntu), rpm-pkg (Fedora/SUSE), targz-pkg (Arch/Gentoo/other)."
        ),
    ] = "auto",
    force_dkms: Annotated[
        bool, typer.Option(help="Allow --execute even with DKMS modules present")
    ] = False,
    localmodconfig: Annotated[
        bool,
        typer.Option(
            "--localmodconfig",
            help="After dropping final.config, also run `make LSMOD=<snap>/lsmod localmodconfig` to disable every module not currently loaded on the host. Cuts module count ~6000→~250 on stock Ubuntu, build time 5-10× faster.",
        ),
    ] = False,
    compiler: Annotated[
        str,
        typer.Option(
            "--compiler",
            help="Compiler toolchain. 'clang' (default; CC=clang), 'llvm' (LLVM=1: clang+lld+llvm-bin; required for clang-LTO/CFI), 'gcc'.",
        ),
    ] = "clang",
    lto: Annotated[
        str,
        typer.Option(
            "--lto",
            help="Link-time optimization. 'none' (default, fastest builds), 'thin' (clang thin-LTO; +5-10% throughput, +30% build), 'full' (clang full-LTO; +5-12%, +200% build).",
        ),
    ] = "none",
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
            "[red]DKMS modules detected[/red] — refusing --execute without "
            "--force-dkms. Verify they rebuild against the new kernel first."
        )
        raise typer.Exit(3)

    # Compiler pre-flight: fail fast when --compiler=X but X isn't on
    # PATH. Without this, the build would die mid-make with a confusing
    # "command not found" deep in the kernel's recipe chain.
    import shutil as _shutil

    compiler_bin = {"clang": "clang", "llvm": "clang", "gcc": "gcc"}.get(compiler)
    if compiler_bin and _shutil.which(compiler_bin) is None:
        raise err.fail(
            f"--compiler={compiler!r} but {compiler_bin!r} is not on PATH",
            why=(
                "the build verb defaults to clang as of v0.15. Either install "
                "the toolchain (recommended) or pass --compiler=gcc to use "
                "the gcc fallback."
            ),
            fix="autokernel install-deps --for build --execute",
            exit_code=2,
        )

    # ── prepare ──────────────────────────────────────────────────────────
    if localmodconfig:
        lsmod_path = snapshot_dir / "lsmod"
        if not lsmod_path.exists():
            err_console.print(
                f"[red]--localmodconfig requested but {lsmod_path} not found[/red]\n"
                f"  Re-run `autokernel scan {snapshot_dir}` to refresh the snapshot."
            )
            raise typer.Exit(2)
        console.print(
            f"[dim]preparing {kernel_source} with {final_config} "
            f"+ localmodconfig from {lsmod_path}…[/dim]"
        )
    else:
        console.print(f"[dim]preparing {kernel_source} with {final_config}…[/dim]")
        lsmod_path = None
    try:
        prep = build_mod.prepare(
            source_dir=kernel_source,
            config_path=final_config,
            snapshot_dir=snapshot_dir,
            localmodconfig=localmodconfig,
            lsmod_path=lsmod_path,
            compiler=compiler,
            lto=lto,
        )
    except FileNotFoundError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None

    _render_step_results("prepare", prep.steps, prep.log_dir)

    if not prep.ok:
        err_console.print("[red]olddefconfig failed — see log dir.[/red]")
        raise typer.Exit(prep.steps[-1].exit_code)

    if not execute:
        console.print(
            Panel.fit(
                f"[green]✓ prepared[/green]\n"
                f"  source:  {prep.source_dir}\n"
                f"  config:  {prep.config_path}\n"
                f"  logs:    {prep.log_dir}\n"
                f"\n[dim]run with --execute to invoke `make {target}` "
                f"(this is the slow step; ~15-60 min).[/dim]",
                title="autokernel build (prepare-only)",
            )
        )
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
        compiler=compiler,
        lto=lto,
    )

    _render_step_results("build", bres.steps, bres.log_dir)

    if not bres.ok:
        err_console.print("[red]build failed — see log dir.[/red]")
        raise typer.Exit(bres.steps[-1].exit_code)

    if bres.deb_paths:
        body = "\n".join(f"  {p}" for p in bres.deb_paths)
        console.print(
            Panel.fit(
                f"[green]✓ built[/green]\n"
                f"  source:  {bres.source_dir}\n"
                f"  logs:    {bres.log_dir}\n\n"
                f"[bold]artifacts[/bold]\n{body}",
                title="autokernel build",
            )
        )
    elif bres.target == "kernel-only" and bres.bzimage_path:
        bz_size_mb = bres.bzimage_path.stat().st_size / (1024 * 1024)
        console.print(
            Panel.fit(
                f"[green]✓ built (kernel-only)[/green]\n"
                f"  source:  {bres.source_dir}\n"
                f"  logs:    {bres.log_dir}\n"
                f"  bzImage: {bres.bzimage_path} ({bz_size_mb:.2f} MB)",
                title="autokernel build",
            )
        )
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
    snapshot_dir: Annotated[
        Path | None,
        typer.Argument(help="Optional snapshot dir; enables snapshot-aware checks"),
    ] = None,
    kernel_source: Annotated[
        Path | None,
        typer.Option(
            "--kernel-source",
            help="Optional kernel source tree for built-kernel boot-test checks",
        ),
    ] = None,
    for_: Annotated[
        str,
        typer.Option(
            "--for",
            help="Which verb's checks to run: all|scan|propose|apply|build|install|boot-test",
        ),
    ] = "all",
    strict: Annotated[
        bool, typer.Option(help="Treat WARN as failure for the exit code")
    ] = False,
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
        "all": None,
        "scan": {"always", "scan"},
        "propose": {"always", "propose"},
        "apply": {"always", "apply"},
        "build": {"always", "build"},
        "install": {"always", "install"},
        "boot-test": {"always", "boot-test"},
    }
    tags = tag_map.get(for_)
    if for_ not in tag_map:
        err_console.print(
            f"[red]unknown --for value:[/red] {for_!r}; use one of {list(tag_map)}"
        )
        raise typer.Exit(2)

    distro = detect_distro()
    run = preflight_mod.run_checks(
        tags=tags,
        snapshot=snap,
        distro=distro,
        kernel_source=kernel_source,
    )

    _render_preflight(run, distro=distro, for_=for_)

    if run.has_failures:
        raise typer.Exit(1)
    if strict and run.has_warnings:
        raise typer.Exit(1)


def _render_preflight(run, *, distro, for_: str) -> None:
    console.rule(f"preflight — for={for_}")
    console.print(
        f"[dim]host: {distro.pretty_name or distro.id} (family={distro.family.value})[/dim]"
    )

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
    kernel_version: Annotated[
        str | None,
        typer.Option(
            "--kernel-version",
            help="Kernel version (e.g. 6.13.0). Defaults to running uname -r",
        ),
    ] = None,
    method: Annotated[
        FetchMethod, typer.Option(help="Acquisition method")
    ] = FetchMethod.AUTO,
    out: Annotated[
        Path, typer.Option("--out", help="Working directory for downloads + extraction")
    ] = Path.home() / ".cache" / "autokernel" / "kernels",
    dry_run: Annotated[
        bool, typer.Option(help="Print the plan without executing")
    ] = False,
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
            "[yellow]plan requires root for some steps; "
            "prepend `sudo` to the commands above or rerun with sudo[/yellow]"
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
    snapshot_dir: Annotated[
        Path, typer.Argument(help="Where to put the snapshot + artifacts")
    ] = Path.home() / ".local" / "share" / "autokernel" / "quickstart",
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Don't prompt; run all steps")
    ] = False,
    skip_llm: Annotated[
        bool, typer.Option(help="Skip the LLM step (free; deterministic-only proposal)")
    ] = False,
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
    snapshot_dir: Annotated[
        Path, typer.Argument(help="Snapshot directory containing built package(s)")
    ],
    package: Annotated[
        list[Path] | None,
        typer.Option(
            "--package",
            help="Path to a built package (.deb/.rpm/.pkg.tar.zst). Repeatable. If omitted, autokernel scans the snapshot dir for the most recent package.",
        ),
    ] = None,
    kernel_entry: Annotated[
        str | None,
        typer.Option(
            help="GRUB menu entry name to arm for one-shot boot. If omitted, the arm step is skipped (run --commit later instead)."
        ),
    ] = None,
    execute: Annotated[
        bool, typer.Option(help="Actually run the install. Default: dry-run.")
    ] = False,
    commit: Annotated[
        bool,
        typer.Option(
            help="Promote the running kernel to permanent default (after a successful probation boot)."
        ),
    ] = False,
    no_probation: Annotated[
        bool,
        typer.Option(
            "--no-probation",
            help="Skip the one-shot grub-reboot step (NOT RECOMMENDED).",
        ),
    ] = False,
    skip_preflight: Annotated[
        bool,
        typer.Option(
            help="Skip pre-flight checks (use only if you've verified them yourself)."
        ),
    ] = False,
    skip_boot_test: Annotated[
        bool,
        typer.Option(
            help="Don't require a recent successful `boot-test` for this snapshot. Override only when you know what you're doing."
        ),
    ] = False,
    nvidia: Annotated[
        nvidia_mod.NvidiaMode,
        typer.Option(
            "--nvidia",
            help=(
                "NVIDIA handling for custom-kernel installs: auto preserves the "
                "detected driver flavor, open/proprietary force a DKMS flavor, "
                "off disables NVIDIA handling."
            ),
        ),
    ] = nvidia_mod.NvidiaMode.AUTO,
) -> None:
    """Install a built kernel package with one-shot probation, or commit a successful boot."""
    import os

    _validate_snapshot_dir(snapshot_dir)
    distro = detect_distro()
    spec = spec_for(distro)
    bootloader = bootloader_mod.detect()
    snap = snap_mod.load(snapshot_dir)

    # ── commit path ─────────────────────────────────────────────────────
    if commit:
        if kernel_entry is None:
            kernel_entry = os.uname().release
            console.print(
                f"[dim]commit: defaulting to running kernel '{kernel_entry}'[/dim]"
            )
        plan = install_mod.build_commit_plan(
            distro=distro,
            bootloader=bootloader,
            kernel_entry=kernel_entry,
        )
        _render_install_plan(plan, distro=distro, bootloader=bootloader, mode="commit")
        if not plan.is_valid:
            raise err.hint_unsupported_bootloader(bootloader.kind.value)
        if not execute:
            console.print(
                "\n[dim]dry-run; pass --execute to actually run the commands[/dim]"
            )
            return
        if os.geteuid() != 0:
            raise err.hint_not_root("`autokernel install --commit --execute`")
        result = install_mod.execute(plan, snapshot_dir=snapshot_dir)
        _render_install_result(result)
        if not result.ok:
            raise typer.Exit(result.step_runs[-1].exit_code)
        return

    # ── pre-flight (optional skip) ──────────────────────────────────────
    if not skip_preflight:
        run = preflight_mod.run_checks(
            tags={"always", "install"},
            snapshot=snap,
            distro=distro,
            package_paths=tuple(package or ()),
        )
        _render_preflight(run, distro=distro, for_="install")
        if run.has_failures:
            raise err.fail(
                "preflight check failures — refusing to proceed",
                fix="address the FAILed items above, or rerun with --skip-preflight",
                exit_code=1,
            )

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

    nvidia_plan = nvidia_mod.plan_nvidia_support(
        snapshot=snap,
        distro=distro,
        package_paths=package,
        mode=nvidia,
    )
    if nvidia_plan is not None:
        _render_nvidia_install_plan(nvidia_plan)

    # ── plan + render + (maybe) execute ─────────────────────────────────
    plan = install_mod.build_plan(
        distro=distro,
        spec=spec,
        bootloader=bootloader,
        package_paths=package,
        kernel_entry=kernel_entry,
        enable_probation=not no_probation,
        nvidia_plan=nvidia_plan,
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
    console.print(
        Panel.fit(
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
        )
    )


@app.command()
def rollback(
    snapshot_dir: Annotated[Path, typer.Argument(help="Snapshot directory")],
    execute: Annotated[
        bool, typer.Option(help="Actually run the rollback. Default: dry-run.")
    ] = False,
) -> None:
    """Undo the most recent autokernel install for SNAPSHOT_DIR."""
    import os

    _validate_snapshot_dir(snapshot_dir)
    distro = detect_distro()
    bootloader = bootloader_mod.detect()

    plan = rollback_mod.build_plan(
        snapshot_dir=snapshot_dir,
        distro=distro,
        bootloader=bootloader,
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


def _render_nvidia_install_plan(plan: nvidia_mod.NvidiaDriverPlan) -> None:
    evidence = ", ".join(plan.evidence[:5])
    if len(plan.evidence) > 5:
        evidence += ", …"
    console.print(
        Panel.fit(
            "\n".join(
                [
                    "[cyan]NVIDIA custom-kernel support enabled[/cyan]",
                    f"  target kernel: {plan.kernel_release}",
                    f"  driver:        {plan.package_name} ({plan.flavor})",
                    f"  reason:        {plan.reason}",
                    f"  evidence:      {evidence or 'n/a'}",
                ]
            ),
            title="nvidia",
        )
    )


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


# ── inventory sub-app ─────────────────────────────────────────────────────


@inventory_app.command("scan")
def inventory_scan(
    kernel_source: Annotated[
        Path,
        typer.Argument(help="Kernel source tree containing Kconfig and Makefile"),
    ],
    out: Annotated[
        Path,
        typer.Option("--out", help="Directory to write manifest.json + symbols.jsonl"),
    ],
    arch: Annotated[str, typer.Option("--arch", help="Kernel ARCH")] = "x86_64",
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Optional .config to load for current values"),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Only scan the first N symbols for smoke tests"),
    ] = None,
) -> None:
    """Build a deterministic Kconfig/source inventory."""
    from autokernel.inventory import build_inventory, write_inventory

    console.print(f"[dim]scanning Kconfig inventory under {kernel_source}…[/dim]")
    dataset = build_inventory(
        kernel_source,
        arch=arch,
        config_path=config,
        limit=limit,
    )
    write_inventory(dataset, out)
    console.print(
        f"[green]✓ inventory[/green] symbols={len(dataset.symbols)} out={out}"
    )


@inventory_app.command("search")
def inventory_search(
    inventory_dir: Annotated[Path, typer.Argument(help="Inventory directory")],
    query: Annotated[str, typer.Argument(help="Symbol name or text query")],
    text: Annotated[
        bool,
        typer.Option("--text", help="Search Kconfig prompt/help text instead of names"),
    ] = False,
    limit: Annotated[int, typer.Option("--limit")] = 50,
) -> None:
    """Search inventory symbols by name or Kconfig text."""
    from autokernel.inventory import InventoryTools

    tools = InventoryTools.from_dir(inventory_dir)
    hits = (
        tools.search_kconfig_text(query, limit=limit)
        if text
        else tools.search_symbols(query, limit=limit)
    )
    for symbol in hits:
        rec = tools.get_symbol(symbol)
        console.print(f"{rec.symbol}\t{rec.type.value}\t{rec.prompt or ''}")


@inventory_app.command("show")
def inventory_show(
    inventory_dir: Annotated[Path, typer.Argument(help="Inventory directory")],
    symbol: Annotated[str, typer.Argument(help="CONFIG_ symbol")],
) -> None:
    """Print one inventory record as JSON."""
    from autokernel.inventory import InventoryTools

    rec = InventoryTools.from_dir(inventory_dir).get_symbol(symbol)
    console.print_json(rec.model_dump_json(exclude_none=True))


@inventory_app.command("read-file")
def inventory_read_file(
    inventory_dir: Annotated[Path, typer.Argument(help="Inventory directory")],
    path: Annotated[str, typer.Argument(help="Path inside kernel source tree")],
    source_dir: Annotated[
        Path | None,
        typer.Option(
            "--source-dir",
            help="Override manifest source_dir for vendored inventories",
        ),
    ] = None,
    head: Annotated[int | None, typer.Option("--head")] = None,
    start: Annotated[int | None, typer.Option("--start")] = None,
    end: Annotated[int | None, typer.Option("--end")] = None,
) -> None:
    """Read a bounded source file excerpt through inventory path sandboxing."""
    from autokernel.inventory import InventoryTools

    tools = InventoryTools.from_dir(inventory_dir, source_dir=source_dir)
    if head is not None:
        excerpt = tools.read_file_head(path, max_lines=head)
    elif start is not None and end is not None:
        excerpt = tools.read_file_excerpt(path, start_line=start, end_line=end)
    else:
        excerpt = tools.read_file_head(path)
    console.print(excerpt.text)


@inventory_app.command("enrich")
def inventory_enrich(
    inventory_dir: Annotated[Path, typer.Argument(help="Inventory directory")],
    symbols: Annotated[
        list[str] | None,
        typer.Option("--symbol", help="CONFIG_ symbol to enrich; repeatable"),
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    batch_size: Annotated[int, typer.Option("--batch-size")] = 20,
    jobs: Annotated[
        int,
        typer.Option("--jobs", help="Concurrent enrichment batches"),
    ] = 1,
    source_dir: Annotated[
        Path | None,
        typer.Option(
            "--source-dir",
            help="Override manifest source_dir for vendored inventories",
        ),
    ] = None,
    model: Annotated[
        str,
        typer.Option("--model", help="pydantic-ai model id for enrichment"),
    ] = "openai:gpt-5.4-mini",
    service_tier: Annotated[
        str | None,
        typer.Option("--service-tier", help="OpenAI service tier"),
    ] = "flex",
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Write deterministic baseline enrichment"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Re-enrich rows even when the same symbol/fact hash already exists",
        ),
    ] = False,
) -> None:
    """Enrich inventory records into enrichments.jsonl."""
    from autokernel.inventory import InventoryTools
    from autokernel.inventory_agent import (
        enrich_records,
        offline_enrichment,
        write_enrichments,
    )

    tools = InventoryTools.from_dir(inventory_dir, source_dir=source_dir)
    if batch_size < 1:
        raise typer.BadParameter("--batch-size must be at least 1")
    if jobs < 1:
        raise typer.BadParameter("--jobs must be at least 1")
    if symbols:
        records = [tools.get_symbol(s) for s in symbols]
    else:
        records = tools.dataset.symbols
    if limit is not None:
        records = records[:limit]

    out_path = inventory_dir / "enrichments.jsonl"
    if not force and out_path.exists():
        existing: set[tuple[str, str]] = set()
        with out_path.open(encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as e:
                    raise err.fail(
                        "invalid existing enrichment JSONL",
                        why=f"{out_path}:{line_no}: {e}",
                        fix="delete the invalid enrichments.jsonl or pass a clean inventory directory",
                        exit_code=2,
                    ) from e
                symbol = item.get("symbol")
                fact_hash = item.get("fact_hash")
                if isinstance(symbol, str) and isinstance(fact_hash, str):
                    existing.add((symbol, fact_hash))
        before = len(records)
        records = [r for r in records if (r.symbol, r.fact_hash) not in existing]
        skipped = before - len(records)
        if skipped:
            console.print(f"[dim]skipping {skipped} existing enrichment(s)[/dim]")

    chunks = [records[i : i + batch_size] for i in range(0, len(records), batch_size)]

    def enrich_chunk(chunk):
        if offline:
            return [offline_enrichment(r) for r in chunk]
        return enrich_records(
            chunk,
            tools,
            model=model,
            service_tier=service_tier,
        )

    total = 0
    if jobs == 1 or len(chunks) <= 1:
        for chunk in chunks:
            enrichments = enrich_chunk(chunk)
            write_enrichments(enrichments, out_path)
            total += len(enrichments)
            console.print(f"[dim]enriched {total}/{len(records)}[/dim]")
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        next_chunk = 0
        futures = {}
        with ThreadPoolExecutor(max_workers=jobs) as executor:

            def submit_next() -> None:
                nonlocal next_chunk
                if next_chunk < len(chunks):
                    future = executor.submit(enrich_chunk, chunks[next_chunk])
                    futures[future] = next_chunk
                    next_chunk += 1

            for _ in range(min(jobs, len(chunks))):
                submit_next()

            while futures:
                for future in as_completed(list(futures)):
                    futures.pop(future)
                    enrichments = future.result()
                    write_enrichments(enrichments, out_path)
                    total += len(enrichments)
                    console.print(f"[dim]enriched {total}/{len(records)}[/dim]")
                    submit_next()
                    break
    console.print(f"[green]✓ enrichments[/green] wrote {out_path}")


# ── config sub-app: show / test ────────────────────────────────────────────


@config_app.command("show")
def config_show(
    spec: Annotated[
        str,
        typer.Option(
            "--mode",
            help="Pretend the user passed this --llm-mode / --model and show what it'd resolve to. Default: 'auto'.",
        ),
    ] = "auto",
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
        body = f"[red]✗ cannot resolve {spec!r}[/red]\n  [dim]{err_msg}[/dim]"
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
            t.add_row(
                s.provider.value,
                env_label,
                f"[green]✓ {s.api_key_var}[/green]",
                default_auto,
            )
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
    spec: Annotated[
        str,
        typer.Option(
            "--mode", help="Mode preset or literal model id to test (default: 'auto')."
        ),
    ] = "auto",
    service_tier: Annotated[
        str | None, typer.Option("--service-tier", help="OpenAI service_tier override")
    ] = None,
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
            fix="copy .env.example to .env and set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GOOGLE_API_KEY (or run `autokernel config show` to see all options)",
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


# ── install-deps ───────────────────────────────────────────────────────────


@app.command("install-deps")
def install_deps_cmd(
    target: Annotated[
        installdeps_mod.Target,
        typer.Option(
            "--for", help="What to install for: build / boot-test / install / all"
        ),
    ] = installdeps_mod.Target.ALL,
    execute: Annotated[
        bool,
        typer.Option(
            help="Actually run the install. Default: dry-run (just print the command)."
        ),
    ] = False,
    no_recommended: Annotated[
        bool,
        typer.Option(
            "--no-recommended",
            help="Skip recommended-but-not-required packages (ccache, etc.)",
        ),
    ] = False,
    no_virtme: Annotated[
        bool,
        typer.Option(
            "--no-virtme",
            help="Don't suggest/install virtme-ng (the boot-test enhancement)",
        ),
    ] = False,
) -> None:
    """Install missing system packages for build / boot-test / install.

    Distro-aware: produces the right ``apt``/``dnf``/``pacman``/``zypper``
    command for your detected family. Defaults to dry-run; ``--execute``
    actually runs the install (with sudo for system packages and
    ``uv tool install`` for any optional Python tools like virtme-ng).

    Idempotent: re-running when nothing's missing is a no-op.
    """
    distro = detect_distro()
    spec = spec_for(distro)

    p = installdeps_mod.plan(
        distro=distro,
        spec=spec,
        target=target,
        recommended=not no_recommended,
        include_virtme=not no_virtme,
    )

    if not p.is_valid:
        raise err.fail(
            "can't build install-deps plan",
            why=p.rejected_reason or "unknown",
            fix=(
                "this distro family isn't fully supported; install the kernel "
                "build-deps manually using your distro's package manager"
            ),
            exit_code=2,
        )

    _render_install_deps_plan(p, distro=distro)

    if not p.needs_anything:
        console.print("[green]✓ already up to date — nothing to install[/green]")
        return

    if not execute:
        console.print(
            "\n[dim]dry-run; pass --execute to actually run the commands above[/dim]"
        )
        return

    log_dir = Path.home() / ".cache" / "autokernel" / "install-deps"
    log_dir.mkdir(parents=True, exist_ok=True)
    console.print(
        "\n[cyan]running install plan…[/cyan] [dim](will prompt for sudo password if needed)[/dim]"
    )
    result = installdeps_mod.execute(
        p,
        log_dir=log_dir,
        install_virtme=not no_virtme,
    )
    if not result.ok:
        rc = next((r.exit_code for r in result.runs if r.exit_code != 0), 1)
        raise typer.Exit(rc)
    console.print(
        Panel.fit(
            "[green]✓ install complete[/green]\n"
            f"  installed: {len(p.missing)} system package(s)"
            + (
                f" + {len(p.optional_python_pkgs)} uv tool(s)"
                if p.optional_python_pkgs
                else ""
            )
            + f"\n\n[dim]next: `autokernel preflight --for {target.value}` should now be all-green[/dim]",
            title="autokernel install-deps",
        )
    )


def _render_install_deps_plan(plan_obj, *, distro) -> None:
    console.rule(f"install-deps: --for={plan_obj.target.value}")
    console.print(
        f"[dim]distro: {distro.pretty_name or distro.id} (family={plan_obj.family.value})[/dim]"
    )

    if not plan_obj.is_valid:
        console.print(f"\n[red]✗ plan rejected:[/red] {plan_obj.rejected_reason}")
        return

    if plan_obj.already_installed:
        console.print(
            f"\n[green]✓ already installed ({len(plan_obj.already_installed)}):[/green] "
            f"[dim]{', '.join(plan_obj.already_installed)}[/dim]"
        )

    if plan_obj.missing:
        console.print(
            f"\n[bold]missing system packages ({len(plan_obj.missing)}):[/bold]"
        )
        for pkg in plan_obj.missing:
            console.print(f"  · {pkg}")
        argv = plan_obj.full_argv
        console.print(f"\n[bold]command:[/bold]\n  $ {' '.join(argv)}")
    else:
        console.print("\n[dim]no system packages to install[/dim]")

    if plan_obj.optional_python_pkgs:
        console.print(
            f"\n[bold]optional uv tools ({len(plan_obj.optional_python_pkgs)}):[/bold]"
        )
        for pkg in plan_obj.optional_python_pkgs:
            console.print(f"  · {pkg}  [dim]→ uv tool install {pkg}[/dim]")


# ── boot-test ─────────────────────────────────────────────────────────────


@app.command()
def minitram(
    snapshot_dir: Annotated[
        Path, typer.Argument(help="Snapshot directory from `autokernel scan`")
    ],
    out: Annotated[
        Path | None,
        typer.Option(help="Output path. Default: <snap>/initramfs.cpio.zst"),
    ] = None,
    include_dropbear: Annotated[
        bool,
        typer.Option(
            "--dropbear",
            help="Include static dropbear for headless rescue SSH (~700 KB).",
        ),
    ] = False,
    execute: Annotated[
        bool,
        typer.Option(
            "--execute",
            help="Actually build the cpio.zst archive (default is dry-run, just print the plan).",
        ),
    ] = False,
) -> None:
    """Generate a minimal initramfs from snapshot evidence.

    Today Ubuntu's update-initramfs builds a 40 MB initramfs containing
    every module that *might* be needed across all hosts. autokernel
    knows what's actually load-bearing for THIS host (LUKS in the boot
    chain? LVM? RAID? DKMS modules?) and packs only those — typically
    3-5 MB total.

    Without --execute, prints the plan; pass --execute to actually
    pack the cpio.zst archive.
    """
    from autokernel import minitram as minitram_mod

    _validate_snapshot_dir(snapshot_dir)
    snap = snap_mod.load(snapshot_dir)

    p = minitram_mod.plan(snap, include_dropbear=include_dropbear)

    # Render the plan.
    console.rule("autokernel minitram — plan")
    console.print(f"  kernel:    {p.kernel_release}")
    console.print(f"  busybox:   {p.busybox} (always; provides /bin/sh + applets)")
    console.print(f"  tools:     {len(p.tools)}")
    for t in p.tools:
        console.print(f"    · [yellow]{t.name}[/yellow] ({t.host_path}): {t.rationale}")
        if t.libs:
            console.print(
                f"        deps: {', '.join(Path(lib).name for lib in t.libs[:5])}"
            )
    console.print(f"  modules:   {len(p.modules)}")
    for m in p.modules[:15]:
        console.print(f"    · [cyan]{m.name}[/cyan]: {m.rationale}")
    if len(p.modules) > 15:
        console.print(f"    · … {len(p.modules) - 15} more")
    console.print()
    if not execute:
        console.print(
            "[dim]dry-run; pass --execute to actually build initramfs.cpio.zst[/dim]"
        )
        return

    out_path = out or (snapshot_dir / "initramfs.cpio.zst")
    console.print(f"[cyan]packing {out_path}…[/cyan]")
    try:
        result = minitram_mod.build(p, out_path=out_path)
    except RuntimeError as e:
        err_console.print(f"[red]minitram build failed: {e}[/red]")
        raise typer.Exit(1) from None
    size_mb = result.bytes / (1024 * 1024)
    console.print(
        Panel.fit(
            f"[green]✓ built[/green]\n"
            f"  archive:   {result.archive_path}\n"
            f"  size:      {size_mb:.2f} MB ({result.bytes:,} bytes)\n"
            f"  modules:   {result.n_modules}\n"
            f"  tools:     {result.n_tools}\n"
            f"  plan:      {result.plan_path}",
            title="autokernel minitram",
            border_style="green",
        )
    )


@app.command("boot-test")
def boot_test(
    snapshot_dir: Annotated[Path, typer.Argument(help="Snapshot directory")],
    kernel_source: Annotated[
        Path,
        typer.Option(
            "--kernel-source",
            help="Path to the kernel source tree containing the freshly-built bzImage",
        ),
    ],
    method: Annotated[
        boottest_mod.Method, typer.Option(help="virtme | qemu | auto")
    ] = boottest_mod.Method.AUTO,
    timeout: Annotated[float, typer.Option(help="Hard timeout in seconds")] = 60.0,
    dry_run: Annotated[
        bool, typer.Option(help="Print the plan without executing")
    ] = False,
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
    kernel_release = boottest_mod.detect_kernel_release(
        kernel_source,
        bzimage,
        fallback=snap.kernel.release,
    )
    plan_method = method
    if method == boottest_mod.Method.AUTO:
        detected = boottest_mod.detect_method()
        if (
            detected == boottest_mod.Method.VIRTME
            and not boottest_mod.virtme_root_transport_available(kernel_source)
            and boottest_mod.shutil.which("qemu-system-x86_64")
        ):
            plan_method = boottest_mod.Method.QEMU
        else:
            plan_method = method
    elif (
        method == boottest_mod.Method.VIRTME
        and not boottest_mod.virtme_root_transport_available(kernel_source)
    ):
        raise err.fail(
            "virtme root transport unavailable in built kernel config",
            why=(
                "virtme needs CONFIG_VIRTIO_FS or the 9P stack "
                "(CONFIG_NET_9P, CONFIG_NET_9P_VIRTIO, CONFIG_9P_FS) to mount "
                "the host-backed root filesystem. This localmodconfig build has "
                "those disabled."
            ),
            fix="rerun boot-test with --method qemu, or rebuild with virtiofs/9p enabled",
            exit_code=2,
        )
    try:
        plan_obj = boottest_mod.plan(
            method=plan_method,
            bzimage_path=bzimage,
            kernel_release=kernel_release,
            timeout=timeout,
        )
    except RuntimeError as e:
        raise err.fail(
            "no boot-test runtime available",
            why=str(e),
            fix=(
                "`autokernel install-deps --for boot-test --execute` (covers both: "
                "system qemu-system-x86 + uv-tool-installed virtme-ng), or run "
                "`autokernel preflight --for boot-test` for distro-specific hints."
            ),
            exit_code=1,
        )

    _render_boot_test_plan(plan_obj)
    if dry_run:
        console.print("\n[dim]dry-run; pass without --dry-run to actually boot[/dim]")
        return

    console.print(
        f"\n[cyan]booting kernel {plan_obj.kernel_release} via {plan_obj.method.value}…[/cyan]"
    )
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
        console.print(
            Panel.fit(
                f"[green]✓ PASS[/green]\n"
                f"  reason:   {result.verdict.reason}\n"
                f"  duration: {result.duration_s:.1f}s\n"
                f"  bzimage:  sha256:{result.bzimage_sha256[:16]}…\n"
                f"  log:      {result.serial_log_path}\n"
                f"  record:   {result.record_path}",
                title="boot-test",
                border_style="green",
            )
        )
    else:
        console.print(
            Panel.fit(
                f"[red]✗ FAIL[/red]\n"
                f"  reason:   {result.verdict.reason}\n"
                f"  exit:     {result.exit_code}\n"
                f"  duration: {result.duration_s:.1f}s\n"
                f"  log:      {result.serial_log_path}\n"
                f"  [yellow]inspect the serial log to see what went wrong[/yellow]",
                title="boot-test",
                border_style="red",
            )
        )


# ── iterate (closed-loop) ─────────────────────────────────────────────────


@app.command()
def iterate(
    snapshot_dir: Annotated[
        Path, typer.Argument(help="Snapshot directory from `autokernel scan`")
    ],
    kernel_source: Annotated[
        Path,
        typer.Option(
            "--kernel-source",
            help="Kernel source tree (must contain Makefile + Kconfig)",
        ),
    ],
    max_iterations: Annotated[
        int,
        typer.Option(
            "--max-iterations", help="Stop after N rounds even if not converged"
        ),
    ] = 3,
    target: Annotated[
        str,
        typer.Option(
            "--target", help="Fitness function: size | boot-time | surface | balanced"
        ),
    ] = "size",
    converge: Annotated[
        str,
        typer.Option(
            "--converge",
            help="Stop early when: stable-size | no-new-proposals | max-iter",
        ),
    ] = "stable-size",
    auto_revert: Annotated[
        bool,
        typer.Option(
            "--auto-revert/--no-auto-revert",
            help="Revert + continue when an iteration regresses",
        ),
    ] = True,
    execute: Annotated[
        bool,
        typer.Option(
            "--execute",
            help="Actually run the build for each iteration. Without --execute, runs propose+check+apply only (dry).",
        ),
    ] = False,
    # Pass-through to propose:
    dimension: Annotated[
        str, typer.Option("--dimension", help="Which optimization dimensions to run.")
    ] = "all",
    workload: Annotated[str | None, typer.Option("--workload")] = None,
    threat: Annotated[str | None, typer.Option("--threat")] = None,
    modules_strategy: Annotated[str | None, typer.Option("--modules")] = None,
    aggression: Annotated[str | None, typer.Option("--aggression")] = None,
    preset: Annotated[str | None, typer.Option("--preset")] = None,
    autonomy: Annotated[
        AutonomyLevel, typer.Option(help="propose autonomy level")
    ] = AutonomyLevel.ADVISE,
    llm_mode: Annotated[str, typer.Option("--llm-mode")] = "auto",
    model: Annotated[str | None, typer.Option("--model")] = None,
    service_tier: Annotated[str | None, typer.Option()] = None,
    compiler: Annotated[
        str,
        typer.Option(
            "--compiler",
            help="Compiler: clang (default) / llvm / gcc. Forwarded to each iteration's build.",
        ),
    ] = "clang",
    lto: Annotated[
        str,
        typer.Option(
            "--lto",
            help="LTO: none (default) / thin / full. Forwarded to each iteration's build.",
        ),
    ] = "none",
) -> None:
    """Run the closed-loop optimizer: propose → check → apply → build →
    boot-test → measure, for up to ``--max-iterations`` rounds.

    Each round's results feed into the next propose call as context, so
    the LLM learns from regressions and stops re-proposing things that
    failed.

    Without ``--execute`` this runs propose+check+apply only — useful
    to see how the proposals evolve before committing to a slow build.
    With ``--execute`` it goes the full distance per round.
    """
    from autokernel.iteration import (
        has_converged,
        load_history,
        save_record,
    )
    from autokernel.optimize_context import context_from_flags
    from autokernel.workload import detect as detect_workload

    _validate_snapshot_dir(snapshot_dir)
    snap = snap_mod.load(snapshot_dir)

    # Compose the context once; passed to every iteration's propose.
    detection = detect_workload(snap)
    try:
        ctx = context_from_flags(
            preset=preset,
            workload=workload,
            threat=threat,
            modules=modules_strategy,
            aggression=aggression,
            detected_workload=detection.profile,
        )
    except (KeyError, ValueError) as e:
        raise typer.BadParameter(str(e))

    console.rule(
        f"iterate — target={target}, converge={converge}, max={max_iterations}"
    )
    console.print(
        f"context: workload=[bold]{ctx.workload.value}[/bold] "
        f"threat=[bold]{ctx.threat.value}[/bold] "
        f"modules=[bold]{ctx.modules.value}[/bold] "
        f"aggression=[bold]{ctx.aggression.value}[/bold]"
    )

    history = load_history(snapshot_dir)
    if history:
        console.print(f"[dim]loaded {len(history)} prior iteration(s)[/dim]")

    start_iter = (history[-1].iteration + 1) if history else 1
    end_iter = start_iter + max_iterations

    for iter_n in range(start_iter, end_iter):
        console.rule(f"iteration {iter_n}")
        record = _run_one_iteration(
            iter_n=iter_n,
            snap=snap,
            snapshot_dir=snapshot_dir,
            kernel_source=kernel_source,
            ctx=ctx,
            dimension=dimension,
            autonomy=autonomy,
            llm_mode=llm_mode,
            model=model,
            service_tier=service_tier,
            execute=execute,
            auto_revert=auto_revert,
            history=history,
            compiler=compiler,
            lto=lto,
            target=target,
        )
        save_record(snapshot_dir, record)
        history.append(record)

        # Convergence check.
        if converge == "stable-size" and has_converged(
            history, window=2, size_delta_pct=1.0
        ):
            console.print(
                f"[green]converged on size at iteration {iter_n} — stopping.[/green]"
            )
            break
        if converge == "no-new-proposals" and not record.proposals:
            console.print(
                f"[green]no new proposals at iteration {iter_n} — converged.[/green]"
            )
            break

    # Summary.
    console.rule("iterate summary")
    if history:
        sizes = [
            r.measurements.bzimage_bytes
            for r in history
            if r.measurements.bzimage_bytes
        ]
        if len(sizes) >= 2:
            delta = (sizes[-1] - sizes[0]) / sizes[0] * 100
            console.print(
                f"  iterations: {len(history)}"
                f"  bzImage: {sizes[0] / 1e6:.2f}MB → {sizes[-1] / 1e6:.2f}MB "
                f"({delta:+.1f}%)"
            )
        passes = sum(1 for r in history if r.measurements.boot_test_passed)
        fails = sum(1 for r in history if r.measurements.boot_test_passed is False)
        console.print(f"  boot-test: {passes} passed, {fails} failed")


def _run_config_check(snapshot_dir: Path, kernel_source: Path) -> tuple[int, int]:
    """Walk Kconfig + check the snapshot's final.config; render report;
    return (n_errors, n_warnings).

    Output rendered into the iter_dir's progress trail via console.
    Doesn't auto-drop proposals from the kfrag (yet) — that's a v0.16+
    follow-up; for now we just surface the findings.
    """
    final_cfg = snapshot_dir / "final.config"
    if not final_cfg.exists():
        return (0, 0)
    try:
        from autokernel.config_check import check
        from autokernel.kconfig_walk import walk

        surface = walk(
            kernel_source,
            arch="x86_64",
            config_path=snapshot_dir.parent / "running_config"
            if (snapshot_dir.parent / "running_config").exists()
            else None,
        )
        report = check(final_cfg.read_text(), surface)
        if report.errors:
            console.print(f"[red]config_check: {len(report.errors)} errors[/red]")
            for f in report.errors[:5]:
                console.print(f"  [red]✗[/red] {f.symbol}: {f.detail}")
        if report.warnings:
            console.print(
                f"[yellow]config_check: {len(report.warnings)} warnings[/yellow]"
            )
            for f in report.warnings[:3]:
                console.print(f"  [yellow]·[/yellow] {f.symbol}: {f.detail}")
        return (len(report.errors), len(report.warnings))
    except Exception as e:
        # Don't let a check failure block iterate's progress; just log.
        console.print(f"[yellow]config_check skipped: {e}[/yellow]")
        return (0, 0)


def _run_one_iteration(
    *,
    iter_n: int,
    snap,
    snapshot_dir: Path,
    kernel_source: Path,
    ctx,
    dimension: str,
    autonomy: AutonomyLevel,
    llm_mode: str,
    model: str | None,
    service_tier: str | None,
    compiler: str = "clang",
    lto: str = "none",
    target: str = "size",
    execute: bool,
    auto_revert: bool,
    history: list,
):
    """One iteration of the closed loop. Returns an IterationRecord."""
    import sys
    import time
    from autokernel.iteration import (
        IterationRecord,
        iteration_dir,
        summarize_history_for_prompt,
    )
    from autokernel.measurements import measure

    iter_dir = iteration_dir(snapshot_dir, iter_n)
    iter_dir.mkdir(parents=True, exist_ok=True)

    # Tail-able progress log on disk — one line per step transition,
    # always flushed. Lets the user run
    # ``tail -f <snap>/iterations/i<NNN>/progress.log`` to see where we
    # are without depending on subprocess buffering.
    progress_path = iter_dir / "progress.log"
    progress_f = progress_path.open("w", buffering=1)  # line-buffered

    def _step(label: str, *, done: bool = False, extra: str = "") -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        marker = "✓" if done else "→"
        msg = f"[{ts}] i={iter_n} {marker} {label}{(' ' + extra) if extra else ''}"
        # Always flush — both to terminal and to the progress log.
        print(msg, flush=True, file=sys.stderr)
        progress_f.write(msg + "\n")

    _step("starting iteration")

    # 1. propose — invoke the propose verb's logic with our ctx.
    _step(f"propose --dimension={dimension}")
    history_block = (
        summarize_history_for_prompt(history, target=target) if history else None
    )
    if history_block:
        console.print("[dim]" + history_block + "[/dim]")

    # Closed-loop wiring: write the history block to disk so the propose
    # subprocess can read it via --history-from. For iteration N>1, also
    # point --base-config at the previous round's final.config so this
    # round proposes on top of the prior round's accepted changes.
    history_path: Path | None = None
    if history_block:
        history_path = iter_dir / "history.txt"
        history_path.write_text(history_block)

    # base-config: the .config the LLM compares against. We prefer the
    # post-build .config (what *actually* got compiled — i.e. what
    # olddefconfig + localmodconfig settled on) over the kfrag-merged
    # final.config (what we *asked for*). Otherwise the LLM in round
    # N+1 sees =n for symbols that olddefconfig stripped to =y, and
    # re-proposes them every round forever. Fall back to final.config
    # only when we don't have a post-build snapshot (dry-run or
    # build-failed cases).
    base_config_path: Path | None = None
    if history:
        prev = iteration_dir(snapshot_dir, history[-1].iteration)
        post_build = prev / "post_build.config"
        kfrag_merged = prev / "final.config"
        if post_build.exists():
            base_config_path = post_build
        elif kfrag_merged.exists():
            base_config_path = kfrag_merged

    # NOTE: we call the propose function directly, not through Typer.
    # This requires reusing the workhorse logic. For brevity and to
    # avoid duplicating the entire 250-line propose function, we just
    # invoke `autokernel propose` as a subprocess in this iteration's
    # output dir. That keeps each iteration's artifacts separate and
    # keeps this orchestrator small.
    import subprocess

    proposal_argv = [
        "uv",
        "run",
        "autokernel",
        "propose",
        str(snapshot_dir),
        f"--autonomy={autonomy.value}",
        f"--dimension={dimension}",
        f"--workload={ctx.workload.value}",
        f"--threat={ctx.threat.value}",
        f"--modules={ctx.modules.value}",
        f"--aggression={ctx.aggression.value}",
        f"--kernel-source={kernel_source}",
        f"--out={iter_dir / 'proposal.json'}",
        f"--llm-mode={llm_mode}",
    ]
    if history_path is not None:
        proposal_argv += [f"--history-from={history_path}"]
    if base_config_path is not None:
        proposal_argv += [f"--base-config={base_config_path}"]
    if model:
        proposal_argv += [f"--model={model}"]
    if service_tier:
        proposal_argv += [f"--service-tier={service_tier}"]

    t0 = time.time()
    # PYTHONUNBUFFERED=1 + FORCE_COLOR=1 keep the propose subprocess's
    # per-batch progress flushing through Rich/typer without TTY
    # buffering. Otherwise users see nothing for ~10 minutes during the
    # LLM batch loop.
    propose_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    rc = subprocess.run(proposal_argv, cwd=Path.cwd(), env=propose_env).returncode
    _step("propose done", done=True, extra=f"({time.time() - t0:.1f}s, rc={rc})")

    # propose --out wrote to iter_dir/proposal.json. The subsequent
    # review verb reads from <snap>/proposal.json — copy it across so
    # the chain works. (Each iteration's authoritative artifact lives
    # in iter_dir/; the snap_dir/ copy is just plumbing for review.)
    iter_proposal = iter_dir / "proposal.json"
    if iter_proposal.exists():
        (snapshot_dir / "proposal.json").write_text(iter_proposal.read_text())

    if rc != 0:
        return IterationRecord(
            iteration=iter_n,
            ctx_summary={
                "workload": ctx.workload.value,
                "threat": ctx.threat.value,
                "modules": ctx.modules.value,
                "aggression": ctx.aggression.value,
            },
            proposals=[],
            measurements=__import__(
                "autokernel.measurements", fromlist=["BuildMeasurements"]
            ).BuildMeasurements(),
            regressed=True,
            revert_reason=f"propose returned {rc}",
        )

    # Read what was proposed.
    proposal_path = iter_dir / "proposal.json"
    proposal = json.loads(proposal_path.read_text()) if proposal_path.exists() else {}
    proposed_syms = [
        p["config"]
        for p in (proposal.get("auto_applied", []) + proposal.get("needs_review", []))
    ]
    console.print(f"[dim]i={iter_n}: {len(proposed_syms)} proposals[/dim]")

    if not execute:
        # Dry run: still run review + apply (both fast — no LLM, just
        # data merging) so iteration N+1 has a real final.config to
        # chain off of via --base-config. Skip the slow build.
        import subprocess

        _step("review (--accept-recommended)")
        t0 = time.time()
        subprocess.run(
            [
                "uv",
                "run",
                "autokernel",
                "review",
                str(snapshot_dir),
                "--accept-recommended",
            ],
            cwd=Path.cwd(),
        )
        _step("review done", done=True, extra=f"({time.time() - t0:.1f}s)")
        _step("apply")
        t0 = time.time()
        subprocess.run(
            ["uv", "run", "autokernel", "apply", str(snapshot_dir)],
            cwd=Path.cwd(),
        )
        _step("apply done", done=True, extra=f"({time.time() - t0:.1f}s)")
        # Chain artifact for the next round.
        final_cfg_src = snapshot_dir / "final.config"
        if final_cfg_src.exists():
            (iter_dir / "final.config").write_text(final_cfg_src.read_text())
            _step("snapshotted final.config to iter dir for chaining", done=True)
        progress_f.close()

        from autokernel.measurements import BuildMeasurements

        return IterationRecord(
            iteration=iter_n,
            ctx_summary={
                "workload": ctx.workload.value,
                "threat": ctx.threat.value,
                "modules": ctx.modules.value,
                "aggression": ctx.aggression.value,
            },
            proposals=proposed_syms,
            measurements=BuildMeasurements(proposed_count=len(proposed_syms)),
            note="dry run (no --execute)",
        )

    # 2. review + apply via subprocess (same reuse pattern).
    _step("review (--accept-recommended)")
    t0 = time.time()
    review_argv = [
        "uv",
        "run",
        "autokernel",
        "review",
        str(snapshot_dir),
        "--accept-recommended",
    ]
    subprocess.run(review_argv, cwd=Path.cwd())
    _step("review done", done=True, extra=f"({time.time() - t0:.1f}s)")
    _step("apply")
    t0 = time.time()
    apply_argv = ["uv", "run", "autokernel", "apply", str(snapshot_dir)]
    subprocess.run(apply_argv, cwd=Path.cwd())
    _step("apply done", done=True, extra=f"({time.time() - t0:.1f}s)")

    # Snapshot iteration N's final.config into the iter dir so iter N+1
    # can chain off of it via --base-config (closed-loop).
    final_cfg_src = snapshot_dir / "final.config"
    if final_cfg_src.exists():
        (iter_dir / "final.config").write_text(final_cfg_src.read_text())

    # 2.5. config_check — catches LLM hallucinations + dead-letter
    # choices (parent feature disabled) BEFORE the slow build wastes
    # time. Errors block; we drop the affected proposals from the
    # kfrag and re-apply.
    _step("config_check (against target Kconfig)")
    t0 = time.time()
    n_errors, n_warnings = _run_config_check(snapshot_dir, kernel_source)
    _step(
        "config_check done",
        done=True,
        extra=f"({time.time() - t0:.1f}s, {n_errors} errors, {n_warnings} warnings)",
    )

    # 3. build prepare + execute.
    _step("build --execute --localmodconfig (slow — see iter dir build.log)")
    t0 = time.time()
    build_log_path = iter_dir / "build.log"
    build_argv = [
        "uv",
        "run",
        "autokernel",
        "build",
        str(snapshot_dir),
        f"--kernel-source={kernel_source}",
        "--localmodconfig",
        "--execute",
        # Iterate doesn't install — just needs `bzImage modules` to
        # validate the build + boot-test. Skips packaging deps
        # (debhelper-compat, rpmbuild, etc.) that bindeb-pkg/rpm-pkg
        # would require.
        "--target=kernel-only",
        f"--compiler={compiler}",
        f"--lto={lto}",
    ]
    with build_log_path.open("w") as f:
        rc = subprocess.run(
            build_argv, cwd=Path.cwd(), stdout=f, stderr=subprocess.STDOUT
        ).returncode
    build_failed = rc != 0

    # Snapshot the post-build .config — the actual state the kernel
    # was compiled with, after olddefconfig + localmodconfig + olddefconfig.
    # iteration N+1's --base-config will point here so the LLM sees what
    # really took effect, not just what we asked for.
    if not build_failed:
        post_build = kernel_source / ".config"
        if post_build.exists():
            (iter_dir / "post_build.config").write_text(post_build.read_text())

    _step(
        "build done" if not build_failed else "build FAILED",
        done=True,
        extra=f"({time.time() - t0:.0f}s, rc={rc})",
    )

    # 4. boot-test (if build passed).
    if not build_failed:
        _step("boot-test")
        t0 = time.time()
        bt_argv = [
            "uv",
            "run",
            "autokernel",
            "boot-test",
            str(snapshot_dir),
            f"--kernel-source={kernel_source}",
        ]
        subprocess.run(bt_argv, cwd=Path.cwd())
        _step("boot-test done", done=True, extra=f"({time.time() - t0:.1f}s)")

    # 5. measure.
    # When build failed, the bzImage/vmlinux/modules in the source tree
    # are STALE artifacts from a prior successful build — measuring them
    # would lie about this iteration's output. Pass source_dir=None so
    # measure() returns Nones for those fields. The proposed-vs-actual
    # diff still works against final.config + the (possibly stale)
    # source/.config; that's still informative.
    final_cfg = (
        (snapshot_dir / "final.config").read_text()
        if (snapshot_dir / "final.config").exists()
        else None
    )
    actual_cfg = (
        (kernel_source / ".config").read_text()
        if (kernel_source / ".config").exists()
        else None
    )
    measurements = measure(
        snapshot_dir=snapshot_dir,
        source_dir=None if build_failed else kernel_source,
        proposed_config_text=final_cfg,
        actual_config_text=actual_cfg,
        build_log=build_log_path.read_text() if build_log_path.exists() else None,
    )

    # 6. detect regression.
    regressed = build_failed or (measurements.boot_test_passed is False)
    revert_reason = None
    if regressed and auto_revert:
        revert_reason = (
            "build failed"
            if build_failed
            else f"boot test failed: {measurements.boot_failure_mode or 'unknown'}"
        )
        # Restore previous final.config from history if available.
        if history:
            prev_dir = iteration_dir(snapshot_dir, history[-1].iteration)
            prev_final = prev_dir / "final.config"
            if prev_final.exists():
                (snapshot_dir / "final.config").write_text(prev_final.read_text())
                console.print(
                    f"[yellow]reverted to i={history[-1].iteration}'s final.config[/yellow]"
                )

    _step(
        f"iteration done — bzImage={measurements.bzimage_bytes}, "
        f"boot_passed={measurements.boot_test_passed}",
        done=True,
    )
    progress_f.close()

    return IterationRecord(
        iteration=iter_n,
        ctx_summary={
            "workload": ctx.workload.value,
            "threat": ctx.threat.value,
            "modules": ctx.modules.value,
            "aggression": ctx.aggression.value,
        },
        proposals=proposed_syms,
        measurements=measurements,
        regressed=regressed,
        revert_reason=revert_reason,
    )


def _main() -> None:
    app()


if __name__ == "__main__":
    _main()
