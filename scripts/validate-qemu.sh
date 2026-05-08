#!/usr/bin/env bash
set -euo pipefail

required="${AUTOKERNEL_QEMU_REQUIRED:-0}"
kernel="${AUTOKERNEL_QEMU_KERNEL:-}"
kernel_source="${AUTOKERNEL_QEMU_KERNEL_SOURCE:-}"
timeout="${AUTOKERNEL_QEMU_TIMEOUT:-45}"
snapshot_dir="${AUTOKERNEL_QEMU_SNAPSHOT_DIR:-}"
input="${1:-}"

if [ -n "$input" ]; then
    if [ -d "$input" ]; then
        kernel_source="$input"
    else
        kernel="$input"
    fi
fi

if ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
    if [ "$required" = "1" ]; then
        echo "qemu-system-x86_64 not found" >&2
        exit 1
    fi
    echo "qemu-system-x86_64 not found; skipping QEMU smoke validation" >&2
    exit 0
fi

if [ -z "$kernel" ] && [ -n "$kernel_source" ]; then
    for candidate in \
        "$kernel_source/arch/x86/boot/bzImage" \
        "$kernel_source/arch/x86_64/boot/bzImage" \
        "$kernel_source/vmlinux"; do
        if [ -r "$candidate" ]; then
            kernel="$candidate"
            break
        fi
    done
fi

if [ -z "$kernel" ]; then
    for candidate in \
        "./arch/x86/boot/bzImage" \
        "./arch/x86_64/boot/bzImage" \
        "./vmlinux" \
        "/boot/vmlinuz-$(uname -r)" \
        "/boot/vmlinuz" \
        "/boot/bzImage-$(uname -r)" \
        "/boot/bzImage"; do
        if [ -r "$candidate" ]; then
            kernel="$candidate"
            break
        fi
    done
fi

if [ -z "$kernel" ] || [ ! -r "$kernel" ]; then
    if [ "$required" = "1" ]; then
        echo "no readable kernel image found; pass a bzImage path, pass a kernel source tree, or set AUTOKERNEL_QEMU_KERNEL" >&2
        exit 1
    fi
    echo "no readable kernel image found; skipping QEMU smoke validation" >&2
    exit 0
fi

if [ -z "$snapshot_dir" ]; then
    snapshot_dir="$(mktemp -d)"
fi
mkdir -p "$snapshot_dir"

uv run python - "$kernel" "$snapshot_dir" "$(uname -r)" "$timeout" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

from autokernel.boottest import Method, execute, plan

kernel = Path(sys.argv[1])
snapshot_dir = Path(sys.argv[2])
release = sys.argv[3]
timeout = float(sys.argv[4])

test_plan = plan(
    method=Method.QEMU,
    bzimage_path=kernel,
    kernel_release=release,
    timeout=timeout,
)
result = execute(test_plan, snapshot_dir=snapshot_dir)
print(f"qemu verdict: {result.verdict.ok} - {result.verdict.reason}")
print(f"serial log: {result.serial_log_path}")
if not result.verdict.ok:
    tail = result.serial_log_path.read_text(errors="replace")[-4000:]
    print(tail)
    raise SystemExit(1)
PY
