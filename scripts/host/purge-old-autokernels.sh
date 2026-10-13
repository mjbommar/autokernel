#!/usr/bin/env bash
# Purge every installed autokernel kernel except KEEP (default: the running one).
# Stock Ubuntu kernels (*-generic) are never touched.
#   sudo bash purge-old-autokernels.sh            # show plan, ask, purge
#   sudo bash purge-old-autokernels.sh --yes      # no prompt
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
KEEP=${KEEP:-$(uname -r)}
[[ $KEEP == *autokernel* ]] || { echo "KEEP=$KEEP is not an autokernel release; set KEEP=..."; exit 1; }
dpkg -s "linux-image-$KEEP" >/dev/null 2>&1 || { echo "linux-image-$KEEP is not installed"; exit 1; }

mapfile -t PKGS < <(dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' 'linux-image-*autokernel*' 'linux-headers-*autokernel*' 2>/dev/null \
  | awk '$1 ~ /^[ir]i/ {print $2}' | grep -v -- "-$KEEP\$" | grep -v -- "-$KEEP-dbg\$" || true)
[ ${#PKGS[@]} -gt 0 ] || { echo "nothing to purge"; exit 0; }

echo "keeping:  $KEEP  (+ all stock *-generic kernels)"
echo "default:  $(grub-editenv list 2>/dev/null | grep saved_entry || echo 'GRUB_DEFAULT=0')"
echo "purging ${#PKGS[@]} packages:"; printf '  %s\n' "${PKGS[@]}"
if [ "${1:-}" != "--yes" ]; then read -rp "proceed? [y/N] " a; [[ $a == [yY]* ]] || exit 1; fi

apt-get purge -y "${PKGS[@]}"
# kdump-tools is disabled; drop its per-kernel copies for kernels that are gone
for f in /var/lib/kdump/initrd.img-* /var/lib/kdump/{latest_sysctls,mem,size_initrd.img}-*; do
  [ -e "$f" ] || continue
  rel=${f#*/initrd.img-}; rel=${rel#*/latest_sysctls-}; rel=${rel#*/mem-}; rel=${rel#*/size_initrd.img-}
  [ -e "/boot/vmlinuz-$rel" ] || rm -f "$f"
done
# leftovers not owned by any package (e.g. modules dirs with stray files)
for d in /lib/modules/*autokernel*; do
  rel=$(basename "$d"); [ "$rel" = "$KEEP" ] || [ -e "/boot/vmlinuz-$rel" ] || rm -rf "$d"
done
update-grub >/dev/null 2>&1
echo; echo "== /boot now"; ls /boot | grep -E '^vmlinuz-' ; df -h /boot | tail -1
echo "default: $(grub-editenv list 2>/dev/null | grep saved_entry)"
