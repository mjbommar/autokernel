#!/usr/bin/env bash
# Rebuild an already-proposed hardware kernel without re-scanning/re-proposing.
#
# Use this after changing the build/localmodconfig logic. It preserves the
# existing snapshot/final.config baseline, stamps a fresh CONFIG_LOCALVERSION,
# rebuilds packages, and runs the VM boot-test. It does not install or reboot.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$REPO_ROOT"

WORK_DIR="${AUTOKERNEL_HW_WORK_DIR:-$HOME/.local/share/autokernel/hardware-boot}"
SNAPSHOT_DIR="${AUTOKERNEL_HW_SNAPSHOT_DIR:-$WORK_DIR/snapshot}"
KERNEL_SOURCE="${AUTOKERNEL_HW_KERNEL_SOURCE:-$WORK_DIR/kernels/linux-6.19}"
TARGET="${AUTOKERNEL_HW_TARGET:-auto}"
COMPILER="${AUTOKERNEL_HW_COMPILER:-clang}"
JOBS="${AUTOKERNEL_HW_JOBS:-$(nproc)}"
BOOT_TEST_METHOD="${AUTOKERNEL_HW_BOOT_TEST_METHOD:-qemu}"
LOCALVERSION="${AUTOKERNEL_HW_LOCALVERSION:--autokernel-$(date -u +%Y%m%d%H%M)}"
AUTOKERNEL_BIN="$REPO_ROOT/.venv/bin/autokernel"

usage() {
    cat <<'EOF'
Usage: scripts/rebuild-existing-hardware-kernel.sh [options]

Rebuilds from an existing hardware smoke snapshot without running scan,
propose, review, or apply again. This is the safe path when the current booted
kernel is an experimental one-shot and the original snapshot is still the good
baseline.

Options:
  --snapshot-dir PATH      Existing snapshot dir (default: hardware-boot/snapshot)
  --kernel-source PATH     Kernel source tree (default: hardware-boot/kernels/linux-6.19)
  --localversion SUFFIX    CONFIG_LOCALVERSION suffix (default: -autokernel-YYYYmmddHHMM)
  --target TARGET          build target (default: auto)
  --compiler NAME          clang, llvm, or gcc (default: clang)
  --jobs N                 make -j value (default: nproc)
  --boot-test-method NAME  qemu, virtme, or auto (default: qemu)
  --help, -h               Show this help
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

run_cmd() {
    printf '+'
    printf ' %q' "$@"
    printf '\n'
    "$@"
}

run_ak() {
    run_cmd uv --project "$REPO_ROOT" run autokernel "$@"
}

set_config_string() {
    local file="$1" key="$2" value="$3"
    local escaped
    escaped="$(printf '%s' "$value" | sed 's/[&|]/\\&/g')"
    if grep -Eq "^${key}=" "$file"; then
        sed -i -E "s|^${key}=.*|${key}=\"${escaped}\"|" "$file"
    elif grep -Eq "^# ${key} is not set" "$file"; then
        sed -i -E "s|^# ${key} is not set|${key}=\"${escaped}\"|" "$file"
    else
        printf '%s="%s"\n' "$key" "$value" >>"$file"
    fi
}

set_config_not_set() {
    local file="$1" key="$2"
    if grep -Eq "^${key}=" "$file"; then
        sed -i -E "s|^${key}=.*|# ${key} is not set|" "$file"
    elif ! grep -Eq "^# ${key} is not set" "$file"; then
        printf '# %s is not set\n' "$key" >>"$file"
    fi
}

find_built_packages() {
    local marker="$1"
    local source_parent
    source_parent="$(dirname -- "$KERNEL_SOURCE")"
    find "$source_parent" -maxdepth 1 -type f -newer "$marker" \
        \( -name 'linux-image-*.deb' -o -name 'linux-headers-*.deb' \) \
        ! -name 'linux-image-*-dbg_*.deb' \
        -print | sort
}

derive_grub_entry() {
    local kernel_release="$1"
    local distro_name="Ubuntu"
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        distro_name="${NAME:-Ubuntu}"
    fi
    printf 'Advanced options for %s>%s, with Linux %s\n' "$distro_name" "$distro_name" "$kernel_release"
}

print_cmd() {
    printf '  '
    printf '%q ' "$@"
    printf '\n'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --snapshot-dir)
            SNAPSHOT_DIR="$2"
            shift 2
            ;;
        --kernel-source)
            KERNEL_SOURCE="$2"
            shift 2
            ;;
        --localversion)
            LOCALVERSION="$2"
            shift 2
            ;;
        --target)
            TARGET="$2"
            shift 2
            ;;
        --compiler)
            COMPILER="$2"
            shift 2
            ;;
        --jobs)
            JOBS="$2"
            shift 2
            ;;
        --boot-test-method)
            BOOT_TEST_METHOD="$2"
            shift 2
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

[ -d "$SNAPSHOT_DIR" ] || die "snapshot dir missing: $SNAPSHOT_DIR"
[ -f "$SNAPSHOT_DIR/manifest" ] || die "not an autokernel snapshot: $SNAPSHOT_DIR"
[ -f "$SNAPSHOT_DIR/final.config" ] || die "missing $SNAPSHOT_DIR/final.config"
[ -f "$KERNEL_SOURCE/Makefile" ] || die "kernel source tree has no Makefile: $KERNEL_SOURCE"

mkdir -p "$WORK_DIR/tmp"
export TMPDIR="$WORK_DIR/tmp"

printf 'snapshot:      %s\n' "$SNAPSHOT_DIR"
printf 'kernel source: %s\n' "$KERNEL_SOURCE"
printf 'TMPDIR:        %s\n' "$TMPDIR"
printf 'localversion:  %s\n' "$LOCALVERSION"

cp "$SNAPSHOT_DIR/final.config" "$SNAPSHOT_DIR/final.config.before-rebuild"
set_config_string "$SNAPSHOT_DIR/final.config" CONFIG_LOCALVERSION "$LOCALVERSION"
set_config_not_set "$SNAPSHOT_DIR/final.config" CONFIG_LOCALVERSION_AUTO

BUILD_MARKER="$(mktemp)"
touch "$BUILD_MARKER"

run_ak build "$SNAPSHOT_DIR" \
    --kernel-source "$KERNEL_SOURCE" \
    --localmodconfig \
    --execute \
    --target "$TARGET" \
    --jobs "$JOBS" \
    --compiler "$COMPILER"

run_ak boot-test "$SNAPSHOT_DIR" \
    --kernel-source "$KERNEL_SOURCE" \
    --method "$BOOT_TEST_METHOD" \
    --timeout 120

mapfile -t PACKAGES < <(find_built_packages "$BUILD_MARKER" || true)
rm -f "$BUILD_MARKER"

KERNEL_RELEASE="$(make -s -C "$KERNEL_SOURCE" kernelrelease)"
KERNEL_ENTRY="$(derive_grub_entry "$KERNEL_RELEASE")"

printf '\nbuilt packages:\n'
printf '  %s\n' "${PACKAGES[@]}"
printf '\nGRUB one-shot entry:\n  %s\n' "$KERNEL_ENTRY"

if [ "${#PACKAGES[@]}" -gt 0 ]; then
    INSTALL_CMD=(sudo env "PATH=$PATH" "HOME=$HOME" "$AUTOKERNEL_BIN" install "$SNAPSHOT_DIR")
    for pkg in "${PACKAGES[@]}"; do
        INSTALL_CMD+=(--package "$pkg")
    done
    INSTALL_CMD+=(--kernel-entry "$KERNEL_ENTRY" --execute)

    cat <<EOF

To install this rebuilt kernel without rebuilding:
EOF
    print_cmd "${INSTALL_CMD[@]}"
fi
