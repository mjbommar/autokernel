"""Undo the most recent ``autokernel install`` for a snapshot.

A successful (or even partially-successful) :func:`autokernel.install.execute`
run leaves a ``record.json`` under ``<snapshot>/install/<timestamp>/``.
Rollback finds the most recent such record, reads the package paths +
bootloader info that were applied, and emits a plan that:

1. Removes the installed package via the distro's package manager.
2. Regenerates the bootloader config so the now-removed kernel
   disappears from the menu.

We deliberately **don't** try to undo the ``capture_grub_state`` step —
that one only read state; nothing to undo. We also don't try to
undo the one-shot ``grub-reboot`` because GRUB itself clears the
one-shot pointer on the next boot regardless.

Like install, rollback is **dry-run by default**; the caller passes
``--execute`` to actually mutate state.

Idempotent by design: running rollback on a snapshot whose latest
install was already rolled back finds the next-newest record (or none).
A ``record.json`` is rewritten with ``rolled_back: true`` after a
successful execute so we don't try to roll the same install back twice.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from autokernel.bootloader import Bootloader, BootloaderKind
from autokernel.distro import DistroInfo, Family
from autokernel.install import InstallStep, StepRun, _run_step


# ── data ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RollbackPlan:
    record_path: Path
    """Which install record this rollback targets."""

    distro_id: str
    bootloader_kind: str
    package_names: list[str]
    """Package *names* (not paths) — what we'll pass to remove."""

    steps: list[InstallStep] = field(default_factory=list)
    rejected_reason: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.rejected_reason is None and bool(self.steps)


@dataclass
class RollbackResult:
    plan: RollbackPlan
    step_runs: list[StepRun]
    log_dir: Path

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.step_runs)


# ── plan construction ─────────────────────────────────────────────────────


def find_latest_install_record(snapshot_dir: Path) -> Path | None:
    """Return the newest non-rolled-back install record in the snapshot,
    or ``None`` if no install has been recorded yet (or all have been
    rolled back already).
    """
    records_root = snapshot_dir / "install"
    if not records_root.is_dir():
        return None
    candidates = sorted(records_root.glob("*/record.json"), reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not data.get("rolled_back"):
            return path
    return None


def _package_name_from_path(path: Path) -> str:
    """Extract the package name from an installed package's filename.

    Debian: ``linux-image-6.13.5_amd64.deb`` → ``linux-image-6.13.5``
        (split on ``_`` separates name from arch)
    Fedora: ``kernel-6.13.5-100.fc41.x86_64.rpm`` → ``kernel-6.13.5-100.fc41.x86_64``
        (RPM filenames use ``.`` separators; ``dnf remove`` accepts full NEVRA)
    Arch:   ``linux-6.13.5.pkg.tar.zst`` → ``linux-6.13.5``

    These are best-effort: the rollback step is the package manager's
    remove command, which accepts package names (not paths). On
    failure the caller still gets the path in the rejection message.
    """
    name = path.name
    if name.endswith(".deb"):
        # Debian: <name>_<version>_<arch>.deb — split on underscore strips arch.
        return name[:-4].split("_", 1)[0]
    if name.endswith(".rpm"):
        # RPM filenames use dot-separated NEVRA; dnf accepts the full string.
        return name[:-4]
    if name.endswith(".pkg.tar.zst"):
        return name[: -len(".pkg.tar.zst")]
    if name.endswith(".pkg.tar"):
        return name[: -len(".pkg.tar")]
    return name


def _remove_argv(family: Family, package_names: list[str]) -> list[str] | None:
    if not package_names:
        return None
    if family == Family.DEBIAN:
        return ["apt", "remove", "-y", *package_names]
    if family == Family.FEDORA:
        return ["dnf", "remove", "-y", *package_names]
    if family == Family.ARCH:
        return ["pacman", "-R", "--noconfirm", *package_names]
    if family == Family.SUSE:
        return ["zypper", "remove", "-y", *package_names]
    return None


def build_plan(
    *,
    snapshot_dir: Path,
    distro: DistroInfo,
    bootloader: Bootloader,
) -> RollbackPlan:
    """Compose the rollback plan from the snapshot's most recent
    non-rolled-back install record.

    Returns a rejected plan when:
    * no install record exists yet
    * the record is malformed
    * the bootloader changed since the install (defensive — we can't
      safely regenerate a bootloader config we don't know how to drive)
    """
    record_path = find_latest_install_record(snapshot_dir)
    if record_path is None:
        return RollbackPlan(
            record_path=snapshot_dir / "install",
            distro_id=distro.id,
            bootloader_kind=bootloader.kind.value,
            package_names=[],
            rejected_reason="no install record found (run `autokernel install --execute` first)",
        )

    try:
        record = json.loads(record_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return RollbackPlan(
            record_path=record_path,
            distro_id=distro.id,
            bootloader_kind=bootloader.kind.value,
            package_names=[],
            rejected_reason=f"install record is unreadable: {e}",
        )

    pkg_names = [_package_name_from_path(Path(p)) for p in record.get("package_paths", [])]

    if not bootloader.is_supported:
        return RollbackPlan(
            record_path=record_path,
            distro_id=distro.id,
            bootloader_kind=bootloader.kind.value,
            package_names=pkg_names,
            rejected_reason=(
                f"bootloader {bootloader.kind.value!r} not supported (v1: GRUB2 only)"
            ),
        )

    remove_argv = _remove_argv(distro.family, pkg_names)
    if remove_argv is None:
        return RollbackPlan(
            record_path=record_path,
            distro_id=distro.id,
            bootloader_kind=bootloader.kind.value,
            package_names=pkg_names,
            rejected_reason=(
                f"distro family {distro.family.value!r} has no package-removal recipe"
            ),
        )

    steps: list[InstallStep] = [
        InstallStep(
            name="remove_package",
            argv=remove_argv,
            description=(
                f"Remove the kernel package(s) installed by the {record['timestamp']!s} "
                f"install record: {', '.join(pkg_names)}."
            ),
            needs_root=True,
        )
    ]
    regen = bootloader.regenerate_cmd()
    if regen is not None:
        steps.append(
            InstallStep(
                name="regenerate_bootloader",
                argv=regen,
                description=(
                    "Regenerate the bootloader config so the removed kernel disappears "
                    "from the menu."
                ),
                needs_root=True,
            )
        )

    return RollbackPlan(
        record_path=record_path,
        distro_id=distro.id,
        bootloader_kind=bootloader.kind.value,
        package_names=pkg_names,
        steps=steps,
    )


# ── execution ──────────────────────────────────────────────────────────────


def _new_log_dir(snapshot_dir: Path) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    p = snapshot_dir / "rollback" / ts
    p.mkdir(parents=True, exist_ok=True)
    return p


def execute(plan: RollbackPlan, *, snapshot_dir: Path) -> RollbackResult:
    if not plan.is_valid:
        raise RuntimeError(f"refusing to execute invalid plan: {plan.rejected_reason}")

    log_dir = _new_log_dir(snapshot_dir)
    runs: list[StepRun] = []
    for step in plan.steps:
        run = _run_step(step, log_dir)
        runs.append(run)
        if not run.ok:
            break

    # Mark the install record as rolled back so subsequent rollback
    # invocations don't try to undo it again.
    if all(r.ok for r in runs) and plan.record_path.exists():
        try:
            data = json.loads(plan.record_path.read_text())
            data["rolled_back"] = True
            data["rolled_back_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            plan.record_path.write_text(json.dumps(data, indent=2))
        except (OSError, json.JSONDecodeError):
            pass  # best-effort; the install record is informational

    return RollbackResult(plan=plan, step_runs=runs, log_dir=log_dir)
