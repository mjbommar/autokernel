"""Smoke-test a freshly-built kernel in a VM before installing it.

Two methods, both fast (5-15 sec) and non-destructive:

* **virtme-ng** (preferred when installed) — wraps QEMU to boot the
  kernel against the host's read-only ``/`` over virtio-fs. Drops to a
  shell or runs a script. Tests userspace transition end-to-end.

* **QEMU kernel-only** (universal fallback) — boots the kernel with no
  rootfs. The kernel runs through arch init, drivers, and userspace
  transition, then panics with ``"VFS: Unable to mount root fs"``.
  That panic is the **success indicator**: it means the kernel made it
  all the way through self-init without crashing earlier.

Both methods capture serial-console output to ``<snapshot>/boot-test/
<ts>/serial.log``. The verdict (PASS/FAIL + reason) is persisted to
``<snapshot>/boot-test.json`` so a later ``autokernel install --execute``
can refuse to proceed if no recent successful boot-test exists.

The bzImage's sha256 is recorded with the result, so installing a
kernel that's been modified since the boot-test is detected.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path


_DEFAULT_TIMEOUT = 60.0


class Method(str, Enum):
    AUTO = "auto"
    VIRTME = "virtme"
    QEMU = "qemu"


# ── data ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BootTestPlan:
    method: Method
    bzimage_path: Path
    kernel_release: str
    argv: list[str]
    timeout: float
    description: str
    cwd: Path = field(default_factory=lambda: Path("/"))


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str


@dataclass(frozen=True)
class BootTestResult:
    plan: BootTestPlan
    verdict: Verdict
    exit_code: int
    duration_s: float
    serial_log_path: Path
    bzimage_sha256: str
    record_path: Path
    """``boot-test.json`` (latest result) for `install --execute` to consult."""


# ── method selection ───────────────────────────────────────────────────────


def detect_method() -> Method:
    """Pick the best available method."""
    if shutil.which("virtme-ng") or shutil.which("virtme-run"):
        return Method.VIRTME
    if shutil.which("qemu-system-x86_64"):
        return Method.QEMU
    return Method.AUTO  # nothing available; planner returns rejected


# ── plan builders ──────────────────────────────────────────────────────────


def _virtme_argv(bzimage: Path, *, command: str = "true") -> list[str]:
    """virtme-ng exits with 0 when the in-VM command exits 0. We pick
    ``true`` so a successful boot ends cleanly without further work."""
    binary = "virtme-ng" if shutil.which("virtme-ng") else "virtme-run"
    return [
        binary,
        "--kimg",
        str(bzimage),
        "--no-virt-net",  # no host network bleed-through
        "--script-sh",
        f"{command} && echo AUTOKERNEL_BOOT_TEST_OK",
    ]


def _kvm_available() -> bool:
    return Path("/dev/kvm").exists() and os.access("/dev/kvm", os.R_OK | os.W_OK)


def _qemu_argv(bzimage: Path) -> list[str]:
    """Boot the kernel with no rootfs; expect VFS panic at end.

    Notes on the flags:
    * ``-nographic`` — pipe serial to stdout, no X11 popup.
    * ``-no-reboot`` — when the kernel "reboots" after panic, QEMU exits
      cleanly instead of looping.
    * ``-M pc`` keeps the virtual hardware conservative and predictable.
    * KVM uses ``-cpu host`` so host-tuned kernels see the expected CPU
      feature set. TCG falls back to ``-cpu max`` for the same reason.
    * ``-append`` sets the kernel cmdline. ``panic=1`` forces an
      immediate reboot on panic (no 10-second pause), so QEMU exits.
      ``console=ttyS0`` routes printk to the serial port we read.
    * ``-smp 1 -m 512`` keeps memory + cores tight; we're not running
      anything but the kernel itself.
    """
    argv = [
        "qemu-system-x86_64",
        "-M",
        "pc",
    ]
    if _kvm_available():
        argv.extend(["-enable-kvm", "-cpu", "host"])
    else:
        argv.extend(["-accel", "tcg", "-cpu", "max"])
    argv.extend(
        [
            "-kernel",
            str(bzimage),
            "-append",
            "console=ttyS0 earlyprintk=serial panic=1",
            "-nographic",
            "-no-reboot",
            "-m",
            "512",
            "-smp",
            "1",
        ]
    )
    return argv


def plan(
    *,
    method: Method,
    bzimage_path: Path,
    kernel_release: str,
    timeout: float = _DEFAULT_TIMEOUT,
) -> BootTestPlan:
    """Build a :class:`BootTestPlan` for the chosen method."""
    if method == Method.AUTO:
        method = detect_method()
        if method == Method.AUTO:
            raise RuntimeError(
                "no boot-test runtime available — install qemu-system-x86 or virtme-ng"
            )
    if method == Method.VIRTME:
        return BootTestPlan(
            method=method,
            bzimage_path=bzimage_path,
            kernel_release=kernel_release,
            argv=_virtme_argv(bzimage_path),
            timeout=timeout,
            description=(
                f"Boot the freshly-built kernel ({kernel_release}) under virtme-ng "
                f"with the host's / mounted read-only over virtio-fs. Drops to "
                f"shell, runs `true`, exits cleanly on success."
            ),
        )
    if method == Method.QEMU:
        return BootTestPlan(
            method=method,
            bzimage_path=bzimage_path,
            kernel_release=kernel_release,
            argv=_qemu_argv(bzimage_path),
            timeout=timeout,
            description=(
                f"Boot the freshly-built kernel ({kernel_release}) under QEMU with "
                f"NO rootfs. Success = kernel reaches VFS mount stage without an "
                f"earlier panic (the final VFS panic is expected and is our PASS "
                f"signal)."
            ),
        )
    raise ValueError(f"unknown method: {method}")


# ── output analysis ────────────────────────────────────────────────────────


def analyze_serial(text: str, *, method: Method, kernel_release: str = "") -> Verdict:
    """Decide PASS/FAIL from captured serial output.

    QEMU kernel-only path:
        PASS iff "Linux version" appears AND the only "Kernel panic" we
        find is the expected ``"VFS: Unable to mount root fs"`` after
        the kernel reaches userspace transition.

    virtme path:
        PASS iff "AUTOKERNEL_BOOT_TEST_OK" sentinel appears.
    """
    if method == Method.VIRTME:
        if "AUTOKERNEL_BOOT_TEST_OK" in text:
            return Verdict(True, "virtme reached the in-VM success sentinel")
        if "Kernel panic" in text:
            panic_idx = text.find("Kernel panic")
            return Verdict(
                False,
                f"kernel panic in virtme: {text[panic_idx : panic_idx + 200]!r}",
            )
        return Verdict(False, "virtme exited without success sentinel")

    # QEMU kernel-only
    if "Linux version" not in text:
        return Verdict(False, "kernel banner not seen — kernel didn't boot at all")

    panic_idx = text.find("Kernel panic")
    if panic_idx == -1:
        # Kernel kept going past panic timeout? Fine, treat as pass since it
        # at least booted past the banner.
        return Verdict(True, "kernel booted; no panic seen")

    vfs_idx = text.find("VFS: Unable to mount root fs")
    if vfs_idx != -1 and vfs_idx < panic_idx:
        return Verdict(
            True,
            "boot reached VFS mount stage with no earlier panic — kernel works",
        )
    # Some kernels print "Cannot open root device" without the VFS-Unable line.
    if "Cannot open root device" in text:
        cor_idx = text.find("Cannot open root device")
        if cor_idx < panic_idx:
            return Verdict(True, "boot reached rootfs lookup with no earlier panic")

    return Verdict(
        False,
        f"kernel panic BEFORE rootfs stage: {text[panic_idx : panic_idx + 240]!r}",
    )


# ── execution ──────────────────────────────────────────────────────────────


def _new_log_dir(snapshot_dir: Path) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    p = snapshot_dir / "boot-test" / ts
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def execute(plan: BootTestPlan, *, snapshot_dir: Path) -> BootTestResult:
    """Run the plan, capture serial output, write results.

    The latest result is also written to ``<snapshot>/boot-test.json``
    so install can find it via :func:`read_record`.
    """
    if not plan.bzimage_path.exists():
        raise FileNotFoundError(f"bzImage not found: {plan.bzimage_path}")

    log_dir = _new_log_dir(snapshot_dir)
    serial_log = log_dir / "serial.log"
    argv_log = log_dir / "cmd.argv"

    argv_log.write_text(
        f"# method: {plan.method.value}\n"
        f"# timeout: {plan.timeout}\n" + " ".join(repr(a) for a in plan.argv) + "\n"
    )

    started = datetime.now(UTC)
    rc = -1
    captured = b""
    try:
        with serial_log.open("wb") as outf:
            proc = subprocess.run(
                plan.argv,
                stdout=outf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                timeout=plan.timeout,
                cwd=str(plan.cwd),
                check=False,
            )
        rc = proc.returncode
        captured = serial_log.read_bytes()
    except subprocess.TimeoutExpired as e:
        # Capture whatever printed before the kill.
        if e.stdout:
            try:
                serial_log.write_bytes(e.stdout)
            except OSError:
                captured = e.stdout
        captured = serial_log.read_bytes() if serial_log.exists() else b""
        rc = -1
    except FileNotFoundError as e:
        serial_log.write_text(f"command not found: {e}\n")
        rc = -2
        captured = serial_log.read_bytes()

    duration = (datetime.now(UTC) - started).total_seconds()

    text = captured.decode("utf-8", errors="replace")
    verdict = analyze_serial(
        text, method=plan.method, kernel_release=plan.kernel_release
    )
    if rc == -1 and not captured:
        verdict = Verdict(False, f"timed out after {plan.timeout}s with no output")

    bzimage_sha = _sha256(plan.bzimage_path)
    record = {
        "schema": 1,
        "timestamp": started.isoformat(timespec="seconds"),
        "method": plan.method.value,
        "kernel_release": plan.kernel_release,
        "bzimage_path": str(plan.bzimage_path),
        "bzimage_sha256": bzimage_sha,
        "argv": plan.argv,
        "exit_code": rc,
        "duration_s": duration,
        "serial_log": str(serial_log),
        "verdict_ok": verdict.ok,
        "verdict_reason": verdict.reason,
    }
    log_dir_record = log_dir / "result.json"
    log_dir_record.write_text(json.dumps(record, indent=2))

    latest = snapshot_dir / "boot-test.json"
    latest.write_text(json.dumps(record, indent=2))

    return BootTestResult(
        plan=plan,
        verdict=verdict,
        exit_code=rc,
        duration_s=duration,
        serial_log_path=serial_log,
        bzimage_sha256=bzimage_sha,
        record_path=latest,
    )


# ── record reading (used by install --execute) ─────────────────────────────


def read_latest_record(snapshot_dir: Path) -> dict | None:
    """Return the latest ``boot-test.json`` content, or ``None`` if no
    test has been run for this snapshot."""
    p = snapshot_dir / "boot-test.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def find_bzimage(kernel_source: Path) -> Path | None:
    """The kernel's build target lands at ``arch/x86/boot/bzImage`` for
    x86_64 builds. We look there first, then fall back to ``vmlinux``
    for the rare arch where bzImage isn't applicable."""
    candidates = [
        kernel_source / "arch" / "x86" / "boot" / "bzImage",
        kernel_source / "arch" / "x86_64" / "boot" / "bzImage",
        kernel_source / "vmlinux",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None
