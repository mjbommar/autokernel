"""Parse the directory of files produced by ``scripts/collect.sh`` into a
typed :class:`autokernel.models.Snapshot`.

Each collector output file in ``$OUTDIR`` is parsed independently and the
results are merged. Missing/failed files degrade gracefully — never raise
unless the snapshot is fundamentally unreadable (no kernel info).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from autokernel.models import (
    BlockDevice,
    BootContext,
    CpuInfo,
    DkmsModule,
    FirmwareLoad,
    KernelInfo,
    LoadedModule,
    Modalias,
    Mount,
    NetworkLink,
    PciDevice,
    Snapshot,
    UsbDevice,
)


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except FileNotFoundError:
        return ""


def _read_lines(path: Path) -> list[str]:
    text = _read(path)
    return [ln for ln in text.splitlines() if ln.strip()]


def _read_json(path: Path) -> dict | list | None:
    text = _read(path)
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _parse_manifest(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _read_lines(path):
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _parse_cpuinfo(path: Path) -> CpuInfo:
    blocks = _read(path).split("\n\n")
    if not blocks or not blocks[0].strip():
        return CpuInfo(vendor_id="unknown")
    fields: dict[str, str] = {}
    for line in blocks[0].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    cores = sum(1 for b in blocks if b.strip().startswith("processor"))
    flags = fields.get("flags", "").split()
    return CpuInfo(
        vendor_id=fields.get("vendor_id", "unknown"),
        cpu_family=int(fields["cpu family"])
        if fields.get("cpu family", "").isdigit()
        else None,
        model=int(fields["model"]) if fields.get("model", "").isdigit() else None,
        model_name=fields.get("model name"),
        flags=flags,
        cores=cores or 1,
    )


_LSPCI_BLOCK_RE = re.compile(r"^([A-Z][A-Za-z]+):\s*(.*)$")


def _parse_lspci_vmmnk(path: Path) -> list[PciDevice]:
    text = _read(path)
    devices: list[PciDevice] = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        modules: list[str] = []
        for line in block.splitlines():
            m = _LSPCI_BLOCK_RE.match(line)
            if not m:
                continue
            key, val = m.group(1), m.group(2)
            if key == "Module":
                modules.append(val.strip())
            else:
                fields[key] = val.strip()
        if "Slot" not in fields or "Vendor" not in fields:
            continue
        devices.append(
            PciDevice(
                slot=fields["Slot"],
                vendor_id=fields["Vendor"],
                device_id=fields.get("Device", ""),
                class_id=fields.get("Class"),
                subsystem_vendor=fields.get("SVendor"),
                subsystem_device=fields.get("SDevice"),
                driver=fields.get("Driver"),
                modules=modules,
                description=fields.get("Device"),
            )
        )
    return devices


_LSUSB_RE = re.compile(
    r"^Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-f]{4}):([0-9a-f]{4})\s*(.*)$"
)


def _parse_lsusb(path: Path) -> list[UsbDevice]:
    out: list[UsbDevice] = []
    for line in _read_lines(path):
        m = _LSUSB_RE.match(line)
        if m:
            out.append(
                UsbDevice(
                    bus=m.group(1),
                    device=m.group(2),
                    vendor_id=m.group(3),
                    product_id=m.group(4),
                    description=m.group(5).strip() or None,
                )
            )
    return out


def _parse_sys_modaliases(path: Path) -> list[Modalias]:
    out: list[Modalias] = []
    for line in _read_lines(path):
        if "\t" not in line:
            continue
        sysfs_path, raw = line.split("\t", 1)
        bus = raw.split(":", 1)[0] if ":" in raw else "unknown"
        out.append(Modalias(sysfs_path=sysfs_path, raw=raw, bus=bus))
    return out


def _parse_bound_drivers(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _read_lines(path):
        if "\t" in line:
            dev, drv = line.split("\t", 1)
            out[dev] = drv
    return out


def _parse_lsmod(path: Path) -> list[LoadedModule]:
    out: list[LoadedModule] = []
    lines = _read_lines(path)
    if lines and lines[0].lower().startswith("module"):
        lines = lines[1:]
    for line in lines:
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        name, size_s, used_s = parts[0], parts[1], parts[2]
        used_by_list: list[str] = []
        if len(parts) >= 4 and parts[3].strip() != "-":
            used_by_list = [u for u in parts[3].split(",") if u]
        try:
            size = int(size_s)
            used_by = int(used_s)
        except ValueError:
            continue
        out.append(
            LoadedModule(
                name=name, size=size, used_by_count=used_by, used_by=used_by_list
            )
        )
    return out


def _parse_mounts(path: Path) -> list[Mount]:
    out: list[Mount] = []
    for line in _read_lines(path):
        parts = line.split()
        if len(parts) >= 4:
            out.append(
                Mount(
                    source=parts[0], target=parts[1], fstype=parts[2], options=parts[3]
                )
            )
    return out


def _parse_lsblk(path: Path) -> list[BlockDevice]:
    data = _read_json(path)
    if not isinstance(data, dict):
        return []
    out: list[BlockDevice] = []

    def walk(nodes: list) -> None:
        for n in nodes:
            if isinstance(n, dict):
                out.append(
                    BlockDevice(
                        name=n.get("name", ""),
                        fstype=n.get("fstype"),
                        type=n.get("type"),
                    )
                )
                if isinstance(n.get("children"), list):
                    walk(n["children"])

    walk(data.get("blockdevices", []) or [])
    return out


def _parse_ip_link(path: Path) -> list[NetworkLink]:
    data = _read_json(path)
    if not isinstance(data, list):
        return []
    out: list[NetworkLink] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("ifname") or entry.get("ifindex", "")
        operstate = entry.get("operstate")
        link_info = entry.get("linkinfo", {})
        driver = link_info.get("info_kind") if isinstance(link_info, dict) else None
        out.append(
            NetworkLink(
                name=str(name),
                driver=driver,
                operstate=operstate,
                is_active=operstate == "UP",
            )
        )
    return out


def _parse_dkms(path: Path) -> list[DkmsModule]:
    out: list[DkmsModule] = []
    # `dkms status` format: "name/version, kernel, arch: status" (varies by version)
    for line in _read_lines(path):
        m = re.match(r"^([^/,]+)[/,]\s*([^,]+),\s*([^,]+),.*?:\s*(.+)$", line)
        if m:
            out.append(
                DkmsModule(
                    name=m.group(1).strip(),
                    version=m.group(2).strip(),
                    kernel=m.group(3).strip(),
                    status=m.group(4).strip(),
                )
            )
    return out


def _parse_firmware(snapdir: Path) -> list[FirmwareLoad]:
    out: list[FirmwareLoad] = []
    seen: set[str] = set()

    # Source 1: dmesg / journalctl scrape (may be empty under dmesg_restrict).
    for src_name, src_label in [
        ("dmesg_firmware", "dmesg"),
        ("journal_firmware", "journal"),
    ]:
        text = _read(snapdir / src_name)
        for m in re.finditer(r"([\w./+-]+\.(?:bin|fw|fwz|cis|dat|sbin|ucode))", text):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            out.append(FirmwareLoad(name=name, source=src_label))

    # Source 2: modinfo's declared firmware: per loaded module — authoritative
    # and survives dmesg restrictions.
    text = _read(snapdir / "module_firmware")
    for line in text.splitlines():
        if "\t" not in line:
            continue
        _, fw = line.split("\t", 1)
        fw = fw.strip()
        if fw and fw not in seen:
            seen.add(fw)
            out.append(FirmwareLoad(name=fw, source="modinfo"))

    return out


def _parse_cmdline(raw: str) -> tuple[dict[str, str], list[str]]:
    """Tokenize /proc/cmdline into (key=value dict, blacklisted_modules list).

    Tokens are whitespace-separated. ``foo=bar`` becomes ``params['foo'] = 'bar'``;
    bare flags become ``params[flag] = ''``. ``module_blacklist=a,b,c`` is
    extracted into the dedicated list.

    Quoted values (``init="x y z"``) are not handled — the kernel doesn't
    accept quotes in the bootloader-passed cmdline anyway.
    """
    params: dict[str, str] = {}
    for tok in raw.split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            params[k] = v
        else:
            params[tok] = ""
    blacklist_raw = params.get("module_blacklist", "") or params.get("blacklist", "")
    blacklist = [m.strip() for m in blacklist_raw.split(",") if m.strip()]
    return params, blacklist


def _detect_boot_context(snapdir: Path, mounts: list[Mount]) -> BootContext:
    cmdline = _read(snapdir / "cmdline").strip()
    cmdline_params, blacklisted = _parse_cmdline(cmdline)

    efi_rc = (
        (snapdir / "efi_present.rc").read_text().strip()
        if (snapdir / "efi_present.rc").exists()
        else "1"
    )
    efi = efi_rc == "0"

    sb_text = _read(snapdir / "secureboot")
    if "SecureBoot enabled" in sb_text:
        sb: bool | None = True
    elif "SecureBoot disabled" in sb_text:
        sb = False
    else:
        sb = None

    root_fs = next((m.fstype for m in mounts if m.target == "/"), None)
    boot_fs = next((m.fstype for m in mounts if m.target == "/boot"), None)

    # cmdline corroborates: cryptdevice= or rd.luks= confirms LUKS even if
    # cryptsetup wasn't reachable for our scan. /proc/mounts /dev/mapper/*
    # also confirms it.
    crypt_status = _read(snapdir / "crypt_status")
    luks = (
        bool(crypt_status.strip())
        or "cryptdevice" in cmdline_params
        or any(k.startswith("rd.luks") for k in cmdline_params)
    )

    return BootContext(
        cmdline=cmdline,
        cmdline_params=cmdline_params,
        blacklisted_modules=blacklisted,
        secure_boot=sb,
        efi=efi,
        root_fstype=root_fs or cmdline_params.get("rootfstype"),
        boot_fstype=boot_fs,
        luks_in_chain=luks,
    )


def _parse_initramfs_modules(snapdir: Path) -> list[str]:
    text = _read(snapdir / "initramfs_modules")
    return sorted({line.strip() for line in text.splitlines() if line.strip()})


def _parse_initramfs_firmware(snapdir: Path) -> list[str]:
    text = _read(snapdir / "initramfs_firmware")
    return sorted({line.strip() for line in text.splitlines() if line.strip()})


def load(snapshot_dir: Path | str) -> Snapshot:
    snapdir = Path(snapshot_dir).resolve()
    if not snapdir.is_dir():
        raise FileNotFoundError(f"snapshot dir not found: {snapdir}")

    manifest = _parse_manifest(snapdir / "manifest")
    collected_at = manifest.get("collected_at")
    try:
        ts = (
            datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
            if collected_at
            else datetime.now(UTC)
        )
    except (ValueError, AttributeError):
        ts = datetime.now(UTC)

    uname = _read(snapdir / "uname").strip()
    arch = uname.split()[-2] if len(uname.split()) >= 2 else "unknown"
    kernel = KernelInfo(
        release=_read(snapdir / "kernel_release").strip() or "unknown",
        version=_read(snapdir / "kernel_version").strip(),
        arch=arch,
    )

    mounts = _parse_mounts(snapdir / "mounts")

    running_cfg = snapdir / "running_config"
    running_cfg_path = (
        running_cfg if running_cfg.exists() and running_cfg.stat().st_size > 0 else None
    )

    def _modpath(key: str) -> Path | None:
        p = _read(snapdir / key).strip()
        return Path(p) if p and Path(p).exists() else None

    return Snapshot(
        collected_at=ts,
        host=manifest.get("host", "unknown"),
        snapshot_dir=snapdir,
        kernel=kernel,
        cpu=_parse_cpuinfo(snapdir / "cpuinfo"),
        boot=_detect_boot_context(snapdir, mounts),
        pci=_parse_lspci_vmmnk(snapdir / "lspci_vmmnk"),
        usb=_parse_lsusb(snapdir / "lsusb"),
        modaliases=_parse_sys_modaliases(snapdir / "sys_modaliases"),
        bound_drivers=_parse_bound_drivers(snapdir / "sys_bound_drivers"),
        loaded_modules=_parse_lsmod(snapdir / "lsmod"),
        mounts=mounts,
        block_devices=_parse_lsblk(snapdir / "lsblk_j"),
        network=_parse_ip_link(snapdir / "ip_link_j"),
        firmware=_parse_firmware(snapdir),
        dkms=_parse_dkms(snapdir / "dkms_status"),
        initramfs_modules=_parse_initramfs_modules(snapdir),
        initramfs_firmware=_parse_initramfs_firmware(snapdir),
        running_config_path=running_cfg_path,
        modules_alias_path=_modpath("modules_alias_path"),
        modules_dep_path=_modpath("modules_dep_path"),
        modules_builtin_path=_modpath("modules_builtin_path"),
        modules_builtin_modinfo_path=_modpath("modules_builtin_modinfo_path"),
    )
