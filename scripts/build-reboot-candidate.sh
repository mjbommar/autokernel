#!/usr/bin/env bash
# Build, package, boot-test, and optionally one-shot install an autokernel kernel.
#
# This is the narrow "get me to a viable reboot candidate" path:
#   1. keep sudo warm for privileged probes/dependency/install steps
#   2. optionally install missing deps
#   3. scan if the snapshot is missing or --rescan is passed
#   4. ensure final.config exists
#   5. build distro packages from the kernel source
#   6. QEMU boot-test the built bzImage
#   7. dry-run install, or --install / --reboot when explicitly requested
#
# Default behavior does not touch /boot and does not reboot.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$REPO_ROOT"

SNAPSHOT_DIR="${AUTOKERNEL_SNAPSHOT_DIR:-kernel-01}"
KERNEL_SOURCE="${AUTOKERNEL_KERNEL_SOURCE:-$HOME/.cache/autokernel/kernels/linux-torvalds-v7.1-rc3}"
JOBS="${AUTOKERNEL_JOBS:-$(nproc)}"
COMPILER="${AUTOKERNEL_COMPILER:-clang}"
TARGET="${AUTOKERNEL_TARGET:-auto}"
BOOT_TEST_METHOD="${AUTOKERNEL_BOOT_TEST_METHOD:-qemu}"
LOCALVERSION="${AUTOKERNEL_LOCALVERSION:--autokernel-$(date -u +%Y%m%d%H%M)}"
KERNEL_ENTRY="${AUTOKERNEL_KERNEL_ENTRY:-}"
NVIDIA_MODE="${AUTOKERNEL_NVIDIA:-auto}"
TMP_ROOT="${AUTOKERNEL_TMP_ROOT:-$HOME/.local/share/autokernel/reboot-candidate/tmp}"

INSTALL=0
REBOOT=0
YES=0
NO_DEPS=0
RESCAN=0
KEEP_CONFIG=0
FORCE_DKMS=0
SUDO_KEEPALIVE_PID=""

usage() {
    cat <<'EOF'
Usage: scripts/build-reboot-candidate.sh [options]

Build/package/test only. Does not install or reboot:
  scripts/build-reboot-candidate.sh

Install the built packages and arm a one-shot GRUB boot:
  scripts/build-reboot-candidate.sh --install

Install, arm one-shot GRUB, then reboot immediately:
  scripts/build-reboot-candidate.sh --reboot --yes

Options:
  --snapshot-dir PATH      Snapshot dir (default: kernel-01)
  --kernel-source PATH     Kernel source tree (default: ~/.cache/.../linux-torvalds-v7.1-rc3)
  --jobs N                 make -j value (default: nproc)
  --compiler NAME          clang, llvm, or gcc (default: clang)
  --target TARGET          build target (default: auto, Ubuntu -> bindeb-pkg)
  --boot-test-method NAME  qemu, virtme, or auto (default: qemu)
  --localversion SUFFIX    CONFIG_LOCALVERSION suffix (default: -autokernel-YYYYmmddHHMM)
  --kernel-entry ENTRY     GRUB one-shot entry; auto-derived when omitted
  --nvidia MODE            auto, open, proprietary, or off (default: auto)
  --tmp-root PATH          TMPDIR root for package builds
  --rescan                 Re-run hardware scan before building
  --keep-config            Do not stamp CONFIG_LOCALVERSION
  --force-dkms             Pass --force-dkms to autokernel build
  --no-deps                Do not run autokernel install-deps --execute
  --install                Install packages and arm one-shot GRUB
  --reboot                 Implies --install, then reboot
  --yes, -y                Do not prompt before install/reboot
  --help, -h               Show this help

Run as your normal user. The script requests sudo only for dependency
installation, read-only scan probes, install, and reboot.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

step() {
    printf '\n==> %s\n' "$*"
}

quote_cmd() {
    printf '+'
    printf ' %q' "$@"
    printf '\n'
}

run_cmd() {
    quote_cmd "$@"
    "$@"
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

cleanup() {
    if [ -n "$SUDO_KEEPALIVE_PID" ]; then
        kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

need_value() {
    [ "$#" -ge 2 ] || die "$1 requires a value"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --snapshot-dir)
            need_value "$@"
            SNAPSHOT_DIR="$2"
            shift 2
            ;;
        --kernel-source)
            need_value "$@"
            KERNEL_SOURCE="$2"
            shift 2
            ;;
        --jobs)
            need_value "$@"
            JOBS="$2"
            shift 2
            ;;
        --compiler)
            need_value "$@"
            COMPILER="$2"
            shift 2
            ;;
        --target)
            need_value "$@"
            TARGET="$2"
            shift 2
            ;;
        --boot-test-method)
            need_value "$@"
            BOOT_TEST_METHOD="$2"
            shift 2
            ;;
        --localversion)
            need_value "$@"
            LOCALVERSION="$2"
            shift 2
            ;;
        --kernel-entry)
            need_value "$@"
            KERNEL_ENTRY="$2"
            shift 2
            ;;
        --nvidia)
            need_value "$@"
            NVIDIA_MODE="$2"
            shift 2
            ;;
        --tmp-root)
            need_value "$@"
            TMP_ROOT="$2"
            shift 2
            ;;
        --rescan)
            RESCAN=1
            shift
            ;;
        --keep-config)
            KEEP_CONFIG=1
            shift
            ;;
        --force-dkms)
            FORCE_DKMS=1
            shift
            ;;
        --no-deps)
            NO_DEPS=1
            shift
            ;;
        --install)
            INSTALL=1
            shift
            ;;
        --reboot)
            INSTALL=1
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

[ "$(id -u)" -ne 0 ] || die "run this as your normal user; sudo is used only where needed"
command -v uv >/dev/null 2>&1 || die "uv is not on PATH"
command -v sudo >/dev/null 2>&1 || die "sudo is not on PATH"

AUTOKERNEL=(uv --project "$REPO_ROOT" run autokernel)
AUTOKERNEL_BIN="$REPO_ROOT/.venv/bin/autokernel"

run_ak() {
    run_cmd "${AUTOKERNEL[@]}" "$@"
}

sudo_ak() {
    if [ -x "$AUTOKERNEL_BIN" ]; then
        run_cmd sudo env "PATH=$PATH" "HOME=$HOME" "$AUTOKERNEL_BIN" "$@"
    else
        run_cmd sudo env "PATH=$PATH" "HOME=$HOME" uv --project "$REPO_ROOT" run autokernel "$@"
    fi
}

request_sudo() {
    step "Requesting sudo credentials"
    run_cmd sudo -v
    (
        while true; do
            sleep 60
            sudo -n true 2>/dev/null || exit
        done
    ) &
    SUDO_KEEPALIVE_PID="$!"
}

set_config_string() {
    local file="$1" key="$2" value="$3" escaped
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

derive_grub_entry() {
    local kernel_release="$1" cfg="/boot/grub/grub.cfg"
    local running title_line title submenu_line submenu distro_name grub_cfg_text
    running="$(uname -r)"

    if [ -r "$cfg" ]; then
        grub_cfg_text="$(cat "$cfg")"
    elif sudo -n test -r "$cfg" 2>/dev/null; then
        grub_cfg_text="$(sudo -n cat "$cfg")"
    else
        grub_cfg_text=""
    fi

    if [ -n "$grub_cfg_text" ]; then
        title_line="$(
            printf '%s\n' "$grub_cfg_text" \
                | grep -F "Linux $running" \
                | grep "menuentry '" \
                | grep -v "recovery mode" \
                | head -n 1 || true
        )"
        if [ -n "$title_line" ]; then
            title="$(printf '%s\n' "$title_line" | sed -E "s/^[[:space:]]*menuentry '([^']+)'.*/\\1/")"
            title="${title//$running/$kernel_release}"
        fi

        submenu_line="$(printf '%s\n' "$grub_cfg_text" | grep -m 1 "submenu 'Advanced options" || true)"
        if [ -n "$submenu_line" ]; then
            submenu="$(printf '%s\n' "$submenu_line" | sed -E "s/^[[:space:]]*submenu '([^']+)'.*/\\1/")"
        fi
    fi

    if [ -n "${title:-}" ]; then
        if [ -n "${submenu:-}" ]; then
            printf '%s>%s\n' "$submenu" "$title"
        else
            printf '%s\n' "$title"
        fi
        return 0
    fi

    distro_name="Ubuntu"
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        distro_name="${NAME:-Ubuntu}"
    fi
    printf 'Advanced options for %s>%s, with Linux %s\n' "$distro_name" "$distro_name" "$kernel_release"
}

find_built_packages() {
    local marker="$1" source_parent
    source_parent="$(dirname -- "$KERNEL_SOURCE")"

    case "$TARGET" in
        auto|bindeb-pkg)
            find "$source_parent" -maxdepth 1 -type f -newer "$marker" \
                \( -name 'linux-image-*.deb' -o -name 'linux-headers-*.deb' \) \
                ! -name 'linux-image-*-dbg_*.deb' \
                -print | sort
            ;;
        rpm-pkg)
            find "$HOME/rpmbuild/RPMS" -type f -newer "$marker" -name 'kernel-*.rpm' -print 2>/dev/null | sort
            ;;
        *)
            find "$source_parent" -maxdepth 1 -type f -newer "$marker" \
                \( -name '*.deb' -o -name '*.rpm' -o -name '*.pkg.tar.zst' -o -name '*.tar.zst' \) \
                -print | sort
            ;;
    esac
}

assert_boot_test_matches_bzimage() {
    local record="$SNAPSHOT_DIR/boot-test.json"
    local bzimage="$KERNEL_SOURCE/arch/x86/boot/bzImage"
    local expected actual
    [ -r "$record" ] || die "boot-test record missing: $record"
    [ -r "$bzimage" ] || die "bzImage missing: $bzimage"
    expected="$(sha256sum "$bzimage" | awk '{print $1}')"
    actual="$(sed -n -E 's/.*"bzimage_sha256": "([^"]+)".*/\1/p' "$record" | head -n 1)"
    [ "$actual" = "$expected" ] || die "boot-test record does not match the current bzImage"
}

print_install_command() {
    local -n packages_ref="$1"
    local kernel_entry="$2"
    local -a install_cmd
    install_cmd=(sudo env "PATH=$PATH" "HOME=$HOME")
    if [ -x "$AUTOKERNEL_BIN" ]; then
        install_cmd+=("$AUTOKERNEL_BIN")
    else
        install_cmd+=(uv --project "$REPO_ROOT" run autokernel)
    fi
    install_cmd+=(install "$SNAPSHOT_DIR")
    for pkg in "${packages_ref[@]}"; do
        install_cmd+=(--package "$pkg")
    done
    install_cmd+=(--kernel-entry "$kernel_entry" --nvidia "$NVIDIA_MODE" --execute)
    quote_cmd "${install_cmd[@]}"
}

KERNEL_SOURCE="$(cd -- "$KERNEL_SOURCE" && pwd -P)"
[ -f "$KERNEL_SOURCE/Makefile" ] || die "kernel source tree has no Makefile: $KERNEL_SOURCE"

mkdir -p "$SNAPSHOT_DIR" "$TMP_ROOT"
export TMPDIR="$TMP_ROOT"

printf 'snapshot:      %s\n' "$SNAPSHOT_DIR"
printf 'kernel source: %s\n' "$KERNEL_SOURCE"
printf 'target:        %s\n' "$TARGET"
printf 'compiler:      %s\n' "$COMPILER"
printf 'jobs:          %s\n' "$JOBS"
printf 'TMPDIR:        %s\n' "$TMPDIR"

if [ "$NO_DEPS" -eq 0 ] || [ "$RESCAN" -eq 1 ] || [ "$INSTALL" -eq 1 ] || [ "$REBOOT" -eq 1 ]; then
    request_sudo
fi

step "Syncing Python environment"
run_cmd uv --project "$REPO_ROOT" sync --frozen

if [ "$NO_DEPS" -eq 0 ]; then
    step "Installing missing build, boot-test, and install dependencies"
    run_ak install-deps --execute
fi

if [ "$RESCAN" -eq 1 ] || [ ! -f "$SNAPSHOT_DIR/manifest" ]; then
    step "Scanning host with sudo-backed read-only probes"
    run_ak scan "$SNAPSHOT_DIR" --sudo-probes
fi

[ -f "$SNAPSHOT_DIR/manifest" ] || die "snapshot has no manifest: $SNAPSHOT_DIR"
[ -f "$SNAPSHOT_DIR/final.config" ] || die "snapshot has no final.config; run propose/review/apply first"

step "Build preflight"
run_ak preflight "$SNAPSHOT_DIR" --for build

step "Boot-test preflight"
run_ak preflight "$SNAPSHOT_DIR" --for boot-test --kernel-source "$KERNEL_SOURCE"

if [ "$KEEP_CONFIG" -eq 0 ]; then
    step "Stamping unique CONFIG_LOCALVERSION"
    cp "$SNAPSHOT_DIR/final.config" "$SNAPSHOT_DIR/final.config.before-reboot-candidate"
    set_config_string "$SNAPSHOT_DIR/final.config" CONFIG_LOCALVERSION "$LOCALVERSION"
    set_config_not_set "$SNAPSHOT_DIR/final.config" CONFIG_LOCALVERSION_AUTO
    printf 'CONFIG_LOCALVERSION="%s"\n' "$LOCALVERSION"
fi

BUILD_MARKER="$(mktemp)"
touch "$BUILD_MARKER"

BUILD_ARGS=(
    build "$SNAPSHOT_DIR"
    --kernel-source "$KERNEL_SOURCE"
    --localmodconfig
    --execute
    --target "$TARGET"
    --jobs "$JOBS"
    --compiler "$COMPILER"
)
if [ "$FORCE_DKMS" -eq 1 ]; then
    BUILD_ARGS+=(--force-dkms)
fi

step "Building installable kernel package"
run_ak "${BUILD_ARGS[@]}"

step "QEMU/VM boot-test"
run_ak boot-test "$SNAPSHOT_DIR" --kernel-source "$KERNEL_SOURCE" --method "$BOOT_TEST_METHOD" --timeout 120
assert_boot_test_matches_bzimage

mapfile -t PACKAGES < <(find_built_packages "$BUILD_MARKER" || true)
rm -f "$BUILD_MARKER"

[ "${#PACKAGES[@]}" -gt 0 ] || die "build succeeded, but no installable packages were found near the kernel source"

printf '\nbuilt packages:\n'
printf '  %s\n' "${PACKAGES[@]}"

KERNEL_RELEASE="$(make -s -C "$KERNEL_SOURCE" kernelrelease)"
if [ -z "$KERNEL_ENTRY" ]; then
    KERNEL_ENTRY="$(derive_grub_entry "$KERNEL_RELEASE")"
fi
printf '%s\n' "$KERNEL_ENTRY" >"$SNAPSHOT_DIR/grub-one-shot-entry"

step "Install preflight"
run_ak preflight "$SNAPSHOT_DIR" --for install

step "Install dry-run"
INSTALL_DRY_RUN=(install "$SNAPSHOT_DIR")
for pkg in "${PACKAGES[@]}"; do
    INSTALL_DRY_RUN+=(--package "$pkg")
done
INSTALL_DRY_RUN+=(--kernel-entry "$KERNEL_ENTRY" --nvidia "$NVIDIA_MODE")
run_ak "${INSTALL_DRY_RUN[@]}"

cat <<EOF

Build/package/boot-test completed.

Kernel release:
  $KERNEL_RELEASE

GRUB one-shot entry:
  $KERNEL_ENTRY

To install and arm one-shot GRUB later:
EOF
print_install_command PACKAGES "$KERNEL_ENTRY"

if [ "$INSTALL" -eq 0 ]; then
    cat <<'EOF'

No install was performed. Re-run with --install to install, or --reboot --yes
to install and immediately reboot into the one-shot kernel.
EOF
    exit 0
fi

confirm "Install these packages into /boot and arm one-shot GRUB entry '$KERNEL_ENTRY'?"
step "Installing package and arming one-shot GRUB"
SUDO_INSTALL_ARGS=(install "$SNAPSHOT_DIR")
for pkg in "${PACKAGES[@]}"; do
    SUDO_INSTALL_ARGS+=(--package "$pkg")
done
SUDO_INSTALL_ARGS+=(--kernel-entry "$KERNEL_ENTRY" --nvidia "$NVIDIA_MODE" --execute)
sudo_ak "${SUDO_INSTALL_ARGS[@]}"

cat <<EOF

Installed and armed one-shot boot.

After a successful boot into the new kernel, promote it permanently with:
EOF
quote_cmd sudo env "PATH=$PATH" "HOME=$HOME" "$AUTOKERNEL_BIN" install "$SNAPSHOT_DIR" --commit --kernel-entry "$KERNEL_ENTRY" --execute
cat <<EOF

If the new kernel fails to boot, GRUB should fall back on the next boot.
Rollback command:
EOF
quote_cmd sudo env "PATH=$PATH" "HOME=$HOME" "$AUTOKERNEL_BIN" rollback "$SNAPSHOT_DIR" --execute

if [ "$REBOOT" -eq 1 ]; then
    confirm "Reboot now into the one-shot kernel?"
    step "Rebooting"
    run_cmd sudo systemctl reboot
fi
