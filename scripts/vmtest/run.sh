#!/usr/bin/env bash
# Boot each kernel image in QEMU/KVM with the test initramfs; interleave
# kernels across passes to cancel host drift. Results -> $OUTDIR/*.log + summary.
#   run.sh [-p PASSES] [-r ROUNDS] name=/path/to/bzImage ...
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
PASSES=3; ROUNDS=3
while getopts p:r: o; do case $o in p) PASSES=$OPTARG;; r) ROUNDS=$OPTARG;; *) exit 2;; esac; done; shift $((OPTIND-1))
OUTDIR=${OUTDIR:-$HERE/../../snapshots/vmtest/$(date +%Y%m%dT%H%M%S)}
mkdir -p "$OUTDIR"; INITRD=$OUTDIR/vmtest.cpio.gz
bash "$HERE/build-initramfs.sh" "$INITRD" >/dev/null
for p in $(seq 1 "$PASSES"); do for kv in "$@"; do
  n=${kv%%=*}; img=${kv#*=}
  timeout 300 taskset -c "${PIN:-0,1,3,6}" qemu-system-x86_64 -M q35 -enable-kvm -cpu host -smp 4 -m 2048 \
    -kernel "$img" -initrd "$INITRD" -display none -monitor none -no-reboot \
    -serial file:"$OUTDIR/$n.pass$p.console" -serial file:"$OUTDIR/$n.pass$p.log" \
    -append "console=ttyS0 quiet panic=-1 vmt_rounds=$ROUNDS" >/dev/null 2>&1
  printf "pass %s %-10s %s\n" "$p" "$n" "$(grep -c '^CHECK.*PASS' "$OUTDIR/$n.pass$p.log") pass, $(grep -c '^CHECK.*FAIL' "$OUTDIR/$n.pass$p.log") fail, done=$(grep -c VMTEST_DONE "$OUTDIR/$n.pass$p.log")"
done; done
python3 -I "$HERE/summarize.py" "$OUTDIR" | tee "$OUTDIR/summary.txt"
