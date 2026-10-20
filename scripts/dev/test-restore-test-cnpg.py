#!/usr/bin/env python3
"""The CNPG restore test (apps/base/backup/databases/restore/restore_cnpg.py), offline: the comparison, the guard, the scratch
Cluster it asks for, the metrics, and the whole flow against a kubectl double (testdata/fake-kubectl-restore.py) for each
damage: a flipped hash, a hole in the chain, a copy that trails too far, a Cluster that never gets healthy, one stuck on a
finalizer, a Job killed at its deadline. Whatever happens the scratch Cluster must be gone and nothing may be written outside
the scratch namespace (but the metrics ConfigMap). Real SQL and a real PostgreSQL: test-restore-test-cnpg-sql.sh.
Run: python3 -I scripts/dev/test-restore-test-cnpg.py
"""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
D = os.path.join(ROOT, "apps/base/backup/databases/restore")
spec = importlib.util.spec_from_file_location("rc", os.path.join(D, "restore_cnpg.py"))
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)
FAKE = os.path.join(ROOT, "scripts/dev/testdata/fake-kubectl-restore.py")
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%06d" % int((t % 1) * 1e6)


def chain(n, now, step=300, start=1000):
    rows, prev = [], ""
    for i in range(n):
        c, ts = start + i, stamp(now - (n - 1 - i) * step)
        prev = rc.chain_sha(prev, c, ts)
        rows.append((c, ts, prev))
    return rows


def snapshot(rows, tables=("public.a", "public.b"), counts=(5, 7)):
    t = "##tables\n" + "".join(x + "\n" for x in tables) + "##rows\n" + "".join(f"{x}\t{n}\n" for x, n in zip(tables, counts))
    return t + "##canary\n" + "".join(f"{c}\t{ts}\t{s}\n" for c, ts, s in rows) + "##end\n"


NOW = time.time()
SRC = chain(600, NOW)   # 50 h of rows, one per 5 min
src = rc.parse_snapshot(snapshot(SRC))

print("=== comparison ===")
check("an intact chain has no error", rc.chain_errors(SRC) == [])
bad = list(SRC); bad[300] = (bad[300][0], bad[300][1], "0" * 64)
check("a flipped hash breaks the chain", any("does not follow" in e for e in rc.chain_errors(bad)))
hole = SRC[:300] + SRC[302:]
check("a hole breaks the chain", any("jumps" in e for e in rc.chain_errors(hole)))
ok, why, info = rc.compare(src, rc.parse_snapshot(snapshot(SRC[:-2])), "primary", "latest")
check("a copy 2 rows behind passes on the primary store (lag 600 s)", ok and abs(info["lag"] - 600) < 1, (why, info))
ok, why, _ = rc.compare(src, rc.parse_snapshot(snapshot(SRC[:-5])), "primary", "latest")
check("5 rows behind exceeds the primary budget", not ok and "trails" in " ".join(why))
ok, why, _ = rc.compare(src, rc.parse_snapshot(snapshot(SRC[:-5])), "replica", "latest")
check("5 rows behind is within the replica budget", ok, why)
ok, why, _ = rc.compare(src, rc.parse_snapshot(snapshot(SRC[:-11])), "replica", "latest")
check("11 rows behind exceeds the replica budget", not ok)
other = chain(600, NOW, start=5000)
ok, why, _ = rc.compare(src, rc.parse_snapshot(snapshot(other)), "primary", "latest")
check("another database's chain is refused", not ok, why)
ok, why, _ = rc.compare(src, rc.parse_snapshot(snapshot(SRC, tables=())), "primary", "latest")
check("a restored database without table fails", not ok and "no table" in " ".join(why))
ok, why, _ = rc.compare(src, rc.parse_snapshot(snapshot([])), "primary", "latest")
check("no canary row in the restored database fails", not ok)
big = rc.parse_snapshot(snapshot(SRC, counts=(5000, 7)))
ok, why, _ = rc.compare(big, rc.parse_snapshot(snapshot(SRC, counts=(0, 7))), "primary", "latest")
check("a table emptied by the restore fails", not ok and "empty" in " ".join(why))
try:
    rc.compare(rc.parse_snapshot(snapshot([])), src, "primary", "latest")
    check("a live database without canary is a skip", False)
except rc.Skip:
    check("a live database without canary is a skip, not a pass or a failure", True)
tgt = rc.pick_pitr_target(SRC, NOW - 86400, NOW)
check("the PITR target is the newest row older than 2 h and after the base backup",
      tgt is not None and rc.epoch(tgt[1]) <= NOW - 7200 and rc.epoch(tgt[1]) > NOW - 7200 - 301, tgt)
check("no row after a recent base backup: no target", rc.pick_pitr_target(SRC, NOW - 600, NOW) is None)
ok, why, _ = rc.compare(src, rc.parse_snapshot(snapshot(SRC[:tgt[0] - 1000 + 1])), "primary", "pitr", tgt)
check("PITR ending exactly at the target row passes", ok, why)
ok, why, _ = rc.compare(src, rc.parse_snapshot(snapshot(SRC[:tgt[0] - 1000 + 2])), "primary", "pitr", tgt)
check("PITR ending one row late fails", not ok and "exactly" in " ".join(why))
check("the recovery target is the row's second plus 2 s", rc.target_time("2026-10-20T10:00:05.123456") == "2026-10-20 10:00:07+00")

print("=== guard and scratch Cluster ===")
for ns, name in (("foundation-databases", "rt-x"), ("backup-restore-test", "zitadel-db"), ("backup-restore-test", "x-rt"), ("default", "rt-a")):
    try:
        rc.guard(ns, name)
        check(f"guard refuses {ns}/{name}", False)
    except RuntimeError:
        check(f"guard refuses {ns}/{name}", True)
rc.guard("backup-restore-test", "rt-zitadel-db-replica-latest")
live = {"spec": {"imageName": "img:1", "storage": {"size": "5Gi", "storageClass": "local-path-retain"},
                 "postgresql": {"parameters": {"max_connections": "200"}}}}
m = rc.recovery_manifest("zitadel-db", "replica", "latest", live)
s = m["spec"]
check("the scratch Cluster lives in the scratch namespace under an rt- name", m["metadata"]["namespace"] == "backup-restore-test" and m["metadata"]["name"].startswith("rt-"))
check("one instance, local-path (Delete) storage, the source's image and max_connections",
      s["instances"] == 1 and s["storage"]["storageClass"] == "local-path" and s["imageName"] == "img:1" and s["postgresql"]["parameters"]["max_connections"] == "200")
check("it recovers from the ObjectStore of the store and never archives",
      s["externalClusters"][0]["plugin"]["parameters"] == {"barmanObjectName": "zitadel-db-replica", "serverName": "zitadel-db"} and "plugins" not in s and "backup" not in s)
check("limits are set", "limits" in s["resources"])
check("PITR sets the recovery target", rc.recovery_manifest("grafana-db", "primary", "pitr", live, "2026-10-20 10:00:07+00")["spec"]["bootstrap"]["recovery"]["recoveryTarget"]["targetTime"] == "2026-10-20 10:00:07+00")

print("=== metrics ===")
lab = rc.labels("cnpg", "zitadel-db", "replica", "latest")
t = rc.build_metrics([{"labels": lab, "ok": True, "duration": 90, "recovery": 60, "lag": 600, "missing": 0}], 1000.0, {})
check("a pass exports ok, last success, last run, duration, recovery and lag",
      all(f"{n}{{{lab}}}" in t for n in ("oa_restore_test_ok", "oa_restore_test_last_success_timestamp_seconds", "oa_restore_test_last_run_timestamp_seconds",
                                         "oa_restore_test_duration_seconds", "oa_restore_test_recovery_seconds", "oa_restore_test_data_lag_seconds")))
prev = rc.parse_previous(t)
t2 = rc.build_metrics([{"labels": lab, "ok": False, "duration": 5, "recovery": None, "lag": None, "missing": None}], 2000.0, prev)
check("a failure says 0 and keeps the last success", f"oa_restore_test_ok{{{lab}}} 0" in t2 and f"oa_restore_test_last_success_timestamp_seconds{{{lab}}} 1000" in t2)
t3 = rc.build_metrics([{"labels": lab, "ok": None, "duration": 1, "recovery": None, "lag": None, "missing": None}], 3000.0, prev)
check("a skip exports no ok series and keeps the last success", "oa_restore_test_ok{" not in t3 and "last_success_timestamp_seconds{" in t3)

print("=== flow against a kubectl double ===")
NAMES = [f"rt-{c}-{s}" for c in ("zitadel-db", "grafana-db") for s in ("primary", "replica")]


def flow(mode, snaps, src_rows=SRC, env_extra=None, sigterm_after=None, key_prev=None):
    w = tempfile.mkdtemp()
    state, work, sn = (os.path.join(w, x) for x in ("state", "work", "snaps"))
    for x in (state, work, sn):
        os.makedirs(x)
    for c in ("zitadel-db", "grafana-db"):
        open(os.path.join(work, c + ".src"), "w").write(snapshot(src_rows) if src_rows else snapshot([]))
    for n, rows in snaps.items():
        open(os.path.join(sn, n + f"-{mode}.snap"), "w").write(snapshot(rows) if isinstance(rows, list) else rows)
    if key_prev:
        os.makedirs(os.path.join(state, "cm"))
        open(os.path.join(state, "cm", "cnpg-" + mode), "w").write(key_prev)
    env = dict(os.environ, KUBECTL=FAKE, FAKE_STATE=state, FAKE_SNAPS=sn, POLL="0.01", HEALTHY_TIMEOUT="5", DELETE_TIMEOUT="1",
               WORK=work, PITR_MIN_AGE="7200")
    env.update(env_extra or {})
    p = subprocess.Popen([sys.executable, "-I", os.path.join(D, "restore_cnpg.py"), "--mode", mode, "--publish"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if sigterm_after:
        time.sleep(sigterm_after)
        p.send_signal(signal.SIGTERM)
    out = p.communicate(timeout=120)[0]
    calls = open(os.path.join(state, "calls.log")).read().splitlines()
    left = [f for f in os.listdir(os.path.join(state, "scratch")) if f.endswith(".json")]
    cm = os.path.join(state, "cm", "cnpg-" + mode)
    return p.returncode, out, calls, left, (open(cm).read() if os.path.exists(cm) else ""), state


def writes_ok(calls):
    for c in calls:
        w = c.split()
        if w[0] in ("apply", "delete", "patch", "exec"):
            if w[:3] == ["patch", "configmap", "oa-restore-test-metrics"] and "foundation-databases" in w:
                continue
            if "backup-restore-test" not in w:
                return False, c
            if w[0] in ("delete", "patch") and w[2].startswith("-l") is False and not w[2].startswith("rt-") and w[1] not in ("job", "pod", "pvc"):
                return False, c
    return True, ""


good = {n: SRC[:-2] for n in NAMES}
rcode, out, calls, left, cm, st = flow("latest", good)
check("all four recoveries pass: exit 0", rcode == 0, out[-400:])
check("every pass is a metric ok=1", cm.count("oa_restore_test_ok{") == 4 and " 0\n" not in "".join(l + "\n" for l in cm.splitlines() if l.startswith("oa_restore_test_ok{")))
check("no scratch Cluster is left", left == [], left)
w_ok, w_bad = writes_ok(calls)
check("no write outside the scratch namespace (but the metrics ConfigMap)", w_ok, w_bad)
applied = [json.loads(l) for l in open(os.path.join(st, "applied.jsonl"))]
check("four scratch Clusters were applied, none archives or targets a live name",
      len(applied) == 4 and all(a["metadata"]["namespace"] == "backup-restore-test" and a["metadata"]["name"] in [n + "-latest" for n in NAMES] and "plugins" not in a["spec"] for a in applied))

flip = dict(good); fl = list(SRC[:-2]); fl[-1] = (fl[-1][0], fl[-1][1], "f" * 64)
flip["rt-zitadel-db-replica"] = fl
rcode, out, calls, left, cm, _ = flow("latest", flip)
check("a corrupted replica copy fails the run (exit 1)", rcode == 1, out[-300:])
check("exactly that dataset and store says ok=0", 'oa_restore_test_ok{kind="cnpg",dataset="zitadel-db",store="replica",mode="latest"} 0' in cm and [l for l in cm.splitlines() if l.startswith("oa_restore_test_ok{") and l.endswith(" 0")].__len__() == 1)
check("the scratch Cluster of the failure is removed too", left == [], left)
prev = rc.build_metrics([{"labels": rc.labels("cnpg", "zitadel-db", "replica", "latest"), "ok": True, "duration": 1, "recovery": 1, "lag": 1, "missing": 0}], 1234.0, {})
rcode, out, calls, left, cm, _ = flow("latest", flip, key_prev=prev)
check("the last success of the failed dataset is kept", 'last_success_timestamp_seconds{kind="cnpg",dataset="zitadel-db",store="replica",mode="latest"} 1234' in cm)

holey = dict(good); holey["rt-grafana-db-primary"] = SRC[:300] + SRC[302:-2]
rcode, out, *_ = flow("latest", holey)
check("a hole in the restored chain fails", rcode == 1 and "jumps" in out)
trail = dict(good); trail["rt-grafana-db-replica"] = SRC[:-12]
rcode, out, *_ = flow("latest", trail)
check("a replica copy that trails more than its budget fails", rcode == 1 and "trails" in out)

rcode, out, calls, left, cm, _ = flow("latest", good, env_extra={"FAKE_MODE": "never-healthy", "HEALTHY_TIMEOUT": "0.3"})
check("a Cluster that never gets healthy fails and is removed", rcode == 1 and left == [] and "not healthy" in out, out[-300:])
rcode, out, calls, left, cm, _ = flow("latest", good, env_extra={"FAKE_MODE": "stuck-finalizer"})
check("a Cluster stuck on a finalizer is released and removed", rcode == 0 and left == [] and any(c.startswith("patch cluster") for c in calls), (left, out[-200:]))
rcode, out, calls, left, cm, _ = flow("latest", good, env_extra={"FAKE_MODE": "never-healthy", "HEALTHY_TIMEOUT": "60"}, sigterm_after=1.5)
check("a Job killed at its deadline (SIGTERM) removes the scratch Cluster before it dies", left == [] and "terminated" in out, (left, out[-300:]))
rcode, out, calls, left, cm, _ = flow("latest", good, src_rows=[])
check("a live database without canary: skipped, nothing restored, exit 0", rcode == 0 and not any(c.startswith("apply") for c in calls) and "oa_restore_test_ok{" not in cm, out[-300:])

tgt = rc.pick_pitr_target(SRC, NOW - 86400, NOW)
cut = SRC[:tgt[0] - 1000 + 1]
rcode, out, calls, left, cm, st = flow("pitr", {n: cut for n in NAMES})
check("PITR to a canary row: every recovery ends at exactly that row", rcode == 0 and left == [], out[-400:])
check("the recovery target of the manifest is that row's time", all(json.loads(l)["spec"]["bootstrap"]["recovery"]["recoveryTarget"]["targetTime"] == rc.target_time(tgt[1]) for l in open(os.path.join(st, "applied.jsonl"))))
rcode, out, *_ = flow("pitr", {n: SRC[:-2] for n in NAMES})
check("a PITR that replays past its target fails", rcode == 1 and "exactly" in out)

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
