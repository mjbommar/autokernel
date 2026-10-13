#!/usr/bin/env bash
# Build the rc6 toolchain A/B variants out-of-tree from the patched clean worktree.
#   base    : clang + GNU ld        (CC=clang)
#   llvm    : clang + lld + llvm-*  (LLVM=1)
#   thinlto : LLVM=1 + CONFIG_LTO_CLANG_THIN=y
# Configs: snapshots/fw13-7.3-rc6/variants/<v>.config  (already olddefconfig'd)
# ccache is used, so build times are NOT comparable across variants.
set -uo pipefail
K=$HOME/.cache/autokernel/kernels
SRC=$K/linux-torvalds-v7.3-rc6-src
S=$HOME/projects/personal/autokernel/snapshots/fw13-7.3-rc6
LOGDIR=$S/build; mkdir -p "$LOGDIR"
export KBUILD_BUILD_HOST=autokernel KBUILD_BUILD_USER=autokernel
for v in "${@:-base llvm thinlto}"; do for v in $v; do
  O=$K/build-rc6-$v
  case $v in base) MK=(CC="ccache clang" HOSTCC=clang);; *) MK=(LLVM=1 CC="ccache clang");; esac
  cp "$S/variants/$v.config" "$O/.config"
  echo "=== $v start $(date +%T)"
  t0=$(date +%s)
  make -C "$SRC" O="$O" -j"$(nproc)" "${MK[@]}" bindeb-pkg > "$LOGDIR/$v.log" 2>&1; rc=$?
  echo "=== $v rc=$rc $(( $(date +%s)-t0 ))s release=$(cat $O/include/config/kernel.release 2>/dev/null)"
  [ $rc -eq 0 ] || tail -20 "$LOGDIR/$v.log"
done; done
ls -la $K/*.deb | grep -E 'rc6-autokernel-[0-9]+-(base|llvm|thinlto)' | grep -v dbg
