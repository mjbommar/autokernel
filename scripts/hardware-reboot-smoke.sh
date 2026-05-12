#!/usr/bin/env bash
# Build and optionally one-shot boot an autokernel kernel on this host.
#
# Default path:
#   - install/check deps
#   - scan this hardware
#   - fetch source if needed
#   - use LLM optimization for bounded choice/toggle/tunable dimensions
#   - apply it
#   - set a unique CONFIG_LOCALVERSION
#   - build distro packages with --localmodconfig for module trimming
#   - boot-test the built bzImage in a VM
#
# The script does not install into /boot or reboot unless --install or --reboot
# is passed. Run this as your normal user; it will ask sudo for the steps that
# need it. Use --skip-llm only as a debugging fallback.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="${AUTOKERNEL_HW_REPO_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd -P)}"

if [ "${AUTOKERNEL_HW_SCRIPT_SNAPSHOT:-0}" != "1" ]; then
    SNAPSHOT_RUN_DIR="${AUTOKERNEL_HW_WORK_DIR:-$HOME/.local/share/autokernel/hardware-boot}/tmp"
    mkdir -p "$SNAPSHOT_RUN_DIR"
    SNAPSHOT_SCRIPT="$(mktemp "$SNAPSHOT_RUN_DIR/hardware-reboot-smoke.XXXXXX.sh")"
    cp "$0" "$SNAPSHOT_SCRIPT"
    chmod +x "$SNAPSHOT_SCRIPT"
    export AUTOKERNEL_HW_REPO_ROOT="$REPO_ROOT"
    export AUTOKERNEL_HW_SCRIPT_SNAPSHOT=1
    export AUTOKERNEL_HW_SCRIPT_COPY="$SNAPSHOT_SCRIPT"
    exec bash "$SNAPSHOT_SCRIPT" "$@"
fi

cd "$REPO_ROOT"
if [ "${AUTOKERNEL_HW_SCRIPT_SNAPSHOT:-0}" = "1" ] && [ -n "${AUTOKERNEL_HW_SCRIPT_COPY:-}" ]; then
    trap 'rm -f "$AUTOKERNEL_HW_SCRIPT_COPY"' EXIT
fi

WORK_DIR="${AUTOKERNEL_HW_WORK_DIR:-$HOME/.local/share/autokernel/hardware-boot}"
SNAPSHOT_DIR="${AUTOKERNEL_HW_SNAPSHOT_DIR:-}"
KERNEL_CACHE="${AUTOKERNEL_HW_KERNEL_CACHE:-}"
KERNEL_SOURCE="${AUTOKERNEL_HW_KERNEL_SOURCE:-}"
KERNEL_VERSION="${AUTOKERNEL_HW_KERNEL_VERSION:-$(uname -r)}"
FETCH_METHOD="${AUTOKERNEL_HW_FETCH_METHOD:-tarball}"
JOBS="${AUTOKERNEL_HW_JOBS:-$(nproc)}"
TARGET="${AUTOKERNEL_HW_TARGET:-auto}"
COMPILER="${AUTOKERNEL_HW_COMPILER:-clang}"
LOCALVERSION="${AUTOKERNEL_HW_LOCALVERSION:--autokernel-$(date -u +%Y%m%d%H%M)}"
KERNEL_ENTRY="${AUTOKERNEL_HW_KERNEL_ENTRY:-}"
DIMENSION="${AUTOKERNEL_HW_DIMENSION:-choices,toggles,tunables}"
CANDIDATE_SCOPE="${AUTOKERNEL_HW_CANDIDATE_SCOPE:-focused}"
MAX_CANDIDATES="${AUTOKERNEL_HW_MAX_CANDIDATES:-480}"
LLM_MODE="${AUTOKERNEL_HW_LLM_MODE:-auto}"
MODEL="${AUTOKERNEL_HW_MODEL:-}"
SERVICE_TIER="${AUTOKERNEL_HW_SERVICE_TIER:-}"
PRESET="${AUTOKERNEL_HW_PRESET:-}"
WORKLOAD="${AUTOKERNEL_HW_WORKLOAD:-}"
THREAT="${AUTOKERNEL_HW_THREAT:-balanced}"
MODULES_STRATEGY="${AUTOKERNEL_HW_MODULES:-distro}"
AGGRESSION="${AUTOKERNEL_HW_AGGRESSION:-aggressive}"
BOOT_TEST_METHOD="${AUTOKERNEL_HW_BOOT_TEST_METHOD:-qemu}"
NVIDIA_MODE="${AUTOKERNEL_HW_NVIDIA:-auto}"

SKIP_LLM=0
INSTALL=0
REBOOT=0
YES=0
NO_DEPS=0
ALLOW_SECURE_BOOT=0
SUDO_KEEPALIVE_PID=""

usage() {
    cat <<'EOF'
Usage: scripts/hardware-reboot-smoke.sh [options]

Safe default:
  scripts/hardware-reboot-smoke.sh

Actually install the built package and arm a one-shot GRUB boot:
  scripts/hardware-reboot-smoke.sh --install

Install, arm one-shot GRUB, then reboot immediately:
  scripts/hardware-reboot-smoke.sh --install --reboot

Options:
  --work-dir PATH          Work directory (default: ~/.local/share/autokernel/hardware-boot)
  --snapshot-dir PATH      Snapshot/artifact directory (default: WORK_DIR/snapshot)
  --kernel-cache PATH      Source cache directory (default: WORK_DIR/kernels)
  --kernel-source PATH     Existing kernel source tree; skips fetch-source
  --kernel-version REL     Version to fetch when --kernel-source is omitted (default: uname -r)
  --fetch-method METHOD    autokernel fetch-source method (default: tarball)
  --jobs N                make -j value (default: nproc)
  --target TARGET          build target (default: auto; Ubuntu resolves to bindeb-pkg)
  --compiler NAME          clang, llvm, or gcc (default: clang)
  --localversion SUFFIX    CONFIG_LOCALVERSION suffix (default: -autokernel-YYYYmmddHHMM)
  --kernel-entry ENTRY     GRUB entry for one-shot boot; auto-derived when omitted
  --dimension VALUE        modules, choices, toggles, tunables, or all (default: choices,toggles,tunables)
  --candidate-scope SCOPE  focused or all when module LLM is enabled (default: focused)
  --max-candidates N       Cost guard only when module LLM is enabled; 0 means no cap (default: 480)
  --llm-mode MODE          auto, cheap, fast, or quality (default: auto)
  --model MODEL            Literal pydantic-ai model id; overrides --llm-mode
  --service-tier TIER      Provider service tier, e.g. OpenAI flex/priority/auto
  --preset NAME            Optional autokernel preset; per-axis flags still apply
  --workload NAME          desktop, laptop, server, vm-guest, realtime, embedded
  --threat NAME            permissive, balanced, paranoid (default: balanced)
  --modules NAME           distro, monolithic, modular (default: distro)
  --aggression NAME        conservative, balanced, aggressive (default: aggressive)
  --boot-test-method NAME  qemu, virtme, or auto (default: qemu)
  --nvidia MODE            auto, open, proprietary, or off (default: auto)
  --skip-llm               Debug fallback: deterministic-only proposal
  --no-deps                Skip autokernel install-deps --execute
  --install                Install the package and arm one-shot GRUB
  --reboot                 Reboot after a successful --install
  --yes, -y                Do not prompt before install/reboot
  --allow-secure-boot      Do not abort when mokutil reports Secure Boot enabled
  --help, -h               Show this help

Environment variables mirror the AUTOKERNEL_HW_* option names.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

step() {
    printf '\n==> %s\n' "$*"
}

run_cmd() {
    printf '+'
    printf ' %q' "$@"
    printf '\n'
    "$@"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --work-dir)
            WORK_DIR="$2"
            shift 2
            ;;
        --snapshot-dir)
            SNAPSHOT_DIR="$2"
            shift 2
            ;;
        --kernel-cache)
            KERNEL_CACHE="$2"
            shift 2
            ;;
        --kernel-source)
            KERNEL_SOURCE="$2"
            shift 2
            ;;
        --kernel-version)
            KERNEL_VERSION="$2"
            shift 2
            ;;
        --fetch-method)
            FETCH_METHOD="$2"
            shift 2
            ;;
        --jobs)
            JOBS="$2"
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
        --localversion)
            LOCALVERSION="$2"
            shift 2
            ;;
        --kernel-entry)
            KERNEL_ENTRY="$2"
            shift 2
            ;;
        --dimension)
            DIMENSION="$2"
            shift 2
            ;;
        --candidate-scope)
            CANDIDATE_SCOPE="$2"
            shift 2
            ;;
        --max-candidates)
            MAX_CANDIDATES="$2"
            shift 2
            ;;
        --llm-mode)
            LLM_MODE="$2"
            shift 2
            ;;
        --model)
            MODEL="$2"
            shift 2
            ;;
        --service-tier)
            SERVICE_TIER="$2"
            shift 2
            ;;
        --preset)
            PRESET="$2"
            shift 2
            ;;
        --workload)
            WORKLOAD="$2"
            shift 2
            ;;
        --threat)
            THREAT="$2"
            shift 2
            ;;
        --modules)
            MODULES_STRATEGY="$2"
            shift 2
            ;;
        --aggression)
            AGGRESSION="$2"
            shift 2
            ;;
        --boot-test-method)
            BOOT_TEST_METHOD="$2"
            shift 2
            ;;
        --nvidia)
            NVIDIA_MODE="$2"
            shift 2
            ;;
        --skip-llm)
            SKIP_LLM=1
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
        --allow-secure-boot)
            ALLOW_SECURE_BOOT=1
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

SNAPSHOT_DIR="${SNAPSHOT_DIR:-$WORK_DIR/snapshot}"
KERNEL_CACHE="${KERNEL_CACHE:-$WORK_DIR/kernels}"

if [ "$(id -u)" -eq 0 ]; then
    die "run this as your normal user; the script invokes sudo only where needed"
fi

command -v uv >/dev/null 2>&1 || die "uv is not on PATH"
command -v sudo >/dev/null 2>&1 || die "sudo is not on PATH"

AUTOKERNEL=(uv --project "$REPO_ROOT" run autokernel)
AUTOKERNEL_VENV_BIN="$REPO_ROOT/.venv/bin/autokernel"

run_ak() {
    run_cmd "${AUTOKERNEL[@]}" "$@"
}

sudo_ak() {
    if [ -x "$AUTOKERNEL_VENV_BIN" ]; then
        run_cmd sudo env "PATH=$PATH" "HOME=$HOME" "$AUTOKERNEL_VENV_BIN" "$@"
    else
        run_cmd sudo env "PATH=$PATH" "HOME=$HOME" uv --project "$REPO_ROOT" run autokernel "$@"
    fi
}

cleanup() {
    if [ -n "$SUDO_KEEPALIVE_PID" ]; then
        kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    fi
    if [ "${AUTOKERNEL_HW_SCRIPT_SNAPSHOT:-0}" = "1" ] && [ -n "${AUTOKERNEL_HW_SCRIPT_COPY:-}" ]; then
        rm -f "$AUTOKERNEL_HW_SCRIPT_COPY"
    fi
}
trap cleanup EXIT INT TERM

require_sudo() {
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

confirm() {
    local prompt="$1"
    local answer
    if [ "$YES" -eq 1 ]; then
        return 0
    fi
    printf '%s [y/N] ' "$prompt" >&2
    read -r answer
    case "$answer" in
        y|Y|yes|YES)
            return 0
            ;;
        *)
            die "cancelled"
            ;;
    esac
}

check_secure_boot() {
    if [ "$ALLOW_SECURE_BOOT" -eq 1 ]; then
        return 0
    fi
    if command -v mokutil >/dev/null 2>&1 && mokutil --sb-state 2>/dev/null | grep -q "SecureBoot enabled"; then
        die "Secure Boot is enabled. Disable it, enroll/sign the kernel, or rerun with --allow-secure-boot."
    fi
}

source_name_for_release() {
    local release_base major minor patch
    release_base="${1%%-*}"
    IFS=. read -r major minor patch _ <<<"$release_base"
    [ -n "${major:-}" ] && [ -n "${minor:-}" ] || return 1
    patch="${patch:-0}"
    if [ "$patch" = "0" ]; then
        printf 'linux-%s.%s\n' "$major" "$minor"
    else
        printf 'linux-%s.%s.%s\n' "$major" "$minor" "$patch"
    fi
}

resolve_source_dir() {
    local expected latest
    expected="$(source_name_for_release "$KERNEL_VERSION" || true)"
    if [ -n "$expected" ] && [ -f "$KERNEL_CACHE/$expected/Makefile" ]; then
        printf '%s\n' "$KERNEL_CACHE/$expected"
        return 0
    fi
    latest="$(
        find "$KERNEL_CACHE" -mindepth 2 -maxdepth 2 -type f -name Makefile -printf '%T@ %h\n' 2>/dev/null \
            | sort -nr \
            | awk 'NR == 1 { $1=""; sub(/^ /, ""); print }'
    )"
    [ -n "$latest" ] && [ -f "$latest/Makefile" ] || return 1
    printf '%s\n' "$latest"
}

resolve_or_die_after_fetch() {
    local expected candidate
    expected="$(source_name_for_release "$KERNEL_VERSION" || true)"
    if [ -n "$expected" ]; then
        candidate="$KERNEL_CACHE/$expected"
        if [ -f "$candidate/Makefile" ]; then
            KERNEL_SOURCE="$(cd -- "$candidate" && pwd -P)"
            return 0
        fi
    fi

    if KERNEL_SOURCE="$(resolve_source_dir)"; then
        KERNEL_SOURCE="$(cd -- "$KERNEL_SOURCE" && pwd -P)"
        return 0
    fi

    printf 'kernel cache contents under %s:\n' "$KERNEL_CACHE" >&2
    find "$KERNEL_CACHE" -maxdepth 2 -type f -name Makefile -print >&2 2>/dev/null || true
    die "could not locate fetched kernel source under $KERNEL_CACHE"
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

derive_grub_entry() {
    local kernel_release="$1"
    local cfg="/boot/grub/grub.cfg"
    local current grub_cfg_text title_line title submenu_line submenu distro_name
    current="$(uname -r)"

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
                | grep -F "Linux $current" \
                | grep "menuentry '" \
                | grep -v "recovery mode" \
                | head -n 1 || true
        )"
        if [ -n "$title_line" ]; then
            title="$(printf '%s\n' "$title_line" | sed -E "s/^[[:space:]]*menuentry '([^']+)'.*/\\1/")"
            title="${title//$current/$kernel_release}"
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

    distro_name="$(
        if [ -r /etc/os-release ]; then
            . /etc/os-release
        fi
        printf '%s\n' "${NAME:-Linux}"
    )"
    printf 'Advanced options for %s>%s, with Linux %s\n' "$distro_name" "$distro_name" "$kernel_release"
}

find_built_packages() {
    local marker="$1"
    local source_parent
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
            return 1
            ;;
    esac
}

print_cmd() {
    printf '  '
    printf '%q ' "$@"
    printf '\n'
}

assert_boot_test_matches_bzimage() {
    local record="$SNAPSHOT_DIR/boot-test.json"
    local bzimage="$KERNEL_SOURCE/arch/x86/boot/bzImage"
    local actual expected
    [ -r "$record" ] || die "boot-test record missing: $record"
    [ -r "$bzimage" ] || die "bzImage missing: $bzimage"
    expected="$(sha256sum "$bzimage" | awk '{print $1}')"
    actual="$(sed -n -E 's/.*"bzimage_sha256": "([^"]+)".*/\1/p' "$record" | head -n 1)"
    [ "$actual" = "$expected" ] || die "boot-test record does not match the current bzImage"
}

mkdir -p "$WORK_DIR" "$SNAPSHOT_DIR" "$KERNEL_CACHE"
mkdir -p "$WORK_DIR/tmp"
export TMPDIR="$WORK_DIR/tmp"
printf 'work dir: %s\n' "$WORK_DIR"
printf 'TMPDIR:   %s\n' "$TMPDIR"

if [ "$NO_DEPS" -eq 0 ] || [ "$INSTALL" -eq 1 ] || [ "$REBOOT" -eq 1 ]; then
    require_sudo
fi

check_secure_boot

step "Syncing Python environment"
run_cmd uv --project "$REPO_ROOT" sync --frozen

step "Host preflight"
run_ak preflight --for all

if [ "$NO_DEPS" -eq 0 ]; then
    step "Installing missing build, boot-test, and install dependencies"
    run_ak install-deps --execute
fi

step "Scanning this host"
run_ak scan "$SNAPSHOT_DIR"

step "Snapshot-aware preflight"
run_ak preflight "$SNAPSHOT_DIR" --for all

if [ -z "$KERNEL_SOURCE" ]; then
    step "Fetching kernel source"
    run_ak fetch-source --kernel-version "$KERNEL_VERSION" --method "$FETCH_METHOD" --out "$KERNEL_CACHE"
    resolve_or_die_after_fetch
else
    KERNEL_SOURCE="$(cd -- "$KERNEL_SOURCE" && pwd -P)"
fi
[ -f "$KERNEL_SOURCE/Makefile" ] || die "kernel source tree has no Makefile: $KERNEL_SOURCE"
printf 'kernel source: %s\n' "$KERNEL_SOURCE"

step "Generating config proposal"
PROPOSE_ARGS=(
    propose "$SNAPSHOT_DIR"
    --dimension "$DIMENSION"
    --candidate-scope "$CANDIDATE_SCOPE"
    --kernel-source "$KERNEL_SOURCE"
    --max-candidates "$MAX_CANDIDATES"
    --llm-mode "$LLM_MODE"
    --threat "$THREAT"
    --modules "$MODULES_STRATEGY"
    --aggression "$AGGRESSION"
)
if [ -n "$MODEL" ]; then
    PROPOSE_ARGS+=(--model "$MODEL")
fi
if [ -n "$SERVICE_TIER" ]; then
    PROPOSE_ARGS+=(--service-tier "$SERVICE_TIER")
fi
if [ -n "$PRESET" ]; then
    PROPOSE_ARGS+=(--preset "$PRESET")
fi
if [ -n "$WORKLOAD" ]; then
    PROPOSE_ARGS+=(--workload "$WORKLOAD")
fi
if [ "$SKIP_LLM" -eq 1 ]; then
    PROPOSE_ARGS+=(--skip-llm)
    printf 'warning: --skip-llm selected; this bypasses the optimizer and is only for debugging.\n' >&2
fi
run_ak "${PROPOSE_ARGS[@]}"

step "Reviewing and applying policy rules"
run_ak review "$SNAPSHOT_DIR" \
    --reject-subsystem crypto \
    --reject-subsystem security \
    --reject-subsystem kasan \
    --accept-recommended
run_ak apply "$SNAPSHOT_DIR"

step "Stamping a unique kernel localversion"
set_config_string "$SNAPSHOT_DIR/final.config" CONFIG_LOCALVERSION "$LOCALVERSION"
set_config_not_set "$SNAPSHOT_DIR/final.config" CONFIG_LOCALVERSION_AUTO
printf 'CONFIG_LOCALVERSION="%s"\n' "$LOCALVERSION"

BUILD_MARKER="$(mktemp)"
touch "$BUILD_MARKER"

step "Building kernel package with localmodconfig"
run_ak build "$SNAPSHOT_DIR" \
    --kernel-source "$KERNEL_SOURCE" \
    --localmodconfig \
    --execute \
    --target "$TARGET" \
    --jobs "$JOBS" \
    --compiler "$COMPILER"

step "Boot-testing built kernel in a VM"
run_ak boot-test "$SNAPSHOT_DIR" --kernel-source "$KERNEL_SOURCE" --method "$BOOT_TEST_METHOD" --timeout 120
assert_boot_test_matches_bzimage

mapfile -t PACKAGES < <(find_built_packages "$BUILD_MARKER" || true)
rm -f "$BUILD_MARKER"

if [ "${#PACKAGES[@]}" -gt 0 ]; then
    printf '\nbuilt packages:\n'
    printf '  %s\n' "${PACKAGES[@]}"
else
    printf '\nno installable packages found for target=%s\n' "$TARGET"
fi

if [ "$INSTALL" -eq 0 ]; then
    INSTALL_KERNEL_RELEASE="$(make -s -C "$KERNEL_SOURCE" kernelrelease)"
    INSTALL_KERNEL_ENTRY="$KERNEL_ENTRY"
    if [ -z "$INSTALL_KERNEL_ENTRY" ]; then
        INSTALL_KERNEL_ENTRY="$(derive_grub_entry "$INSTALL_KERNEL_RELEASE" || true)"
    fi
    if [ -n "$INSTALL_KERNEL_ENTRY" ]; then
        printf '%s\n' "$INSTALL_KERNEL_ENTRY" >"$SNAPSHOT_DIR/grub-one-shot-entry"
    fi
    INSTALL_CMD=(sudo env "PATH=$PATH" "HOME=$HOME" "$AUTOKERNEL_VENV_BIN" install "$SNAPSHOT_DIR")
    for pkg in "${PACKAGES[@]}"; do
        INSTALL_CMD+=(--package "$pkg")
    done
    if [ -n "$INSTALL_KERNEL_ENTRY" ]; then
        INSTALL_CMD+=(--kernel-entry "$INSTALL_KERNEL_ENTRY")
    fi
    INSTALL_CMD+=(--nvidia "$NVIDIA_MODE")
    INSTALL_CMD+=(--execute)

    cat <<EOF

Build and VM boot-test completed without installing into /boot.

EOF
    if [ "${#PACKAGES[@]}" -eq 0 ]; then
        cat <<EOF
No install command was generated because no installable packages were found.
EOF
        exit 0
    fi

    cat <<EOF
To install these already-built packages and arm one-shot GRUB:
EOF
    print_cmd "${INSTALL_CMD[@]}"
    cat <<EOF

To reboot immediately after install:
EOF
    print_cmd "${INSTALL_CMD[@]}"
    printf '  sudo systemctl reboot\n'
    exit 0
fi

[ "${#PACKAGES[@]}" -gt 0 ] || die "install requested but no installable packages were found"

KERNEL_RELEASE="$(make -s -C "$KERNEL_SOURCE" kernelrelease)"
if [ -z "$KERNEL_ENTRY" ]; then
    KERNEL_ENTRY="$(derive_grub_entry "$KERNEL_RELEASE")" || die "could not derive a GRUB entry; pass --kernel-entry"
fi
printf '%s\n' "$KERNEL_ENTRY" >"$SNAPSHOT_DIR/grub-one-shot-entry"

PACKAGE_ARGS=()
for pkg in "${PACKAGES[@]}"; do
    PACKAGE_ARGS+=(--package "$pkg")
done

step "Dry-run install plan"
run_ak install "$SNAPSHOT_DIR" "${PACKAGE_ARGS[@]}" --kernel-entry "$KERNEL_ENTRY" --nvidia "$NVIDIA_MODE"

confirm "Install these packages into /boot and arm one-shot GRUB entry '$KERNEL_ENTRY'?"

step "Installing package and arming one-shot GRUB"
sudo_ak install "$SNAPSHOT_DIR" "${PACKAGE_ARGS[@]}" --kernel-entry "$KERNEL_ENTRY" --nvidia "$NVIDIA_MODE" --execute

cat <<EOF

Installed and armed one-shot boot:
  $KERNEL_ENTRY

After a successful boot into the new kernel, promote it permanently with:
  sudo env PATH="\$PATH" HOME="\$HOME" "$AUTOKERNEL_VENV_BIN" install "$SNAPSHOT_DIR" --commit --kernel-entry "$KERNEL_ENTRY" --execute

If the new kernel fails to boot, GRUB should fall back on the following boot.
Rollback command:
  sudo env PATH="\$PATH" HOME="\$HOME" "$AUTOKERNEL_VENV_BIN" rollback "$SNAPSHOT_DIR" --execute
EOF

if [ "$REBOOT" -eq 1 ]; then
    confirm "Reboot now into the one-shot kernel?"
    step "Rebooting"
    run_cmd sudo systemctl reboot
fi
