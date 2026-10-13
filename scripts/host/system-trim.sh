#!/usr/bin/env bash
# One-shot system trim for mobile5 (Framework 13). Run: sudo bash system-trim.sh
# Undo instructions are printed at the end and saved next to this script.
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
UNDO=$(cd "$(dirname "$0")" && pwd)/system-trim.UNDO.txt
step(){ echo; echo "==> $*"; }

step "1. Docker/containerd on demand (docker.socket stays enabled); drop wait-online"
systemctl disable docker.service containerd.service
systemctl is-enabled docker.socket
systemctl disable NetworkManager-wait-online.service

step "2. Disable services with no use on this laptop"
systemctl disable --now ModemManager.service nvmf-autoconnect.service nvmefc-boot-connections.service \
  ubuntu-fan.service sssd.service openvpn.service postfix.service cups-browsed.service 2>&1 | grep -v '^Removed' || true

step "3. Remove kdump (pstore + crash_kexec_post_notifiers keeps panic logs)"
systemctl disable --now kdump-tools.service          # stop also unloads the crash kernel
sysctl -q kernel.panic_on_oops=0                     # back to stock: an oops kills the task, not the box
cat > /etc/default/grub.d/zz-autokernel-no-crashkernel.cfg <<'CFG'
# autokernel: kdump disabled -> don't reserve crashkernel memory (sourced after kdump-tools.cfg)
GRUB_CMDLINE_LINUX_DEFAULT="$(echo "$GRUB_CMDLINE_LINUX_DEFAULT" | sed -E 's/ ?crashkernel=[^ ]+//g')"
CFG
update-grub 2>&1 | grep -E 'Generating|done' 
echo "crashkernel= left in grub.cfg linux lines: $(grep -P '^\s*linux\s' /boot/grub/grub.cfg | grep -c crashkernel)"
echo "crash_kexec_post_notifiers in grub.cfg linux lines: $(grep -P '^\s*linux\s' /boot/grub/grub.cfg | grep -c crash_kexec_post_notifiers)"

step "State"
for u in docker.service containerd.service docker.socket NetworkManager-wait-online.service ModemManager.service \
         nvmf-autoconnect.service nvmefc-boot-connections.service ubuntu-fan.service sssd.service openvpn.service \
         postfix.service cups-browsed.service kdump-tools.service cups.service; do
  printf "  %-36s %-9s %s\n" "$u" "$(systemctl is-enabled $u 2>/dev/null)" "$(systemctl is-active $u 2>/dev/null)"
done
echo "  kexec_crash_loaded=$(cat /sys/kernel/kexec_crash_loaded) panic_on_oops=$(cat /proc/sys/kernel/panic_on_oops)"

cat > "$UNDO" <<'U'
# Undo system-trim.sh
sudo systemctl enable --now docker.service containerd.service NetworkManager-wait-online.service
sudo systemctl enable --now ModemManager nvmf-autoconnect nvmefc-boot-connections ubuntu-fan sssd openvpn postfix cups-browsed
sudo rm /etc/default/grub.d/zz-autokernel-no-crashkernel.cfg && sudo update-grub && sudo systemctl enable kdump-tools   # takes effect after reboot
# user-level (no sudo):
systemctl --user unmask spice-vdagent.service; rm ~/.config/autostart/spice-vdagent.desktop
gnome-extensions enable snapd-prompting@canonical.com snapd-search-provider@canonical.com web-search-provider@ubuntu.com ding@rastersoft.com
systemctl --user unmask ydotool.service snap.prompting-client.daemon.service; systemctl --user enable docker.service
U
chown "${SUDO_USER:-root}": "$UNDO"
echo; echo "Undo steps saved to $UNDO"
echo "Reboot when convenient: frees the ~1 GB crashkernel reservation and shows the faster boot (systemd-analyze)."
