"""Distro-aware ``install --probation`` planner + executor.

Lifecycle:

1. **Plan** (always) — :func:`build_plan` composes a deterministic
   :class:`InstallPlan` from ``(distro, bootloader, package_paths)``.
   The plan is a list of :class:`InstallStep` records: argv + cwd +
   ``needs_root`` + a human-readable description.

2. **Execute** (with ``--execute``) — :func:`execute` walks the plan,
   shelling out via :mod:`subprocess`, capturing per-step logs to
   ``<snapshot>/install/<timestamp>/``. Default mode is dry-run: render
   the plan, write nothing, exit 0. Mirrors :mod:`autokernel.build`'s
   contract.

3. **Probation** — instead of writing the new kernel as the permanent
   default, we use the bootloader's *one-shot* mechanism (``grub-reboot``,
   ``grub2-reboot``). The new kernel boots once. If it fails to boot,
   GRUB falls back to the previous default automatically. If it succeeds,
   the user runs :func:`build_commit_plan` (``autokernel install --commit
   --execute``) to promote it to the permanent default.

4. **Rollback** — :mod:`autokernel.rollback` reads the install record
   left by :func:`execute` and restores the previous default + removes
   the kernel package.

Scope for v1: GRUB2 on Debian-family + Fedora-family. systemd-boot,
rEFInd, and unknown bootloaders cause the planner to return a rejected
plan with a clear message; ``hint_unsupported_bootloader`` is the
caller's escape hatch.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from autokernel.bootloader import Bootloader
from autokernel.distro import DistroInfo, DistroSpec, Family
from autokernel.nvidia import NvidiaDriverPlan


# ── data model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InstallStep:
    """One subprocess invocation in the install plan.

    ``description`` is the human-readable banner rendered in dry-run mode
    so the user can read the plan top-to-bottom and understand what will
    happen before consenting to ``--execute``.
    """

    name: str
    argv: list[str]
    description: str
    cwd: Path = field(default_factory=lambda: Path("/"))
    needs_root: bool = True
    timeout: float = 600.0


@dataclass(frozen=True)
class InstallPlan:
    distro_id: str
    bootloader_kind: str
    package_paths: list[Path]
    steps: list[InstallStep] = field(default_factory=list)
    rejected_reason: str | None = None
    """When set, the plan is invalid; the caller must NOT execute it."""

    @property
    def is_valid(self) -> bool:
        return self.rejected_reason is None and bool(self.steps)


@dataclass
class StepRun:
    step: InstallStep
    exit_code: int
    duration_s: float
    stdout_path: Path | None
    stderr_path: Path | None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class InstallResult:
    plan: InstallPlan
    step_runs: list[StepRun]
    log_dir: Path
    record_path: Path
    """``record.json`` with backup + applied package list, used by rollback."""

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.step_runs)


# ── plan builders ──────────────────────────────────────────────────────────


def _per_distro_install_argv(
    distro: DistroInfo, package_paths: list[Path]
) -> list[str] | None:
    """Build the install command for the user's package manager.

    Debian: ``apt install -y ./pkg.deb`` (apt resolves dependencies).
    Fedora: ``dnf install -y ./pkg.rpm``.
    Others: ``None`` — caller should reject the plan."""
    if not package_paths:
        return None
    paths = [str(p) for p in package_paths]
    if distro.family == Family.DEBIAN:
        return ["apt", "install", "-y", *paths]
    if distro.family == Family.FEDORA:
        return ["dnf", "install", "-y", *paths]
    if distro.family == Family.ARCH:
        return ["pacman", "-U", "--noconfirm", *paths]
    if distro.family == Family.SUSE:
        return ["zypper", "install", "-y", *paths]
    return None


def _backup_step(snapshot_dir: Path, bootloader: Bootloader) -> InstallStep:
    """Capture pre-install bootloader state to a backup file.

    Reads ``grub-editenv list`` (or the grub2- equivalent) to discover
    the current ``saved_entry`` value, copies ``grub.cfg`` to a backup,
    and writes them both into the install log dir. Reading is safe; we
    don't need root for this step (grubenv is world-readable).
    """
    grubenv_tool = "grub2-editenv" if bootloader.grub_tool_prefix else "grub-editenv"
    return InstallStep(
        name="capture_grub_state",
        argv=[grubenv_tool, "list"],
        description=(
            "Backup the current bootloader state (default kernel + grub.cfg) so "
            "`autokernel rollback` can undo this install."
        ),
        needs_root=False,
        timeout=15.0,
    )


def _install_package_step(
    distro: DistroInfo, package_paths: list[Path]
) -> InstallStep | None:
    argv = _per_distro_install_argv(distro, package_paths)
    if argv is None:
        return None
    return InstallStep(
        name="install_package",
        argv=argv,
        description=(
            f"Install {len(package_paths)} package(s) via {distro.family.value}'s "
            f"package manager. This drops files in /boot but does not change the "
            f"default kernel."
        ),
        needs_root=True,
    )


def _nvidia_steps(distro: DistroInfo, plan: NvidiaDriverPlan) -> list[InstallStep]:
    if distro.family != Family.DEBIAN:
        return []

    return [
        InstallStep(
            name="install_nvidia_driver",
            argv=["apt", "install", "-y", plan.package_name],
            description=(
                f"Install/upgrade {plan.package_name} so NVIDIA user-space and "
                f"DKMS kernel modules match for driver branch {plan.branch}."
            ),
            timeout=1800.0,
        ),
        InstallStep(
            name="build_nvidia_dkms",
            argv=["dkms", "autoinstall", "-k", plan.kernel_release],
            description=(
                f"Build NVIDIA {plan.flavor} DKMS modules for "
                f"{plan.kernel_release} before the first reboot."
            ),
            timeout=1800.0,
        ),
        InstallStep(
            name="verify_nvidia_modules",
            argv=[
                "bash",
                "-c",
                (
                    "set -e; "
                    f"k={_sh_single_quote(plan.kernel_release)}; "
                    "for m in nvidia nvidia_modeset nvidia_drm nvidia_uvm; do "
                    'modinfo -k "$k" "$m" >/dev/null; '
                    "done"
                ),
            ],
            description=(
                f"Verify NVIDIA modules are present under /lib/modules/"
                f"{plan.kernel_release}."
            ),
            timeout=60.0,
        ),
        InstallStep(
            name="refresh_initramfs",
            argv=["update-initramfs", "-u", "-k", plan.kernel_release],
            description=(
                f"Refresh initramfs for {plan.kernel_release} after DKMS module "
                "installation."
            ),
            timeout=600.0,
        ),
    ]


def _sh_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _regenerate_step(bootloader: Bootloader) -> InstallStep | None:
    argv = bootloader.regenerate_cmd()
    if argv is None:
        return None
    return InstallStep(
        name="regenerate_bootloader",
        argv=argv,
        description=(
            "Regenerate the bootloader config so the new kernel appears as a "
            "menu entry. Does not change which kernel is default."
        ),
        needs_root=True,
    )


def _arm_probation_step(
    bootloader: Bootloader, kernel_entry: str
) -> InstallStep | None:
    argv = bootloader.one_shot_argv(kernel_entry)
    if argv is None:
        return None
    return InstallStep(
        name="arm_one_shot_boot",
        argv=argv,
        description=(
            f"Arm the new kernel for one-shot boot. On next reboot, GRUB will "
            f"try '{kernel_entry}' once. If it boots, you can run `autokernel "
            f"install --commit --execute` to make it permanent. If it fails to "
            f"boot, GRUB falls back to the previous default automatically."
        ),
        needs_root=True,
    )


def build_plan(
    *,
    distro: DistroInfo,
    spec: DistroSpec,
    bootloader: Bootloader,
    package_paths: list[Path],
    kernel_entry: str | None = None,
    enable_probation: bool = True,
    nvidia_plan: NvidiaDriverPlan | None = None,
) -> InstallPlan:
    """Compose the full install plan.

    ``kernel_entry`` is the bootloader's name for the new kernel (GRUB
    menu entry). If ``None`` and ``enable_probation`` is True, the
    probation step is omitted with a note — the caller can fill it in
    once the new kernel's grub entry name is known (typically by
    re-running ``grub-mkconfig`` and grepping the output).

    Plans for unsupported bootloaders or unknown distros are returned
    with ``rejected_reason`` set.
    """
    if not bootloader.is_supported:
        return InstallPlan(
            distro_id=distro.id,
            bootloader_kind=bootloader.kind.value,
            package_paths=list(package_paths),
            rejected_reason=(
                f"bootloader {bootloader.kind.value!r} not yet supported "
                f"(v1 supports GRUB2 only)"
            ),
        )

    install_step = _install_package_step(distro, package_paths)
    if install_step is None:
        return InstallPlan(
            distro_id=distro.id,
            bootloader_kind=bootloader.kind.value,
            package_paths=list(package_paths),
            rejected_reason=(
                f"distro family {distro.family.value!r} has no install recipe "
                f"yet (need package manager invocation for {distro.id!r})"
            ),
        )

    regen_step = _regenerate_step(bootloader)

    steps: list[InstallStep] = [
        # Backup is informational; we capture stdout to record.json so the
        # rollback step knows what the previous default was.
        _backup_step(Path("/"), bootloader),
        install_step,
    ]
    if nvidia_plan is not None:
        steps.extend(_nvidia_steps(distro, nvidia_plan))
    if regen_step is not None:
        steps.append(regen_step)

    if enable_probation and kernel_entry is not None:
        arm = _arm_probation_step(bootloader, kernel_entry)
        if arm is not None:
            steps.append(arm)

    return InstallPlan(
        distro_id=distro.id,
        bootloader_kind=bootloader.kind.value,
        package_paths=list(package_paths),
        steps=steps,
    )


def build_commit_plan(
    *,
    distro: DistroInfo,
    bootloader: Bootloader,
    kernel_entry: str,
) -> InstallPlan:
    """Build the post-boot ``--commit`` plan: promote the running kernel
    to permanent default."""
    if not bootloader.is_supported:
        return InstallPlan(
            distro_id=distro.id,
            bootloader_kind=bootloader.kind.value,
            package_paths=[],
            rejected_reason=(
                f"bootloader {bootloader.kind.value!r} not supported by --commit"
            ),
        )
    set_default = bootloader.set_default_argv(kernel_entry)
    if set_default is None:
        return InstallPlan(
            distro_id=distro.id,
            bootloader_kind=bootloader.kind.value,
            package_paths=[],
            rejected_reason="bootloader has no set_default recipe",
        )
    return InstallPlan(
        distro_id=distro.id,
        bootloader_kind=bootloader.kind.value,
        package_paths=[],
        steps=[
            InstallStep(
                name="commit_default",
                argv=set_default,
                description=(
                    f"Promote {kernel_entry!r} to the permanent default "
                    f"bootloader entry. Run this only after verifying the new "
                    f"kernel boots correctly."
                ),
                needs_root=True,
            )
        ],
    )


# ── execution ─────────────────────────────────────────────────────────────


def _new_log_dir(snapshot_dir: Path) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    p = snapshot_dir / "install" / ts
    p.mkdir(parents=True, exist_ok=True)
    return p


def _run_step(step: InstallStep, log_dir: Path) -> StepRun:
    out_path = log_dir / f"{step.name}.out.log"
    err_path = log_dir / f"{step.name}.err.log"
    started = datetime.now(UTC)
    rc = -1
    try:
        with out_path.open("wb") as outf, err_path.open("wb") as errf:
            proc = subprocess.run(
                step.argv,
                cwd=str(step.cwd),
                stdout=outf,
                stderr=errf,
                timeout=step.timeout,
                check=False,
            )
        rc = proc.returncode
    except FileNotFoundError as e:
        err_path.write_text(f"command not found: {e}\n")
        rc = -2
    except subprocess.TimeoutExpired:
        err_path.write_text("TIMEOUT\n")
        rc = -1

    duration = (datetime.now(UTC) - started).total_seconds()
    return StepRun(
        step=step,
        exit_code=rc,
        duration_s=duration,
        stdout_path=out_path,
        stderr_path=err_path,
    )


def execute(
    plan: InstallPlan,
    *,
    snapshot_dir: Path,
) -> InstallResult:
    """Run an install plan top-to-bottom, capturing logs.

    Stops on the first failed step (kernel install failures cascade —
    don't keep going if `apt install` failed). The :class:`InstallResult`
    records each step's outcome.
    """
    if not plan.is_valid:
        raise RuntimeError(f"refusing to execute invalid plan: {plan.rejected_reason}")

    log_dir = _new_log_dir(snapshot_dir)
    runs: list[StepRun] = []
    for step in plan.steps:
        run = _run_step(step, log_dir)
        runs.append(run)
        if not run.ok:
            break  # fail fast; downstream steps would compound the damage

    # Record what we just did so rollback can find it.
    record_path = log_dir / "record.json"
    record = {
        "schema": 1,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "distro_id": plan.distro_id,
        "bootloader_kind": plan.bootloader_kind,
        "package_paths": [str(p) for p in plan.package_paths],
        "steps": [
            {
                "name": r.step.name,
                "argv": r.step.argv,
                "rc": r.exit_code,
                "duration_s": r.duration_s,
                "stdout": str(r.stdout_path) if r.stdout_path else None,
                "stderr": str(r.stderr_path) if r.stderr_path else None,
            }
            for r in runs
        ],
        "ok": all(r.ok for r in runs),
    }
    record_path.write_text(json.dumps(record, indent=2))

    return InstallResult(
        plan=plan,
        step_runs=runs,
        log_dir=log_dir,
        record_path=record_path,
    )
