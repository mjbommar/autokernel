#!/usr/bin/env python3
"""gapprobe.py — boot once, capture systemd's own phase timestamps to attribute
the SECURITY_FINISH -> GENERATORS_START window precisely. A/B two cmdlines."""
import os, re, select, subprocess, sys, time

KERNEL = "/data1/kbuild/linux-7.0.12/arch/x86/boot/bzImage"
IMG = "/dev/shm/akvm/fork.img"
CSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")
OSC = re.compile(rb"\x1b\][0-9].*?(?:\x07|\x1b\\)")
def strip(b): return CSI.sub(b"", OSC.sub(b"", b))

PROPS = [
    "UserspaceTimestampMonotonic",
    "SecurityStartTimestampMonotonic",
    "SecurityFinishTimestampMonotonic",
    "GeneratorsStartTimestampMonotonic",
    "GeneratorsFinishTimestampMonotonic",
    "UnitsLoadStartTimestampMonotonic",
    "UnitsLoadFinishTimestampMonotonic",
    "FinishTimestampMonotonic",
]

def boot(append, timeout=45):
    cmdline = f"root=/dev/vda rw console=ttyS0 systemd.unit=multi-user.target {append}".strip()
    args = ["qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-m", "2048",
            "-smp", "2", "-nodefaults", "-display", "none", "-serial", "mon:stdio",
            "-no-reboot", "-kernel", KERNEL, "-append", cmdline,
            "-drive", f"file={IMG},if=virtio,format=raw,cache=unsafe"]
    p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, bufsize=0)
    buf = b""; t0 = time.time(); synced = False; asked = False; last = 0.0
    show = "systemctl show -p " + " -p ".join(PROPS)
    try:
        while time.time() - t0 < timeout:
            r, _, _ = select.select([p.stdout], [], [], 0.2)
            if r:
                d = os.read(p.stdout.fileno(), 65536)
                if not d: break
                buf += d
            text = strip(buf).decode("utf-8", "replace").replace("\r", "\n")
            now = time.time()
            if not synced:
                if now - last > 0.6:
                    p.stdin.write(b"\nstty -echo; PS1=; PROMPT_COMMAND=; echo AK" b"SYNCED\n"); p.stdin.flush()
                    last = now
                if re.search(r"(?m)^AKSYNCED\s*$", text): synced = True
            elif not asked:
                p.stdin.write(b"echo AK''BEGIN; " + show.encode() + b"; echo AK''DONE\n")
                p.stdin.flush(); asked = True
            else:
                if re.search(r"(?m)^AKDONE\s*$", text): break
        p.stdin.write(b"systemctl poweroff --no-block\n"); p.stdin.flush()
        try: p.wait(timeout=8)
        except subprocess.TimeoutExpired: p.kill()
    finally:
        if p.poll() is None: p.kill(); p.wait()
    text = strip(buf).decode("utf-8", "replace").replace("\r", "\n")
    vals = {}
    for k in PROPS:
        m = re.search(rf"(?m)^{k}=(\d+)\s*$", text)
        vals[k] = int(m.group(1)) if m else None
    if all(v is None for v in vals.values()) and os.environ.get("DBG"):
        seg = text
        i = text.rfind("AKBEGIN"); j = text.rfind("AKDONE")
        if i >= 0 and j > i: seg = text[i:j]
        print("---RAW---\n" + seg[-1500:] + "\n---END---")
    return vals

def report(label, v):
    def us(k): return v[k]
    print(f"\n== {label} ==")
    order = ["UserspaceTimestampMonotonic","SecurityStartTimestampMonotonic",
             "SecurityFinishTimestampMonotonic","GeneratorsStartTimestampMonotonic",
             "GeneratorsFinishTimestampMonotonic","UnitsLoadStartTimestampMonotonic",
             "UnitsLoadFinishTimestampMonotonic","FinishTimestampMonotonic"]
    prev = None
    for k in order:
        x = v[k]
        if x is None:
            print(f"  {k:36s} = (missing)"); continue
        dl = "" if prev is None else f"  (+{(x-prev)/1000:.1f}ms)"
        print(f"  {k:36s} = {x/1000:8.1f}ms{dl}")
        prev = x
    sf, gs = v["SecurityFinishTimestampMonotonic"], v["GeneratorsStartTimestampMonotonic"]
    if sf and gs:
        print(f"  >>> SECURITY_FINISH -> GENERATORS_START gap = {(gs-sf)/1000:.1f}ms")

if __name__ == "__main__":
    base = "quiet loglevel=3 systemd.show_status=0 tsc=reliable nowatchdog"
    size = " systemd.tty.rows.console=24 systemd.tty.columns.console=80"
    term = " TERM=linux"
    report("baseline", boot(base))
    report("+TERM=linux only", boot(base + term))
    report("+console-size only", boot(base + size))
    report("+both (size + TERM)", boot(base + size + term))
