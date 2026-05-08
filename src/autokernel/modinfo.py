"""Parse kernel module metadata: ``modules.builtin.modinfo`` (binary) and
``modinfo --field=filename`` (one shell-out for loadable modules).

The point of this module is to give every observed kernel module a real
filesystem path (``drivers/gpu/drm/i915/i915.ko``) — that path is what lets
:mod:`autokernel.resolve` derive the correct ``CONFIG_*`` symbol via prefix
heuristics, instead of the naive ``CONFIG_<UPPER>`` guess.

Two sources, in order of preference:

1. **modules.builtin.modinfo** — a NUL-separated stream of
   ``<modname>.<key>=<value>`` records. ``.file=`` gives the source path
   relative to the kernel tree (no ``.ko`` extension). Authoritative for
   built-in modules; cheap to parse (~150KB).

2. **modinfo --field=filename <name>** — for each loadable module the kernel
   ships as ``.ko``/``.ko.zst``. We invoke ``modinfo`` once per snapshot
   batch and cache results in the snapshot directory.

Both sources land in a single ``ModuleInfo`` map: ``{module_name: ModuleInfo}``.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ModuleInfo:
    """Where a kernel module lives in the source tree, relative to ``./``.

    ``source_path`` for ``i915`` is ``drivers/gpu/drm/i915/i915`` (no
    extension; the kernel's modinfo system trims it). It's the value of the
    ``.file=`` field for builtins, or the basename-stripped relative path for
    loadable modules.
    """

    name: str
    source_path: str | None = None  # 'drivers/gpu/drm/i915/i915' (no .ko)
    is_builtin: bool = False
    extras: dict[str, list[str]] = field(default_factory=dict)


_BUILTIN_RECORD_RE = re.compile(r"^([\w\-]+)\.([a-zA-Z_]+)=(.*)$", re.DOTALL)


def parse_builtin_modinfo(path: Path | str) -> dict[str, ModuleInfo]:
    """Parse ``/lib/modules/<ver>/modules.builtin.modinfo``.

    Returns ``{module_name: ModuleInfo(is_builtin=True)}``. Modules without
    a ``.file=`` record (rare; some virtual modules) get ``source_path=None``.
    """
    path = Path(path)
    out: dict[str, ModuleInfo] = {}
    if not path.exists():
        return out

    raw = path.read_bytes()
    records = raw.split(b"\0")

    # accumulate per-module {key: [values]}
    per_module: dict[str, dict[str, list[str]]] = {}
    for rec in records:
        if not rec:
            continue
        try:
            text = rec.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            continue
        m = _BUILTIN_RECORD_RE.match(text)
        if not m:
            continue
        modname, key, value = m.group(1), m.group(2), m.group(3)
        # Module names use '-' in source but '_' as the modname prefix.
        # The keys in modules.builtin.modinfo always use the canonical
        # underscored module name.
        per_module.setdefault(modname, {}).setdefault(key, []).append(value)

    for modname, kv in per_module.items():
        files = kv.get("file") or []
        out[modname] = ModuleInfo(
            name=modname,
            source_path=files[0] if files else None,
            is_builtin=True,
            extras={k: v for k, v in kv.items() if k not in {"file"}},
        )
    return out


def query_loadable_modinfo(
    module_names: list[str],
    *,
    timeout: float = 30.0,
) -> dict[str, ModuleInfo]:
    """For each name, run ``modinfo -F filename`` and parse the resulting path.

    The returned ``source_path`` is the path **relative to**
    ``/lib/modules/<ver>/kernel/`` with the ``.ko`` (and optional ``.zst``/
    ``.gz``/``.xz``) extension stripped. So
    ``/lib/modules/6.13.0/kernel/drivers/gpu/drm/i915/i915.ko.zst`` becomes
    ``drivers/gpu/drm/i915/i915``.

    Modules ``modinfo`` reports as ``(builtin)`` are returned with
    ``source_path=None`` and ``is_builtin=True`` — caller should fall back to
    :func:`parse_builtin_modinfo` for those. Unknown modules are omitted.
    """
    if not module_names:
        return {}

    # Single subprocess call: pass all module names at once (modinfo accepts
    # multiple). Output interleaves filenames in argument order.
    try:
        result = subprocess.run(
            ["modinfo", "-F", "filename", *module_names],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}

    lines = result.stdout.splitlines()
    out: dict[str, ModuleInfo] = {}
    # modinfo emits one line per module, in order. On error it writes to
    # stderr but still emits a blank line in stdout for that module — we
    # rely on positional alignment.
    if len(lines) != len(module_names):
        # Best-effort fallback: parse one-by-one. Slower, more reliable.
        return _query_loadable_modinfo_individually(module_names, timeout=timeout)

    for name, line in zip(module_names, lines):
        info = _parse_modinfo_filename_line(name, line)
        if info is not None:
            out[name] = info
    return out


def _query_loadable_modinfo_individually(
    module_names: list[str], *, timeout: float
) -> dict[str, ModuleInfo]:
    out: dict[str, ModuleInfo] = {}
    for name in module_names:
        try:
            r = subprocess.run(
                ["modinfo", "-F", "filename", name],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        line = r.stdout.strip()
        info = _parse_modinfo_filename_line(name, line)
        if info is not None:
            out[name] = info
    return out


_KO_EXT_RE = re.compile(r"\.ko(\.(zst|gz|xz))?$")


def _parse_modinfo_filename_line(name: str, line: str) -> ModuleInfo | None:
    """Convert ``/lib/modules/<ver>/kernel/<rel>.ko[.zst]`` to a ModuleInfo."""
    line = line.strip()
    if not line:
        return None
    if line == "(builtin)":
        return ModuleInfo(name=name, source_path=None, is_builtin=True)

    # Find the 'kernel/' segment and take everything after it.
    parts = line.split("/kernel/", 1)
    if len(parts) != 2:
        return None
    rel = parts[1]
    rel = _KO_EXT_RE.sub("", rel)
    return ModuleInfo(name=name, source_path=rel, is_builtin=False)


def collect_module_info(
    module_names: list[str],
    builtin_modinfo_path: Path | str | None,
    *,
    cache_path: Path | None = None,
) -> dict[str, ModuleInfo]:
    """Combined source-of-truth: builtins from ``modules.builtin.modinfo``,
    everything else via ``modinfo`` shell-out.

    ``cache_path``, when given, is used to memoize the modinfo subprocess
    output across runs (snapshot reuse). Reading the cache is cheap; writing
    is a single dump after we've called modinfo.
    """
    builtin: dict[str, ModuleInfo] = {}
    if builtin_modinfo_path:
        builtin = parse_builtin_modinfo(builtin_modinfo_path)

    needed_loadable = [n for n in module_names if n not in builtin]

    cached: dict[str, ModuleInfo] = {}
    if cache_path and cache_path.exists():
        cached = _read_cache(cache_path)
        needed_loadable = [n for n in needed_loadable if n not in cached]

    fresh = query_loadable_modinfo(needed_loadable) if needed_loadable else {}

    if cache_path is not None and fresh:
        merged = {**cached, **fresh}
        _write_cache(cache_path, merged)
        cached = merged

    return {**builtin, **cached, **fresh}


def _read_cache(path: Path) -> dict[str, ModuleInfo]:
    out: dict[str, ModuleInfo] = {}
    try:
        for line in path.read_text().splitlines():
            if "\t" not in line:
                continue
            name, rest = line.split("\t", 1)
            sp, _, builtin_flag = rest.partition("\t")
            out[name] = ModuleInfo(
                name=name,
                source_path=sp or None,
                is_builtin=builtin_flag == "1",
            )
    except OSError:
        pass
    return out


def _write_cache(path: Path, data: dict[str, ModuleInfo]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{i.name}\t{i.source_path or ''}\t{'1' if i.is_builtin else '0'}"
        for i in data.values()
    ]
    path.write_text("\n".join(lines) + "\n")
