#!/usr/bin/env bash
#
# cups-probe-bisect.sh — reproduce what GNOME Settings > Printers does on open
# (CUPS device discovery, i.e. `lpinfo -v`), one backend at a time, leaving an
# fsync'd breadcrumb trail so a hard freeze still tells us which one did it.
#
# Each backend is run with no args (= discovery mode), as the same user
# cups-deviced would use (root if not world-executable, else lp), with a timeout.
# Hardware-touching backends go last so the network-only ones are cleared first.
#
# Breadcrumbs: /var/log/cups-bisect.log   (START/END per backend, synced to disk)
#              /var/log/cups-bisect.dmesg (kernel log, synced per line)
# After a crash:  tail /var/log/cups-bisect.log /var/log/cups-bisect.dmesg
#
# Usage:
#   sudo bash cups-probe-bisect.sh               # all backends, in order
#   sudo bash cups-probe-bisect.sh usb hp        # only these
#   sudo bash cups-probe-bisect.sh --lpinfo      # the whole thing via CUPS at once
#   sudo bash cups-probe-bisect.sh --ppds        # what the Printers panel does ON OPEN:
#                                                #   CUPS_GET_PPDS -> each /usr/lib/cups/driver/* list
#
set -uo pipefail

BK=/usr/lib/cups/backend
LOG=/var/log/cups-bisect.log
KLOG=/var/log/cups-bisect.dmesg
TIMEOUT=90
ORDER_LAST="parallel serial bluetooth hp usb"   # most hardware-adjacent last

[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo bash $0 $*" >&2; exit 1; }

crumb() {
  local msg; msg="$(date '+%F %T.%N') [$(uname -r)] $*"
  echo "$msg" | tee -a "$LOG"
  sync -f "$LOG"
  logger -p user.crit -t cups-bisect "$*"
  journalctl --sync 2>/dev/null || true
}

# Kernel log mirror, fsync'd per line (journald only flushes every few minutes).
: >"$KLOG"
( dmesg -w --time-format iso | while IFS= read -r l; do echo "$l" >>"$KLOG"; sync -f "$KLOG"; done ) &
KPID=$!
trap 'kill $KPID 2>/dev/null; pkill -P $KPID 2>/dev/null' EXIT
sleep 1

run_one() {
  local b=$1 f="$BK/$1" as=root
  [ -x "$f" ] || { crumb "SKIP $b (not executable)"; return; }
  # cups-deviced runs a backend as root only if it is not executable by others.
  [ "$(( 0$(stat -Lc %a "$f") & 01 ))" -eq 1 ] && as=lp
  crumb "START $b (as $as)"
  local out rc
  if [ "$as" = root ]; then
    out=$(timeout "$TIMEOUT" "$f" 2>&1); rc=$?
  else
    out=$(timeout "$TIMEOUT" runuser -u lp -- "$f" 2>&1); rc=$?
  fi
  printf '%s\n' "$out" | sed "s/^/    [$b] /" | tee -a "$LOG"
  crumb "END $b rc=$rc$([ $rc -eq 124 ] && echo ' (TIMEOUT)')"
}

crumb "=== session start (kernel $(uname -r), cmdline: $(cat /proc/cmdline))"

if [ "${1:-}" = "--ppds" ]; then
  for d in /usr/lib/cups/driver/*; do
    b=$(basename "$d")
    crumb "START driver $b list"
    timeout "$TIMEOUT" runuser -u lp -- "$d" list >/dev/null 2>>"$LOG"
    crumb "END driver $b rc=$?"
  done
  crumb "START lpinfo -m (cups-driverd, full)"
  timeout 300 lpinfo -m >/dev/null 2>>"$LOG"
  crumb "END lpinfo -m rc=$?"
elif [ "${1:-}" = "--lpinfo" ]; then
  crumb "START lpinfo -v"
  timeout 180 lpinfo -v 2>&1 | tee -a "$LOG"
  crumb "END lpinfo -v rc=${PIPESTATUS[0]}"
elif [ $# -gt 0 ]; then
  for b in "$@"; do run_one "$b"; done
else
  first=(); for f in "$BK"/*; do
    b=$(basename "$f"); [[ " $ORDER_LAST " == *" $b "* ]] || first+=("$b")
  done
  for b in "${first[@]}" $ORDER_LAST; do run_one "$b"; done
fi

crumb "=== session done — machine survived"
