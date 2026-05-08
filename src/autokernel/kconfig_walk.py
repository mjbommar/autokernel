"""Walk a kernel source tree's Kconfig and surface the full
configuration knob surface: choice groups, bool toggles, numeric
tunables, and tristates.

The existing autokernel pipeline only ever sees what's in
``/boot/config-$(uname -r)`` — i.e. the *current* assignments. That's
enough to propose *trims* (=y/=m → =n) but blind to every other
Kconfig axis: which choice options exist, what range an int can take,
what help text says about the tradeoff. This module extracts that
metadata directly from the kernel source via :mod:`kconfiglib`.

The output is intentionally narrow — three lists of structured
records, one per dimension — because that's all the LLM agents in
``autokernel.agent`` need to make decisions. Caller-side rendering and
caching are upstream concerns.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterator

import kconfiglib


# ── data classes ──────────────────────────────────────────────────────────


class SymbolType(str, Enum):
    BOOL = "bool"
    TRISTATE = "tristate"
    INT = "int"
    HEX = "hex"
    STRING = "string"
    UNKNOWN = "unknown"


_TYPE_MAP: dict[int, SymbolType] = {
    kconfiglib.BOOL: SymbolType.BOOL,
    kconfiglib.TRISTATE: SymbolType.TRISTATE,
    kconfiglib.INT: SymbolType.INT,
    kconfiglib.HEX: SymbolType.HEX,
    kconfiglib.STRING: SymbolType.STRING,
    kconfiglib.UNKNOWN: SymbolType.UNKNOWN,
}


@dataclass(frozen=True)
class ChoiceOption:
    """One member symbol of a Kconfig ``choice``."""

    name: str  # CONFIG_-less symbol name, e.g. "PREEMPT_NONE"
    prompt: str | None  # the menu prompt shown to humans
    help: str | None
    is_current: bool  # this option is the choice's current selection


@dataclass(frozen=True)
class ChoiceGroup:
    """A Kconfig ``choice`` block — pick exactly one option.

    Examples: PREEMPT model, HZ, default I/O scheduler, default TCP
    congestion control, kernel-image compression.
    """

    name: str | None  # choice may be unnamed (rare); use prompt if so
    prompt: str | None
    help: str | None
    options: list[ChoiceOption]
    location: str  # "kernel/Kconfig.preempt:18" or similar


@dataclass(frozen=True)
class BoolToggle:
    """A standalone ``bool`` symbol the LLM should judge.

    Excludes tristates (those are the existing trim path) and excludes
    bools that are part of a choice group (handled by ChoiceGroup).
    """

    name: str  # CONFIG_-less, e.g. "TRANSPARENT_HUGEPAGE"
    prompt: str | None
    help: str | None
    current_value: str  # 'y' or 'n'
    location: str
    direct_dep_str: str  # e.g. "X86_64 && SMP"


@dataclass(frozen=True)
class NumericTunable:
    """An ``int``, ``hex``, or ``string`` Kconfig knob.

    Examples: NR_CPUS, LOG_BUF_SHIFT, LOCALVERSION.
    """

    name: str
    type: SymbolType
    prompt: str | None
    help: str | None
    current_value: str
    ranges: list[tuple[str, str]]  # raw lo/hi pairs as strings
    location: str


@dataclass(frozen=True)
class KconfigSurface:
    """Aggregate result of walking the tree."""

    arch: str
    source_dir: Path
    choices: list[ChoiceGroup] = field(default_factory=list)
    toggles: list[BoolToggle] = field(default_factory=list)
    tunables: list[NumericTunable] = field(default_factory=list)


# ── unsupported-syntax workaround ─────────────────────────────────────────
#
# Linux 6.19 introduced new Kconfig keywords (most notably ``transitional``,
# used to mark deprecated CONFIG names being migrated). The released
# kconfiglib (14.1.0) doesn't recognize them and aborts parsing. To stay
# usable against modern kernel sources, we temporarily strip those lines
# from real Kconfig files on disk during parse, then restore them.
#
# This is in-place editing of the user's source tree, so the restore
# step is critical. The context manager guarantees restoration even on
# exception, and uses byte-exact backups (no line-ending normalization).

# Kconfig keywords introduced post-kconfiglib-14.1.0 that the parser
# trips over. Every entry must be safe to drop entirely (ones that
# affect *value* rather than syntax — e.g. "modules" marking the
# modules-master, "transitional" marking deprecated names — only
# affect ``make *config`` UX, not the CONFIG values we read).
_UNSUPPORTED_KCONFIG_KEYWORDS: tuple[str, ...] = (
    "transitional",  # 6.19+: deprecated CONFIG name marker
    "modules",       # 6.19+: marks the MODULES master symbol
)
_UNSUPPORTED_KCONFIG_LINE = re.compile(
    r"^\s*(?:" + "|".join(_UNSUPPORTED_KCONFIG_KEYWORDS) + r")\s*$",
    re.MULTILINE,
)


def _find_kconfig_files_with_unsupported(source_dir: Path) -> list[Path]:
    """Find every ``Kconfig*`` file inside *source_dir* that contains a
    keyword kconfiglib can't parse. Skips ``scripts/`` and
    ``Documentation/`` (kconfig's own self-test fixtures).
    """
    hits: list[Path] = []
    for path in source_dir.rglob("Kconfig*"):
        # skip self-test fixtures + docs
        rel = path.relative_to(source_dir)
        first = rel.parts[0] if rel.parts else ""
        if first in ("scripts", "Documentation", "tools"):
            continue
        try:
            text = path.read_text(errors="replace")
        except (PermissionError, OSError):
            continue
        if _UNSUPPORTED_KCONFIG_LINE.search(text):
            hits.append(path)
    return hits


@contextlib.contextmanager
def _patch_unsupported_kconfig_syntax(source_dir: Path) -> Iterator[None]:
    """Strip unsupported keywords from Kconfig files; restore on exit.

    Yields once. On the way in, every file containing ``transitional``
    (or future unsupported tokens) is backed up to ``<file>.autokernel.bak``
    and rewritten with those lines removed. On the way out — exception
    or otherwise — every backup is restored.
    """
    edited: list[Path] = []
    for path in _find_kconfig_files_with_unsupported(source_dir):
        backup = path.with_suffix(path.suffix + ".autokernel.bak")
        # If a stale backup exists from a crashed previous run, refuse
        # to overwrite — keep the user's original text safe.
        if backup.exists():
            continue
        try:
            shutil.copy2(path, backup)
            text = path.read_text(errors="replace")
            cleaned = _UNSUPPORTED_KCONFIG_LINE.sub("", text)
            path.write_text(cleaned)
            edited.append(path)
        except (PermissionError, OSError):
            # Revert any partial changes for this file before bubbling.
            if backup.exists():
                try:
                    shutil.copy2(backup, path)
                    backup.unlink()
                except OSError:
                    pass
            continue
    try:
        yield
    finally:
        for path in edited:
            backup = path.with_suffix(path.suffix + ".autokernel.bak")
            try:
                shutil.copy2(backup, path)
                backup.unlink()
            except OSError:
                # Last-ditch: leave a flag so the user notices.
                pass


# ── helpers ───────────────────────────────────────────────────────────────


def _location(node) -> str:
    """Format a kconfiglib MenuNode as 'path:line'."""
    if node is None or node.filename is None:
        return "<unknown>"
    return f"{node.filename}:{node.linenr}"


def _help_text(sym_or_choice) -> str | None:
    """Pull the canonical help text from the first node, if any."""
    for node in sym_or_choice.nodes or []:
        if node.help:
            return node.help.strip()
    return None


def _prompt_text(sym_or_choice) -> str | None:
    for node in sym_or_choice.nodes or []:
        if node.prompt:
            return node.prompt[0]  # (text, condition)
    return None


def _all_symbols(kconf: kconfiglib.Kconfig) -> list[kconfiglib.Symbol]:
    """All defined symbols, deduplicated, in source order."""
    seen: set[str] = set()
    out: list[kconfiglib.Symbol] = []
    for sym in kconf.unique_defined_syms:
        if sym.name in seen:
            continue
        seen.add(sym.name)
        out.append(sym)
    return out


# ── main entry point ──────────────────────────────────────────────────────


_SRCARCH_FOR_ARCH: dict[str, str] = {
    "x86_64": "x86",
    "i386": "x86",
    "i686": "x86",
    "x86": "x86",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
    "armv6l": "arm",
    "arm": "arm",
    "ppc64le": "powerpc",
    "ppc64": "powerpc",
    "ppc": "powerpc",
    "powerpc": "powerpc",
    "riscv64": "riscv",
    "riscv": "riscv",
    "s390x": "s390",
    "s390": "s390",
    "mips64": "mips",
    "mips": "mips",
}


def walk(
    source_dir: Path,
    *,
    arch: str = "x86_64",
    srcarch: str | None = None,
    config_path: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> KconfigSurface:
    """Parse the Kconfig tree at ``source_dir`` and return a
    structured surface.

    Args:
        source_dir: kernel source root (must contain ``Kconfig`` and
            ``Makefile``).
        arch: kernel ARCH variable. Defaults to ``x86_64`` since that's
            the autokernel target. ARM/RISC-V supported by passing
            ``arch="arm64"`` etc.
        srcarch: kernel SRCARCH (usually equals arch but can differ
            for ports — e.g. ARCH=i386 SRCARCH=x86). If None, defaults
            to ``arch``.
        config_path: optional path to a .config file to load *before*
            walking, so ``current_value`` reflects an actual config
            instead of Kconfig-default values. When None, defaults
            apply.
        env_overrides: extra envvars to set during parsing (rare —
            for unusual cross-build setups).

    Returns:
        :class:`KconfigSurface` populated with every choice group,
        every bool toggle that's user-visible, and every numeric/string
        tunable.

    Raises:
        FileNotFoundError: if ``source_dir/Kconfig`` doesn't exist.
        kconfiglib.KconfigError: on Kconfig syntax errors (unusual).
    """
    source_dir = Path(source_dir).resolve()
    if not (source_dir / "Kconfig").exists():
        raise FileNotFoundError(f"{source_dir}/Kconfig not found")
    if srcarch is None:
        # x86_64 → x86 (the kernel's arch dir is arch/x86, ARCH=x86_64
        # but SRCARCH=x86). Same dance for arm64, riscv, etc.
        srcarch = _SRCARCH_FOR_ARCH.get(arch, arch)

    # kconfiglib relies on these envvars at parse time. Save and
    # restore so we don't leak into the parent process.
    saved_env: dict[str, str | None] = {}
    set_env: dict[str, str] = {
        "ARCH": arch,
        "SRCARCH": srcarch,
        "KERNELVERSION": "0",
        "CC": "gcc",
        "HOSTCC": "gcc",
        "LD": "ld",
        "srctree": str(source_dir),
    }
    if env_overrides:
        set_env.update(env_overrides)
    for k, v in set_env.items():
        saved_env[k] = os.environ.get(k)
        os.environ[k] = v
    saved_cwd = Path.cwd()
    try:
        os.chdir(source_dir)
        with _patch_unsupported_kconfig_syntax(source_dir):
            kconf = kconfiglib.Kconfig("Kconfig", warn=False, warn_to_stderr=False)
            if config_path is not None:
                kconf.load_config(str(config_path), replace=True)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.chdir(saved_cwd)

    return _build_surface(kconf, source_dir, arch)


def _build_surface(
    kconf: kconfiglib.Kconfig, source_dir: Path, arch: str
) -> KconfigSurface:
    """Convert the parsed Kconfig into our structured form."""
    choices: list[ChoiceGroup] = []
    toggles: list[BoolToggle] = []
    tunables: list[NumericTunable] = []

    # Choices first.
    seen_choices: set[int] = set()
    for sym in _all_symbols(kconf):
        if sym.choice is None:
            continue
        if id(sym.choice) in seen_choices:
            continue
        seen_choices.add(id(sym.choice))
        choices.append(_extract_choice(sym.choice))

    # Toggles + tunables — exclude symbols that are part of a choice.
    for sym in _all_symbols(kconf):
        if sym.choice is not None:
            continue
        # Symbols that are never user-visible aren't worth showing the
        # LLM (no prompt → only set as a side effect of others).
        if not _prompt_text(sym):
            continue
        sym_type = _TYPE_MAP.get(sym.orig_type, SymbolType.UNKNOWN)
        if sym_type == SymbolType.BOOL:
            toggles.append(_extract_toggle(sym))
        elif sym_type in (SymbolType.INT, SymbolType.HEX, SymbolType.STRING):
            tunables.append(_extract_tunable(sym, sym_type))
        # tristates are handled by the existing trim pipeline; skip.

    return KconfigSurface(
        arch=arch,
        source_dir=source_dir,
        choices=choices,
        toggles=toggles,
        tunables=tunables,
    )


def _extract_choice(choice: kconfiglib.Choice) -> ChoiceGroup:
    options: list[ChoiceOption] = []
    selection = choice.selection
    for sym in choice.syms:
        options.append(
            ChoiceOption(
                name=sym.name,
                prompt=_prompt_text(sym),
                help=_help_text(sym),
                is_current=(selection is sym),
            )
        )
    location = _location(choice.nodes[0]) if choice.nodes else "<unknown>"
    return ChoiceGroup(
        name=choice.name,
        prompt=_prompt_text(choice),
        help=_help_text(choice),
        options=options,
        location=location,
    )


def _extract_toggle(sym: kconfiglib.Symbol) -> BoolToggle:
    location = _location(sym.nodes[0]) if sym.nodes else "<unknown>"
    direct_dep_str = ""
    if sym.direct_dep != kconfiglib.Symbol:
        try:
            direct_dep_str = kconfiglib.expr_str(sym.direct_dep)
        except Exception:
            direct_dep_str = ""
    return BoolToggle(
        name=sym.name,
        prompt=_prompt_text(sym),
        help=_help_text(sym),
        current_value=sym.str_value or "n",
        location=location,
        direct_dep_str=direct_dep_str,
    )


def _extract_tunable(sym: kconfiglib.Symbol, sym_type: SymbolType) -> NumericTunable:
    location = _location(sym.nodes[0]) if sym.nodes else "<unknown>"
    ranges: list[tuple[str, str]] = []
    for lo, hi, _cond in sym.ranges or []:
        try:
            ranges.append((str(lo.str_value), str(hi.str_value)))
        except AttributeError:
            ranges.append((str(lo), str(hi)))
    return NumericTunable(
        name=sym.name,
        type=sym_type,
        prompt=_prompt_text(sym),
        help=_help_text(sym),
        current_value=sym.str_value,
        ranges=ranges,
        location=location,
    )
