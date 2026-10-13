#!/usr/bin/env bash
#
# Install an autokernel-built kernel (.deb pair from the autokernel cache) and
# arm a ONE-SHOT GRUB boot into it. The default entry is unchanged, so a plain
# reboot after that returns to the previous kernel. Make it permanent after
# checking it with: sudo grub-set-default "Advanced options for Ubuntu>Ubuntu, with Linux RELEASE"
#
# Originally written for the 7.3-rc5 Landlock __counted_by FORTIFY oops: when
# run from that kernel it unloads kdump and sets panic_on_oops=0 first, so a
# packaging helper hitting the oops kills only itself.
#
#   sudo bash install-kernel-oneshot.sh RELEASE [--reboot]
#     e.g. 7.3.0-rc7-autokernel-rc7+   (KC= overrides the .deb directory)
#
set -euo pipefail
REL=''
DO_REBOOT=0
for a in "$@"; do case "$a" in --reboot) DO_REBOOT=1 ;; -*) echo "unknown flag $a"; exit 1 ;; *) REL="$a" ;; esac; done
KC=${KC:-$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)/.cache/autokernel/kernels}
[ -n "$REL" ] || { echo "usage: sudo bash $0 RELEASE [--reboot]"; exit 1; }
ENTRY="Advanced options for Ubuntu>Ubuntu, with Linux $REL"
[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo bash $0 RELEASE [--reboot]"; exit 1; }

shopt -s nullglob
IMAGE=("$KC"/linux-image-"$REL"_*.deb)
HEADERS=("$KC"/linux-headers-"$REL"_*.deb)
[ ${#IMAGE[@]} -eq 1 ] && [ ${#HEADERS[@]} -eq 1 ] || { echo "expected exactly one image+headers .deb for $REL in $KC"; exit 1; }

if [[ "$(uname -r)" == 7.3.0-rc5-autokernel-202609290947 ]]; then
  echo "==> On the unpatched 7.3: unloading kdump, panic_on_oops=0 for this install"
  systemctl stop kdump-tools || true
  sysctl -q kernel.panic_on_oops=0
fi
oops0=$(dmesg | grep -c '__fortify_panic+' || true)

echo "==> Installing ${IMAGE[0]##*/} ${HEADERS[0]##*/}"
dpkg -i "${IMAGE[0]}" "${HEADERS[0]}"
[ -s "/boot/initrd.img-$REL" ] || { echo "!! /boot/initrd.img-$REL missing/empty"; exit 1; }
update-grub
grep -qF "with Linux $REL'" /boot/grub/grub.cfg || { echo "!! $REL not in grub.cfg"; exit 1; }
grub-reboot "$ENTRY"
grub-editenv list | grep next_entry

oops1=$(dmesg | grep -c '__fortify_panic+' || true)
if [ "$oops1" -gt "$oops0" ]; then
  echo "!! $((oops1-oops0)) new fortify oops lines during install:"
  dmesg | grep -E 'Comm:' | tail -3
  echo "!! check the steps above succeeded before rebooting"
fi

echo "==> One-shot armed: $ENTRY  (default boot unchanged; next reboot after that returns to it)"
echo "==> After boot:  bash $(cd "$(dirname "$0")" && pwd)/verify-landlock-fix.sh"
[ "$DO_REBOOT" -eq 1 ] && { sync; systemctl reboot; }
exit 0
