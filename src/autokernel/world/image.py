"""World image: rootfs from the world repo → bootable ext4 → QEMU boot.

Path A of W5 (docs/WORLD.md): the fastest honest "first boot" — our
packages, direct ``-kernel`` QEMU boot, no bootloader/initramfs. The
UEFI/qcow2 path (Path B) layers on top later.

Everything is unprivileged:

* assembly reuses the W2 verify-chroot recipe — mmdebstrap from stock
  mirrors, then hooks sync the signed world repo in, pin it at 1001,
  dist-upgrade onto our +ak packages, and install the init set (ring 0
  is Priority:required, which deliberately has no init — but systemd,
  udev, kmod, agetty and login are all built by ring-0 *sources*, so
  the image just has to ask for them);
* a sentinel unit prints AUTOKERNEL_WORLD_BOOT_OK to the console at
  multi-user.target — the same convention as the kernel-side boot
  test, and the only success signal we trust;
* ext4 packing is fakeroot + ``mkfs.ext4 -d``: ownership recorded by
  fakeroot during tar extraction is what mkfs bakes into the image —
  no sudo, no loop mounts;
* the boot kernel defaults to the host's running kernel *extracted
  from its .deb* (apt-get download + dpkg-deb -x), because /boot is
  0600 on modern Ubuntu while the identical bits are world-readable
  in the archive. Ubuntu's generic kernel has VIRTIO_BLK/PCI and EXT4
  built in, so ``root=/dev/vda`` needs no initramfs.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from autokernel.world.models import BaseRelease

BOOT_SENTINEL = "AUTOKERNEL_WORLD_BOOT_OK"

# Ring 0 (Priority: required) has no init by design; these turn the
# package set into a bootable system. All are produced by ring-0
# sources and therefore come from the world repo, not stock.
DEFAULT_INCLUDES = [
    "systemd",
    "systemd-sysv",  # /sbin/init symlink
    "udev",
    "kmod",
    "login",
    "passwd",
]

_SENTINEL_UNIT = f"""\
[Unit]
Description=autokernel world boot sentinel
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c '/bin/echo {BOOT_SENTINEL} > /dev/console; \
grep -q autokernel.boottest /proc/cmdline && systemctl poweroff --no-block || true'

[Install]
WantedBy=multi-user.target
"""

_AUTOLOGIN_DROPIN = """\
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear %I $TERM
"""


def assemble_argv(
    *,
    base: BaseRelease,
    repo_dir: Path,
    keyring: Path,
    out_tar: Path,
    includes: list[str],
) -> list[str]:
    """mmdebstrap argv for the bootable rootfs tar (pure; testable)."""
    comps = " ".join(base.components)
    hooks = [
        'mkdir -p "$1/srv/world-repo" "$1/usr/share/keyrings"',
        f"sync-in {repo_dir} /srv/world-repo",
        f"copy-in {keyring} /usr/share/keyrings",
        # Wire the world repo, pinned above stock.
        'echo "deb [signed-by=/usr/share/keyrings/'
        + keyring.name
        + '] file:///srv/world-repo ./" > "$1/etc/apt/sources.list.d/world.list"',
        'printf "Package: *\\nPin: release o=autokernel-world\\nPin-Priority: 1001\\n"'
        ' > "$1/etc/apt/preferences.d/world"',
        'chroot "$1" apt-get update',
        # Everything installed so far jumps to the +ak builds, then the
        # init set (also resolved from the world repo where built).
        'chroot "$1" apt-get -y dist-upgrade',
        'chroot "$1" apt-get -y install ' + " ".join(includes),
        # Bootability: fstab, hostname, root autologin on the serial
        # console, and the boot sentinel.
        'printf "/dev/vda / ext4 defaults 0 1\\n" > "$1/etc/fstab"',
        'echo autokernel-world > "$1/etc/hostname"',
        'chroot "$1" passwd -d root',
        'mkdir -p "$1/etc/systemd/system/serial-getty@ttyS0.service.d"',
        "upload-autologin-dropin",  # placeholder; replaced below
        "upload-sentinel-unit",  # placeholder; replaced below
        'mkdir -p "$1/etc/systemd/system/multi-user.target.wants"',
        "ln -sf /etc/systemd/system/world-boot-ok.service"
        ' "$1/etc/systemd/system/multi-user.target.wants/world-boot-ok.service"',
        # The image must not reference host paths once built.
        'rm -rf "$1/srv/world-repo" "$1/etc/apt/sources.list.d/world.list"'
        ' "$1/etc/apt/preferences.d/world"',
    ]
    argv = ["mmdebstrap", "--mode=unshare", "--variant=required"]
    for hook in hooks:
        argv.append(f"--customize-hook={hook}")
    argv += [
        base.suite,
        str(out_tar),
        f"deb {base.mirror} {base.suite} {comps}",
        f"deb {base.mirror} {base.suite}-updates {comps}",
        f"deb {base.mirror} {base.suite}-security {comps}",
    ]
    return argv


def _materialize_file_hooks(argv: list[str], staging: Path) -> list[str]:
    """Replace the upload placeholders with real hooks that copy staged
    files in (mmdebstrap's copy-in needs them on disk)."""
    staging.mkdir(parents=True, exist_ok=True)
    autologin = staging / "autologin.conf"
    autologin.write_text(_AUTOLOGIN_DROPIN, encoding="utf-8")
    sentinel = staging / "world-boot-ok.service"
    sentinel.write_text(_SENTINEL_UNIT, encoding="utf-8")
    out: list[str] = []
    for arg in argv:
        if arg == "--customize-hook=upload-autologin-dropin":
            out.append(
                f"--customize-hook=copy-in {autologin} "
                "/etc/systemd/system/serial-getty@ttyS0.service.d"
            )
        elif arg == "--customize-hook=upload-sentinel-unit":
            out.append(f"--customize-hook=copy-in {sentinel} /etc/systemd/system")
        else:
            out.append(arg)
    return out


def build_rootfs_tar(
    *,
    base: BaseRelease,
    repo_dir: Path,
    keyring: Path,
    out_tar: Path,
    includes: list[str] | None = None,
    log: Path,
) -> None:
    out_tar.parent.mkdir(parents=True, exist_ok=True)
    argv = assemble_argv(
        base=base,
        repo_dir=repo_dir,
        keyring=keyring,
        out_tar=out_tar,
        includes=includes or DEFAULT_INCLUDES,
    )
    argv = _materialize_file_hooks(argv, out_tar.parent / "staging")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as f:
        f.write(("\n$ " + " ".join(argv) + "\n").encode())
        f.flush()
        proc = subprocess.run(argv, stdout=f, stderr=subprocess.STDOUT, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"mmdebstrap rootfs assembly failed (see {log})")


def image_size_mb(tar_path: Path, *, explicit_mb: int = 0) -> int:
    """Sized for the extracted tree plus apt/log headroom."""
    if explicit_mb:
        return explicit_mb
    tar_mb = tar_path.stat().st_size // (1024 * 1024)
    return int(tar_mb * 1.4) + 256


def pack_ext4(tar_path: Path, img_path: Path, *, size_mb: int, log: Path) -> None:
    """tar → ext4 image, unprivileged. fakeroot makes the ownership
    recorded at extraction visible to mkfs.ext4 -d, which bakes it in."""
    workdir = img_path.parent / "rootfs.tree"
    script = (
        f"rm -rf {workdir} && mkdir -p {workdir} && "
        f"tar -xpf {tar_path} -C {workdir} && "
        f"rm -f {img_path} && truncate -s {size_mb}M {img_path} && "
        f"mkfs.ext4 -q -F -L worldroot -d {workdir} {img_path} && "
        f"rm -rf {workdir}"
    )
    with log.open("ab") as f:
        f.write(f"\n$ fakeroot sh -c '{script}'\n".encode())
        f.flush()
        proc = subprocess.run(
            ["fakeroot", "sh", "-c", script],
            stdout=f,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"ext4 packing failed (see {log})")


# ── boot kernel ─────────────────────────────────────────────────────────────


def _downloadable_kernel_pkg() -> str:
    """The running kernel's package if the archive still has that exact
    build, else the newest generic image via the meta-package (the
    installed build is often superseded — same flavor, same builtin
    virtio, equally bootable)."""
    release = subprocess.run(
        ["uname", "-r"], capture_output=True, text=True, check=True
    ).stdout.strip()
    exact = f"linux-image-{release}"
    policy = subprocess.run(
        ["apt-get", "download", "--print-uris", exact],
        capture_output=True,
        text=True,
        check=False,
    )
    if policy.returncode == 0 and policy.stdout.strip():
        return exact
    depends = subprocess.run(
        ["apt-cache", "depends", "linux-image-generic"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    for line in depends.splitlines():
        line = line.strip()
        if line.startswith("Depends:") and "linux-image-" in line:
            return line.split()[-1]
    raise RuntimeError("no downloadable generic kernel package found")


def extract_boot_kernel(dest_dir: Path, *, log: Path) -> Path:
    """A bootable generic-flavor vmlinuz, extracted from its .deb
    (modern Ubuntu keeps /boot/vmlinuz-* at 0600; the archive bits are
    public). Cached in dest_dir."""
    pkg = _downloadable_kernel_pkg()
    cached = dest_dir / f"vmlinuz-{pkg.removeprefix('linux-image-')}"
    if cached.exists():
        return cached
    dest_dir.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as f:
        proc = subprocess.run(
            ["apt-get", "download", pkg],
            cwd=dest_dir,
            stdout=f,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"apt-get download {pkg} failed (see {log})")
        deb = next(dest_dir.glob(f"{pkg}_*.deb"))
        extract = dest_dir / "kernel-extract"
        proc = subprocess.run(
            ["dpkg-deb", "-x", str(deb), str(extract)],
            stdout=f,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"dpkg-deb -x {deb.name} failed (see {log})")
    vmlinuz = next(extract.glob("boot/vmlinuz-*"))
    vmlinuz.rename(cached)
    deb.unlink()
    subprocess.run(["rm", "-rf", str(extract)], check=False)
    return cached


# ── boot test ───────────────────────────────────────────────────────────────


def analyze_world_serial(text: str) -> tuple[bool, str]:
    if BOOT_SENTINEL in text:
        return True, f"sentinel {BOOT_SENTINEL} reached (multi-user.target up)"
    if m := re.search(r"Kernel panic[^\n]*", text):
        return False, m.group(0)
    if "Failed to mount" in text or "Cannot open root device" in text:
        return False, "root filesystem mount failed"
    return False, "no sentinel before timeout"


def _kvm_state() -> str:
    """'direct' (usable now), 'sg' (in the kvm group but not in this
    session — group changes need re-login), or 'none'."""
    import grp
    import os

    if os.access("/dev/kvm", os.R_OK | os.W_OK):
        return "direct"
    try:
        if os.environ.get("USER") in grp.getgrnam("kvm").gr_mem:
            return "sg"
    except KeyError:
        pass
    return "none"


def boot_image(
    img_path: Path,
    kernel: Path,
    *,
    serial_log: Path,
    timeout: int = 120,
    memory_mb: int = 1024,
) -> tuple[bool, str]:
    """Direct-kernel QEMU boot of the ext4 image; verdict by sentinel.

    -cpu host is mandatory for -march=native worlds: a generic QEMU
    CPU model SIGILLs init. With TCG fallback, -cpu max emulates most
    ISA extensions but native worlds may still SIGILL — KVM is the
    supported path.
    """
    import shlex

    kvm = _kvm_state()
    cpu = "host" if kvm in ("direct", "sg") else "max"
    accel = "kvm" if kvm in ("direct", "sg") else "tcg"
    argv = [
        "qemu-system-x86_64",
        "-machine", f"accel={accel}",
        "-cpu", cpu,
        "-m", str(memory_mb),
        "-nographic",
        "-no-reboot",
        "-kernel", str(kernel),
        "-append",
        # quiet+show_status=0 cut ~11% off boot by not blocking on the
        # emulated serial UART for every printk / [ OK ] line (measured
        # with bootbench.py; the win is entirely console I/O, not CPU).
        # loglevel=3 keeps KERN_ERR+ and panics visible so the sentinel
        # parser still sees init death.
        "root=/dev/vda rw quiet loglevel=3 systemd.show_status=0 "
        "console=ttyS0 systemd.unit=multi-user.target autokernel.boottest",
        "-drive", f"file={img_path},if=virtio,format=raw",
    ]  # fmt: skip
    if kvm == "sg":
        argv = ["sg", "kvm", "-c", shlex.join(argv)]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
        out = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode(errors="replace") + (exc.stderr or b"").decode(
            errors="replace"
        )
    serial_log.parent.mkdir(parents=True, exist_ok=True)
    serial_log.write_text(out, encoding="utf-8")
    return analyze_world_serial(out)
