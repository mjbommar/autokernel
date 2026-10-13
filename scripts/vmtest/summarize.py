import sys
import glob
import os
import re
import statistics as st

d = sys.argv[1]
higher = {"futex_hash_ops"}
checks, bench, order = {}, {}, []
for f in sorted(glob.glob(os.path.join(d, "*.pass*.log"))):
    n = os.path.basename(f).split(".pass")[0]
    if n not in order:
        order.append(n)
    for line in open(f, errors="replace"):
        line = line.strip().replace("\r", "")
        m = re.match(r"CHECK (\S+) (PASS|FAIL|INFO)\s*(.*)", line)
        if m:
            checks.setdefault(n, {}).setdefault(m[1], set()).add(
                f"{m[2]} {m[3]}".strip()
            )
        m = re.match(r"BENCH (\S+) \d+ ([0-9.]+)$", line)
        if m:
            bench.setdefault(m[1], {}).setdefault(n, []).append(float(m[2]))
print("== checks")
for n in order:
    print(
        f"  {n:10s} "
        + "; ".join(f"{k}={'/'.join(sorted(v))}" for k, v in checks.get(n, {}).items())
    )
print("== benchmarks: median (cv%)  [delta vs first kernel; + = better]")
ref = order[0]
for b in sorted(bench):
    row = f"  {b:24s}"
    base = st.median(bench[b][ref]) if ref in bench[b] else None
    for n in order:
        v = bench[b].get(n)
        if not v:
            row += f" {n}=n/a"
            continue
        med = st.median(v)
        cv = 100 * st.pstdev(v) / med if med else 0
        delta = ""
        if base and n != ref:
            pct = 100 * (med - base) / base
            delta = f" {(+pct if b in higher else -pct):+.1f}%"
        row += f" | {n} {med:.4g} ({cv:.0f}%){delta}"
    print(row)
