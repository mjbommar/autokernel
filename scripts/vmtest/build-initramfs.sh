#!/usr/bin/env bash
# Build a self-contained test initramfs: static busybox + static landlock repro
# + host xz and perf (with their shared libs). Output: $1 (default ./vmtest.cpio.gz)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:-$PWD/vmtest.cpio.gz}
R=$(mktemp -d); trap 'rm -rf "$R"' EXIT
mkdir -p "$R"/{bin,sbin,proc,sys,dev,tmp,etc,usr/bin,lib64}
cp /usr/bin/busybox "$R/bin/busybox"
for a in sh mount umount mkdir rm seq cat grep awk date sed echo true sleep poweroff dmesg ls wc head tail tr sync; do ln -s busybox "$R/bin/$a"; done
cc -O2 -static -o "$R/bin/llrepro" "$HERE/../landlock-nested-repro.c"
copy_with_libs(){ install -D "$1" "$R$1"; ldd "$1" | grep -oE '/[^ ]+\.so[^ ]*' | while read -r l; do install -D "$l" "$R$l"; done; }
copy_with_libs /usr/bin/xz
copy_with_libs /usr/bin/perf
copy_with_libs /usr/bin/date
copy_with_libs /usr/bin/true
install -m 755 "$HERE/init" "$R/init"
(cd "$R" && find . | cpio -o -H newc --quiet) | gzip -1 > "$OUT"
echo "built $OUT ($(du -h "$OUT" | cut -f1))"
