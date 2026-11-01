#!/usr/bin/env python3
"""The pgbench result parser of the database benchmark (apps/components/cnpg-profile-bench/db-bench-parse.py): exact percentiles
from the transaction logs, pgbench's own TPS, failures counted, a run without data refused. No database needed.
Run: python3 -I scripts/dev/test-db-bench-parse.py
"""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
P = REPO / "apps/components/cnpg-profile-bench/db-bench-parse.py"
spec = importlib.util.spec_from_file_location("p", P)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond


# 100 transactions of 1..100 ms: nearest-rank p50 = 50 ms, p95 = 95 ms, p99 = 99 ms
vals = list(range(1000, 101000, 1000))
check(m.percentile(vals, 50) == 50000 and m.percentile(vals, 95) == 95000 and m.percentile(vals, 99) == 99000,
      "nearest-rank percentiles of 1..100 ms")
check(m.percentile([7], 99) == 7 and m.percentile([], 50) is None, "one value and no value")
SUMMARY = "transaction type: <builtin: TPC-B (sort of)>\nnumber of failed transactions: 2 (0.020%)\nlatency average = 1.234 ms\ntps = 810.5 (without initial connection time)\n"
check(m.summary(SUMMARY) == (810.5, 1.234, 2), "tps, average and failed transactions come from pgbench's summary")
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    (d / "pgb.100").write_text("".join("0 %d %d 0 1700000000 %d\n" % (i, v, i) for i, v in enumerate(vals[:50])))
    (d / "pgb.100.1").write_text("".join("1 %d %d 0 1700000000 %d\n" % (i, v, i) for i, v in enumerate(vals[50:])) + "1 999 failed 0 1700000000 1\n")
    (d / "stdout").write_text(SUMMARY)
    base = [sys.executable, "-I", str(P), "row", "--dir", str(d), "--stdout", str(d / "stdout"), "--profile", "ha", "--path", "pooler",
            "--mode", "ro", "--scale", "10", "--clients", "16", "--secs", "60"]
    r = subprocess.run(base + ["--md"], capture_output=True, text=True)
    check(r.stdout.strip() == "| ha | pooler | ro | 10 | 16 | 60 | 810.5 | 1.23 | 50.00 | 95.00 | 99.00 | 3 |",
          "a row merges the logs of both threads and counts the failed line (%s)" % r.stdout.strip())
    r = subprocess.run(base, capture_output=True, text=True)
    check(r.stdout.count("\t") == 11, "the TSV form has 12 cells")
    (d / "stdout").write_text("no summary\n")
    r = subprocess.run(base, capture_output=True, text=True)
    check(r.returncode != 0 and "failed" in r.stderr, "a run without a summary is refused, not reported as zero")
    for f in d.glob("pgb*"):
        f.unlink()
    (d / "stdout").write_text(SUMMARY)
    r = subprocess.run(base, capture_output=True, text=True)
    check(r.returncode != 0, "a run without transaction logs is refused")
h = subprocess.run([sys.executable, "-I", str(P), "header", "--md"], capture_output=True, text=True).stdout.splitlines()
check(h[0].startswith("| profile | path | mode |") and h[1].count("---") == 12, "the Markdown header matches the row")
print("%d failed" % fails)
sys.exit(1 if fails else 0)
