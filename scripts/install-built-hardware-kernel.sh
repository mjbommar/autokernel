#!/usr/bin/env bash
# Install the latest already-built hardware smoke kernel package without rebuilding.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"

WORK_DIR="${AUTOKERNEL_HW_WORK_DIR:-$HOME/.local/share/autokernel/hardware-boot}"
SNAPSHOT_DIR="${AUTOKERNEL_HW_SNAPSHOT_DIR:-$WORK_DIR/snapshot}"
KERNEL_DIR="${AUTOKERNEL_HW_KERNEL_CACHE:-$WORK_DIR/kernels}"
AUTOKERNEL_BIN="${AUTOKERNEL_BIN:-$REPO_ROOT/.venv/bin/autokernel}"
KERNEL_RELEASE="${AUTOKERNEL_HW_KERNEL_RELEASE:-}"
KERNEL_ENTRY="${AUTOKERNEL_HW_KERNEL_ENTRY:-}"
YES=0
REBOOT=0

usage() {
    cat <<'EOF'
Usage: scripts/install-built-hardware-kernel.sh [options]

Installs the latest already-built non-debug autokernel image/header .deb pair
from ~/.local/share/autokernel/hardware-boot/kernels. This does not rebuild.

Options:
  --kernel-release REL  Install this exact kernel release instead of latest
  --kernel-entry ENTRY  GRUB one-shot entry; defaults to Ubuntu advanced entry
  --snapshot-dir PATH   Snapshot dir with boot-test.json
  --kernel-dir PATH     Directory containing built .deb artifacts
  --reboot              Reboot after successful install
  --yes, -y             Do not prompt before install/reboot
  --help, -h            Show this help
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

confirm() {
    local prompt="$1" answer
    if [ "$YES" -eq 1 ]; then
        return 0
    fi
    printf '%s [y/N] ' "$prompt" >&2
    read -r answer
    case "$answer" in
        y|Y|yes|YES) ;;
        *) die "cancelled" ;;
    esac
}

quote_cmd() {
    printf '+'
    printf ' %q' "$@"
    printf '\n'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --kernel-release)
            KERNEL_RELEASE="$2"
            shift 2
            ;;
        --kernel-entry)
            KERNEL_ENTRY="$2"
            shift 2
            ;;
        --snapshot-dir)
            SNAPSHOT_DIR="$2"
            shift 2
            ;;
        --kernel-dir)
            KERNEL_DIR="$2"
            shift 2
            ;;
        --reboot)
            REBOOT=1
            shift
            ;;
        --yes|-y)
            YES=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

[ -x "$AUTOKERNEL_BIN" ] || die "autokernel binary not executable: $AUTOKERNEL_BIN"
[ -d "$SNAPSHOT_DIR" ] || die "snapshot dir missing: $SNAPSHOT_DIR"
[ -d "$KERNEL_DIR" ] || die "kernel artifact dir missing: $KERNEL_DIR"

if [ -z "$KERNEL_RELEASE" ]; then
    IMAGE_DEB="$(
        find "$KERNEL_DIR" -maxdepth 1 -type f \
            -name 'linux-image-*-autokernel-*_*.deb' \
            ! -name 'linux-image-*-dbg_*.deb' \
            -printf '%T@ %p\n' \
            | sort -nr \
            | awk 'NR == 1 { $1=""; sub(/^ /, ""); print }'
    )"
    [ -n "$IMAGE_DEB" ] || die "no non-debug autokernel linux-image .deb found in $KERNEL_DIR"
    image_name="$(basename -- "$IMAGE_DEB")"
    KERNEL_RELEASE="${image_name#linux-image-}"
    KERNEL_RELEASE="${KERNEL_RELEASE%%_*}"
else
    IMAGE_DEB="$(
        find "$KERNEL_DIR" -maxdepth 1 -type f \
            -name "linux-image-${KERNEL_RELEASE}_*.deb" \
            ! -name 'linux-image-*-dbg_*.deb' \
            -printf '%T@ %p\n' \
            | sort -nr \
            | awk 'NR == 1 { $1=""; sub(/^ /, ""); print }'
    )"
    [ -n "$IMAGE_DEB" ] || die "no image package found for $KERNEL_RELEASE in $KERNEL_DIR"
fi

HEADERS_DEB="$(
    find "$KERNEL_DIR" -maxdepth 1 -type f \
        -name "linux-headers-${KERNEL_RELEASE}_*.deb" \
        -printf '%T@ %p\n' \
        | sort -nr \
        | awk 'NR == 1 { $1=""; sub(/^ /, ""); print }'
)"
[ -n "$HEADERS_DEB" ] || die "no headers package found for $KERNEL_RELEASE in $KERNEL_DIR"

if [ -z "$KERNEL_ENTRY" ]; then
    distro_name="Ubuntu"
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        distro_name="${NAME:-Ubuntu}"
    fi
    KERNEL_ENTRY="Advanced options for ${distro_name}>${distro_name}, with Linux ${KERNEL_RELEASE}"
fi

printf 'snapshot:       %s\n' "$SNAPSHOT_DIR"
printf 'kernel release: %s\n' "$KERNEL_RELEASE"
printf 'image package:  %s\n' "$IMAGE_DEB"
printf 'header package: %s\n' "$HEADERS_DEB"
printf 'GRUB one-shot:  %s\n' "$KERNEL_ENTRY"

confirm "Install this already-built kernel and arm one-shot GRUB?"

INSTALL_CMD=(
    sudo env "PATH=$PATH" "HOME=$HOME" "$AUTOKERNEL_BIN" install "$SNAPSHOT_DIR"
    --package "$IMAGE_DEB"
    --package "$HEADERS_DEB"
    --kernel-entry "$KERNEL_ENTRY"
    --execute
)

quote_cmd "${INSTALL_CMD[@]}"
"${INSTALL_CMD[@]}"

if [ "$REBOOT" -eq 1 ]; then
    confirm "Reboot now into the one-shot kernel?"
    quote_cmd sudo systemctl reboot
    sudo systemctl reboot
fi
