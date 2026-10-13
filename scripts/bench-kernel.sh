#!/usr/bin/env bash
# Kernel-bound A/B benchmark. Run unprivileged, on AC power, after the desktop
# has settled (~2 min after login), with the same apps open each time.
#   bash bench-kernel.sh            # 5 rounds (default)
#   ROUNDS=3 bash bench-kernel.sh
# Results: snapshots/bench/ (or $OUT)/<uname -r>.tsv  (+ .env with conditions)
set -uo pipefail
ROUNDS=${ROUNDS:-5}
OUT=${OUT:-$(cd "$(dirname "$0")/.." && pwd)/snapshots/bench}
SRC=$HOME/.cache/autokernel/kernels/linux-torvalds-v7.3-rc6-src
mkdir -p "$OUT"; R=$(uname -r); TSV=$OUT/$R.tsv; ENV=$OUT/$R.env
T=$(mktemp -d)

{
  echo "kernel=$R"; echo "date=$(date -Is)"
  echo "ac_online=$(cat /sys/class/power_supply/ACAD/online 2>/dev/null)"
  echo "power_profile=$(powerprofilesctl get 2>/dev/null)"
  echo "governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
  echo "epp=$(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null)"
  echo "sched_ext=$(cat /sys/kernel/sched_ext/root/ops 2>/dev/null || echo none)"
  echo "cmdline=$(cat /proc/cmdline)"
  echo "tainted=$(cat /proc/sys/kernel/tainted)"
  echo "boot_time=$(systemd-analyze time 2>/dev/null | head -1)"
  echo "vmlinuz_bytes=$(stat -c %s /boot/vmlinuz-$R 2>/dev/null)"
  echo "kernel_text_kb=$(awk '/Kernel code/{print}' /proc/iomem 2>/dev/null | head -1)"
  echo "loadavg_start=$(cut -d' ' -f1-3 /proc/loadavg)"
} > "$ENV"
if [ "$(cat /sys/class/power_supply/ACAD/online 2>/dev/null)" != 1 ] && [ -z "${FORCE:-}" ]; then
  echo "ABORT: not on AC power (results would not be comparable). Plug in, or FORCE=1 to override." >&2; rm -f "$ENV"; exit 1
fi

printf "bench\tround\tvalue\tunit\tbetter\n" > "$TSV"
rec(){ printf "%s\t%s\t%s\t%s\t%s\n" "$1" "$2" "$3" "$4" "$5" >> "$TSV"; printf "  %-22s r%s %12s %s\n" "$1" "$2" "$3" "$4"; }

# warm the dentry/page cache for the fs benches
git -C "$SRC" status --porcelain >/dev/null 2>&1; find "$SRC" -xdev >/dev/null 2>&1

for r in $(seq 1 "$ROUNDS"); do
  echo "round $r/$ROUNDS"
  # context switch + pipe wakeups
  v=$(perf bench -f simple sched pipe -l 500000 2>/dev/null | awk 'NR==1{print $1}'); rec sched_pipe "$r" "$v" s lower
  # hackbench-style messaging: fork + socket IPC + scheduling
  v=$(perf bench -f simple sched messaging -g 20 -l 500 2>/dev/null | awk 'NR==1{print $1}'); rec sched_messaging "$r" "$v" s lower
  # raw syscall entry/exit
  v=$(perf bench syscall basic -l 10000000 2>/dev/null | awk '/usecs\/op/{print $1}'); rec syscall_basic "$r" "$v" usec/op lower
  # futex hashing
  v=$(perf bench futex hash -r 5 2>/dev/null | awk '/Averaged/{print $2}'); rec futex_hash "$r" "$v" ops/s higher
  # VFS: stat-heavy walks (warm cache)
  s=$(date +%s.%N); find "$SRC" -xdev -printf '' 2>/dev/null; e=$(date +%s.%N); rec find_walk "$r" "$(echo "$e - $s" | bc)" s lower
  s=$(date +%s.%N); git -C "$SRC" status --porcelain >/dev/null 2>&1; e=$(date +%s.%N); rec git_status "$r" "$(echo "$e - $s" | bc)" s lower
  # fork/exec storm
  s=$(date +%s.%N); for i in $(seq 2000); do /bin/true; done; e=$(date +%s.%N); rec fork_exec_2000 "$r" "$(echo "$e - $s" | bc)" s lower
  # small-file create/write/unlink on tmpfs
  s=$(date +%s.%N); mkdir -p "$T/f"; for i in $(seq 20000); do echo x > "$T/f/$i"; done; rm -rf "$T/f"; e=$(date +%s.%N); rec tmpfs_files_20k "$r" "$(echo "$e - $s" | bc)" s lower
done

# one realistic mixed workload: allnoconfig kernel build, no ccache
echo "allnoconfig build"
B=$T/kb; make -s -C "$SRC" O="$B" LLVM=1 allnoconfig >/dev/null 2>&1
s=$(date +%s.%N); CCACHE_DISABLE=1 make -s -C "$SRC" O="$B" LLVM=1 -j"$(nproc)" vmlinux >/dev/null 2>&1; e=$(date +%s.%N)
rec kbuild_allnoconfig 1 "$(echo "$e - $s" | bc)" s lower
rm -rf "$T"
echo "loadavg_end=$(cut -d' ' -f1-3 /proc/loadavg)" >> "$ENV"
echo "ac_online_end=$(cat /sys/class/power_supply/ACAD/online 2>/dev/null)" >> "$ENV"

echo; echo "medians ($R):"
awk -F'\t' 'NR>1{v[$1]=v[$1]" "$3; u[$1]=$4} END{for(b in v){n=split(v[b],a," "); asort(a); m=(n%2)?a[(n+1)/2]:(a[n/2]+a[n/2+1])/2; printf "  %-22s %12.4f %s\n", b, m, u[b]}}' "$TSV" | sort
echo "saved: $TSV"
