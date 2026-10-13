#!/usr/bin/env bash
# Install mobile5 sysctl hardening/tuning and apply it now.
#   sudo bash sysctl-harden.sh            # recommended set
#   sudo bash sysctl-harden.sh --mtu      # also enable TCP MTU probing (VPN/tunnel stalls)
#   sudo bash sysctl-harden.sh --undo     # remove the file (kexec lock stays until reboot)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
F=/etc/sysctl.d/61-autokernel-hardening.conf

if [ "${1:-}" = "--undo" ]; then
  rm -f "$F"; sysctl --system >/dev/null
  echo "removed $F (values revert on reboot; kexec_load_disabled cannot be cleared until reboot)"; exit 0
fi

cat > "$F" <<'CONF'
# autokernel: mobile5 hardening/tuning (see scripts/sysctl-harden.sh)
# kdump is disabled; nothing needs kexec. One-way until reboot.
kernel.kexec_load_disabled = 1
# laptop on untrusted networks: don't accept or send ICMP redirects
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
# dev: editors/file watchers on large trees
fs.inotify.max_user_watches = 524288
fs.inotify.max_user_instances = 1024
CONF
if [ "${1:-}" = "--mtu" ]; then
  printf '%s\n' '# VPN/tunnel PMTU blackholes' 'net.ipv4.tcp_mtu_probing = 1' >> "$F"
fi
echo "wrote $F"
sysctl --system >/dev/null

echo "now:"
for k in $(grep -vE '^\s*#|^\s*$' "$F" | cut -d= -f1); do
  printf "  %-42s %s\n" "$k" "$(sysctl -n "$k")"
done
printf "  %-42s %s\n" user.max_user_namespaces "$(sysctl -n user.max_user_namespaces)  (from 60-no-userns.conf)"
