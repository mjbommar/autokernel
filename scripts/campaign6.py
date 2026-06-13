#!/usr/bin/env python3
"""campaign6.py — sweep QEMU machine-model / topology knobs (not cmdline).
Same noise-floored A/B method as campaign.py, but each candidate varies the
boot_once() kwargs (minimal device set, smp, mem, machine, qemu_extra) rather
than the kernel append. Append is fixed at the round-5 optimized cmdline."""
import statistics as st, sys
sys.path.insert(0, "/data1")
from bootbench import boot_once

IMG = "/dev/shm/akvm/fork.img"
KERNEL = "/data1/kbuild/linux-7.0.12/arch/x86/boot/bzImage"
N = 9

KB = "rcupdate.rcu_expedited=1 audit=0 no_timer_check random.trust_cpu=on random.trust_bootloader=on"
MB = " ".join("systemd.mask="+u for u in [
    "modprobe@drm.service","modprobe@efi_pstore.service","modprobe@configfs.service",
    "modprobe@fuse.service","e2scrub_reap.service","dev-hugepages.mount","dev-mqueue.mount",
    "sys-kernel-debug.mount","sys-kernel-tracing.mount","getty-static.service"])
APPEND = ("quiet loglevel=3 systemd.show_status=0 tsc=reliable nowatchdog TERM=linux "
          "systemd.tty.rows.console=24 systemd.tty.columns.console=80 " + KB + " " + MB)

# baseline kwargs == what the round-5 numbers were measured with
BASE_KW = dict(mem=2048, smp=2, machine=None, minimal=False, qemu_extra=None)

# each candidate overrides some kwargs (and optionally extends the append)
CANDIDATES = [
    ("minimal-devset",   dict(minimal=True)),
    ("q35",              dict(machine="q35")),
    ("q35+minimal",      dict(machine="q35", minimal=True)),
    ("smp1",             dict(smp=1)),
    ("smp1+minimal",     dict(smp=1, minimal=True)),
    ("smp4",             dict(smp=4)),
    ("mem512",           dict(mem=512)),
    ("mem512+smp1+min",  dict(mem=512, smp=1, minimal=True)),
    ("no-hpet",          dict(minimal=True, qemu_extra="-no-hpet")),
    ("cstate-cmdline",   dict(minimal=True, _append=" processor.max_cstate=1 intel_idle.max_cstate=0")),
]

def series(kw, n):
    extra_app = kw.pop("_append", "")
    runs = []
    for _ in range(n):
        r = boot_once(IMG, KERNEL, APPEND + extra_app, **kw)
        if r: runs.append(r)
    return runs

def med(runs, key="total"):
    return st.median(r[key]*1000 for r in runs) if runs else None

def main():
    print(f"n={N} per arm; append=round-5 optimized; varying QEMU kwargs\n")
    base = series(dict(BASE_KW), N)
    bvals = sorted(r["total"]*1000 for r in base)
    bmed = st.median(bvals); spread = bvals[-1]-bvals[0]
    floor = max(12.0, spread/2)
    print(f"baseline (smp2/mem2048/pc/legacy-dev): median={bmed:.0f}ms "
          f"min={bvals[0]:.0f} max={bvals[-1]:.0f} (spread {spread:.0f}) floor=±{floor:.0f}\n")
    rows=[]
    for name, over in CANDIDATES:
        kw = dict(BASE_KW); kw.update(over)
        runs = series(kw, N)
        if not runs:
            rows.append((name,None,None,"BOOT-FAIL")); print(f"  {name:18s} BOOT-FAIL"); continue
        m=med(runs); km=med(runs,"kernel"); d=bmed-m
        flag = "WIN" if d>=floor else ("regress" if d<=-floor else "~noise")
        rows.append((name,m,d,flag))
        print(f"  {name:18s} total={m:5.0f}ms (kern {km:3.0f})  Δ={d:+5.0f}ms  {flag}")
    print("\n== ranked ==")
    for name,m,d,flag in sorted([r for r in rows if r[2] is not None], key=lambda x:-x[2]):
        print(f"  {d:+6.0f}ms  {name:18s} {flag}")

if __name__ == "__main__":
    main()
