#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/qemu-busybox-shell.sh [KERNEL_SOURCE|BZIMAGE]

Boot a built kernel with a tiny BusyBox initramfs and drop to /bin/sh.

Environment:
  AUTOKERNEL_QEMU_KERNEL         Direct path to bzImage/vmlinux.
  AUTOKERNEL_QEMU_KERNEL_SOURCE  Kernel source tree containing arch/x86/boot/bzImage.
  AUTOKERNEL_QEMU_MEMORY         VM memory in MiB. Default: 1024.
  AUTOKERNEL_QEMU_SHELL_COMMAND  Non-interactive command to run before powering off.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

kernel="${AUTOKERNEL_QEMU_KERNEL:-}"
kernel_source="${AUTOKERNEL_QEMU_KERNEL_SOURCE:-}"
memory="${AUTOKERNEL_QEMU_MEMORY:-1024}"
shell_command="${AUTOKERNEL_QEMU_SHELL_COMMAND:-}"
input="${1:-}"

if [ -n "$input" ]; then
    if [ -d "$input" ]; then
        kernel_source="$input"
    else
        kernel="$input"
    fi
fi

if ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
    echo "qemu-system-x86_64 not found" >&2
    exit 1
fi

if ! command -v busybox >/dev/null 2>&1; then
    echo "busybox not found" >&2
    exit 1
fi

if ! command -v cpio >/dev/null 2>&1; then
    echo "cpio not found" >&2
    exit 1
fi

if [ -z "$kernel" ] && [ -n "$kernel_source" ]; then
    for candidate in \
        "$kernel_source/arch/x86/boot/bzImage" \
        "$kernel_source/arch/x86_64/boot/bzImage" \
        "$kernel_source/vmlinux"; do
        if [ -r "$candidate" ]; then
            kernel="$candidate"
            break
        fi
    done
fi

if [ -z "$kernel" ]; then
    for candidate in \
        "./arch/x86/boot/bzImage" \
        "./arch/x86_64/boot/bzImage" \
        "./vmlinux"; do
        if [ -r "$candidate" ]; then
            kernel="$candidate"
            break
        fi
    done
fi

if [ -z "$kernel" ] || [ ! -r "$kernel" ]; then
    echo "no readable kernel image found; pass a bzImage path, pass a kernel source tree, or set AUTOKERNEL_QEMU_KERNEL" >&2
    exit 1
fi

workdir="$(mktemp -d)"
cleanup() {
    rm -rf "$workdir"
}
trap cleanup EXIT

root="$workdir/initramfs"
initrd="$workdir/initramfs.cpio.gz"
mkdir -p "$root/bin" "$root/dev" "$root/etc" "$root/proc" "$root/root" "$root/run" "$root/sbin" "$root/sys" "$root/tmp" "$root/usr/bin" "$root/usr/sbin"
chmod 1777 "$root/tmp"

cp "$(command -v busybox)" "$root/bin/busybox"
for applet in \
    ash cat chmod cttyhack dd dmesg echo env find grep hexdump id ifconfig ip \
    less ln ls lsmod mkdir mknod modprobe more mount poweroff ps reboot setsid \
    sh sleep uname vi; do
    ln -s busybox "$root/bin/$applet"
done

if [ -n "$shell_command" ]; then
    printf '%s\n' "$shell_command" >"$root/etc/init-command"
fi

cat >"$root/init" <<'EOF'
#!/bin/sh
set -eu

export HOME=/root
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
export TERM=linux

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || {
    mount -t tmpfs tmpfs /dev
    mknod -m 600 /dev/console c 5 1
    mknod -m 666 /dev/null c 1 3
    mknod -m 666 /dev/zero c 1 5
    mknod -m 666 /dev/tty c 5 0
    mknod -m 666 /dev/ttyS0 c 4 64
}
mount -t tmpfs tmpfs /tmp
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts 2>/dev/null || true

echo
echo "autokernel BusyBox shell"
echo "kernel: $(uname -a)"
echo "cmdline: $(cat /proc/cmdline)"
echo
echo "Useful commands: dmesg, lsmod, cat /proc/config.gz, poweroff -f"
echo

if [ -s /etc/init-command ]; then
    /bin/sh /etc/init-command
    poweroff -f || reboot -f
fi

exec setsid cttyhack /bin/sh
EOF
chmod +x "$root/init"

(
    cd "$root"
    find . -print0 | cpio --null -o --format=newc 2>/dev/null | gzip -9 >"$initrd"
)

qemu_args=(
    qemu-system-x86_64
    -M pc
)
if [ -e /dev/kvm ] && [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
    qemu_args+=(-enable-kvm -cpu host)
else
    qemu_args+=(-accel tcg -cpu max)
fi
qemu_args+=(
    -kernel "$kernel"
    -initrd "$initrd"
    -append "console=ttyS0 earlyprintk=serial panic=1 rdinit=/init"
    -nographic
    -no-reboot
    -m "$memory"
    -smp 1
)

printf 'kernel: %s\n' "$kernel" >&2
printf 'initrd: %s\n' "$initrd" >&2
printf 'exit QEMU monitor with Ctrl-a x if needed\n\n' >&2
exec "${qemu_args[@]}"
