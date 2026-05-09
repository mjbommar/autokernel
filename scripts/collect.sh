#!/usr/bin/env bash
# autokernel hardware/system collector
#
# Dumb data dumper: invokes system tools, writes raw output into $OUT/.
# Python (autokernel.snapshot) parses these files into the typed Snapshot.
#
# Designed to be safe to run as an unprivileged user. Degrades gracefully
# when tools are missing or when /dev/kmsg etc. require root.
#
# Usage: collect.sh [OUTDIR]   (default: ./snapshot-<timestamp>)

set -u
set -o pipefail

OUT="${1:-snapshot-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$OUT"

run() {
    # run "name" cmd args...
    # writes stdout to $OUT/$name, stderr to $OUT/$name.err, exit code to $OUT/$name.rc
    local name="$1"
    shift
    local out="$OUT/$name"
    if "$@" >"$out" 2>"$out.err"; then
        rm -f "$out.err"
        echo 0 >"$out.rc"
    else
        echo $? >"$out.rc"
    fi
}

have() { command -v "$1" >/dev/null 2>&1; }

can_sudo_probe() {
    [ "${AUTOKERNEL_SCAN_SUDO:-0}" = "1" ] || return 1
    [ "$(id -u)" -eq 0 ] && return 0
    have sudo && sudo -n true >/dev/null 2>&1
}

priv() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif can_sudo_probe; then
        sudo -n "$@"
    else
        "$@"
    fi
}

software_feature() {
    # feature<TAB>source<TAB>name<TAB>detail
    local feature="$1" source="$2" name="$3" detail="${4:-}"
    printf '%s\t%s\t%s\t%s\n' "$feature" "$source" "$name" "$detail"
}

# ── identity / kernel ────────────────────────────────────────────────────────
run uname        uname -a
run os_release   cat /etc/os-release
run kernel_release uname -r
run kernel_version uname -v
run cmdline      cat /proc/cmdline
run cpuinfo      cat /proc/cpuinfo
run meminfo      cat /proc/meminfo
run mounts       cat /proc/mounts

# Current kernel's own .config — most important reference!
KREL=$(uname -r)
if [ -r /proc/config.gz ]; then
    zcat /proc/config.gz >"$OUT/running_config" 2>/dev/null && echo 0 >"$OUT/running_config.rc"
elif [ -r "/boot/config-$KREL" ]; then
    cp "/boot/config-$KREL" "$OUT/running_config" && echo 0 >"$OUT/running_config.rc"
else
    echo "no running config found" >"$OUT/running_config.err"
    echo 1 >"$OUT/running_config.rc"
fi

# ── modules: what's loaded, what could be loaded ────────────────────────────
run lsmod        lsmod
run modules_alias_path bash -c 'echo /lib/modules/$(uname -r)/modules.alias'
run modules_dep_path   bash -c 'echo /lib/modules/$(uname -r)/modules.dep'
run modules_builtin_path bash -c 'echo /lib/modules/$(uname -r)/modules.builtin'
run modules_builtin_modinfo_path bash -c 'echo /lib/modules/$(uname -r)/modules.builtin.modinfo'

# ── hardware: PCI / USB / DMI ───────────────────────────────────────────────
have lspci  && run lspci_vmmnk lspci -vmmnk
have lspci  && run lspci_tree  lspci -tv
have lsusb  && run lsusb       lsusb
have lsusb  && run lsusb_t     lsusb -t
have dmidecode && run dmidecode priv dmidecode
have lscpu  && run lscpu_j     lscpu -J
have lsblk  && run lsblk_j     lsblk -J -O
have findmnt && run findmnt_j  findmnt -J -A
have ip     && run ip_link_j   ip -j link
have ip     && run ip_addr_j   ip -j addr
have lshw   && run lshw_j      priv lshw -json

# ── /sys: every modalias the kernel has ever registered for a device ────────
# This catches devices that aren't currently bound to a driver but exist.
if [ -d /sys/devices ]; then
    find /sys/devices -name modalias -type f 2>/dev/null \
        | while read -r f; do
            printf '%s\t' "$f"
            cat "$f" 2>/dev/null
          done >"$OUT/sys_modaliases"
    echo 0 >"$OUT/sys_modaliases.rc"
fi

# Active drivers (what's actually bound right now)
if [ -d /sys/bus ]; then
    {
        for bus in /sys/bus/*/devices/*/driver; do
            [ -L "$bus" ] || continue
            dev="${bus%/driver}"
            drv=$(readlink -f "$bus" 2>/dev/null)
            printf '%s\t%s\n' "$dev" "${drv##*/}"
        done
    } >"$OUT/sys_bound_drivers" 2>/dev/null
    echo 0 >"$OUT/sys_bound_drivers.rc"
fi

# ── filesystems / block / boot ──────────────────────────────────────────────
run efi_present  test -d /sys/firmware/efi
run secureboot   bash -c 'have() { command -v "$1" >/dev/null; }; have mokutil && mokutil --sb-state || echo "mokutil missing"'

# Crypto / LUKS detection — load-bearing for boot if /boot or / is on LUKS
have lsblk && run crypt_devices lsblk -no NAME,FSTYPE,TYPE
have cryptsetup && run crypt_status bash -c 'for d in /dev/mapper/*; do [ -e "$d" ] && cryptsetup status "$(basename "$d")"; done 2>/dev/null'

# DKMS — out-of-tree modules that must rebuild against any new kernel
have dkms && run dkms_status dkms status

# ── software intent: packages/binaries/services that imply late-loaded modules ─
# lsmod answers "loaded now"; this captures installed/runnable software that can
# request modules later, e.g. libvirt/Docker netfilter rules after daemon start.
{
    for bin in docker dockerd containerd podman nerdctl kubelet kubeadm kubectl crictl \
        virsh libvirtd virtqemud qemu-system-x86_64 qemu-img iptables ip6tables nft \
        firewalld ufw; do
        if have "$bin"; then
            case "$bin" in
                docker|dockerd|containerd|podman|nerdctl) feature="containers" ;;
                kubelet|kubeadm|kubectl|crictl) feature="kubernetes" ;;
                virsh|libvirtd|virtqemud|qemu-system-x86_64|qemu-img) feature="virtualization" ;;
                iptables|ip6tables|nft|firewalld|ufw) feature="firewall" ;;
                *) feature="software" ;;
            esac
            software_feature "$feature" binary "$bin" "$(command -v "$bin" 2>/dev/null || true)"
        fi
    done

    if have systemctl; then
        systemctl list-unit-files --no-legend --no-pager 2>/dev/null \
            | awk '{print $1 "\t" $2}' \
            | grep -E '^(docker|containerd|podman|libvirtd|virtqemud|virtlogd|virtstoraged|kubelet|microk8s|firewalld|ufw)\.' \
            | while read -r unit state; do
                case "$unit" in
                    docker.*|containerd.*|podman.*) feature="containers" ;;
                    kubelet.*|microk8s.*) feature="kubernetes" ;;
                    libvirtd.*|virtqemud.*|virtlogd.*|virtstoraged.*) feature="virtualization" ;;
                    firewalld.*|ufw.*) feature="firewall" ;;
                    *) feature="software" ;;
                esac
                software_feature "$feature" systemd "$unit" "$state"
            done
    fi

    if have dpkg-query; then
        for pkg in docker.io docker-ce docker-ce-cli containerd containerd.io podman \
            libvirt-daemon libvirt-daemon-system libvirt-clients qemu-system-x86 \
            kubelet kubeadm kubectl microk8s nftables iptables firewalld ufw; do
            dpkg-query -W -f='${db:Status-Abbrev}\t${binary:Package}\t${Version}\n' "$pkg" 2>/dev/null \
                | awk '$1 ~ /^ii/ { print $2 "\t" $3 }' \
                | while read -r found version; do
                    case "$found" in
                        docker*|containerd*|podman) feature="containers" ;;
                        kubelet|kubeadm|kubectl|microk8s) feature="kubernetes" ;;
                        libvirt*|qemu-system-x86) feature="virtualization" ;;
                        nftables|iptables|firewalld|ufw) feature="firewall" ;;
                        *) feature="software" ;;
                    esac
                    software_feature "$feature" dpkg "$found" "$version"
                done
        done
    elif have rpm; then
        for pkg in docker docker-ce docker-ce-cli containerd podman libvirt qemu-system-x86 \
            kubelet kubeadm kubectl nftables iptables firewalld ufw; do
            if rpm -q "$pkg" >/dev/null 2>&1; then
                software_feature software rpm "$pkg" "$(rpm -q "$pkg" 2>/dev/null)"
            fi
        done
    fi
} >"$OUT/software_features" 2>/dev/null
echo 0 >"$OUT/software_features.rc"

# Firmware actually loaded — try multiple sources because dmesg may be
# restricted on Ubuntu 22.04+ (kernel.dmesg_restrict=1).
DMESG_OK=0
if priv dmesg >/dev/null 2>&1; then
    DMESG_OK=1
    run dmesg_firmware priv bash -c 'dmesg 2>/dev/null | grep -iE "firmware|loading.*\.bin|microcode" || true'
    run dmesg_full     priv bash -c 'dmesg 2>/dev/null || true'
fi

# Always try journalctl as a parallel source — `journalctl -k -b 0` reads
# the persistent kernel log regardless of dmesg_restrict.
if have journalctl; then
    run journal_firmware bash -c 'journalctl -k -b 0 -o cat 2>/dev/null | grep -iE "firmware|loading.*\.bin|microcode" || true'
    if [ "$DMESG_OK" = "0" ]; then
        run journal_full bash -c 'journalctl -k -b 0 -o cat 2>/dev/null | head -2000 || true'
    fi
fi

# Authoritative source #1: ask each loaded module what firmware it declares
# via modinfo's `firmware:` field. Independent of dmesg/journal access.
if have modinfo && [ -r /proc/modules ]; then
    {
        for mod in $(awk '{print $1}' /proc/modules); do
            modinfo -F firmware "$mod" 2>/dev/null | while read -r fw; do
                [ -n "$fw" ] && printf '%s\t%s\n' "$mod" "$fw"
            done
        done
    } >"$OUT/module_firmware" 2>/dev/null
    echo 0 >"$OUT/module_firmware.rc"
fi

# Authoritative source #2: /sys/class/firmware (live in-flight loads).
if [ -d /sys/class/firmware ]; then
    find /sys/class/firmware -maxdepth 2 -name 'loading' 2>/dev/null >"$OUT/firmware_class"
fi

# ── initramfs enumeration ──────────────────────────────────────────────────
# The initramfs contains every module needed to mount /. These are by
# definition load-bearing. lsinitramfs needs read access to /boot/initrd*.
INITRD="/boot/initrd.img-$(uname -r)"
if { [ -r "$INITRD" ] || can_sudo_probe; } && have lsinitramfs; then
    run initramfs_modules priv bash -c "lsinitramfs '$INITRD' 2>/dev/null | grep -E '\.ko(\.|$)' | sed 's,^.*/,,;s,\\.ko.*$,,' | sort -u"
    run initramfs_firmware priv bash -c "lsinitramfs '$INITRD' 2>/dev/null | grep -E 'lib/firmware/' | sed 's,^.*lib/firmware/,,' | sort -u"
elif have unmkinitramfs && { [ -r "$INITRD" ] || can_sudo_probe; }; then
    # Fallback for hosts that have unmkinitramfs but no lsinitramfs in PATH.
    run initramfs_modules priv bash -c "TMPD=\$(mktemp -d) && unmkinitramfs '$INITRD' \"\$TMPD\" 2>/dev/null; find \"\$TMPD\" -name '*.ko*' 2>/dev/null | sed 's,^.*/,,;s,\\.ko.*$,,' | sort -u; rm -rf \"\$TMPD\""
fi

# ── input / sound / graphics — devices userspace is actually using ──────────
have ls && run dev_inputs ls -la /dev/input/
have ls && run dev_dri    ls -la /dev/dri/
have ls && run dev_snd    ls -la /dev/snd/
[ -r /proc/asound/cards ] && run asound_cards cat /proc/asound/cards

# ── manifest ────────────────────────────────────────────────────────────────
{
    echo "schema_version=1"
    echo "collected_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "host=$(hostname)"
    echo "user=$(id -un)"
    echo "uid=$(id -u)"
} >"$OUT/manifest"

echo "$OUT"
