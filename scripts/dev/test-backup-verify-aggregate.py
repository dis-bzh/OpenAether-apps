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

# apps #151: a store that lost its copy is not a skip
R = lambda ds, st, k, msg, ref="5": (ds, st, k, ref, msg)
promoted = c.promote_missing([R("etcd", "primary", "skip", "no repository at etcd"), R("etcd", "replica", "1", "restic check passed")])
check("skip on the primary while the replica passed becomes a failure (0), naming the replica as holder",
      promoted[0][2] == "0" and "MISSING" in promoted[0][4] and "replica store holds data" in promoted[0][4] and promoted[1][2] == "1")
promoted = c.promote_missing([R("etcd", "primary", "skip", "no snapshot yet"), R("etcd", "replica", "skip", "no snapshot yet")])
check("skip on every store stays a skip (nothing backed up yet)", [r[2] for r in promoted] == ["skip", "skip"])
promoted = c.promote_missing([R("etcd", "primary", "skip", "no snapshot yet"), R("etcd", "replica", "0", "repository unreadable")])
check("skip next to an UNREADABLE store stays a skip (that store already fails by itself)", promoted[0][2] == "skip")
promoted = c.promote_missing([R("cnpg", "primary", "skip", "zitadel-db: empty archive"), R("cnpg", "replica", "1", "zitadel-db: ok"),
                              R("cnpg", "primary", "skip", "grafana-db: no base backup yet"), R("cnpg", "replica", "skip", "grafana-db: no base backup yet")])
check("cnpg: grouped per cluster, zitadel-db primary fails while grafana-db (young on both) stays a skip",
      [r[2] for r in promoted] == ["0", "1", "skip", "skip"])
text, log = c.build([(r[0], r[1], r[2] == "1", None, r[4]) for r in c.promote_missing([R("etcd", "primary", "skip", "no repository at etcd"), R("etcd", "replica", "1", "ok")])], now, {})
check("the exposition then says oa_backup_verify_ok 0 and store_ok{store=primary} 0 (not a missing series)",
      'oa_backup_verify_ok{dataset="etcd"} 0' in text and 'oa_backup_verify_store_ok{dataset="etcd",store="primary"} 0' in text
      and 'oa_backup_verify_store_ok{dataset="etcd",store="replica"} 1' in text)
sys.exit(1 if fails else 0)
