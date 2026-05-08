"""Centralized error hints + a uniform renderer.

Goal: every error path the CLI exposes follows the same shape — a
one-line summary of what went wrong, optionally a one-line *why*, and
exactly one actionable next step. The user should never be left
guessing what to type next.

Two pieces:

* :func:`fail` is the single ``err_console.print + typer.Exit`` call.
  Verbs use it for every non-success exit path so format and exit codes
  stay consistent.
* The ``hint_*`` helpers wrap the most common failure modes (missing
  snapshot, missing API key, unsupported bootloader, etc.) so the
  wording is written once and reused. They take just the data they need
  and emit a fully-formatted message.

The renderer uses the project's existing Rich console; no new deps.
Hints are intentionally **distro-aware** wherever possible — a missing
``flex`` becomes ``sudo apt install -y flex`` or ``sudo dnf install -y
flex`` based on the detected family.
"""

from __future__ import annotations

import typer
from rich.console import Console

from autokernel.distro import DistroSpec

err_console = Console(stderr=True)


# ── core renderer ──────────────────────────────────────────────────────────


def fail(
    summary: str,
    *,
    fix: str | None = None,
    why: str | None = None,
    exit_code: int = 1,
) -> "typer.Exit":
    """Render an error to stderr in the standard 3-line format and raise.

    Always raises :class:`typer.Exit` — the function's return type is
    annotated only so callers can ``raise fail(...)`` for type-checker
    happiness::

        raise fail("snapshot not found", fix="run `autokernel scan DIR`")

    The output looks like::

        ✗ snapshot not found
          why: /tmp/x has no manifest
          → run `autokernel scan /tmp/x`
    """
    err_console.print(f"[red]✗[/red] [red]{summary}[/red]")
    if why:
        err_console.print(f"  [dim]why: {why}[/dim]")
    if fix:
        err_console.print(f"  [cyan]→ {fix}[/cyan]")
    return typer.Exit(exit_code)


# ── snapshot / artifact path hints ─────────────────────────────────────────


def hint_not_a_snapshot(path) -> "typer.Exit":
    return fail(
        f"not an autokernel snapshot: {path}",
        why=f"{path} has no manifest file (or doesn't exist)",
        fix=f"run `autokernel scan {path}` to create one",
        exit_code=2,
    )


def hint_no_running_config(path) -> "typer.Exit":
    return fail(
        f"snapshot has no running .config: {path}",
        why="/proc/config.gz and /boot/config-$(uname -r) were both unreadable at scan time",
        fix=(
            "rerun `autokernel scan` on a host with /proc/config.gz available "
            "(set CONFIG_IKCONFIG_PROC=y) or /boot/config-$(uname -r) readable"
        ),
        exit_code=1,
    )


def hint_missing_proposal(snapshot_dir, proposal_path) -> "typer.Exit":
    return fail(
        f"proposal not found: {proposal_path}",
        fix=f"run `autokernel propose {snapshot_dir}` first",
        exit_code=2,
    )


def hint_missing_kfrag(snapshot_dir, kfrag_path) -> "typer.Exit":
    return fail(
        f"kfrag not found: {kfrag_path}",
        fix=f"run `autokernel review {snapshot_dir} --accept-recommended` first",
        exit_code=2,
    )


def hint_missing_final_config(snapshot_dir, final_path) -> "typer.Exit":
    return fail(
        f"final.config not found: {final_path}",
        fix=f"run `autokernel apply {snapshot_dir}` first",
        exit_code=2,
    )


def hint_missing_kernel_source(path) -> "typer.Exit":
    return fail(
        f"kernel source dir not found or invalid: {path}",
        why="must contain a top-level Makefile",
        fix="run `autokernel fetch-source` to acquire one, or pass --kernel-source PATH",
        exit_code=1,
    )


# ── system / environment hints ─────────────────────────────────────────────


def hint_missing_api_key() -> "typer.Exit":
    return fail(
        "no LLM API key configured",
        fix=(
            "copy .env.example to .env and fill in ANTHROPIC_API_KEY or OPENAI_API_KEY, "
            "or export one in your shell. Use `--skip-llm` to run without an LLM."
        ),
        exit_code=1,
    )


def hint_missing_build_deps(spec: DistroSpec, missing: list[str]) -> "typer.Exit":
    """Distro-aware hint when build tools / dev libs are missing."""
    pkgs = " ".join(missing)
    install_cmd = (
        f"{' '.join(spec.install_cmd)} {pkgs}"
        if spec.install_cmd
        else f"install {pkgs} via your distro's package manager"
    )
    sudo = "sudo " if spec.install_cmd else ""
    return fail(
        f"missing build dependencies: {', '.join(missing)}",
        fix=f"{sudo}{install_cmd}  (or run `autokernel preflight --for build` for a full check)",
        exit_code=1,
    )


def hint_dkms_blocks_auto(dkms_names: list[str]) -> "typer.Exit":
    return fail(
        f"DKMS modules present ({', '.join(dkms_names)}) — refusing auto-* autonomy",
        why="custom kernels need each DKMS module to rebuild successfully or boot will fail",
        fix=(
            "verify rebuilds with `dkms autoinstall` against the new kernel, "
            "then re-run with --force-dkms; or use --autonomy=advise for manual review"
        ),
        exit_code=3,
    )


# ── bootloader / install hints ─────────────────────────────────────────────


def hint_unsupported_bootloader(bl_kind: str) -> "typer.Exit":
    return fail(
        f"bootloader '{bl_kind}' not yet supported by `autokernel install`",
        why="v1 supports GRUB2 only (Debian/Fedora/Arch defaults)",
        fix=(
            "install the .deb/.rpm manually with your package manager, then use the "
            "bootloader's own one-shot mechanism (e.g. `bootctl set-oneshot` for systemd-boot)"
        ),
        exit_code=4,
    )


def hint_not_root(operation: str) -> "typer.Exit":
    return fail(
        f"{operation} requires root",
        fix=f"rerun with `sudo`, or omit `--execute` to dry-run instead",
        exit_code=5,
    )


def hint_only_kernel_installed() -> "typer.Exit":
    return fail(
        "refusing to install: this would be the only kernel on the system",
        why=(
            "probation needs a fallback kernel to boot if the new one fails. "
            "If your custom kernel hangs, you'd be stuck with no recovery path."
        ),
        fix="keep at least one distro-provided kernel installed alongside; or override with --no-probation (NOT RECOMMENDED)",
        exit_code=4,
    )


def hint_load_bearing_brick(symbols: list[str]) -> "typer.Exit":
    sample = ", ".join(symbols[:3]) + ("…" if len(symbols) > 3 else "")
    return fail(
        f"merge would brick the box: {len(symbols)} load-bearing symbol(s) end up disabled",
        why=f"affected: {sample}",
        fix=(
            "remove the offending entries from auto.kfrag, or re-run "
            "`autokernel review` with `--reject-pattern`/`--reject-subsystem` to keep them; "
            "or pass --no-validate to override (NOT RECOMMENDED — it could prevent boot)"
        ),
        exit_code=4,
    )
