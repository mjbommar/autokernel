"""Static validation between apply and build.

The propose pipeline can produce proposals that look valid in isolation
but won't survive olddefconfig:

* the symbol doesn't exist in the target kernel (LLM hallucination,
  symbol renamed across kernel versions, or older proposal vs newer
  source);
* the symbol's ``depends on`` chain isn't satisfied (proposing
  ``CONFIG_X=y`` when ``X depends on Y`` and Y is =n);
* the symbol is part of a choice group whose parent feature is
  disabled (the dead-letter ``KASAN_OUTLINE`` when KASAN=n problem we
  saw in the v0.13 e2e run).

This module walks the proposed final.config + the target kernel's
Kconfig surface (via :mod:`autokernel.kconfig_walk`) and reports:

* :class:`CheckError` — would be silently dropped or actively wrong;
  caller should drop the proposal from the kfrag.
* :class:`CheckWarning` — quirky but maybe intentional; caller should
  surface but not block.

``check(final_config, surface)`` returns a :class:`CheckReport`. The
``iterate`` verb consumes it to filter the kfrag automatically; the
``apply --check`` flag surfaces it for human review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from autokernel.kconfig_walk import (
    ChoiceGroup,
    KconfigSurface,
    NumericTunable,
    SymbolType,
)


class FindingKind(str, Enum):
    UNKNOWN_SYMBOL = "unknown_symbol"  # symbol doesn't exist in target Kconfig
    OLDDEFCONFIG_STRIPPED = "olddefconfig_stripped"  # set in proposed but not in actual
    DEAD_LETTER_CHOICE = "dead_letter_choice"  # choice option whose parent is disabled
    OUT_OF_RANGE_TUNABLE = "out_of_range_tunable"  # int outside ranges
    DEMOTED_TO_DEFAULT = (
        "demoted_to_default"  # set to value that matches Kconfig default
    )


@dataclass(frozen=True)
class Finding:
    """One issue with the proposed config."""

    kind: FindingKind
    symbol: str  # CONFIG_-prefixed
    detail: str  # human-readable explanation
    is_error: bool  # error → drop the proposal; warning → surface only


@dataclass(frozen=True)
class CheckReport:
    """Result of :func:`check`.

    Errors block; warnings don't. The caller decides whether to drop
    error-flagged symbols from the kfrag (``iterate`` does;
    ``apply --check`` just renders).
    """

    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def all(self) -> list[Finding]:
        return [*self.errors, *self.warnings]

    def error_symbols(self) -> set[str]:
        return {f.symbol for f in self.errors}


# ── parsing helpers ───────────────────────────────────────────────────────


_SET_RE = re.compile(r"^(CONFIG_[A-Z0-9_]+)=(.*)$")
_NOT_SET_RE = re.compile(r"^#\s+(CONFIG_[A-Z0-9_]+) is not set\s*$")


def _parse_config(text: str) -> dict[str, str]:
    """Parse a .config-format file into a {symbol: value} dict.

    Disabled symbols (``# CONFIG_X is not set``) map to ``"n"``.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _SET_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
            continue
        m = _NOT_SET_RE.match(line)
        if m:
            out[m.group(1)] = "n"
    return out


def _index_surface(surface: KconfigSurface) -> dict[str, object]:
    """Map ``CONFIG_<name>`` → the surface entry (ChoiceOption, BoolToggle,
    NumericTunable). Used to answer "does this symbol exist?"."""
    idx: dict[str, object] = {}
    # Toggles + tunables are direct.
    for t in surface.toggles:
        idx[f"CONFIG_{t.name}"] = t
    for t in surface.tunables:
        idx[f"CONFIG_{t.name}"] = t
    # Choice options are nested — index each option's CONFIG name and
    # remember the parent choice group for dead-letter analysis.
    for c in surface.choices:
        for o in c.options:
            idx[f"CONFIG_{o.name}"] = (c, o)
    return idx


# ── checks ────────────────────────────────────────────────────────────────


def check(
    proposed_config_text: str,
    surface: KconfigSurface,
    *,
    actual_config_text: str | None = None,
) -> CheckReport:
    """Validate ``proposed_config_text`` against the target kernel's
    Kconfig surface.

    Args:
        proposed_config_text: contents of the kfrag-merged .config (i.e.
            ``<snap>/final.config`` BEFORE olddefconfig has run).
        surface: result of :func:`autokernel.kconfig_walk.walk` against
            the target kernel source.
        actual_config_text: optional contents of the source dir's
            ``.config`` AFTER olddefconfig — when provided, we can flag
            symbols that olddefconfig silently stripped. (Skip this
            argument from a pre-build check; pass it post-prepare to
            confirm what survived.)

    Returns:
        :class:`CheckReport` with errors (drop these from the kfrag)
        and warnings (worth surfacing).
    """
    proposed = _parse_config(proposed_config_text)
    actual = _parse_config(actual_config_text) if actual_config_text else None
    idx = _index_surface(surface)

    errors: list[Finding] = []
    warnings: list[Finding] = []

    for sym, value in proposed.items():
        entry = idx.get(sym)
        # Symbol doesn't exist in target Kconfig at all.
        if entry is None:
            errors.append(
                Finding(
                    kind=FindingKind.UNKNOWN_SYMBOL,
                    symbol=sym,
                    detail=(
                        f"{sym}={value!r} not found in target kernel's Kconfig — "
                        "either renamed, version-skew, or hallucinated by the LLM."
                    ),
                    is_error=True,
                )
            )
            continue

        # Choice-option dead-letter check: the parent's "is this
        # selectable" gate isn't visible from our surface, but we can
        # check whether ANY option in the choice was already chosen
        # (current). If no option in the choice has is_current=True,
        # the parent feature is disabled and our proposal is a
        # dead-letter.
        if isinstance(entry, tuple):
            parent_choice, _opt = entry
            if not isinstance(parent_choice, ChoiceGroup):
                continue
            any_current = any(o.is_current for o in parent_choice.options)
            if not any_current and value == "y":
                warnings.append(
                    Finding(
                        kind=FindingKind.DEAD_LETTER_CHOICE,
                        symbol=sym,
                        detail=(
                            f"{sym}=y is an option of choice "
                            f"{parent_choice.prompt!r} whose parent feature is "
                            f"disabled — olddefconfig will silently drop this."
                        ),
                        is_error=False,
                    )
                )

        # Out-of-range tunable check.
        if isinstance(entry, NumericTunable) and entry.type in (
            SymbolType.INT,
            SymbolType.HEX,
        ):
            if entry.ranges:
                v = _strip_quotes(value)
                try:
                    n = (
                        int(v, 0)
                        if entry.type == SymbolType.HEX or v.startswith("0x")
                        else int(v)
                    )
                except ValueError:
                    n = None
                if n is not None:
                    in_range = any(
                        int(lo, 0) <= n <= int(hi, 0) for lo, hi in entry.ranges
                    )
                    if not in_range:
                        errors.append(
                            Finding(
                                kind=FindingKind.OUT_OF_RANGE_TUNABLE,
                                symbol=sym,
                                detail=(
                                    f"{sym}={value!r} is outside the symbol's "
                                    f"declared range(s) {entry.ranges}."
                                ),
                                is_error=True,
                            )
                        )

    # Post-olddefconfig stripped check.
    if actual is not None:
        for sym, value in proposed.items():
            actual_value = actual.get(sym)
            if actual_value is None:
                # Wasn't in the proposed final.config? Skip.
                continue
            # If the proposal said =y but olddefconfig left it =n (or
            # vice versa), it was stripped.
            if _normalize(actual_value) != _normalize(value):
                warnings.append(
                    Finding(
                        kind=FindingKind.OLDDEFCONFIG_STRIPPED,
                        symbol=sym,
                        detail=(
                            f"{sym} proposed as {value!r} but olddefconfig "
                            f"resolved to {actual_value!r}. Probably a "
                            f"dependency conflict."
                        ),
                        is_error=False,
                    )
                )

    return CheckReport(errors=errors, warnings=warnings)


def _strip_quotes(v: str) -> str:
    v = v.strip()
    if v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    return v


def _normalize(v: str) -> str:
    """Compare-friendly form: 'y'/'m'/'n', stripped quotes for strings,
    integer literals."""
    v = v.strip()
    if v in {"y", "m", "n"}:
        return v
    return _strip_quotes(v)
