#!/usr/bin/env bash
# Post-boot check for the Landlock __counted_by fix. Runs unprivileged.
# Output is pasteable into the LKML Tested-by reply.
set -uo pipefail
R=$(cd "$(dirname "$0")" && pwd)
OUT=$(mktemp -d)
cc -O2 -o "$OUT/repro" "$R/../landlock-nested-repro.c" || exit 1
k0=$(journalctl -k -b0 -o cat | grep -cE '__fortify_panic|fortify_report' || true)

echo "kernel:    $(uname -r) ($(grep -o 'clang version [0-9.]*\|gcc[^,]*' /proc/version | head -1))"
echo "taint:     $(cat /proc/sys/kernel/tainted)"
echo "lsm:       $(cat /sys/kernel/security/lsm)"
echo
echo "--- nested landlock_restrict_self ---"
"$OUT/repro"; echo "exit=$?"
echo "--- xz 5.8 round trip (sandboxes itself with nested Landlock) ---"
echo hello | xz | xz -d; echo "exit=${PIPESTATUS[2]}"
echo "--- CUPS driver list as lp would (what GNOME Printers does on open) ---"
timeout 120 lpinfo -m >/dev/null; echo "lpinfo -m exit=$?"
sleep 1
k1=$(journalctl -k -b0 -o cat | grep -cE '__fortify_panic|fortify_report' || true)
echo
if [ "$k1" -eq "$k0" ] && [ "$k0" -eq 0 ]; then echo "RESULT: PASS (no fortify reports this boot)"
else echo "RESULT: FAIL ($k1 fortify lines in kernel log)"; journalctl -k -b0 -o cat | grep -B2 -A12 'cut here' | head -40; fi
rm -rf "$OUT"
