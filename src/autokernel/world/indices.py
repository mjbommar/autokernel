"""Fetch and parse deb-src Sources indices.

The planner needs, per source package: the archive version (what
``apt-get source`` will fetch), the binaries it produces (to map the
world's binary packages back to sources, and build-deps to sources),
and Build-Depends (to order waves).

Fetching is cache-by-URL under the world dir; parsing uses
python-debian's deb822. Tests feed ``parse_sources`` fixture text
directly — no network.
"""

from __future__ import annotations

import gzip
import hashlib
import lzma
import urllib.error
import urllib.request
from pathlib import Path

from debian import deb822
from debian.debian_support import Version
from pydantic import BaseModel, ConfigDict, Field

from autokernel.world.models import BaseRelease


class SourceMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    binaries: list[str] = Field(default_factory=list)
    build_depends: list[str] = Field(default_factory=list)
    """Binary package names from Build-Depends + Build-Depends-Indep,
    stripped of version constraints, alternatives flattened."""


# ── fetch ───────────────────────────────────────────────────────────────────


def _dists(base: BaseRelease) -> list[str]:
    return [base.suite, f"{base.suite}-updates", f"{base.suite}-security"]


def fetch_sources_text(
    base: BaseRelease, cache_dir: Path, *, timeout: int = 60
) -> tuple[str, list[str]]:
    """Concatenated Sources text for all dists × components.

    Returns ``(text, missing_urls)`` — a missing index (404) is
    tolerated and reported, not fatal: e.g. a freshly released suite
    may have an empty -security dist.
    """
    if not base.mirror.startswith(("http://", "https://")):
        raise ValueError(f"mirror must be http(s), got {base.mirror!r}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    missing: list[str] = []
    for dist in _dists(base):
        for comp in base.components:
            stem = f"{base.mirror}/dists/{dist}/{comp}/source/Sources"
            text = None
            for suffix, decomp in ((".xz", lzma.decompress), (".gz", gzip.decompress)):
                url = stem + suffix
                cache_file = cache_dir / (
                    hashlib.sha256(url.encode()).hexdigest()[:16] + suffix
                )
                try:
                    if not cache_file.exists():
                        # Scheme validated above (http/https only).
                        with urllib.request.urlopen(  # nosec B310
                            url, timeout=timeout
                        ) as resp:
                            cache_file.write_bytes(resp.read())
                    text = decomp(cache_file.read_bytes()).decode(
                        "utf-8", errors="replace"
                    )
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code == 404:
                        continue
                    raise
            if text is None:
                missing.append(stem + ".{xz,gz}")
            else:
                chunks.append(text)
    return "\n".join(chunks), missing


# ── parse ───────────────────────────────────────────────────────────────────


def _strip_dep(dep: str) -> str:
    """'debhelper-compat (= 13)' → 'debhelper-compat';
    'a | b' callers split on '|' first; '<!nocheck>' qualifiers dropped."""
    return dep.split("(")[0].split("[")[0].split("<")[0].strip()


def _parse_build_depends(stanza: deb822.Deb822) -> list[str]:
    names: list[str] = []
    for field in ("Build-Depends", "Build-Depends-Indep", "Build-Depends-Arch"):
        raw = stanza.get(field, "")
        for clause in raw.split(","):
            # Alternatives ('a | b') all become edges: any of them being
            # in-closure is a reason to order after it. Cheap and safe —
            # a spurious edge only delays a wave, never breaks a build.
            for alt in clause.split("|"):
                name = _strip_dep(alt)
                if name:
                    names.append(name)
    return sorted(set(names))


def parse_sources(text: str) -> dict[str, SourceMeta]:
    """Sources text → {source name: SourceMeta}, keeping the highest
    version when a source appears in several dists (base vs -updates)."""
    out: dict[str, SourceMeta] = {}
    for stanza in deb822.Sources.iter_paragraphs(text, use_apt_pkg=False):
        name = stanza.get("Package")
        version = stanza.get("Version")
        if not name or not version:
            continue
        if name in out and Version(out[name].version) >= Version(version):
            continue
        binaries = [b.strip() for b in stanza.get("Binary", "").split(",") if b.strip()]
        out[name] = SourceMeta(
            name=name,
            version=version,
            binaries=binaries,
            build_depends=_parse_build_depends(stanza),
        )
    return out


def binary_to_source_map(sources: dict[str, SourceMeta]) -> dict[str, str]:
    return {binary: meta.name for meta in sources.values() for binary in meta.binaries}
