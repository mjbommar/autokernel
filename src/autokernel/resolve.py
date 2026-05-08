"""Deterministic resolution: hardware evidence → required kernel modules → CONFIG_* symbols.

Pipeline::

    Snapshot.modaliases  ──┐
    Snapshot.loaded_modules├──>  required_modules ──┐
    Snapshot.bound_drivers ┘                        ├──> required_configs
                            modules.builtin.modinfo │
                            + modinfo --filename ───┘
                                + kconfig_map.resolve_module_to_config
                                  (path-aware candidate generator)

This is the *deterministic* half of the system. No LLM. The output is the
set of symbols we are confident MUST stay enabled. Everything not in the
required set is a candidate for trimming — that's where the LLM agent
earns its keep.

The module → CONFIG_* mapping is the trickiest correctness lever in the
project. For every observed module we:

1. Look up the module's source path (``drivers/gpu/drm/i915/i915``) from
   :mod:`autokernel.modinfo`. Sources, in order: ``modules.builtin.modinfo``
   for built-in modules, then ``modinfo --field=filename`` for loadable.
2. Generate ordered candidate ``CONFIG_*`` symbols using
   :mod:`autokernel.kconfig_map`'s subsystem-aware prefix table.
3. Pick the first candidate that exists in the running ``.config`` with
   value ``=y`` or ``=m``.
4. If nothing matches, record the module as **unresolved**. The caller
   (typically :mod:`autokernel.policy`) treats unresolved-but-required
   modules as load-bearing for safety.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

from autokernel.kconfig_map import resolve_module_to_config
from autokernel.modinfo import collect_module_info
from autokernel.models import Snapshot


@dataclass
class ResolutionResult:
    """The deterministic-keep set."""

    required_modules: set[str] = field(default_factory=set)
    required_configs: set[str] = field(default_factory=set)
    # module name → list of evidence strings (which device/snapshot field caused us to keep it)
    evidence: dict[str, list[str]] = field(default_factory=dict)
    # module name → resolved CONFIG_* symbol (or None if unresolved)
    module_to_config: dict[str, str | None] = field(default_factory=dict)
    unresolved_modules: set[str] = field(default_factory=set)
    unresolved_modaliases: list[str] = field(default_factory=list)


# ── modules.alias parsing ────────────────────────────────────────────────────
# Lines look like:  alias <pattern>  <module>
# e.g.  alias pci:v00008086d000056A6sv*sd*bc*sc*i*  i915

_ALIAS_RE = re.compile(r"^alias\s+(\S+)\s+(\S+)\s*$")


def _load_modules_alias(path: Path) -> dict[str, list[tuple[str, str]]]:
    """Load modules.alias bucketed by bus prefix.

    The alias file has ~37K entries on a stock Ubuntu kernel. Naive
    cross-product matching against snapshot modaliases is O(N*M) and quadratic-
    looking in practice. Bucketing by leading ``bus:`` token (``pci:``,
    ``usb:``, ``acpi:``, …) cuts the work ~10x and keeps lookups linear in
    the size of the relevant bucket only.
    """
    buckets: dict[str, list[tuple[str, str]]] = {}
    try:
        with path.open() as f:
            for line in f:
                m = _ALIAS_RE.match(line)
                if not m:
                    continue
                pattern, mod = m.group(1), m.group(2)
                bus = pattern.split(":", 1)[0] if ":" in pattern else pattern
                buckets.setdefault(bus, []).append((pattern, mod))
    except OSError:
        return {}
    return buckets


def _resolve_modalias(raw: str, buckets: dict[str, list[tuple[str, str]]]) -> set[str]:
    """Match a modalias against the bucketed alias table. Aliases use shell
    glob syntax (fnmatch)."""
    bus = raw.split(":", 1)[0] if ":" in raw else raw
    bucket = buckets.get(bus, [])
    return {mod for pattern, mod in bucket if fnmatch.fnmatchcase(raw, pattern)}


# Static map for cross-cutting symbols that aren't 1:1 with module names
# (filesystems are tied to mount evidence, not loaded modules).

_FS_TO_CONFIG: dict[str, list[str]] = {
    "ext4": ["CONFIG_EXT4_FS"],
    "ext3": ["CONFIG_EXT4_FS"],  # ext3 served by ext4 driver in modern kernels
    "ext2": ["CONFIG_EXT2_FS", "CONFIG_EXT4_FS"],
    "xfs": ["CONFIG_XFS_FS"],
    "btrfs": ["CONFIG_BTRFS_FS"],
    "f2fs": ["CONFIG_F2FS_FS"],
    "vfat": ["CONFIG_VFAT_FS", "CONFIG_FAT_FS"],
    "fat": ["CONFIG_FAT_FS"],
    "msdos": ["CONFIG_MSDOS_FS", "CONFIG_FAT_FS"],
    "exfat": ["CONFIG_EXFAT_FS"],
    "ntfs3": ["CONFIG_NTFS3_FS"],
    # NTFS_FS is the legacy read-only driver, slated for removal. ntfs3 supersedes.
    "ntfs": ["CONFIG_NTFS3_FS", "CONFIG_NTFS_FS"],
    "iso9660": ["CONFIG_ISO9660_FS"],
    "udf": ["CONFIG_UDF_FS"],
    "squashfs": ["CONFIG_SQUASHFS"],
    "overlay": ["CONFIG_OVERLAY_FS"],
    "tmpfs": ["CONFIG_TMPFS"],
    "proc": ["CONFIG_PROC_FS"],
    "sysfs": ["CONFIG_SYSFS"],
    "devtmpfs": ["CONFIG_DEVTMPFS"],
    "cgroup2": ["CONFIG_CGROUPS"],
    "cgroup": ["CONFIG_CGROUPS"],
    "fuse": ["CONFIG_FUSE_FS"],
    "bpf": ["CONFIG_BPF_SYSCALL"],
    "binfmt_misc": ["CONFIG_BINFMT_MISC"],
    "autofs": ["CONFIG_AUTOFS_FS"],
    "nfs": ["CONFIG_NFS_FS"],
    "nfs4": ["CONFIG_NFS_V4"],
    "cifs": ["CONFIG_CIFS"],
    "smb3": ["CONFIG_CIFS"],
    "ceph": ["CONFIG_CEPH_FS"],
    "9p": ["CONFIG_9P_FS", "CONFIG_NET_9P"],
    "zfs": ["CONFIG_ZFS"],
}


def _running_config_symbols(running_config_path: Path | None) -> dict[str, str]:
    """Parse a .config file → {symbol: value} where value ∈ {'y','m','n', string}."""
    if not running_config_path or not running_config_path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        with running_config_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("# CONFIG_") and line.endswith("is not set"):
                    sym = line.split()[1]
                    out[sym] = "n"
                elif line.startswith("CONFIG_") and "=" in line:
                    sym, _, val = line.partition("=")
                    out[sym] = val.strip().strip('"')
    except OSError:
        pass
    return out


# ── main resolver ────────────────────────────────────────────────────────────


def resolve(snap: Snapshot) -> ResolutionResult:
    """Build the deterministic keep-set from a Snapshot."""
    result = ResolutionResult()

    # 1. Modules currently loaded — keep them all
    for mod in snap.loaded_modules:
        result.required_modules.add(mod.name)
        result.evidence.setdefault(mod.name, []).append("loaded (lsmod)")

    # 1b. Modules in the initramfs — load-bearing for early boot regardless
    # of whether they are loaded right now (the initramfs may load + unload
    # them, e.g. resume-from-disk drivers).
    for mod in snap.initramfs_modules:
        canonical = mod.replace("-", "_")
        result.required_modules.add(canonical)
        result.evidence.setdefault(canonical, []).append("in initramfs")

    # 2. Drivers bound right now in /sys/bus/*/drivers/*
    # PnP drivers expose multi-word names like "i8042 kbd" / "i8042 aux" — the
    # kernel disambiguates roles by suffix but the underlying module is the
    # first token. Normalise so we feed module names downstream.
    for sysfs_path, driver in snap.bound_drivers.items():
        if not driver or driver in {"unbound", "(null)"}:
            continue
        canonical = driver.split()[0]
        result.required_modules.add(canonical)
        result.evidence.setdefault(canonical, []).append(f"bound at {sysfs_path}")

    # 3. PCI device modules (lspci -k says "Modules: foo, bar" for available drivers)
    for pci in snap.pci:
        if pci.driver:
            result.required_modules.add(pci.driver)
            result.evidence.setdefault(pci.driver, []).append(
                f"pci driver for {pci.slot} ({pci.description or pci.vendor_id})"
            )
        for mod in pci.modules:
            result.required_modules.add(mod)
            result.evidence.setdefault(mod, []).append(
                f"pci candidate module for {pci.slot}"
            )

    # 4. Modaliases → modules via modules.alias
    if snap.modules_alias_path:
        alias_table = _load_modules_alias(snap.modules_alias_path)
        for ma in snap.modaliases:
            mods = _resolve_modalias(ma.raw, alias_table)
            if not mods:
                result.unresolved_modaliases.append(ma.raw)
                continue
            for m in mods:
                result.required_modules.add(m)
                result.evidence.setdefault(m, []).append(f"modalias {ma.raw[:60]}")

    # 5. Filesystems in active use
    for mount in snap.mounts:
        for cfg in _FS_TO_CONFIG.get(mount.fstype, []):
            result.required_configs.add(cfg)
            result.evidence.setdefault(f"<config>{cfg}", []).append(
                f"mount {mount.target} ({mount.fstype})"
            )

    # 6. Active network interfaces — driver names go through the mapper below.

    # 7. Firmware blobs in use — keep CONFIG_FW_LOADER untouched if any
    if snap.firmware:
        result.required_configs.update({"CONFIG_FW_LOADER", "CONFIG_EXTRA_FIRMWARE"})

    # 8. Boot context essentials
    if snap.boot.efi:
        result.required_configs.update(
            {"CONFIG_EFI", "CONFIG_EFI_STUB", "CONFIG_EFIVAR_FS", "CONFIG_FB_EFI"}
        )
    if snap.boot.luks_in_chain:
        result.required_configs.update(
            {
                "CONFIG_DM_CRYPT",
                "CONFIG_CRYPTO_AES",
                "CONFIG_CRYPTO_XTS",
                "CONFIG_CRYPTO_USER_API_SKCIPHER",
            }
        )

    # 9. CPU vendor-specific
    if snap.cpu.vendor_id == "GenuineIntel":
        result.required_configs.add("CONFIG_INTEL_IDLE")
        result.required_configs.add("CONFIG_X86_INTEL_PSTATE")
        result.required_configs.add("CONFIG_MICROCODE_INTEL")
    elif snap.cpu.vendor_id == "AuthenticAMD":
        result.required_configs.add("CONFIG_X86_AMD_PSTATE")
        result.required_configs.add("CONFIG_MICROCODE_AMD")

    # 9b. Cmdline-named filesystems / drivers
    rootfs = snap.boot.cmdline_params.get("rootfstype")
    if rootfs:
        for cfg in _FS_TO_CONFIG.get(rootfs, []):
            result.required_configs.add(cfg)
            result.evidence.setdefault(f"<config>{cfg}", []).append(
                f"cmdline rootfstype={rootfs}"
            )

    # 9c. Cmdline-blacklisted modules are explicitly NOT load-bearing — drop
    # them from required_modules so the LLM/policy can propose removing them.
    for mod in snap.boot.blacklisted_modules:
        canonical = mod.replace("-", "_")
        result.required_modules.discard(canonical)
        result.evidence.setdefault(canonical, []).append(
            "cmdline module_blacklist (NOT required)"
        )

    # 10. Translate every required module to a CONFIG_ symbol via the
    # path-aware resolver. Falls back to "unresolved" — caller treats those
    # as load-bearing for safety.
    running = _running_config_symbols(snap.running_config_path)

    cache_path = (
        snap.snapshot_dir / "loadable_modinfo_cache" if snap.snapshot_dir else None
    )
    module_info_map = collect_module_info(
        sorted(result.required_modules),
        snap.modules_builtin_modinfo_path,
        cache_path=cache_path,
    )

    for mod in result.required_modules:
        info = module_info_map.get(mod)
        source_path = info.source_path if info else None
        cfg = resolve_module_to_config(mod, source_path, running)
        result.module_to_config[mod] = cfg
        if cfg is not None:
            result.required_configs.add(cfg)
            result.evidence.setdefault(f"<config>{cfg}", []).append(f"module {mod}")
        else:
            result.unresolved_modules.add(mod)

    return result


# ── reverse resolver: candidate trims ────────────────────────────────────────


def candidate_trims(snap: Snapshot, resolution: ResolutionResult) -> list[str]:
    """Symbols enabled in the running config that are NOT in the required set.

    These are *candidates* for trimming — never auto-trimmed without further
    review (policy filter + LLM advice + load-bearing blocklist).
    """
    running = _running_config_symbols(snap.running_config_path)
    return sorted(
        sym
        for sym, val in running.items()
        if val in ("y", "m") and sym not in resolution.required_configs
    )
