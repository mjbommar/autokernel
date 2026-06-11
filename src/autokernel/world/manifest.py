"""World manifest: init from dpkg state, axes→flags mapping, load/save.

``world init`` is deterministic (no LLM): it captures the installed
package set at the requested ring, derives GlobalFlags from the
aggression/threat axes per the docs/WORLD.md table, and applies the
preset toolchain gate. ``world propose`` (W6) is where the LLM edits
this skeleton.
"""

from __future__ import annotations

import json
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from autokernel.optimize_context import Aggression, ThreatModel
from autokernel.world.models import (
    BaseRelease,
    GlobalFlags,
    HardeningTier,
    Lto,
    Ring,
    WorldEntry,
    WorldManifest,
    toolchain_gate_overrides,
)

_RING_PRIORITIES: dict[Ring, set[str] | None] = {
    Ring.REQUIRED: {"required"},
    Ring.IMPORTANT: {"required", "important"},
    Ring.EVERYTHING: None,  # no filter
}


# ── axes → flags (docs/WORLD.md table) ──────────────────────────────────────


def flags_for_axes(
    aggression: Aggression, threat: ThreatModel, *, compiler: str = "gcc"
) -> GlobalFlags:
    if aggression == Aggression.CONSERVATIVE:
        march, opt, lto = "x86-64-v3", "-O2", Lto.NONE
        build_options: list[str] = []
        build_profiles: list[str] = []
    elif aggression == Aggression.AGGRESSIVE:
        march, opt, lto = "native", "-O3", Lto.AUTO
        build_options = ["nocheck", "nodoc"]
        build_profiles = ["nocheck", "nodoc"]
    else:  # BALANCED
        march, opt, lto = "native", "-O2", Lto.NONE
        build_options = []
        build_profiles = []

    if threat == ThreatModel.PERMISSIVE:
        hardening = HardeningTier.DISTRO_DEFAULT
    elif threat == ThreatModel.PARANOID:
        hardening = HardeningTier.PARANOID
    else:
        hardening = HardeningTier.FORTIFY_PLUS

    return GlobalFlags(
        march=march,
        opt=opt,
        lto=lto,
        compiler=compiler,
        hardening=hardening,
        build_options=build_options,
        build_profiles=build_profiles,
    )


# ── host probes ─────────────────────────────────────────────────────────────


def detect_base() -> BaseRelease:
    """BaseRelease from /etc/os-release. Mirrors match the W0 spike."""
    info: dict[str, str] = {}
    text = Path("/etc/os-release").read_text(encoding="utf-8")
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            info[k] = v.strip().strip('"')
    distro_id = info.get("ID", "")
    suite = info.get("VERSION_CODENAME", "")
    if distro_id == "ubuntu":
        return BaseRelease(
            distro_id=distro_id,
            suite=suite,
            mirror="http://archive.ubuntu.com/ubuntu",
            components=["main", "universe"],
        )
    if distro_id == "debian":
        return BaseRelease(
            distro_id=distro_id,
            suite=suite,
            mirror="http://deb.debian.org/debian",
            components=["main"],
        )
    raise RuntimeError(
        f"unsupported distro id {distro_id!r} for `world` (need ubuntu or debian)"
    )


_DPKG_QUERY_FMT = (
    "${Package}\t${Priority}\t${source:Package}\t${source:Version}"
    "\t${Installed-Size}\t${db:Status-Status}\n"
)


def installed_entries() -> list[WorldEntry]:
    """Every installed binary package as a WorldEntry, via dpkg-query."""
    result = subprocess.run(
        ["dpkg-query", "-W", f"-f={_DPKG_QUERY_FMT}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    entries: list[WorldEntry] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        binary, priority, source, source_version, size, status = parts
        if status != "installed":
            continue
        try:
            kb = int(size)
        except ValueError:
            kb = 0
        entries.append(
            WorldEntry(
                binary=binary,
                source=source or binary,
                source_version=source_version,
                priority=priority or "optional",
                installed_kb=kb,
            )
        )
    return entries


def filter_ring(entries: list[WorldEntry], ring: Ring) -> list[WorldEntry]:
    wanted = _RING_PRIORITIES[ring]
    if wanted is None:
        return sorted(entries, key=lambda e: e.binary)
    return sorted((e for e in entries if e.priority in wanted), key=lambda e: e.binary)


# ── init / load / save ──────────────────────────────────────────────────────


def init_manifest(
    *,
    ring: Ring,
    aggression: Aggression,
    threat: ThreatModel,
    compiler: str = "gcc",
    base: BaseRelease | None = None,
    entries: list[WorldEntry] | None = None,
    host: str | None = None,
) -> WorldManifest:
    base = base or detect_base()
    entries = entries if entries is not None else installed_entries()
    world = filter_ring(entries, ring)
    flags = flags_for_axes(aggression, threat, compiler=compiler)
    if compiler == "clang":
        # clang-world default (Phase 0): bfd+LLVMgold preserves .symver
        # versioned symbols under ThinLTO where lld hard-fails. The
        # masquerade (gcc→clang) is NOT default: it breaks build-time gcc
        # helpers in important packages (systemd's BPF, libselinux's
        # -aux-info — Phase 1.9). CC=clang + the majority identity audit
        # let those stay clang while hardcoded-gcc packages become honest
        # force-gcc. Opt in with masquerade=True for maximal force-clang.
        flags = flags.model_copy(update={"linker": "bfd"})
    sources = sorted({e.source for e in world})
    return WorldManifest(
        created_at=datetime.now(UTC),
        host=host or platform.node(),
        base=base,
        ring=ring,
        axes={"aggression": aggression.value, "threat": threat.value},
        flags=flags,
        world=world,
        overrides=toolchain_gate_overrides(sources),
    )


def save_manifest(manifest: WorldManifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")


def load_manifest(path: Path) -> WorldManifest:
    return WorldManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def default_world_dir() -> Path:
    return Path.home() / ".local" / "share" / "autokernel" / "world" / platform.node()
