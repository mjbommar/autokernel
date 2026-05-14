"""Source-derived Kconfig inventory.

This module builds the deterministic half of the Kconfig intelligence
layer. It parses Kconfig, Kbuild/Makefile references, and source-code
CONFIG_* usages into stable JSONL records. LLM enrichment is layered on
top of these records; facts here are the auditable source of truth.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import kconfiglib
from pydantic import BaseModel, Field

from autokernel import kconfig_walk
from autokernel.kconfig_walk import SymbolType


SCHEMA_VERSION = "1"
GENERATOR_VERSION = "1"

CONFIG_RE = re.compile(r"\bCONFIG_[A-Z0-9_]+\b")
BARE_SYMBOL_RE = re.compile(r"\b[A-Z][A-Z0-9_]{1,}\b")
KBUILD_OBJECT_RE = re.compile(r"([A-Za-z0-9_./+-]+\.o)\b")
FIRMWARE_RE = re.compile(r'MODULE_FIRMWARE\(\s*"([^"]+)"\s*\)')
MODULE_DEVICE_TABLE_RE = re.compile(r"MODULE_DEVICE_TABLE\(\s*([A-Za-z0-9_]+)\s*,")

KBUILD_FILENAMES = {"Kbuild", "Makefile"}
SOURCE_SUFFIXES = {".c", ".h", ".S", ".s", ".rs"}
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "Documentation",
    "scripts",
    "tools",
    "samples",
    "usr",
}


class SourceLocation(BaseModel):
    path: str
    line: int


class KconfigDefault(BaseModel):
    value: str
    condition: str | None = None


class KconfigRange(BaseModel):
    low: str
    high: str
    condition: str | None = None


class ChoiceMembership(BaseModel):
    name: str | None = None
    prompt: str | None = None
    options: list[str] = Field(default_factory=list)


class KbuildBinding(BaseModel):
    path: str
    line: int
    text: str
    objects: list[str] = Field(default_factory=list)


class SourceUsage(BaseModel):
    path: str
    line: int
    text: str


class SourceRefSummary(BaseModel):
    usage_count: int = 0
    kbuild_count: int = 0
    top_paths: list[str] = Field(default_factory=list)


class HardwareSupportSummary(BaseModel):
    buses: list[str] = Field(default_factory=list)
    firmware: list[str] = Field(default_factory=list)


class KconfigSymbolRecord(BaseModel):
    symbol: str
    name: str
    type: SymbolType
    prompt: str | None = None
    help: str | None = None
    locations: list[SourceLocation] = Field(default_factory=list)
    menu_path: list[str] = Field(default_factory=list)
    depends_on: str | None = None
    depends_symbols: list[str] = Field(default_factory=list)
    selects: list[str] = Field(default_factory=list)
    selected_by: list[str] = Field(default_factory=list)
    implies: list[str] = Field(default_factory=list)
    implied_by: list[str] = Field(default_factory=list)
    defaults: list[KconfigDefault] = Field(default_factory=list)
    ranges: list[KconfigRange] = Field(default_factory=list)
    choice: ChoiceMembership | None = None
    current_value: str | None = None
    kbuild: list[KbuildBinding] = Field(default_factory=list)
    source_usages: list[SourceUsage] = Field(default_factory=list)
    source_refs: SourceRefSummary = Field(default_factory=SourceRefSummary)
    modules: list[str] = Field(default_factory=list)
    hardware: HardwareSupportSummary = Field(default_factory=HardwareSupportSummary)
    subsystem_tags: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    fact_hash: str


class InventoryManifest(BaseModel):
    schema_version: str = SCHEMA_VERSION
    generator_version: str = GENERATOR_VERSION
    kernel_version: str | None = None
    source_dir: str
    source_git_commit: str | None = None
    source_git_describe: str | None = None
    arch: str
    srcarch: str
    config_path: str | None = None
    generated_at: str
    symbol_count: int
    kconfiglib_version: str | None = None


class InventoryDataset(BaseModel):
    manifest: InventoryManifest
    symbols: list[KconfigSymbolRecord]


class FileExcerpt(BaseModel):
    path: str
    start_line: int
    end_line: int
    text: str


def build_inventory(
    source_dir: Path,
    *,
    arch: str = "x86_64",
    srcarch: str | None = None,
    config_path: Path | None = None,
    limit: int | None = None,
    symbol_names: Iterable[str] | None = None,
) -> InventoryDataset:
    """Build an in-memory inventory from a kernel source tree."""
    source_dir = source_dir.expanduser().resolve()
    if config_path is not None:
        config_path = config_path.expanduser().resolve()
    if not (source_dir / "Kconfig").exists():
        raise FileNotFoundError(f"{source_dir}/Kconfig not found")
    if srcarch is None:
        srcarch = kconfig_walk._SRCARCH_FOR_ARCH.get(arch, arch)

    kconf = _parse_kconfig(
        source_dir, arch=arch, srcarch=srcarch, config_path=config_path
    )
    all_names = {sym.name for sym in kconf.unique_defined_syms if sym.name}
    wanted = _normalize_symbol_filter(symbol_names)

    kbuild_index = _scan_kbuild(source_dir)
    source_index = _scan_source_usages(source_dir)

    selected_by, implied_by = _reverse_relation_indexes(kconf)
    records: list[KconfigSymbolRecord] = []
    for sym in kconf.unique_defined_syms:
        if not sym.name:
            continue
        name = cast(str, sym.name)
        if wanted is not None and name not in wanted:
            continue
        records.append(
            _symbol_record(
                sym,
                source_dir=source_dir,
                all_names=all_names,
                kbuild_index=kbuild_index,
                source_index=source_index,
                selected_by=selected_by,
                implied_by=implied_by,
            )
        )
        if limit is not None and len(records) >= limit:
            break

    records.sort(key=lambda r: r.symbol)
    manifest = InventoryManifest(
        source_dir=str(source_dir),
        source_git_commit=_git_output(source_dir, "rev-parse", "HEAD"),
        source_git_describe=_git_output(
            source_dir, "describe", "--tags", "--always", "--dirty"
        ),
        arch=arch,
        srcarch=srcarch,
        config_path=str(config_path.expanduser().resolve()) if config_path else None,
        generated_at=datetime.now(UTC).isoformat(),
        symbol_count=len(records),
        kconfiglib_version=getattr(kconfiglib, "__version__", None),
    )
    return InventoryDataset(manifest=manifest, symbols=records)


def _git_output(source_dir: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_dir), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def write_inventory(dataset: InventoryDataset, out_dir: Path) -> None:
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(dataset.manifest.model_dump_json(indent=2))
    with (out_dir / "symbols.jsonl").open("w") as f:
        for rec in sorted(dataset.symbols, key=lambda r: r.symbol):
            f.write(rec.model_dump_json(exclude_none=True))
            f.write("\n")


def read_inventory(inv_dir: Path) -> InventoryDataset:
    inv_dir = inv_dir.expanduser().resolve()
    manifest = InventoryManifest.model_validate_json(
        (inv_dir / "manifest.json").read_text()
    )
    symbols: list[KconfigSymbolRecord] = []
    with (inv_dir / "symbols.jsonl").open() as f:
        for line in f:
            if line.strip():
                symbols.append(KconfigSymbolRecord.model_validate_json(line))
    return InventoryDataset(manifest=manifest, symbols=symbols)


class InventoryTools:
    """Read/search tools for inventory enrichment agents."""

    def __init__(self, dataset: InventoryDataset, *, source_dir: Path | None = None):
        self.dataset = dataset
        self.source_dir = (
            source_dir.expanduser().resolve()
            if source_dir is not None
            else Path(dataset.manifest.source_dir).expanduser().resolve()
        )
        self.by_symbol = {rec.symbol: rec for rec in dataset.symbols}
        self.by_name = {rec.name: rec for rec in dataset.symbols}

    @classmethod
    def from_dir(
        cls, inv_dir: Path, *, source_dir: Path | None = None
    ) -> "InventoryTools":
        return cls(read_inventory(inv_dir), source_dir=source_dir)

    def list_symbols(
        self,
        *,
        kind: str | None = None,
        prefix: str | None = None,
        subsystem: str | None = None,
        limit: int = 100,
    ) -> list[str]:
        out: list[str] = []
        for rec in self.dataset.symbols:
            if kind and rec.type.value != kind:
                continue
            if prefix and not rec.symbol.startswith(_normalize_config(prefix)):
                continue
            if subsystem and subsystem not in rec.subsystem_tags:
                continue
            out.append(rec.symbol)
            if len(out) >= limit:
                break
        return out

    def search_symbols(self, query: str, *, limit: int = 50) -> list[str]:
        q = query.upper()
        hits = [rec.symbol for rec in self.dataset.symbols if q in rec.symbol.upper()]
        return hits[:limit]

    def search_kconfig_text(self, query: str, *, limit: int = 50) -> list[str]:
        q = query.lower()
        hits: list[str] = []
        for rec in self.dataset.symbols:
            hay = "\n".join(v for v in (rec.prompt, rec.help) if v)
            if q in hay.lower():
                hits.append(rec.symbol)
                if len(hits) >= limit:
                    break
        return hits

    def get_symbol(self, symbol: str) -> KconfigSymbolRecord:
        key = _normalize_config(symbol)
        try:
            return self.by_symbol[key]
        except KeyError as e:
            raise KeyError(f"unknown symbol {key}") from e

    def get_symbol_relations(self, symbol: str) -> dict[str, list[str]]:
        rec = self.get_symbol(symbol)
        return {
            "depends_symbols": rec.depends_symbols,
            "selects": rec.selects,
            "selected_by": rec.selected_by,
            "implies": rec.implies,
            "implied_by": rec.implied_by,
            "choice_options": rec.choice.options if rec.choice else [],
        }

    def search_config_usages(
        self, symbol: str, *, limit: int = 100
    ) -> list[SourceUsage]:
        rec = self.get_symbol(symbol)
        return rec.source_usages[:limit]

    def search_kbuild_usages(
        self, symbol: str, *, limit: int = 100
    ) -> list[KbuildBinding]:
        rec = self.get_symbol(symbol)
        return rec.kbuild[:limit]

    def list_files(
        self, path_prefix: str = "", *, globs: list[str] | None = None, limit: int = 100
    ) -> list[str]:
        root = self._safe_path(path_prefix) if path_prefix else self.source_dir
        if root.is_file():
            return [str(root.relative_to(self.source_dir))]
        out: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or _has_skipped_part(
                path.relative_to(self.source_dir)
            ):
                continue
            rel = str(path.relative_to(self.source_dir))
            if globs and not any(fnmatch.fnmatch(rel, pat) for pat in globs):
                continue
            out.append(rel)
            if len(out) >= limit:
                break
        return out

    def read_file_head(self, path: str, *, max_lines: int = 80) -> FileExcerpt:
        return self.read_file_excerpt(path, start_line=1, end_line=max_lines)

    def read_file_excerpt(
        self, path: str, *, start_line: int, end_line: int
    ) -> FileExcerpt:
        safe = self._safe_path(path)
        lines = safe.read_text(errors="replace").splitlines()
        start = max(1, start_line)
        end = max(start, min(end_line, len(lines)))
        text = "\n".join(lines[start - 1 : end])
        return FileExcerpt(path=path, start_line=start, end_line=end, text=text)

    def read_file_around_match(
        self, path: str, *, line: int, context: int = 40
    ) -> FileExcerpt:
        return self.read_file_excerpt(
            path,
            start_line=max(1, line - context),
            end_line=line + context,
        )

    def _safe_path(self, path: str) -> Path:
        candidate = (self.source_dir / path).resolve()
        if not candidate.is_relative_to(self.source_dir):
            raise ValueError(f"path escapes kernel source root: {path}")
        if not candidate.exists():
            raise FileNotFoundError(path)
        return candidate


def _parse_kconfig(
    source_dir: Path,
    *,
    arch: str,
    srcarch: str,
    config_path: Path | None,
) -> kconfiglib.Kconfig:
    saved_env: dict[str, str | None] = {}
    set_env = {
        "ARCH": arch,
        "SRCARCH": srcarch,
        "KERNELVERSION": "0",
        "CC": "gcc",
        "HOSTCC": "gcc",
        "LD": "ld",
        "srctree": str(source_dir),
    }
    for key, value in set_env.items():
        saved_env[key] = os.environ.get(key)
        os.environ[key] = value
    saved_cwd = Path.cwd()
    try:
        os.chdir(source_dir)
        with kconfig_walk._patch_unsupported_kconfig_syntax(source_dir):
            kconf = kconfiglib.Kconfig("Kconfig", warn=False, warn_to_stderr=False)
            if config_path is not None:
                kconf.load_config(str(config_path), replace=True)
            return kconf
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.chdir(saved_cwd)


def _symbol_record(
    sym: kconfiglib.Symbol,
    *,
    source_dir: Path,
    all_names: set[str],
    kbuild_index: dict[str, list[KbuildBinding]],
    source_index: dict[str, list[SourceUsage]],
    selected_by: dict[str, list[str]],
    implied_by: dict[str, list[str]],
) -> KconfigSymbolRecord:
    name_value = getattr(sym, "name", None)
    if not isinstance(name_value, str):
        raise ValueError("anonymous Kconfig symbol cannot be inventoried")
    name = name_value
    symbol = f"CONFIG_{name}"
    locations = [_node_location(n, source_dir) for n in sym.nodes]
    depends_on = _expr_to_str(getattr(sym, "direct_dep", None))
    selects = _relation_targets(sym.selects)
    implies = _relation_targets(sym.implies)
    kbuild = kbuild_index.get(symbol, [])
    usages = source_index.get(symbol, [])
    sym_type = kconfig_walk._TYPE_MAP.get(sym.orig_type, SymbolType.UNKNOWN)
    modules = (
        _module_names_from_kbuild(kbuild) if sym_type == SymbolType.TRISTATE else []
    )
    hardware = _hardware_summary(source_dir, usages, kbuild)

    rec = KconfigSymbolRecord(
        symbol=symbol,
        name=name,
        type=sym_type,
        prompt=kconfig_walk._prompt_text(sym),
        help=kconfig_walk._help_text(sym),
        locations=locations,
        menu_path=_menu_path(sym),
        depends_on=depends_on,
        depends_symbols=_symbols_from_expr(depends_on, all_names),
        selects=selects,
        selected_by=selected_by.get(name, []),
        implies=implies,
        implied_by=implied_by.get(name, []),
        defaults=_defaults(sym.defaults),
        ranges=_ranges(sym.ranges),
        choice=_choice_membership(sym),
        current_value=sym.str_value,
        kbuild=kbuild,
        source_usages=usages[:100],
        source_refs=SourceRefSummary(
            usage_count=len(usages),
            kbuild_count=len(kbuild),
            top_paths=_top_paths(usages, kbuild),
        ),
        modules=modules,
        hardware=hardware,
        subsystem_tags=_subsystem_tags(locations, usages, kbuild),
        risk_tags=_risk_tags(symbol, locations, usages, kbuild),
        fact_hash="",
    )
    rec.fact_hash = _fact_hash(rec)
    return rec


def _scan_kbuild(source_dir: Path) -> dict[str, list[KbuildBinding]]:
    index: dict[str, list[KbuildBinding]] = defaultdict(list)
    for path in _walk_files(source_dir, names=KBUILD_FILENAMES):
        rel = str(path.relative_to(source_dir))
        for lineno, line in _iter_lines(path):
            symbols = sorted(set(CONFIG_RE.findall(line)))
            if not symbols:
                continue
            binding = KbuildBinding(
                path=rel,
                line=lineno,
                text=line.strip(),
                objects=KBUILD_OBJECT_RE.findall(line),
            )
            for symbol in symbols:
                index[symbol].append(binding)
    return dict(index)


def _scan_source_usages(source_dir: Path) -> dict[str, list[SourceUsage]]:
    index: dict[str, list[SourceUsage]] = defaultdict(list)
    for path in _walk_files(source_dir, suffixes=SOURCE_SUFFIXES):
        rel = str(path.relative_to(source_dir))
        for lineno, line in _iter_lines(path):
            symbols = sorted(set(CONFIG_RE.findall(line)))
            if not symbols:
                continue
            usage = SourceUsage(path=rel, line=lineno, text=line.strip())
            for symbol in symbols:
                index[symbol].append(usage)
    return dict(index)


def _walk_files(
    source_dir: Path,
    *,
    names: set[str] | None = None,
    suffixes: set[str] | None = None,
) -> Iterable[Path]:
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source_dir)
        if _has_skipped_part(rel):
            continue
        if names is not None and path.name not in names:
            continue
        if suffixes is not None and path.suffix not in suffixes:
            continue
        yield path


def _iter_lines(path: Path) -> Iterable[tuple[int, str]]:
    with contextlib.suppress(UnicodeDecodeError, OSError):
        for idx, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            yield idx, line


def _reverse_relation_indexes(
    kconf: kconfiglib.Kconfig,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    selected_by: dict[str, list[str]] = defaultdict(list)
    implied_by: dict[str, list[str]] = defaultdict(list)
    for sym in kconf.unique_defined_syms:
        if not sym.name:
            continue
        source = cast(str, sym.name)
        for target in _relation_targets(sym.selects):
            selected_by[target].append(source)
        for target in _relation_targets(sym.implies):
            implied_by[target].append(source)
    return _sorted_dict(selected_by), _sorted_dict(implied_by)


def _relation_targets(items: list[Any]) -> list[str]:
    out: list[str] = []
    for item in items or []:
        target = item[0] if isinstance(item, tuple) and item else item
        name = getattr(target, "name", None)
        if name:
            out.append(cast(str, name))
    return sorted(set(out))


def _defaults(items: list[Any]) -> list[KconfigDefault]:
    out: list[KconfigDefault] = []
    for item in items or []:
        value = item[0] if isinstance(item, tuple) and item else item
        cond = item[1] if isinstance(item, tuple) and len(item) > 1 else None
        out.append(
            KconfigDefault(value=_value_to_str(value), condition=_expr_to_str(cond))
        )
    return out


def _ranges(items: list[Any]) -> list[KconfigRange]:
    out: list[KconfigRange] = []
    for item in items or []:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        cond = item[2] if len(item) > 2 else None
        out.append(
            KconfigRange(
                low=_value_to_str(item[0]),
                high=_value_to_str(item[1]),
                condition=_expr_to_str(cond),
            )
        )
    return out


def _choice_membership(sym: kconfiglib.Symbol) -> ChoiceMembership | None:
    choice = sym.choice
    if choice is None:
        return None
    return ChoiceMembership(
        name=choice.name,
        prompt=kconfig_walk._prompt_text(choice),
        options=sorted(s.name for s in choice.syms if s.name),
    )


def _node_location(node: Any, source_dir: Path) -> SourceLocation:
    raw = kconfig_walk._location(node)
    path, _, line = raw.rpartition(":")
    if not path:
        return SourceLocation(path="<unknown>", line=0)
    p = Path(path)
    rel = (
        str(p.relative_to(source_dir))
        if p.is_absolute() and p.is_relative_to(source_dir)
        else path
    )
    try:
        lineno = int(line)
    except ValueError:
        lineno = 0
    return SourceLocation(path=rel, line=lineno)


def _menu_path(sym: kconfiglib.Symbol) -> list[str]:
    if not sym.nodes:
        return []
    node = sym.nodes[0]
    out: list[str] = []
    parent = getattr(node, "parent", None)
    while parent is not None:
        prompt = getattr(parent, "prompt", None)
        if prompt:
            out.append(str(prompt[0]))
        parent = getattr(parent, "parent", None)
    return list(reversed(out))


def _expr_to_str(expr: Any) -> str | None:
    if expr is None:
        return None
    try:
        text = kconfiglib.expr_str(expr)
    except Exception:
        text = str(expr)
    return text if text and text != "y" else None


def _value_to_str(value: Any) -> str:
    return str(getattr(value, "str_value", getattr(value, "name", value)))


def _symbols_from_expr(expr: str | None, all_names: set[str]) -> list[str]:
    if not expr:
        return []
    return sorted({tok for tok in BARE_SYMBOL_RE.findall(expr) if tok in all_names})


def _module_names_from_kbuild(bindings: list[KbuildBinding]) -> list[str]:
    names: set[str] = set()
    for binding in bindings:
        for obj in binding.objects:
            stem = Path(obj).name.removesuffix(".o")
            if stem and stem not in {"built-in", "modules"}:
                names.add(stem.replace("-", "_"))
    return sorted(names)


def _hardware_summary(
    source_dir: Path, usages: list[SourceUsage], bindings: list[KbuildBinding]
) -> HardwareSupportSummary:
    candidate_paths = {u.path for u in usages}
    candidate_dirs: set[str] = set()
    for binding in bindings:
        base = Path(binding.path).parent
        candidate_dirs.add(str(base))
        for obj in binding.objects:
            candidate_paths.add(str(base / Path(obj).with_suffix(".c")))
        for rel_dir in re.findall(r"\+=\s*([A-Za-z0-9_./+-]+)/", binding.text):
            candidate_dirs.add(str(base / rel_dir))
    buses: set[str] = set()
    firmware: set[str] = set()
    for rel in sorted(candidate_paths)[:50]:
        path = source_dir / rel
        if not path.exists() or path.suffix not in SOURCE_SUFFIXES:
            continue
        text = path.read_text(errors="replace")
        buses.update(MODULE_DEVICE_TABLE_RE.findall(text))
        firmware.update(FIRMWARE_RE.findall(text))
    for rel_dir in sorted(candidate_dirs)[:20]:
        root = source_dir / rel_dir
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.c"))[:100]:
            text = path.read_text(errors="replace")
            buses.update(MODULE_DEVICE_TABLE_RE.findall(text))
            firmware.update(FIRMWARE_RE.findall(text))
    return HardwareSupportSummary(buses=sorted(buses), firmware=sorted(firmware))


def _top_paths(usages: list[SourceUsage], bindings: list[KbuildBinding]) -> list[str]:
    counts: dict[str, int] = defaultdict(int)
    for usage in usages:
        counts[usage.path] += 1
    for binding in bindings:
        counts[binding.path] += 1
    return [
        p for p, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    ]


def _subsystem_tags(
    locations: list[SourceLocation],
    usages: list[SourceUsage],
    bindings: list[KbuildBinding],
) -> list[str]:
    paths = (
        [loc.path for loc in locations]
        + [u.path for u in usages]
        + [b.path for b in bindings]
    )
    tags: set[str] = set()
    for path in paths:
        parts = Path(path).parts
        if not parts:
            continue
        first = parts[0]
        if first in {
            "drivers",
            "fs",
            "net",
            "mm",
            "kernel",
            "crypto",
            "security",
            "sound",
            "arch",
        }:
            tags.add(first)
        if len(parts) > 1 and first == "drivers":
            tags.add(f"drivers/{parts[1]}")
    return sorted(tags)


def _risk_tags(
    symbol: str,
    locations: list[SourceLocation],
    usages: list[SourceUsage],
    bindings: list[KbuildBinding],
) -> list[str]:
    paths = (
        [p.path for p in locations]
        + [u.path for u in usages]
        + [b.path for b in bindings]
    )
    text = " ".join([symbol] + paths).lower()
    tokens = _path_tokens(paths)
    tags: set[str] = set()
    if tokens & {"efi", "initrd", "initramfs", "root", "boot"}:
        tags.add("boot_path")
    if tokens & {"fs", "block", "nvme", "scsi", "ata", "dm"}:
        tags.add("storage")
    if tokens & {"net", "ethernet", "wifi", "wireless"}:
        tags.add("network")
    if tokens & {"security", "crypto", "mitigation", "lockdown"}:
        tags.add("security")
    if "debug" in text or "trace" in text:
        tags.add("debug")
    return sorted(tags)


def _path_tokens(paths: list[str]) -> set[str]:
    tokens: set[str] = set()
    for path in paths:
        for part in Path(path.lower()).parts:
            tokens.update(tok for tok in re.split(r"[^a-z0-9]+", part) if tok)
    return tokens


def _fact_hash(rec: KconfigSymbolRecord) -> str:
    payload = rec.model_dump(
        exclude={
            "fact_hash",
            "source_usages",
        },
        mode="json",
    )
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()[:24]


def _normalize_symbol_filter(symbol_names: Iterable[str] | None) -> set[str] | None:
    if symbol_names is None:
        return None
    out = {name.removeprefix("CONFIG_") for name in symbol_names}
    return out or None


def _normalize_config(symbol: str) -> str:
    return symbol if symbol.startswith("CONFIG_") else f"CONFIG_{symbol}"


def _has_skipped_part(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def _sorted_dict(values: dict[str, list[str]]) -> dict[str, list[str]]:
    return {k: sorted(set(v)) for k, v in values.items()}
