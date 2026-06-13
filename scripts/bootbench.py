#!/usr/bin/env python3
"""bootbench.py — reliable QEMU boot-time harness.

Boots a disk image under KVM N times, drives the autologin serial shell to
run `systemd-analyze`, parses (kernel, userspace, total) seconds, and reports
robust statistics. Optionally captures the critical-chain for analysis.

Design notes for reliability:
- syncs to the live shell by probing a unique marker until its *output* line
  appears (not just the command echo), so it never races the boot;
- strips ANSI/OSC escapes before parsing;
- cache=unsafe + tmpfs image => storage is never the bottleneck;
- always powers off cleanly (-no-reboot) so qemu exits and we get EOF;
- median is the headline (robust); min = best-case; stdev = noise.

Usage:
  bootbench.py --img IMG [-n 7] [--append "quiet ..."] [--label L] [--chain]
  bootbench.py --ab IMG --base "" --opt "quiet mitigations=off" -n 7
"""
from __future__ import annotations
import argparse, os, re, select, statistics, subprocess, sys, time

KERNEL_DEFAULT = "/nas4/data/autokernel/world/ring0-clang2/image/vmlinuz-7.0.0-22-generic"
ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>]")
_FINISH = re.compile(
    r"Startup finished in ([\d.]+)(m?s) \(kernel\)"
    r"(?: \+ ([\d.]+)(m?s) \(initrd\))?"
    r" \+ ([\d.]+)(m?s) \(userspace\) = ([\d.]+)(m?s)"
)


def _sec(num: str, unit: str) -> float:
    return float(num) / 1000.0 if unit == "ms" else float(num)


def boot_once(img, kernel, append, timeout=45, chain=False, mem=2048, smp=2):
    cmdline = f"root=/dev/vda rw console=ttyS0 systemd.unit=multi-user.target {append}".strip()
    args = [
        "qemu-system-x86_64", "-enable-kvm", "-cpu", "host",
        "-m", str(mem), "-smp", str(smp), "-nographic", "-no-reboot",
        "-kernel", kernel, "-append", cmdline,
        "-drive", f"file={img},if=virtio,format=raw,cache=unsafe",
    ]
    p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, bufsize=0)
    buf = b""
    t0 = time.time()
    synced = False
    analyzed = False
    last_probe = 0.0
    extra = "; systemd-analyze critical-chain --no-pager" if chain else ""
    try:
        while time.time() - t0 < timeout:
            r, _, _ = select.select([p.stdout], [], [], 0.2)
            if r:
                try:
                    d = os.read(p.stdout.fileno(), 65536)
                except OSError:
                    break
                if not d:
                    break
                buf += d
            text = ANSI.sub(b"", buf).decode("utf-8", "replace").replace("\r", "\n")
            now = time.time()
            if not synced:
                if now - last_probe > 0.6:
                    try:
                        p.stdin.write(b"\nexport PS1=; echo AK''SYNCED\n")
                        p.stdin.flush()
                    except BrokenPipeError:
                        break
                    last_probe = now
                if re.search(r"(?m)^AKSYNCED\s*$", text):
                    synced = True
            elif not analyzed:
                try:
                    p.stdin.write(
                        b"TERM=dumb; echo AK''BEGIN; systemd-analyze time"
                        + extra.encode() + b"; echo AK''DONE\n"
                    )
                    p.stdin.flush()
                except BrokenPipeError:
                    break
                analyzed = True
            else:
                if re.search(r"(?m)^AKDONE\s*$", text) and "Startup finished" in text:
                    break
        try:
            p.stdin.write(b"systemctl poweroff --no-block\n")
            p.stdin.flush()
        except Exception:
            pass
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    text = ANSI.sub(b"", buf).decode("utf-8", "replace").replace("\r", "\n")
    m = _FINISH.search(text)
    if not m:
        return None
    k = _sec(m.group(1), m.group(2))
    u = _sec(m.group(5), m.group(6))
    tot = _sec(m.group(7), m.group(8))
    out = {"kernel": k, "userspace": u, "total": tot}
    if chain:
        cm = re.search(r"(The time when unit.*?)\nAKDONE", text, re.S)
        out["chain"] = cm.group(1).strip() if cm else None
    return out


def series(img, kernel, append, n, chain_first=False, warmup=1, **kw):
    runs = []
    chain = None
    for i in range(n + warmup):
        want_chain = chain_first and i == warmup
        r = boot_once(img, kernel, append, chain=want_chain, **kw)
        if r is None:
            print(f"  run {i}: FAILED (no systemd-analyze output)", file=sys.stderr)
            continue
        if want_chain:
            chain = r.get("chain")
        if i >= warmup:  # drop warmup run(s)
            runs.append(r)
            print(f"  run {i-warmup+1}/{n}: total={r['total']*1000:.0f}ms "
                  f"(kernel {r['kernel']*1000:.0f} + userspace {r['userspace']*1000:.0f})")
    return runs, chain


def stats(runs, key="total"):
    xs = sorted(r[key] for r in runs)
    return {
        "n": len(xs), "min": xs[0], "max": xs[-1],
        "median": statistics.median(xs), "mean": statistics.mean(xs),
        "stdev": statistics.pstdev(xs) if len(xs) > 1 else 0.0,
    }


def fmt(s):
    return (f"median={s['median']*1000:.0f}ms  min={s['min']*1000:.0f}ms  "
            f"mean={s['mean']*1000:.0f}±{s['stdev']*1000:.0f}ms  (n={s['n']})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--img", required=True)
    ap.add_argument("--kernel", default=KERNEL_DEFAULT)
    ap.add_argument("-n", type=int, default=7)
    ap.add_argument("--append", default="")
    ap.add_argument("--label", default="run")
    ap.add_argument("--chain", action="store_true")
    ap.add_argument("--ab", help="second append string to A/B against --append")
    args = ap.parse_args()

    print(f"== {args.label}: append='{args.append}' ==")
    runs, chain = series(args.img, args.kernel, args.append, args.n, chain_first=args.chain)
    s = stats(runs)
    print(f"  TOTAL     {fmt(s)}")
    print(f"  kernel    {fmt(stats(runs,'kernel'))}")
    print(f"  userspace {fmt(stats(runs,'userspace'))}")
    if chain:
        print("  -- critical-chain --")
        for line in chain.splitlines()[:14]:
            print("   ", line)

    if args.ab is not None:
        print(f"\n== A/B opt: append='{args.ab}' ==")
        runs2, _ = series(args.img, args.kernel, args.ab, args.n)
        s2 = stats(runs2)
        print(f"  TOTAL     {fmt(s2)}")
        base, opt = s["median"], s2["median"]
        print(f"\n  Δ median: {base*1000:.0f}ms -> {opt*1000:.0f}ms  "
              f"= {(base-opt)/base*100:+.1f}%  ({'PASS' if opt<=0.9*base else 'short of -10%'})")


if __name__ == "__main__":
    main()
