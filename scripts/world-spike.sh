#!/usr/bin/env bash
# autokernel world — W0 spike (docs/WORLD.md).
#
# Proves the full source-rebuild loop on one package (zlib), end to end:
#
#   1. fetch source via deb-src (private apt state dir — the host's apt
#      config is never touched and host deb-src lines are not required)
#   2. bump version with a +ak1 local suffix (dch --local)
#   3. build in a clean sbuild unshare chroot with
#      DEB_C{,XX}FLAGS_APPEND="-march=native -O3"
#   4. audit the build log: our flags actually reached the compiler
#      (grep) and hardening flags survived (blhc)
#   5. publish to a flat, GPG-signed local repo (origin=autokernel-world)
#   6. throwaway mmdebstrap chroot: pin the repo at 1001, apt-install
#      zlib1g, assert the +ak1 version landed
#   7. exercise the rebuilt library for real: a `dpkg-deb -Zgzip`
#      build/extract round-trip (libz deflate+inflate) plus a fresh
#      `apt-get update` with the rebuilt zlib1g in place
#
# Exit criteria (WORLD.md W0): +ak1 visible in dpkg -s inside the chroot,
# flag audit passes, round-trip passes.
#
# Everything lands under $WORLD_SPIKE_DIR (default ~/.cache/autokernel/
# world-spike). The sbuild chroot tarball is cached in ~/.cache/sbuild/
# (where the real W2 builder will also look). Re-running reuses it.
set -euo pipefail

spike_dir="${WORLD_SPIKE_DIR:-$HOME/.cache/autokernel/world-spike}"
source_pkg="${WORLD_SPIKE_SOURCE:-zlib}"
binary_pkg="${WORLD_SPIKE_BINARY:-zlib1g}"
march_flags="${WORLD_SPIKE_FLAGS:--march=native -O3}"
jobs="${WORLD_SPIKE_JOBS:-$(nproc)}"

. /etc/os-release
suite="${WORLD_SPIKE_SUITE:-$VERSION_CODENAME}"
case "$ID" in
    ubuntu) mirror="${WORLD_SPIKE_MIRROR:-http://archive.ubuntu.com/ubuntu}"
            components="main universe" ;;
    debian) mirror="${WORLD_SPIKE_MIRROR:-http://deb.debian.org/debian}"
            components="main" ;;
    *)      echo "FAIL: unsupported distro id '$ID' (need ubuntu or debian)" >&2
            exit 2 ;;
esac
arch="$(dpkg --print-architecture)"

say()  { printf '\n==> %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# ── preflight ───────────────────────────────────────────────────────────────
say "preflight"

for tool in sbuild mmdebstrap dch dpkg-source apt-ftparchive blhc gpg \
            unshare newuidmap apt-get; do
    command -v "$tool" >/dev/null 2>&1 \
        || fail "missing tool: $tool — run: autokernel install-deps --for world --execute"
done

grep -q "^$(id -un):" /etc/subuid && grep -q "^$(id -un):" /etc/subgid \
    || fail "no subuid/subgid range for $(id -un); unshare-mode chroots need one (usermod --add-subuids/--add-subgids)"

# Ubuntu ≥24.04 restricts unprivileged userns by default
# (kernel.apparmor_restrict_unprivileged_userns=1), so a bare
# `unshare --user` probe fails — but mmdebstrap and sbuild ship
# AppArmor profiles granting `userns,`, so the tools themselves work.
# Accept either: unrestricted userns, or the profiles being present.
if ! unshare --user --map-root-user true 2>/dev/null; then
    restrict=$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)
    if [ "$restrict" = "1" ] \
        && grep -qs userns /etc/apparmor.d/mmdebstrap \
        && grep -qs userns /etc/apparmor.d/sbuild; then
        echo "userns: restricted, but mmdebstrap/sbuild AppArmor profiles grant it"
    else
        fail "unprivileged user namespaces unavailable and no mmdebstrap/sbuild AppArmor userns profiles found"
    fi
fi

mkdir -p "$spike_dir"
avail_kb=$(df --output=avail "$spike_dir" | tail -1)
[ "$avail_kb" -gt $((3 * 1024 * 1024)) ] \
    || fail "need ~3 GB free under $spike_dir (have $((avail_kb / 1024)) MB)"

echo "suite=$suite mirror=$mirror arch=$arch flags='$march_flags'"

# ── 1. fetch source via private apt state ──────────────────────────────────
say "fetching $source_pkg source (private apt state, no host config changes)"

apt_dir="$spike_dir/apt"
src_dir="$spike_dir/src"
rm -rf "$src_dir"
mkdir -p "$apt_dir/state/lists/partial" "$apt_dir/cache/archives/partial" \
         "$apt_dir/sources.list.d" "$src_dir"

{
    echo "deb-src $mirror $suite $components"
    echo "deb-src $mirror $suite-updates $components"
    echo "deb-src $mirror $suite-security $components"
} > "$apt_dir/sources.list"

apt_opts=(
    -o "Dir::Etc::SourceList=$apt_dir/sources.list"
    -o "Dir::Etc::SourceParts=$apt_dir/sources.list.d"
    -o "Dir::State=$apt_dir/state"
    -o "Dir::Cache=$apt_dir/cache"
    -o "Dir::State::Status=/var/lib/dpkg/status"
    -o "APT::Sandbox::User=$(id -un)"
    -o "Acquire::Languages=none"
)
apt-get "${apt_opts[@]}" update >"$spike_dir/apt-update.log" 2>&1 \
    || { tail -20 "$spike_dir/apt-update.log"; fail "apt-get update against $mirror failed"; }
(cd "$src_dir" && apt-get "${apt_opts[@]}" source "$source_pkg" \
    >"$spike_dir/apt-source.log" 2>&1) \
    || { tail -20 "$spike_dir/apt-source.log"; fail "apt-get source $source_pkg failed"; }

unpacked=$(find "$src_dir" -maxdepth 1 -mindepth 1 -type d | head -1)
[ -n "$unpacked" ] || fail "no unpacked source dir under $src_dir"
stock_version=$(cd "$unpacked" && dpkg-parsechangelog -SVersion)
echo "stock version: $stock_version"

# ── 2. +ak1 local version suffix ───────────────────────────────────────────
say "bumping version: $stock_version → ${stock_version}+ak1"
(
    cd "$unpacked"
    DEBEMAIL="world-spike@autokernel.local" DEBFULLNAME="autokernel world spike" \
        dch --local +ak "Rebuild with autokernel world flags ($march_flags)."
)
local_version=$(cd "$unpacked" && dpkg-parsechangelog -SVersion)
case "$local_version" in *+ak1) ;; *) fail "version suffix didn't take: $local_version" ;; esac

(cd "$src_dir" && dpkg-source -b "$unpacked" >"$spike_dir/dpkg-source.log" 2>&1) \
    || { tail -20 "$spike_dir/dpkg-source.log"; fail "dpkg-source -b failed"; }
dsc=$(find "$src_dir" -maxdepth 1 -name "*+ak1.dsc" | head -1)
[ -n "$dsc" ] || fail "no +ak1 .dsc produced"

# ── 3. sbuild in a clean unshare chroot ────────────────────────────────────
chroot_tarball="$HOME/.cache/sbuild/${suite}-${arch}.tar.zst"
if [ ! -e "$chroot_tarball" ]; then
    say "creating buildd chroot tarball (one-time, cached at $chroot_tarball)"
    mkdir -p "$HOME/.cache/sbuild"
    mmdebstrap --variant=buildd --mode=unshare "$suite" "$chroot_tarball" \
        "deb $mirror $suite $components" \
        "deb $mirror $suite-updates $components" \
        "deb $mirror $suite-security $components" \
        >"$spike_dir/mmdebstrap-buildd.log" 2>&1 \
        || { tail -20 "$spike_dir/mmdebstrap-buildd.log"; fail "buildd chroot creation failed"; }
fi

say "building $local_version with sbuild (unshare chroot, flags appended)"
# build_environment lands the flags inside the chroot where
# dpkg-buildflags picks them up; environment_filter would otherwise
# strip DEB_* vars passed from outside.
cat > "$spike_dir/sbuildrc" <<EOF
\$chroot_mode = 'unshare';
\$build_environment = {
    'DEB_CFLAGS_APPEND'   => '$march_flags',
    'DEB_CXXFLAGS_APPEND' => '$march_flags',
};
\$run_lintian = 0;
\$run_autopkgtest = 0;
\$run_piuparts = 0;
1;
EOF

(
    cd "$src_dir"
    SBUILD_CONFIG="$spike_dir/sbuildrc" sbuild \
        --dist="$suite" --arch="$arch" --chroot-mode=unshare \
        -j"$jobs" --no-source "$dsc" >"$spike_dir/sbuild.log" 2>&1
) || { tail -40 "$spike_dir/sbuild.log"; fail "sbuild failed (full log: $spike_dir/sbuild.log)"; }

build_log=$(find "$src_dir" -maxdepth 1 -name "${source_pkg}_*+ak1*.build" \
    -not -type l | head -1)
[ -n "$build_log" ] || build_log="$spike_dir/sbuild.log"

# ── 4. flag audit ───────────────────────────────────────────────────────────
say "flag audit"
audit_ok=1
for flag in $march_flags; do
    if grep -qF -- "$flag" "$build_log"; then
        echo "  ✓ $flag present in compiler invocations"
    else
        echo "  ✗ $flag NOT found in build log"
        audit_ok=0
    fi
done
# blhc is informational here: its findings (e.g. -fPIE missing on
# zlib's static-lib objects) are properties of the stock package too,
# so a bare pass/fail blames our rebuild for upstream behavior. The
# real W2 FlagsAudit should diff blhc output against a stock-build
# baseline and fail only on *regressions*. The hard gate in this spike
# is: our appended flags reached the compiler (grep above) and the
# distro hardening flags are still visible in the log (grep below).
if blhc --arch="$arch" --all "$build_log" >"$spike_dir/blhc.log" 2>&1; then
    echo "  ✓ blhc: no findings"
else
    echo "  ~ blhc findings (informational; compare to stock baseline in W2):"
    grep -oE "NONVERBOSE BUILD|CFLAGS missing \([^)]*\)|LDFLAGS missing \([^)]*\)|CPPFLAGS missing \([^)]*\)" \
        "$spike_dir/blhc.log" | sort | uniq -c | sed 's/^/      /'
fi
for hflag in -fstack-protector-strong -fcf-protection; do
    if grep -qF -- "$hflag" "$build_log"; then
        echo "  ✓ hardening flag survived our appends: $hflag"
    else
        echo "  ✗ hardening flag vanished: $hflag"
        audit_ok=0
    fi
done
[ "$audit_ok" = 1 ] || fail "flag audit failed (build log: $build_log)"

# ── 5. flat signed repo ─────────────────────────────────────────────────────
say "publishing to signed flat repo"
repo_dir="$spike_dir/repo"
rm -rf "$repo_dir"
mkdir -p "$repo_dir"
cp "$src_dir"/*.deb "$repo_dir/"

export GNUPGHOME="$spike_dir/gnupg"
if [ ! -d "$GNUPGHOME" ]; then
    mkdir -m 700 "$GNUPGHOME"
    echo "allow-loopback-pinentry" > "$GNUPGHOME/gpg-agent.conf"
    gpg --batch --no-tty --pinentry-mode loopback --passphrase '' \
        --quick-generate-key "autokernel world spike <world-spike@autokernel.local>" \
        ed25519 sign never >/dev/null 2>&1 || fail "gpg key generation failed"
fi
# A stale agent from a previous run may predate gpg-agent.conf; restart
# it so loopback pinentry is honored (signing has no tty here).
gpgconf --kill gpg-agent 2>/dev/null || true
gpg --batch --no-tty --yes --output "$spike_dir/world-spike-keyring.gpg" --export

(
    cd "$repo_dir"
    # Both compressed and uncompressed: apt requires the uncompressed
    # Packages checksum in the Release file to trust the index at all.
    apt-ftparchive packages . > Packages
    gzip -9kf Packages
    apt-ftparchive \
        -o APT::FTPArchive::Release::Origin=autokernel-world \
        -o APT::FTPArchive::Release::Label=autokernel-world-spike \
        -o APT::FTPArchive::Release::Architectures="$arch" \
        release . > Release
    gpg --batch --no-tty --pinentry-mode loopback --passphrase '' \
        --yes --detach-sign --armor --output Release.gpg Release
    gpg --batch --no-tty --pinentry-mode loopback --passphrase '' \
        --yes --clearsign --output InRelease Release
)
ls -l "$repo_dir"

# ── 6+7. throwaway chroot: pinned install + real libz exercise ──────────────
say "throwaway mmdebstrap chroot: pinned install of $binary_pkg $local_version"
testroot_tar="$spike_dir/testroot.tar"
rm -f "$testroot_tar"

roundtrip='set -e
mkdir -p /tmp/zt/DEBIAN /tmp/zx
printf "Package: zspike\nVersion: 1\nArchitecture: all\nMaintainer: spike <s@s>\nDescription: zlib round-trip probe\n" > /tmp/zt/DEBIAN/control
echo payload-round-trip-ok > /tmp/zt/payload
dpkg-deb -Zgzip -b /tmp/zt /tmp/z.deb
dpkg-deb -x /tmp/z.deb /tmp/zx
grep -qx payload-round-trip-ok /tmp/zx/payload'

mmdebstrap --variant=apt --mode=unshare \
    --customize-hook='mkdir -p "$1/srv/world-repo" "$1/usr/share/keyrings"' \
    --customize-hook="sync-in $repo_dir /srv/world-repo" \
    --customize-hook="copy-in $spike_dir/world-spike-keyring.gpg /usr/share/keyrings" \
    --customize-hook='echo "deb [signed-by=/usr/share/keyrings/world-spike-keyring.gpg] file:///srv/world-repo ./" > "$1/etc/apt/sources.list.d/world-spike.list"' \
    --customize-hook='printf "Package: *\nPin: release o=autokernel-world\nPin-Priority: 1001\n" > "$1/etc/apt/preferences.d/world-spike"' \
    --customize-hook='chroot "$1" apt-get update' \
    --customize-hook='chroot "$1" apt-get install -y '"$binary_pkg" \
    --customize-hook='chroot "$1" dpkg -s '"$binary_pkg" \
    --customize-hook='chroot "$1" sh -c "dpkg -s '"$binary_pkg"' | grep -q \"^Version: .*+ak1\""' \
    --customize-hook='chroot "$1" sh -c "rm -rf /var/lib/apt/lists/*; apt-get update"' \
    --customize-hook='chroot "$1" sh -c '"'$roundtrip'" \
    "$suite" "$testroot_tar" \
    "deb $mirror $suite $components" \
    "deb $mirror $suite-updates $components" \
    "deb $mirror $suite-security $components" \
    >"$spike_dir/mmdebstrap-test.log" 2>&1 \
    || { tail -40 "$spike_dir/mmdebstrap-test.log"
         fail "test chroot failed (full log: $spike_dir/mmdebstrap-test.log)"; }
rm -f "$testroot_tar"

# ── summary ─────────────────────────────────────────────────────────────────
say "W0 spike PASSED"
echo "  package:    $source_pkg $stock_version → $local_version"
echo "  flags:      $march_flags (audited in $build_log)"
echo "  repo:       $repo_dir (origin=autokernel-world, signed, pinned 1001)"
echo "  verified:   +ak1 installed via apt in throwaway chroot;"
echo "              dpkg-deb -Zgzip round-trip + fresh apt-get update"
echo "              both ran on the rebuilt libz"
