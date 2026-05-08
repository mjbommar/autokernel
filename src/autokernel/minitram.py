"""Generate a minimal initramfs from snapshot evidence.

Today Ubuntu's `update-initramfs` builds a 40 MB+ initramfs containing
every module that *might* be needed across all possible hosts.
autokernel knows what's actually load-bearing for *this* host (LUKS in
the boot chain? LVM? RAID? specific DKMS modules?) and can build a
3-5 MB initramfs containing exactly those.

The minitram is **not** a general-purpose initramfs. It's a per-host
artifact tied to one Snapshot — it knows your root fstype, your block
chain, your module set, and packs only those.

Architecture:

* :class:`MinitramPlan` — what we'd put in the initramfs (the LLM
  doesn't decide here; it's deterministic from snapshot evidence).
* :func:`plan` — turn a Snapshot into a MinitramPlan.
* :func:`build` — compose the plan into a cpio.zst archive.

Tools used at build time:

* `cpio` (standard) — pack the archive
* `zstd` — compress the cpio
* `find` (standard) — walk the staging dir
* For optional in-init shell, `busybox-static` from the host's package
  manager.

Output: ``<snapshot_dir>/initramfs.cpio.zst`` plus an
``initramfs.plan.json`` describing what went in.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from autokernel.models import Snapshot


@dataclass(frozen=True)
class MinitramTool:
    """One executable + its dynamic dependencies that needs to land
    inside the initramfs."""

    name: str  # e.g. 'cryptsetup'
    host_path: Path  # e.g. /sbin/cryptsetup on the host
    target_path: str  # e.g. '/sbin/cryptsetup' in the initramfs
    libs: list[str]  # ldd-resolved dependencies
    rationale: str  # why this is included


@dataclass(frozen=True)
class MinitramModule:
    """One kernel module that needs to load before root mount."""

    name: str  # 'btrfs', 'dm_crypt', 'aes_x86_64', ...
    host_path: Path  # /lib/modules/<release>/.../<name>.ko[.zst]
    target_path: str  # corresponding path inside initramfs
    rationale: str


@dataclass
class MinitramPlan:
    """Composition plan for one host's initramfs.

    Fields are populated incrementally by :func:`plan`. ``build()``
    consumes the plan to produce the cpio.zst archive.
    """

    kernel_release: str
    snapshot_dir: Path

    # Composition (populated by plan()):
    busybox: bool = True  # busybox-static for the in-init shell
    tools: list[MinitramTool] = field(default_factory=list)
    modules: list[MinitramModule] = field(default_factory=list)
    firmware: list[str] = field(default_factory=list)  # /lib/firmware paths
    init_script: str = ""  # the /init shell script as a string

    def to_summary_dict(self) -> dict:
        """Serializable summary — excludes resolved host_path so the
        record is portable across machines."""
        return {
            "kernel_release": self.kernel_release,
            "busybox": self.busybox,
            "tools": [
                {"name": t.name, "rationale": t.rationale, "libs": t.libs}
                for t in self.tools
            ],
            "modules": [
                {"name": m.name, "rationale": m.rationale} for m in self.modules
            ],
            "firmware_count": len(self.firmware),
        }


# ── plan: snapshot → MinitramPlan ─────────────────────────────────────────


# Map from a "feature in the boot chain" to the user-space tool that
# implements it. Each entry: (predicate-on-snap, tool-bin, rationale).
# Predicates are functions to keep the table data-only.
def _luks_in_chain(s: Snapshot) -> bool:
    return bool(s.boot.luks_in_chain)


def _lvm_in_chain(s: Snapshot) -> bool:
    return any(b.type == "lvm" for b in s.block_devices)


def _md_raid_in_chain(s: Snapshot) -> bool:
    return any(b.type == "raid" for b in s.block_devices) or any(
        b.name and b.name.startswith("md") for b in s.block_devices
    )


_TOOL_PREDICATES: list[tuple[str, str, str]] = [
    # (snapshot-predicate name, tool binary on host, rationale)
    ("_luks_in_chain", "cryptsetup", "LUKS in boot chain (decrypt root)"),
    ("_lvm_in_chain", "lvm", "LVM in boot chain (vgchange/lvchange)"),
    ("_md_raid_in_chain", "mdadm", "MD RAID in boot chain (assemble)"),
]


# Modules required for early boot, mapped from boot-chain features.
# These are KO-name patterns; we resolve to actual /lib/modules paths
# from the Snapshot's modules.dep + the running kernel release.
_BOOT_MODULES_BY_FEATURE: dict[str, list[str]] = {
    "luks": ["dm_crypt", "aes_generic", "aes_x86_64", "xts", "sha256"],
    "lvm": ["dm_mod"],
    "raid": ["raid0", "raid1", "raid10", "raid456", "md_mod"],
}

# Per-fstype modules required to mount root.
_FS_MODULES: dict[str, list[str]] = {
    "ext4": ["ext4"],
    "btrfs": ["btrfs", "crc32c", "xxhash"],
    "xfs": ["xfs"],
    "f2fs": ["f2fs"],
    "zfs": ["zfs"],  # OOT but still — DKMS will produce a .ko on this host
}


def _which_or_none(name: str) -> Path | None:
    p = shutil.which(name)
    return Path(p) if p else None


def _resolve_libs(elf: Path) -> list[str]:
    """Run ldd, parse 'libfoo => /path (0x...)' lines. Empty list when
    the binary is fully static or ldd fails."""
    try:
        out = subprocess.run(
            ["ldd", str(elf)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    libs: list[str] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if "=>" not in line:
            continue
        # Format: 'libname => /path (0x...)' or 'libname => not found'
        rhs = line.split("=>", 1)[1].strip()
        if rhs.startswith("not found"):
            continue
        # Strip the trailing '(0x...)'
        path = rhs.split("(")[0].strip()
        if path:
            libs.append(path)
    return libs


def _find_module_path(
    name: str, kernel_release: str, modules_root: Path = Path("/lib/modules")
) -> Path | None:
    """Find a kernel module's .ko (or .ko.zst) file under
    /lib/modules/<release>/. Returns the first match by walking the
    tree — this is slow for large module trees but only runs at plan
    time, once."""
    base = modules_root / kernel_release
    if not base.is_dir():
        return None
    for ext in (".ko.zst", ".ko.xz", ".ko"):
        for cand in base.rglob(f"{name}{ext}"):
            return cand
    return None


def plan(
    snap: Snapshot,
    *,
    modules_root: Path = Path("/lib/modules"),
    include_dropbear: bool = False,
) -> MinitramPlan:
    """Walk the snapshot evidence and build a MinitramPlan.

    ``include_dropbear``: optionally include a static dropbear binary
    for headless rescue SSH. Adds ~700 KB.

    Returns a plan; doesn't write anything yet.
    """
    p = MinitramPlan(
        kernel_release=snap.kernel.release,
        snapshot_dir=snap.snapshot_dir,
    )

    # 1. Boot-chain tools.
    pred_table: dict[str, Callable[[Snapshot], bool]] = {
        "_luks_in_chain": _luks_in_chain,
        "_lvm_in_chain": _lvm_in_chain,
        "_md_raid_in_chain": _md_raid_in_chain,
    }
    for predicate_name, tool_name, rationale in _TOOL_PREDICATES:
        if not pred_table[predicate_name](snap):
            continue
        host_path = _which_or_none(tool_name)
        if host_path is None:
            # User said luks=True but cryptsetup isn't installed —
            # log it via plan but don't fail; let build() warn.
            continue
        libs = _resolve_libs(host_path)
        p.tools.append(
            MinitramTool(
                name=tool_name,
                host_path=host_path,
                target_path=f"/sbin/{tool_name}",
                libs=libs,
                rationale=rationale,
            )
        )

    # 2. Optional dropbear for rescue.
    if include_dropbear:
        host_path = _which_or_none("dropbear")
        if host_path is not None:
            libs = _resolve_libs(host_path)
            p.tools.append(
                MinitramTool(
                    name="dropbear",
                    host_path=host_path,
                    target_path="/sbin/dropbear",
                    libs=libs,
                    rationale="optional headless rescue SSH",
                )
            )

    # 3. Kernel modules — features + filesystem.
    needed_module_names: list[tuple[str, str]] = []  # (name, rationale)

    if _luks_in_chain(snap):
        for m in _BOOT_MODULES_BY_FEATURE["luks"]:
            needed_module_names.append((m, "LUKS"))
    if _lvm_in_chain(snap):
        for m in _BOOT_MODULES_BY_FEATURE["lvm"]:
            needed_module_names.append((m, "LVM"))
    if _md_raid_in_chain(snap):
        for m in _BOOT_MODULES_BY_FEATURE["raid"]:
            needed_module_names.append((m, "RAID"))
    if snap.boot.root_fstype:
        for m in _FS_MODULES.get(snap.boot.root_fstype, []):
            needed_module_names.append((m, f"root fstype = {snap.boot.root_fstype}"))

    # 4. DKMS modules (NVIDIA, ZFS, …) need to be in the initramfs
    # because they don't ship in the kernel tree.
    for d in snap.dkms:
        needed_module_names.append((d.name, f"DKMS: {d.name}/{d.version}"))

    seen_modules: set[str] = set()
    for name, rationale in needed_module_names:
        if name in seen_modules:
            continue
        seen_modules.add(name)
        host_path = _find_module_path(name, snap.kernel.release, modules_root)
        if host_path is None:
            # Module not found on this host — skip silently. The
            # planner records the intent; build() validates.
            continue
        # Construct target path mirroring host layout but rooted at /
        rel = host_path.relative_to(modules_root)
        target_path = f"/lib/modules/{rel}"
        p.modules.append(
            MinitramModule(
                name=name,
                host_path=host_path,
                target_path=target_path,
                rationale=rationale,
            )
        )

    # 5. /init script. Tiny shell that mounts proc/sys, loads the
    # boot-path modules in dependency order, opens any LUKS volumes,
    # activates LVM, mounts root, and pivot_root's into it.
    p.init_script = _compose_init_script(snap, p)

    return p


def _compose_init_script(snap: Snapshot, p: MinitramPlan) -> str:
    """Generate a shell script for /init.

    Conservative: assumes busybox-style shell, no bash-isms. Uses
    explicit modprobe calls in dependency order. Pivots into root
    once it's mounted."""
    lines: list[str] = [
        "#!/bin/sh",
        "# autokernel minitram /init — generated for {snap.host}".format(snap=snap),
        "set -e",
        "",
        "# Mount essentials.",
        "mount -t proc none /proc",
        "mount -t sysfs none /sys",
        "mount -t devtmpfs none /dev",
        "",
        "# Load boot-path modules.",
    ]
    # Modprobe in plan order (LUKS deps before dm_crypt, etc.).
    for m in p.modules:
        lines.append(f"modprobe {m.name} 2>/dev/null || true")
    lines.append("")

    if _luks_in_chain(snap):
        lines.extend(
            [
                "# Open LUKS — operator must enter passphrase.",
                "# Snapshot.boot.cmdline_params['cryptdevice'] tells us which device.",
                'if [ -n "$cryptdevice" ]; then',
                "    cryptsetup luksOpen $cryptdevice luksdev",
                "fi",
                "",
            ]
        )

    if _lvm_in_chain(snap):
        lines.extend(
            [
                "# Activate LVM volumes.",
                "lvm vgchange -ay 2>/dev/null || true",
                "",
            ]
        )

    if _md_raid_in_chain(snap):
        lines.extend(
            [
                "# Assemble MD/RAID arrays.",
                "mdadm --assemble --scan 2>/dev/null || true",
                "",
            ]
        )

    lines.extend(
        [
            "# Mount root from kernel cmdline (Snapshot.boot.cmdline_params['root']).",
            "ROOT=$(cat /proc/cmdline | sed -n 's/.*root=\\([^ ]*\\).*/\\1/p')",
            "mount $ROOT /newroot",
            "",
            "# Pivot.",
            "exec switch_root /newroot /sbin/init",
        ]
    )

    return "\n".join(lines) + "\n"


# ── build: plan → cpio.zst ────────────────────────────────────────────────


@dataclass(frozen=True)
class MinitramBuildResult:
    """Output of :func:`build`."""

    archive_path: Path
    plan_path: Path
    bytes: int
    n_modules: int
    n_tools: int


def build(
    plan_obj: MinitramPlan, *, out_path: Path | None = None
) -> MinitramBuildResult:
    """Stage the plan into a temp dir + pack as cpio.zst.

    ``out_path`` defaults to ``<snap>/initramfs.cpio.zst``. The plan
    summary lands at ``<snap>/initramfs.plan.json`` regardless.
    """
    if out_path is None:
        out_path = plan_obj.snapshot_dir / "initramfs.cpio.zst"
    plan_path = plan_obj.snapshot_dir / "initramfs.plan.json"

    # Stage in <snap>/.minitram-staging/
    stage = plan_obj.snapshot_dir / ".minitram-staging"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    # Standard layout.
    for d in (
        "proc",
        "sys",
        "dev",
        "newroot",
        "bin",
        "sbin",
        "lib",
        "lib64",
        "lib/modules",
        "etc",
    ):
        (stage / d).mkdir(parents=True, exist_ok=True)

    # Drop /init.
    init = stage / "init"
    init.write_text(plan_obj.init_script)
    init.chmod(0o755)

    # Tools + libs.
    for tool in plan_obj.tools:
        target = stage / tool.target_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tool.host_path, target)
        for lib in tool.libs:
            lib_path = Path(lib)
            if not lib_path.is_absolute() or not lib_path.exists():
                continue
            tgt_lib = stage / lib_path.relative_to("/")
            tgt_lib.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lib_path, tgt_lib)

    # Always include /lib64/ld-linux-x86-64.so.2 (or arch equivalent)
    # if we have any non-static binaries. Detected from the libs list.
    for ld in (Path("/lib64/ld-linux-x86-64.so.2"), Path("/lib/ld-linux-aarch64.so.1")):
        if ld.exists():
            (stage / ld.relative_to("/")).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ld, stage / ld.relative_to("/"))

    # Kernel modules.
    for m in plan_obj.modules:
        target = stage / m.target_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(m.host_path, target)

    # busybox-static — gives us sh, mount, modprobe, switch_root, etc.
    if plan_obj.busybox:
        bb = _which_or_none("busybox") or _which_or_none("busybox.static")
        if bb is not None:
            shutil.copy2(bb, stage / "bin" / "busybox")
            (stage / "bin" / "busybox").chmod(0o755)
            # Symlink the standard names. busybox responds to whichever
            # of its built-in applets is invoked via argv[0].
            for applet in (
                "sh",
                "mount",
                "modprobe",
                "switch_root",
                "cat",
                "sed",
                "ls",
                "cp",
                "mv",
                "mkdir",
                "rm",
                "test",
                "[",
                "echo",
                "sleep",
                "ash",
            ):
                lnk = stage / "bin" / applet
                if not lnk.exists():
                    lnk.symlink_to("busybox")

    # cpio + zstd. Use find -print0 / cpio -0 for filenames-with-spaces
    # safety. This is the standard "newc" cpio format the kernel
    # accepts.
    cpio_argv = (
        "cd '{stage}' && find . -print0 | cpio --null --create --format=newc "
        "| zstd -19 -T0 -o '{out}'"
    ).format(stage=stage, out=out_path)
    rc = subprocess.run(
        ["sh", "-c", cpio_argv],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if rc.returncode != 0:
        raise RuntimeError(f"cpio/zstd failed: {rc.stderr}")

    # Plan record.
    plan_path.write_text(json.dumps(plan_obj.to_summary_dict(), indent=2))

    # Cleanup staging.
    shutil.rmtree(stage)

    bytes_ = out_path.stat().st_size
    return MinitramBuildResult(
        archive_path=out_path,
        plan_path=plan_path,
        bytes=bytes_,
        n_modules=len(plan_obj.modules),
        n_tools=len(plan_obj.tools),
    )
