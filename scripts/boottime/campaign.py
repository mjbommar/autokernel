#!/usr/bin/env python3
"""campaign.py — A/B many boot-time optimizations against one shared baseline.

Boots BASE n times (reference), then each candidate n times, comparing medians.
Flags a candidate as a likely-real win only if its median improvement exceeds a
noise floor derived from the baseline spread (robust to the ~15-30ms jitter)."""
import os, statistics as st
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bootbench import boot_once  # reuse the proven boot path

IMG = os.environ.get("AK_IMG", "/dev/shm/akvm/fork.img")
KERNEL = os.environ.get("AK_KERNEL", "/data1/kbuild/linux-7.0.12/arch/x86/boot/bzImage")
N = 9

BASE = ("quiet loglevel=3 systemd.show_status=0 tsc=reliable nowatchdog "
        "TERM=linux systemd.tty.rows.console=24 systemd.tty.columns.console=80")

MASK = "systemd.mask=%s"
def masks(*units): return " ".join(MASK % u for u in units)

CANDIDATES = [
    # --- kernel cmdline boot-speed knobs ---
    ("k-rcu_expedited",   "rcupdate.rcu_expedited=1"),
    ("k-cryptomgr.notests","cryptomgr.notests"),
    ("k-rng_trust",       "random.trust_cpu=on random.trust_bootloader=on"),
    ("k-no_timer_check",  "no_timer_check"),
    ("k-audit_off",       "audit=0"),
    ("k-meminit_off",     "init_on_alloc=0 init_on_free=0"),
    ("k-noresume_nokaslr","noresume nokaslr"),
    ("k-ALL", "rcupdate.rcu_expedited=1 cryptomgr.notests random.trust_cpu=on "
              "random.trust_bootloader=on no_timer_check audit=0 "
              "init_on_alloc=0 init_on_free=0 noresume"),
    # --- systemd unit masking (no image edit needed) ---
    ("m-modprobe", masks("modprobe@drm.service","modprobe@efi_pstore.service",
                         "modprobe@configfs.service","modprobe@fuse.service")),
    ("m-e2scrub",  masks("e2scrub_reap.service")),
    ("m-pseudofs", masks("dev-hugepages.mount","dev-mqueue.mount",
                         "sys-kernel-debug.mount","sys-kernel-tracing.mount")),
    ("m-gettystatic", masks("getty-static.service")),
    ("m-ALL", masks("modprobe@drm.service","modprobe@efi_pstore.service",
                    "modprobe@configfs.service","modprobe@fuse.service",
                    "e2scrub_reap.service","dev-hugepages.mount","dev-mqueue.mount",
                    "sys-kernel-debug.mount","sys-kernel-tracing.mount",
                    "getty-static.service")),
]

def series(append, n):
    runs = []
    for i in range(n):
        r = boot_once(IMG, KERNEL, append)
        if r: runs.append(r)
    return runs

def med(runs, key="total"):
    return st.median(r[key]*1000 for r in runs) if runs else None

def main():
    print(f"BASE = {BASE}\nn={N} per arm, metric=total ms (median)\n")
    base = series(BASE, N)
    if not base:
        print("BASELINE FAILED TO BOOT"); return
    bvals = sorted(r["total"]*1000 for r in base)
    bmed = st.median(bvals)
    bspread = bvals[-1] - bvals[0]
    floor = max(12.0, bspread/2)  # noise floor: half the baseline range, >=12ms
    print(f"baseline: median={bmed:.0f}ms  min={bvals[0]:.0f}  max={bvals[-1]:.0f}  "
          f"(spread {bspread:.0f})  noise-floor=±{floor:.0f}ms\n")
    rows = []
    for name, extra in CANDIDATES:
        runs = series(BASE + " " + extra, N)
        if not runs:
            rows.append((name, None, None, "BOOT-FAIL")); continue
        m = med(runs)
        d = bmed - m
        flag = "WIN" if d >= floor else ("regress" if d <= -floor else "~noise")
        rows.append((name, m, d, flag))
        print(f"  {name:22s} total={m:5.0f}ms  Δ={d:+5.0f}ms  {flag}")
    print("\n== ranked by Δ ==")
    for name, m, d, flag in sorted([r for r in rows if r[2] is not None],
                                   key=lambda x: -x[2]):
        print(f"  {d:+6.0f}ms  {name:22s} {flag}")
    fails = [r[0] for r in rows if r[3]=="BOOT-FAIL"]
    if fails: print("BOOT-FAIL:", ", ".join(fails))

if __name__ == "__main__":
    main()
