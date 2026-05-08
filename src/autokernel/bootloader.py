"""Detect the system's bootloader and provide per-kind action recipes.

``autokernel install`` needs three things from the bootloader:

1. **Regenerate config** after a kernel package adds files to ``/boot``.
2. **One-shot boot** the new kernel (so a failed boot falls back to the
   previous default automatically — the cornerstone of probation).
3. **Set permanent default** once probation succeeds.

Each bootloader exposes those primitives differently. We encode them
once in :class:`Bootloader` so the install/rollback code can stay
distro-agnostic.

Scope for v1: GRUB2 (the dominant bootloader on Debian/Ubuntu/Fedora/RHEL
and Arch by default). systemd-boot, rEFInd, ELILO, and legacy GRUB are
detected but the action recipes are stubbed; install will refuse to
proceed on those with a clear message until the recipes are filled in.

Detection is **read-only** — never mutates the system. The result
includes a ``detected_via`` string for the user-facing diagnostic.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class BootloaderKind(str, Enum):
    GRUB2 = "grub2"           # Debian/Ubuntu, Fedora/RHEL, Arch (most common)
    GRUB_LEGACY = "grub-legacy"  # very rare today
    SYSTEMD_BOOT = "systemd-boot"
    REFIND = "refind"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Bootloader:
    """Detected bootloader + the argv recipes to drive it.

    ``regenerate_cmd`` is the command that re-emits the bootloader's
    config from the kernels currently present in ``/boot`` — typically
    run after ``dpkg -i linux-image-*.deb`` (or distro equivalent).

    ``set_default_argv`` and ``one_shot_argv`` are factories that take
    the new kernel's *entry name* (which is bootloader-specific:
    GRUB calls them "menu entries", systemd-boot calls them "entry IDs")
    and return the right argv. ``None`` means "this bootloader doesn't
    support this operation in the way we need" — the caller should
    refuse to proceed.
    """

    kind: BootloaderKind
    detected_via: str
    """Human-readable diagnostic, e.g. ``'/boot/grub/grub.cfg present'``."""

    config_dir: Path | None = None
    """For GRUB: ``/boot/grub`` (Debian) or ``/boot/grub2`` (Fedora/RHEL).
    For systemd-boot: ``/boot/loader`` or ``/efi/loader``."""

    grub_tool_prefix: str = ""
    """Empty on Debian (`grub-reboot`); ``grub2-`` on Fedora/RHEL
    (`grub2-reboot`). Set only when :attr:`kind` == GRUB2."""

    # ── argv recipes ────────────────────────────────────────────────────────

    def regenerate_cmd(self) -> list[str] | None:
        if self.kind != BootloaderKind.GRUB2:
            return None
        if self.grub_tool_prefix == "grub2-":
            cfg = "/boot/grub2/grub.cfg"
            return ["grub2-mkconfig", "-o", cfg]
        # Debian path
        if shutil.which("update-grub"):
            return ["update-grub"]
        return ["grub-mkconfig", "-o", "/boot/grub/grub.cfg"]

    def set_default_argv(self, entry: str) -> list[str] | None:
        if self.kind != BootloaderKind.GRUB2:
            return None
        # Debian ships `grub-set-default`; Fedora ships `grub2-set-default`.
        # An empty grub_tool_prefix means Debian → use the bare `grub-` form.
        prefix = self.grub_tool_prefix or "grub-"
        return [f"{prefix}set-default", entry]

    def one_shot_argv(self, entry: str) -> list[str] | None:
        if self.kind != BootloaderKind.GRUB2:
            return None
        prefix = self.grub_tool_prefix or "grub-"
        return [f"{prefix}reboot", entry]

    # ── status helpers ──────────────────────────────────────────────────────

    @property
    def is_supported(self) -> bool:
        """Whether install can proceed against this bootloader. Today:
        only GRUB2."""
        return self.kind == BootloaderKind.GRUB2


# ── detection ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Probe:
    """One filesystem signal we look for during detection."""

    path: Path
    kind: BootloaderKind
    grub_tool_prefix: str = ""
    config_dir: Path | None = None


def _default_probes() -> list[_Probe]:
    """Standard bootloader-detection paths, ordered most-specific first.

    The first probe whose ``path`` exists wins. We test more-specific
    layouts (GRUB2 with a Fedora-style ``/boot/grub2``) before falling
    back to generic ones (``/boot/grub``).
    """
    return [
        _Probe(
            path=Path("/boot/grub2/grub.cfg"),
            kind=BootloaderKind.GRUB2,
            grub_tool_prefix="grub2-",
            config_dir=Path("/boot/grub2"),
        ),
        _Probe(
            path=Path("/boot/grub/grub.cfg"),
            kind=BootloaderKind.GRUB2,
            grub_tool_prefix="",
            config_dir=Path("/boot/grub"),
        ),
        _Probe(
            path=Path("/boot/loader/loader.conf"),
            kind=BootloaderKind.SYSTEMD_BOOT,
            config_dir=Path("/boot/loader"),
        ),
        _Probe(
            path=Path("/efi/loader/loader.conf"),
            kind=BootloaderKind.SYSTEMD_BOOT,
            config_dir=Path("/efi/loader"),
        ),
        _Probe(
            path=Path("/boot/EFI/refind/refind.conf"),
            kind=BootloaderKind.REFIND,
            config_dir=Path("/boot/EFI/refind"),
        ),
        _Probe(
            path=Path("/boot/grub/menu.lst"),
            kind=BootloaderKind.GRUB_LEGACY,
            config_dir=Path("/boot/grub"),
        ),
    ]


def detect(*, probes: list[_Probe] | None = None) -> Bootloader:
    """Detect the bootloader.

    ``probes`` defaults to :func:`_default_probes`; tests can override
    to point at fixture directories. Returns a :class:`Bootloader` with
    ``kind=UNKNOWN`` if no probe matches.
    """
    for probe in probes or _default_probes():
        if probe.path.exists():
            return Bootloader(
                kind=probe.kind,
                detected_via=str(probe.path),
                config_dir=probe.config_dir,
                grub_tool_prefix=probe.grub_tool_prefix,
            )
    return Bootloader(
        kind=BootloaderKind.UNKNOWN,
        detected_via="no known bootloader signature found",
    )


def detect_with_root(root: Path) -> Bootloader:
    """Test/dry-run helper: detect against an alternate filesystem root.

    Each probe path is rooted at ``root`` for the duration of the
    detection. Useful for fixture-driven tests where ``root`` is a
    ``tmp_path`` containing a synthetic ``boot/grub/grub.cfg`` etc.
    """
    rerooted = [
        _Probe(
            path=root / probe.path.relative_to("/"),
            kind=probe.kind,
            grub_tool_prefix=probe.grub_tool_prefix,
            config_dir=(root / probe.config_dir.relative_to("/")) if probe.config_dir else None,
        )
        for probe in _default_probes()
    ]
    return detect(probes=rerooted)
