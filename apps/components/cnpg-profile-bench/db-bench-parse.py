#!/usr/bin/env python3
"""Turns the output of one pgbench run (stdout + the per-transaction logs of `-l`) into one result row.

Latency percentiles come from the transaction logs (exact, not sampled): column 3 of a log line is the transaction latency in
microseconds. TPS, the average and the failed count come from pgbench's own summary. Standard library only (the CNPG image has
python3 and nothing else). Usage:
  db-bench-parse.py row --dir D --stdout F --profile P --path X --mode rw|ro --scale N --clients N --secs N [--md|--tsv]
  db-bench-parse.py header [--md|--tsv]
"""
import argparse
import glob
import math
import re
import sys

COLS = ["profile", "path", "mode", "scale", "clients", "secs", "tps", "avg_ms", "p50_ms", "p95_ms", "p99_ms", "failed"]


def percentile(sorted_vals, q):
    """Nearest-rank percentile (q in 0..100) of an ascending list; None for an empty one."""
    if not sorted_vals:
        return None
    return sorted_vals[max(0, math.ceil(q / 100.0 * len(sorted_vals)) - 1)]


def latencies_us(paths):
    """(latencies in microseconds of the committed transactions, number of failed or skipped lines) from pgbench -l logs."""
    lat, bad = [], 0
    for p in paths:
        with open(p) as f:
            for line in f:
                parts = line.split()
                if len(parts) < 6:
                    continue
                if parts[2].isdigit():
                    lat.append(int(parts[2]))
                else:  # PG 15+ writes the failure kind ("failed", "serialization"...) where the latency would be
                    bad += 1
    lat.sort()
    return lat, bad


def summary(text):
    """tps, average latency (ms) and failed transactions from the pgbench summary."""
    def num(rx):
        m = re.search(rx, text)
        return float(m.group(1)) if m else None
    failed = re.search(r"number of failed transactions:\s*(\d+)", text)
    return num(r"tps = ([0-9.]+)"), num(r"latency average = ([0-9.]+) ms"), int(failed.group(1)) if failed else 0


def row(args):
    logs = sorted(glob.glob(args.dir.rstrip("/") + "/pgb*"))
    lat, bad = latencies_us(logs)
    tps, avg, failed = summary(open(args.stdout).read())
    if not lat or tps is None:
        sys.exit("no transaction in the logs of %s or no tps in %s: the run failed" % (args.dir, args.stdout))
    ms = lambda v: "%.2f" % (v / 1000.0)
    return [args.profile, args.path, args.mode, str(args.scale), str(args.clients), str(args.secs), "%.1f" % tps,
            "%.2f" % (avg if avg is not None else sum(lat) / len(lat) / 1000.0), ms(percentile(lat, 50)),
            ms(percentile(lat, 95)), ms(percentile(lat, 99)), str(failed + bad)]


def fmt(cells, md):
    return ("| " + " | ".join(cells) + " |") if md else "\t".join(cells)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["row", "header"])
    ap.add_argument("--dir"); ap.add_argument("--stdout"); ap.add_argument("--profile", default="-"); ap.add_argument("--path", default="-")
    ap.add_argument("--mode", default="rw"); ap.add_argument("--scale", type=int, default=0)
    ap.add_argument("--clients", type=int, default=0); ap.add_argument("--secs", type=int, default=0)
    ap.add_argument("--md", action="store_true")
    a = ap.parse_args()
    if a.cmd == "header":
        print(fmt(COLS, a.md))
        if a.md:
            print("|" + "|".join(["---"] * len(COLS)) + "|")
        return
    print(fmt(row(a), a.md))


if __name__ == "__main__":
    main()
