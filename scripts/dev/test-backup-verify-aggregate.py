#!/usr/bin/env python3
"""collect.build() keeps a failure of ANY cluster of the cnpg dataset (one row per cluster and store).

Lab 19: two rows for (cnpg, primary) overwrote each other, so a WAL segment missing from zitadel-db's archive was PASS because
grafana-db came last. Pure Python, no cluster."""
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("collect", Path(__file__).resolve().parents[2] / "apps/base/backup/databases/verify/collect.py")
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
fails = []
def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

now = 1_000_000.0
rows = [("cnpg", "primary", False, 100, "zitadel-db: WAL 000..06 missing"), ("cnpg", "primary", True, 50, "grafana-db: ok"),
        ("cnpg", "replica", True, 100, "zitadel-db: ok"), ("cnpg", "replica", True, 50, "grafana-db: ok")]
text, log = c.build(rows, now, {"cnpg": 900.0})
check("one failing cluster fails the store (order: failing first)", 'oa_backup_verify_store_ok{dataset="cnpg",store="primary"} 0' in text)
check("the dataset is 0", 'oa_backup_verify_ok{dataset="cnpg"} 0' in text)
check("the replica store with both clusters ok is 1", 'oa_backup_verify_store_ok{dataset="cnpg",store="replica"} 1' in text)
check("the last success is kept, not renewed", 'oa_backup_verify_last_success_timestamp_seconds{dataset="cnpg"} 900' in text)
check("the worst age is reported", 'oa_backup_verify_newest_backup_age_seconds{dataset="cnpg",store="primary"} 100' in text)
check("the log names the failing cluster", any("FAIL cnpg/primary" in l and "zitadel-db" in l for l in log))
text2, _ = c.build(list(reversed(rows[:2])), now, {})
check("the order of the rows does not matter", 'oa_backup_verify_store_ok{dataset="cnpg",store="primary"} 0' in text2)
sys.exit(1 if fails else 0)
