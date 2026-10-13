#!/usr/bin/env bash
# Add crash_kexec_post_notifiers to every kernel's command line, so panic
# logs are written to EFI pstore *before* kdump starts. With LUKS root the
# kdump capture kernel can fail to unlock; pstore still gets the trace
# (shows up in /var/lib/systemd/pstore/ on the next boot).
#   sudo bash enable-crash-notifiers.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
F=/etc/default/grub.d/95-autokernel-crash.cfg
cat > "$F" <<'CFG'
# autokernel: write panic logs to pstore before kdump takes over
GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT crash_kexec_post_notifiers"
CFG
echo "wrote $F"
update-grub
grep -c crash_kexec_post_notifiers /boot/grub/grub.cfg | xargs echo "grub.cfg entries with crash_kexec_post_notifiers:"
echo "takes effect on next boot; check with: grep -o crash_kexec_post_notifiers /proc/cmdline"
