#!/usr/bin/env bash
# Verify the Ubuntu AppArmor userns restriction on an autokernel build that
# carries patches/ubuntu-apparmor-userns. Runs unprivileged.
set -uo pipefail
pass=0; fail=0
ok(){ echo "  PASS  $*"; pass=$((pass+1)); }
bad(){ echo "  FAIL  $*"; fail=$((fail+1)); }

echo "kernel: $(uname -r)"
for k in apparmor_restrict_unprivileged_userns apparmor_restrict_unprivileged_unconfined \
         apparmor_restrict_unprivileged_userns_force apparmor_restrict_unprivileged_userns_complain; do
  printf "  %-48s %s\n" "kernel.$k" "$(cat /proc/sys/kernel/$k 2>/dev/null || echo MISSING)"
done
[ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null)" = 1 ] \
  && ok "restriction enabled (from /usr/lib/sysctl.d/10-apparmor.conf)" \
  || bad "kernel.apparmor_restrict_unprivileged_userns is not 1"
[ -e /sys/kernel/security/apparmor/features/namespaces/userns_create ] \
  && ok "AppArmor exposes namespaces/userns_create" || bad "features/namespaces/userns_create missing"

echo "-- unconfined process: userns allowed but capabilities stripped"
if unshare -U true 2>/dev/null; then ok "unshare -U succeeds (userns creation itself still allowed)"
else bad "unshare -U denied outright (is the unprivileged_userns profile loaded?)"; fi
# Same as stock Ubuntu: the confined userns cannot even map root (no CAP_SETUID),
# so `unshare -r` fails at the uid_map write.
if unshare -Ur true 2>/dev/null; then bad "unshare -Ur got root mapping (restriction NOT effective)"
else ok "unshare -Ur cannot map root (matches stock Ubuntu)"; fi
if unshare -Urn sh -c 'ip link set lo up' 2>/dev/null; then bad "CAP_NET_ADMIN usable inside userns (restriction NOT effective)"
else ok "CAP_NET_ADMIN denied inside userns"; fi
if unshare -Urn sh -c 'nft add table inet t' 2>/dev/null; then bad "nf_tables reachable from unprivileged userns"
else ok "nf_tables NOT reachable from unprivileged userns"; fi

echo "-- system services that sandbox with user namespaces"
for u in power-profiles-daemon upower colord; do
  [ "$(systemctl is-active $u)" = active ] && ok "$u active" || bad "$u $(systemctl is-active $u)"
done
journalctl -b0 --no-pager -o cat 2>/dev/null | grep -q 'Failed to set up user namespacing' \
  && bad "some unit failed to set up user namespacing this boot" || ok "no 'Failed to set up user namespacing' this boot"

echo "-- AppArmor audit (denials/transitions this boot)"
journalctl -k -b0 --no-pager -o cat | grep -E 'apparmor=.*(userns|unprivileged_userns)' | sed -E 's/.*(operation="[^"]+").*(profile="[^"]+").*(comm="[^"]+").*/  \1 \2 \3/' | sort | uniq -c | head -10

echo "-- now open Chrome (and check chrome://sandbox), Ghostty, VS Code; they must start normally"
echo "RESULT: $pass pass, $fail fail"
[ $fail -eq 0 ]
