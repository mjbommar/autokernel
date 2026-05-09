#!/usr/bin/env python3
"""Rich-monitored hardware boot smoke workflow.

This is the Python counterpart to ``scripts/hardware-reboot-smoke.sh``. It
keeps the same conservative defaults, but manages subprocesses directly so the
slow kernel build can be monitored from the parent process.

Run from the repository root with:

    uv run python scripts/hardware-reboot-smoke.py
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


console = Console()
err_console = Console(stderr=True)


@dataclass
class Step:
    name: str
    status: str = "pending"
    started: float | None = None
    finished: float | None = None
    returncode: int | None = None

    @property
    def elapsed(self) -> float:
        end = self.finished if self.finished is not None else time.monotonic()
        start = self.started if self.started is not None else end
        return max(0.0, end - start)


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int


@dataclass
class MonitorState:
    step: Step
    source_dir: Path | None = None
    snapshot_dir: Path | None = None
    log_dir: Path | None = None
    command_lines: deque[str] = field(default_factory=lambda: deque(maxlen=12))
    stdout_tail: deque[str] = field(default_factory=lambda: deque(maxlen=10))
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=5))
    object_count: int = 0
    clang_jobs: int = 0
    gcc_jobs: int = 0
    tree_size: str = "-"
    packages: list[Path] = field(default_factory=list)
    last_metric_update: float = 0.0


class SudoKeeper:
    """Prompt once with sudo -v and refresh the timestamp in the background."""

    def __init__(self, *, interval_s: float = 45.0) -> None:
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        console.rule("sudo")
        console.print("[bold]Requesting sudo credentials[/bold]")
        rc = subprocess.run(["sudo", "-v"], check=False).returncode
        if rc != 0:
            raise SystemExit(rc)
        self._thread = threading.Thread(target=self._keepalive, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _keepalive(self) -> None:
        while not self._stop.wait(self.interval_s):
            subprocess.run(
                ["sudo", "-n", "true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _env_default(name: str, default: str) -> str:
    return os.environ.get(name, default)


def parse_args(argv: list[str]) -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser(
        description="Build and optionally one-shot boot an autokernel kernel with Rich monitoring."
    )
    parser.add_argument(
        "--work-dir",
        default=_env_default(
            "AUTOKERNEL_HW_WORK_DIR",
            str(home / ".local/share/autokernel/hardware-boot"),
        ),
    )
    parser.add_argument(
        "--snapshot-dir", default=_env_default("AUTOKERNEL_HW_SNAPSHOT_DIR", "")
    )
    parser.add_argument(
        "--kernel-cache", default=_env_default("AUTOKERNEL_HW_KERNEL_CACHE", "")
    )
    parser.add_argument(
        "--kernel-source", default=_env_default("AUTOKERNEL_HW_KERNEL_SOURCE", "")
    )
    parser.add_argument(
        "--kernel-version",
        default=_env_default(
            "AUTOKERNEL_HW_KERNEL_VERSION", _run_text(["uname", "-r"])
        ),
    )
    parser.add_argument(
        "--fetch-method", default=_env_default("AUTOKERNEL_HW_FETCH_METHOD", "tarball")
    )
    parser.add_argument(
        "--jobs",
        default=_env_default("AUTOKERNEL_HW_JOBS", str(os.cpu_count() or 4)),
    )
    parser.add_argument(
        "--target", default=_env_default("AUTOKERNEL_HW_TARGET", "auto")
    )
    parser.add_argument(
        "--compiler", default=_env_default("AUTOKERNEL_HW_COMPILER", "clang")
    )
    parser.add_argument(
        "--localversion",
        default=_env_default(
            "AUTOKERNEL_HW_LOCALVERSION",
            "-autokernel-" + datetime.now(UTC).strftime("%Y%m%d%H%M"),
        ),
    )
    parser.add_argument(
        "--kernel-entry", default=_env_default("AUTOKERNEL_HW_KERNEL_ENTRY", "")
    )
    parser.add_argument(
        "--dimension",
        default=_env_default("AUTOKERNEL_HW_DIMENSION", "choices,toggles,tunables"),
    )
    parser.add_argument(
        "--candidate-scope",
        default=_env_default("AUTOKERNEL_HW_CANDIDATE_SCOPE", "focused"),
    )
    parser.add_argument(
        "--max-candidates",
        default=_env_default("AUTOKERNEL_HW_MAX_CANDIDATES", "480"),
    )
    parser.add_argument(
        "--llm-mode", default=_env_default("AUTOKERNEL_HW_LLM_MODE", "auto")
    )
    parser.add_argument("--model", default=_env_default("AUTOKERNEL_HW_MODEL", ""))
    parser.add_argument(
        "--service-tier", default=_env_default("AUTOKERNEL_HW_SERVICE_TIER", "")
    )
    parser.add_argument("--preset", default=_env_default("AUTOKERNEL_HW_PRESET", ""))
    parser.add_argument(
        "--workload", default=_env_default("AUTOKERNEL_HW_WORKLOAD", "")
    )
    parser.add_argument(
        "--threat", default=_env_default("AUTOKERNEL_HW_THREAT", "balanced")
    )
    parser.add_argument(
        "--modules", default=_env_default("AUTOKERNEL_HW_MODULES", "distro")
    )
    parser.add_argument(
        "--aggression", default=_env_default("AUTOKERNEL_HW_AGGRESSION", "aggressive")
    )
    parser.add_argument(
        "--boot-test-method",
        default=_env_default("AUTOKERNEL_HW_BOOT_TEST_METHOD", "qemu"),
        choices=("qemu", "virtme", "auto"),
    )
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--no-deps", action="store_true")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--reboot", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true")
    parser.add_argument("--allow-secure-boot", action="store_true")
    return parser.parse_args(argv)


def _run_text(argv: list[str]) -> str:
    result = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False
    )
    return result.stdout.strip()


def _ak(repo_root: Path, *args: str) -> list[str]:
    return ["uv", "--project", str(repo_root), "run", "autokernel", *args]


def _direct_autokernel(repo_root: Path, *args: str) -> list[str]:
    return [str(repo_root / ".venv/bin/autokernel"), *args]


def _sudo_env(argv: list[str], env: dict[str, str]) -> list[str]:
    return [
        "sudo",
        "env",
        f"PATH={env.get('PATH', '')}",
        f"HOME={env.get('HOME', str(Path.home()))}",
        *argv,
    ]


def _format_argv(argv: Iterable[str]) -> str:
    return " ".join(shlex_quote(a) for a in argv)


def shlex_quote(value: str) -> str:
    if not value:
        return "''"
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def run_command(
    name: str,
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stream: bool = True,
) -> CommandResult:
    console.rule(name)
    console.print(f"[dim]$ {_format_argv(argv)}[/dim]")
    if not stream:
        rc = subprocess.run(argv, cwd=cwd, env=env, check=False).returncode
        if rc != 0:
            raise SystemExit(rc)
        return CommandResult(argv=argv, returncode=rc)

    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            console.print(line.rstrip())
        rc = proc.wait()
    except KeyboardInterrupt:
        _terminate_process_tree(proc)
        raise
    if rc != 0:
        raise SystemExit(rc)
    return CommandResult(argv=argv, returncode=rc)


def run_monitored_build(
    name: str,
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    source_dir: Path,
    snapshot_dir: Path,
) -> CommandResult:
    step = Step(name=name, status="running", started=time.monotonic())
    state = MonitorState(step=step, source_dir=source_dir, snapshot_dir=snapshot_dir)
    q: queue.Queue[str | None] = queue.Queue()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line.rstrip())
        q.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    try:
        with Live(
            _render_monitor(state),
            console=console,
            refresh_per_second=2,
            transient=False,
        ) as live:
            while True:
                drained = False
                while True:
                    try:
                        item = q.get_nowait()
                    except queue.Empty:
                        break
                    drained = True
                    if item is None:
                        continue
                    state.command_lines.append(item)

                now = time.monotonic()
                if drained or now - state.last_metric_update >= 2.0:
                    _refresh_build_state(state)
                    state.last_metric_update = now
                    live.update(_render_monitor(state))

                rc = proc.poll()
                if rc is not None:
                    step.returncode = rc
                    step.finished = time.monotonic()
                    step.status = "passed" if rc == 0 else "failed"
                    _refresh_build_state(state, force=True)
                    live.update(_render_monitor(state))
                    break
                time.sleep(0.5)
    except KeyboardInterrupt:
        _terminate_process_tree(proc)
        step.status = "interrupted"
        step.returncode = 130
        step.finished = time.monotonic()
        raise

    thread.join(timeout=2.0)
    rc = proc.returncode if proc.returncode is not None else step.returncode or 1
    if rc != 0:
        raise SystemExit(rc)
    return CommandResult(argv=argv, returncode=rc)


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=3)


def _latest_build_log_dir(snapshot_dir: Path) -> Path | None:
    root = snapshot_dir / "build"
    if not root.is_dir():
        return None
    candidates = [p for p in root.iterdir() if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _refresh_build_state(state: MonitorState, *, force: bool = False) -> None:
    if state.snapshot_dir is not None:
        state.log_dir = _latest_build_log_dir(state.snapshot_dir)
    if state.log_dir is not None:
        out_logs = sorted(state.log_dir.glob("make-*.out.log"))
        err_logs = sorted(state.log_dir.glob("make-*.err.log"))
        if out_logs:
            state.stdout_tail = deque(_tail_lines(out_logs[-1], 10), maxlen=10)
        if err_logs:
            state.stderr_tail = deque(_tail_lines(err_logs[-1], 5), maxlen=5)

    now = time.monotonic()
    if not force and now - state.last_metric_update < 5.0:
        state.clang_jobs = _count_process_names({"clang"})
        state.gcc_jobs = _count_process_names({"gcc", "cc1", "cc1plus"})
        return

    if state.source_dir is not None and state.source_dir.exists():
        state.object_count = _count_build_outputs(state.source_dir)
        state.tree_size = _du_sh(state.source_dir)
        state.packages = _find_packages(state.source_dir.parent)
    state.clang_jobs = _count_process_names({"clang"})
    state.gcc_jobs = _count_process_names({"gcc", "cc1", "cc1plus"})


def _tail_lines(path: Path, n: int) -> list[str]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    return lines[-n:]


def _count_build_outputs(source_dir: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(source_dir):
        total += sum(1 for f in files if f.endswith((".o", ".ko")))
    return total


def _count_process_names(names: set[str]) -> int:
    count = 0
    proc_root = Path("/proc")
    for p in proc_root.iterdir():
        if not p.name.isdigit():
            continue
        try:
            comm = (p / "comm").read_text().strip()
        except OSError:
            continue
        if comm in names:
            count += 1
    return count


def _du_sh(path: Path) -> str:
    result = subprocess.run(
        ["du", "-sh", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return "-"
    return result.stdout.split()[0] if result.stdout.split() else "-"


def _find_packages(parent: Path) -> list[Path]:
    patterns = (
        "linux-image-*.deb",
        "linux-headers-*.deb",
        "kernel-*.tar.gz",
        "linux-*.tar.gz",
        "linux-*.tar.zst",
    )
    out: list[Path] = []
    for pattern in patterns:
        out.extend(parent.glob(pattern))
    return sorted((p for p in out if "-dbg_" not in p.name), key=lambda p: p.name)


def _install_command(
    repo_root: Path,
    snapshot_dir: Path,
    packages: Iterable[Path],
    kernel_entry: str,
    *,
    execute: bool,
) -> list[str]:
    cmd = _direct_autokernel(repo_root, "install", str(snapshot_dir))
    for package in packages:
        cmd += ["--package", str(package)]
    if kernel_entry:
        cmd += ["--kernel-entry", kernel_entry]
    if execute:
        cmd.append("--execute")
    return cmd


def _kernel_release(kernel_source: Path, fallback: str) -> str:
    rel_file = kernel_source / "include/config/kernel.release"
    if rel_file.exists():
        return rel_file.read_text(errors="replace").strip()
    return fallback


def _render_monitor(state: MonitorState) -> Group:
    elapsed = _format_duration(state.step.elapsed)
    header = Table.grid(expand=True)
    header.add_column(ratio=1)
    header.add_column(justify="right")
    status_style = (
        "green"
        if state.step.status == "passed"
        else "red"
        if state.step.status == "failed"
        else "cyan"
    )
    header.add_row(
        f"[bold]{state.step.name}[/bold] [{status_style}]{state.step.status}[/{status_style}]",
        f"elapsed {elapsed}",
    )

    metrics = Table(show_header=False, box=None, expand=True)
    metrics.add_column("key", style="dim", width=18)
    metrics.add_column("value")
    metrics.add_row("log dir", str(state.log_dir) if state.log_dir else "-")
    metrics.add_row("objects/modules", str(state.object_count))
    metrics.add_row("clang jobs", str(state.clang_jobs))
    metrics.add_row("gcc jobs", str(state.gcc_jobs))
    metrics.add_row("source size", state.tree_size)
    metrics.add_row(
        "packages",
        "\n".join(p.name for p in state.packages) if state.packages else "-",
    )

    command_text = Text("\n".join(state.command_lines) or "-", overflow="fold")
    stdout_text = Text("\n".join(state.stdout_tail) or "-", overflow="fold")
    stderr_text = Text("\n".join(state.stderr_tail) or "-", overflow="fold")

    return Group(
        Panel(header, border_style="cyan"),
        Panel(metrics, title="build metrics", border_style="blue"),
        Panel(command_text, title="autokernel build output", border_style="magenta"),
        Panel(stdout_text, title="make stdout tail", border_style="green"),
        Panel(stderr_text, title="make stderr tail", border_style="yellow"),
    )


def _format_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def check_secure_boot(*, allow: bool) -> None:
    mokutil = shutil.which("mokutil")
    if not mokutil:
        return
    result = subprocess.run(
        [mokutil, "--sb-state"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if "SecureBoot enabled" in result.stdout and not allow:
        raise SystemExit(
            "Secure Boot is enabled. Re-run with --allow-secure-boot only if you plan to sign/enroll the kernel."
        )


def find_kernel_source(cache_dir: Path, kernel_version: str) -> Path | None:
    candidates = [p.parent for p in cache_dir.glob("*/Makefile")]
    if not candidates:
        candidates = [p.parent for p in cache_dir.glob("*/*/Makefile")]
    if not candidates:
        return None

    match = re.match(r"(\d+\.\d+)", kernel_version)
    if match:
        wanted = f"linux-{match.group(1)}"
        for p in candidates:
            if p.name == wanted:
                return p.resolve()
    return max(candidates, key=lambda p: p.stat().st_mtime).resolve()


def set_config_string(config: Path, key: str, value: str) -> None:
    lines = config.read_text().splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(f"{key}=") or line == f"# {key} is not set":
            out.append(f'{key}="{value}"')
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f'{key}="{value}"')
    config.write_text("\n".join(out) + "\n")


def set_config_not_set(config: Path, key: str) -> None:
    lines = config.read_text().splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(f"{key}=") or line == f"# {key} is not set":
            out.append(f"# {key} is not set")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"# {key} is not set")
    config.write_text("\n".join(out) + "\n")


def derive_grub_entry(kernel_release: str) -> str:
    cfg = Path("/boot/grub/grub.cfg")
    current = _run_text(["uname", "-r"])
    if cfg.exists():
        try:
            text = cfg.read_text(errors="replace")
        except OSError:
            text = ""
        title = ""
        submenu = ""
        for line in text.splitlines():
            if not submenu and "submenu 'Advanced options" in line:
                m = re.search(r"submenu '([^']+)'", line)
                if m:
                    submenu = m.group(1)
            if (
                f"Linux {current}" in line
                and "menuentry '" in line
                and "recovery mode" not in line
            ):
                m = re.search(r"menuentry '([^']+)'", line)
                if m:
                    title = m.group(1).replace(current, kernel_release)
                    break
        if title:
            return f"{submenu}>{title}" if submenu else title
    distro = "Linux"
    os_release = Path("/etc/os-release")
    if os_release.exists():
        for line in os_release.read_text(errors="replace").splitlines():
            if line.startswith("NAME="):
                distro = line.split("=", 1)[1].strip().strip('"')
                break
    return f"Advanced options for {distro}>{distro}, with Linux {kernel_release}"


def confirm_or_exit(prompt: str, *, yes: bool) -> None:
    if yes:
        return
    answer = console.input(f"{prompt} [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        raise SystemExit(0)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.reboot and not args.install:
        raise SystemExit("--reboot requires --install")

    repo_root = _repo_root()
    work_dir = Path(args.work_dir).expanduser().resolve()
    snapshot_dir = (
        Path(args.snapshot_dir).expanduser().resolve()
        if args.snapshot_dir
        else work_dir / "snapshot"
    )
    kernel_cache = (
        Path(args.kernel_cache).expanduser().resolve()
        if args.kernel_cache
        else work_dir / "kernels"
    )
    kernel_source = (
        Path(args.kernel_source).expanduser().resolve() if args.kernel_source else None
    )
    tmp_dir = work_dir / "tmp"

    work_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    kernel_cache.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["TMPDIR"] = str(tmp_dir)

    console.print(f"work dir: [bold]{work_dir}[/bold]")
    console.print(f"TMPDIR:   [bold]{tmp_dir}[/bold]")

    sudo = SudoKeeper()
    if not args.no_deps or args.install or args.reboot:
        sudo.start()

    try:
        check_secure_boot(allow=args.allow_secure_boot)

        run_command(
            "sync python environment",
            ["uv", "--project", str(repo_root), "sync", "--frozen"],
            cwd=repo_root,
            env=env,
        )
        run_command(
            "host preflight",
            _ak(repo_root, "preflight", "--for", "all"),
            cwd=repo_root,
            env=env,
        )
        if not args.no_deps:
            run_command(
                "install dependencies",
                _ak(repo_root, "install-deps", "--execute"),
                cwd=repo_root,
                env=env,
            )
        run_command(
            "scan host",
            _ak(repo_root, "scan", str(snapshot_dir)),
            cwd=repo_root,
            env=env,
        )
        run_command(
            "snapshot preflight",
            _ak(repo_root, "preflight", str(snapshot_dir), "--for", "all"),
            cwd=repo_root,
            env=env,
        )

        if kernel_source is None:
            run_command(
                "fetch kernel source",
                _ak(
                    repo_root,
                    "fetch-source",
                    "--kernel-version",
                    args.kernel_version,
                    "--method",
                    args.fetch_method,
                    "--out",
                    str(kernel_cache),
                ),
                cwd=repo_root,
                env=env,
            )
            kernel_source = find_kernel_source(kernel_cache, args.kernel_version)
            if kernel_source is None:
                raise SystemExit(f"could not locate kernel source under {kernel_cache}")
        if not (kernel_source / "Makefile").exists():
            raise SystemExit(f"kernel source tree has no Makefile: {kernel_source}")
        console.print(f"kernel source: [bold]{kernel_source}[/bold]")

        propose = _ak(
            repo_root,
            "propose",
            str(snapshot_dir),
            "--dimension",
            args.dimension,
            "--candidate-scope",
            args.candidate_scope,
            "--kernel-source",
            str(kernel_source),
            "--max-candidates",
            str(args.max_candidates),
            "--llm-mode",
            args.llm_mode,
            "--threat",
            args.threat,
            "--modules",
            args.modules,
            "--aggression",
            args.aggression,
        )
        if args.model:
            propose += ["--model", args.model]
        if args.service_tier:
            propose += ["--service-tier", args.service_tier]
        if args.preset:
            propose += ["--preset", args.preset]
        if args.workload:
            propose += ["--workload", args.workload]
        if args.skip_llm:
            propose += ["--skip-llm"]
        run_command("generate config proposal", propose, cwd=repo_root, env=env)

        run_command(
            "review proposal",
            _ak(
                repo_root,
                "review",
                str(snapshot_dir),
                "--reject-subsystem",
                "crypto",
                "--reject-subsystem",
                "security",
                "--reject-subsystem",
                "kasan",
                "--accept-recommended",
            ),
            cwd=repo_root,
            env=env,
        )
        run_command(
            "apply config",
            _ak(repo_root, "apply", str(snapshot_dir)),
            cwd=repo_root,
            env=env,
        )

        final_config = snapshot_dir / "final.config"
        set_config_string(final_config, "CONFIG_LOCALVERSION", args.localversion)
        set_config_not_set(final_config, "CONFIG_LOCALVERSION_AUTO")
        console.print(f'CONFIG_LOCALVERSION="{args.localversion}"')

        build_cmd = _ak(
            repo_root,
            "build",
            str(snapshot_dir),
            "--kernel-source",
            str(kernel_source),
            "--localmodconfig",
            "--execute",
            "--target",
            args.target,
            "--jobs",
            str(args.jobs),
            "--compiler",
            args.compiler,
        )
        run_monitored_build(
            "build kernel package",
            build_cmd,
            cwd=repo_root,
            env=env,
            source_dir=kernel_source,
            snapshot_dir=snapshot_dir,
        )
        packages = _find_packages(kernel_source.parent)

        run_command(
            "boot-test built kernel",
            _ak(
                repo_root,
                "boot-test",
                str(snapshot_dir),
                "--kernel-source",
                str(kernel_source),
                "--method",
                args.boot_test_method,
                "--timeout",
                "120",
            ),
            cwd=repo_root,
            env=env,
        )

        if packages:
            console.print("\nbuilt packages:")
            for package in packages:
                console.print(f"  {package}")
        else:
            console.print(f"\nno installable packages found for target={args.target}")

        kernel_release = _kernel_release(kernel_source, args.kernel_version)
        entry = args.kernel_entry or derive_grub_entry(kernel_release)
        if entry:
            (snapshot_dir / "grub-one-shot-entry").write_text(entry + "\n")

        if not args.install:
            console.print(
                "\nBuild and VM boot-test completed without installing into /boot."
            )
            if packages:
                install_cmd = _sudo_env(
                    _install_command(
                        repo_root,
                        snapshot_dir,
                        packages,
                        entry,
                        execute=True,
                    ),
                    env,
                )
                console.print(
                    "\nTo install these already-built packages and arm one-shot GRUB:"
                )
                console.print(f"  {_format_argv(install_cmd)}")
                console.print("\nTo reboot immediately after install:")
                console.print(f"  {_format_argv(install_cmd)}")
                console.print("  sudo systemctl reboot")
            return 0

        if args.install:
            if not packages:
                raise SystemExit(
                    "install requested but no installable packages were found"
                )
            confirm_or_exit(
                f"Install built kernel and arm one-shot GRUB entry {entry!r}?",
                yes=args.yes,
            )
            install_cmd = _install_command(
                repo_root,
                snapshot_dir,
                packages,
                entry,
                execute=True,
            )
            run_command(
                "install kernel",
                _sudo_env(install_cmd, env),
                cwd=repo_root,
                env=env,
            )

        if args.reboot:
            confirm_or_exit("Reboot now?", yes=args.yes)
            run_command(
                "reboot", ["sudo", "systemctl", "reboot"], cwd=repo_root, env=env
            )
    finally:
        sudo.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
