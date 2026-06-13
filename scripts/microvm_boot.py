#!/usr/bin/env python3
"""microvm_boot.py — boot the fork rootfs under QEMU's microvm machine (virtio-mmio,
no PCI/ACPI) and report systemd-analyze (kernel,userspace,total). Compares vs the
q35/pc path. Machine knobs are parametrized so we can iterate if SMP/serial misbehave."""
import os, re, select, statistics as st, subprocess, sys, time

IMG = "/dev/shm/akvm/fork.img"
CSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]"); OSC = re.compile(rb"\x1b\][0-9].*?(?:\x07|\x1b\\)")
strip = lambda b: CSI.sub(b"", OSC.sub(b"", b))
FIN = re.compile(r"Startup finished in (?:([\d.]+)(m?s) \(kernel\) \+ )?([\d.]+)(m?s) \(userspace\) = ([\d.]+)(m?s)")
def _sec(v,u): return float(v)/1000 if u=="ms" else float(v)

KB = "rcupdate.rcu_expedited=1 audit=0 no_timer_check random.trust_cpu=on random.trust_bootloader=on"
MB = " ".join("systemd.mask="+u for u in ["modprobe@drm.service","modprobe@efi_pstore.service",
  "modprobe@configfs.service","modprobe@fuse.service","e2scrub_reap.service","dev-hugepages.mount",
  "dev-mqueue.mount","sys-kernel-debug.mount","sys-kernel-tracing.mount","getty-static.service"])
APP = ("quiet loglevel=3 systemd.show_status=0 tsc=reliable nowatchdog TERM=linux "
       "systemd.tty.rows.console=24 systemd.tty.columns.console=80 "+KB+" "+MB)

def boot_micro(kernel, mtype, smp=4, mem=512, timeout=45):
    cmd = f"console=ttyS0 root=/dev/vda rw systemd.unit=multi-user.target {APP}"
    args = ["qemu-system-x86_64", "-M", mtype, "-enable-kvm", "-cpu", "host",
            "-m", str(mem), "-smp", str(smp), "-nodefaults", "-no-reboot",
            "-kernel", kernel, "-append", cmd,
            "-drive", f"id=root,file={IMG},format=raw,if=none,cache=unsafe",
            "-device", "virtio-blk-device,drive=root",
            "-serial", "stdio"]
    return _drive(args, timeout)

def boot_q35pc(kernel, machine="pc", smp=4, mem=512, timeout=45):
    cmd = f"console=ttyS0 root=/dev/vda rw systemd.unit=multi-user.target {APP}"
    args = ["qemu-system-x86_64", "-machine", f"{machine},accel=kvm", "-cpu", "host",
            "-m", str(mem), "-smp", str(smp), "-nodefaults", "-display", "none",
            "-serial", "mon:stdio", "-no-reboot", "-kernel", kernel, "-append", cmd,
            "-drive", f"file={IMG},if=virtio,format=raw,cache=unsafe"]
    return _drive(args, timeout)

def _drive(args, timeout):
    p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, bufsize=0)
    buf=b"";t0=time.time();synced=False;asked=False;last=0.0
    try:
        while time.time()-t0 < timeout:
            r,_,_ = select.select([p.stdout],[],[],0.2)
            if r:
                d=os.read(p.stdout.fileno(),65536)
                if not d: break
                buf+=d
            text=strip(buf).decode("utf-8","replace").replace("\r","\n");now=time.time()
            if not synced:
                if now-last>0.6:
                    try: p.stdin.write(b"\nstty -echo; PS1=; echo AK" b"SYNCED\n");p.stdin.flush()
                    except BrokenPipeError: break
                    last=now
                if re.search(r"(?m)^AKSYNCED\s*$",text): synced=True
            elif not asked:
                p.stdin.write(b"echo AK" b"BEGIN; systemd-analyze time; echo AK" b"DONE\n");p.stdin.flush();asked=True
            else:
                if re.search(r"(?m)^AKDONE\s*$",text) and "Startup finished" in text: break
        try: p.stdin.write(b"systemctl poweroff --no-block\n");p.stdin.flush();p.wait(timeout=8)
        except Exception: p.kill()
    finally:
        if p.poll() is None: p.kill();p.wait()
    text=strip(buf).decode("utf-8","replace").replace("\r","\n")
    m=FIN.search(text)
    if not m:
        # return tail for debugging boot failures
        return {"fail": True, "tail": text[-1200:]}
    k = _sec(m.group(1),m.group(2)) if m.group(1) else 0.0
    return {"kernel":k, "userspace":_sec(m.group(3),m.group(4)), "total":_sec(m.group(5),m.group(6))}

def series(fn, n, **kw):
    out=[]
    for _ in range(n):
        r=fn(**kw)
        if r.get("fail"): return r  # surface failure immediately
        out.append(r)
    return out

def rep(name, runs):
    if isinstance(runs, dict) and runs.get("fail"):
        print(f"  {name:26s} BOOT-FAIL\n   tail: ...{runs['tail'][-500:]}"); return None
    t=sorted(r["total"]*1000 for r in runs); k=st.median(r["kernel"]*1000 for r in runs)
    u=st.median(r["userspace"]*1000 for r in runs)
    print(f"  {name:26s} med={st.median(t):5.0f}ms  kern={k:3.0f}  user={u:3.0f}  min={t[0]:.0f} max={t[-1]:.0f}")
    return st.median(t)

if __name__ == "__main__":
    MICRO="/data1/kbuild/bzImage-microvm"; PC="/data1/kbuild/bzImage-minimal-pc"
    n=int(sys.argv[1]) if len(sys.argv)>1 else 9
    print(f"n={n}; smp=4 mem=512; optimized cmdline\n")
    # smoke test 1 boot of microvm first
    print("smoke: microvm,acpi=off,pit=off,pic=off ...")
    s=boot_micro(MICRO, "microvm,acpi=off,pit=off,pic=off,rtc=off,isa-serial=on")
    if s.get("fail"):
        print("  acpi=off failed, trying acpi=on..."); print("   tail:", s["tail"][-400:])
        s=boot_micro(MICRO, "microvm,acpi=on,isa-serial=on")
        mt="microvm,acpi=on,isa-serial=on"
    else:
        mt="microvm,acpi=off,pit=off,pic=off,rtc=off,isa-serial=on"
    if s.get("fail"):
        print("  microvm BOOT-FAIL both ways. tail:\n", s["tail"]); sys.exit(0)
    print(f"  microvm boots: total={s['total']*1000:.0f}ms kern={s['kernel']*1000:.0f} (machine={mt})\n")
    base=rep("pc (current best)",  series(boot_q35pc, n, machine="pc"))
    micro=rep(f"microvm",          series(boot_micro, n, mtype=mt))
    if base and micro:
        print(f"\n  microvm vs pc: {base:.0f} -> {micro:.0f}ms = {100*(base-micro)/base:+.1f}%")
