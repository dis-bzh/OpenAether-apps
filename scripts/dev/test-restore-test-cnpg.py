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
ok, why, info = rc.compare(rc.parse_snapshot(snapshot(SRC[:-1])), rc.parse_snapshot(snapshot(SRC)), "primary", "latest")
check("a copy that ends one row AFTER the live snapshot (written during the run) passes, lag 0", ok and info.get("ahead") == 1 and info["lag"] == 0, (why, info))
forked = SRC[:-2] + chain(2, NOW, start=SRC[-2][0])
ok, why, _ = rc.compare(rc.parse_snapshot(snapshot(SRC[:-1])), rc.parse_snapshot(snapshot(forked)), "primary", "latest")
check("a copy that is ahead but does not hold the live newest row (another history) is refused", not ok, why)
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

print("=== a Cluster younger than its first base backup (apps #169) ===")
YOUNG = {"FAKE_CLUSTER_AGE": "3600"}
lines = lambda out, w: [l for l in out.splitlines() if l.startswith(w + " ")]  # noqa: E731
rcode, out, calls, left, cm, _ = flow("latest", good, env_extra=dict(YOUNG, FAKE_BACKUPS="none"))
check("young Cluster, no Backup: four SKIP lines naming the reason, exit 0, nothing restored, no ok series",
      rcode == 0 and len(lines(out, "SKIP")) == 4 and "no completed base backup yet" in out and not lines(out, "FAIL")
      and not any(c.startswith("apply") for c in calls) and "oa_restore_test_ok{" not in cm and left == [], out[-500:])
rcode, out, calls, left, cm, _ = flow("pitr", {n: cut for n in NAMES}, env_extra=dict(YOUNG, FAKE_BACKUPS="running"))
check("PITR on a young Cluster whose only Backup is still running: SKIP, not a target before the base backup",
      rcode == 0 and len(lines(out, "SKIP")) == 4 and not lines(out, "FAIL") and not any(c.startswith("apply") for c in calls), out[-500:])
rcode, out, calls, left, cm, _ = flow("latest", good, env_extra={"FAKE_CLUSTER_AGE": str(5 * 86400), "FAKE_BACKUPS": "none"})
check("a Cluster 5 days old with no completed Backup is a FAILURE (its ScheduledBackups produce nothing), never a skip",
      rcode == 1 and len(lines(out, "FAIL")) == 4 and not lines(out, "SKIP") and "no completed base backup although" in out
      and not any(c.startswith("apply") for c in calls) and cm.count("oa_restore_test_ok{") == 4, out[-500:])
rcode, out, calls, left, cm, _ = flow("latest", good, env_extra=dict(YOUNG, FAKE_BACKUPS="error"))
check("a Backup listing that fails is a FAILURE, never a skip",
      rcode == 1 and len(lines(out, "FAIL")) == 4 and not lines(out, "SKIP") and "backups" in out and not any(c.startswith("apply") for c in calls), out[-500:])
rcode, out, calls, left, cm, _ = flow("latest", flip, env_extra=YOUNG)
check("a base backup exists and the restored copy is corrupted: still a FAILURE on a young Cluster",
      rcode == 1 and len(lines(out, "FAIL")) == 1 and "FAIL zitadel-db/replica/latest" in out and len(lines(out, "PASS")) == 3, out[-500:])
rcode, out, calls, left, cm, _ = flow("latest", good, env_extra=dict(YOUNG, FAKE_BACKUPS="no-stop"))
check("a completed Backup without stoppedAt counts as a base backup (the restore goes on)",
      rcode == 0 and len(lines(out, "PASS")) == 4 and not lines(out, "SKIP"), out[-500:])
# The DR chain's shape (infra scripts/ops/dr-restore-plan.sh, prove_new_archive): a Backup made BY HAND, `dr-base-<cluster>`, with no
# label, taken a few minutes before the restore test; the chain copies it to the replica store, runs the test and requires a PASS
# and no SKIP. A Cluster that young must restore on BOTH stores.
rcode, out, calls, left, cm, _ = flow("latest", good, env_extra=dict(YOUNG, FAKE_BACKUPS="manual", FAKE_BACKUP_STOP=str(time.time() - 300)))
check("a young Cluster whose only base backup is a label-less manual Backup of 5 minutes ago: all four recoveries run and PASS, no SKIP",
      rcode == 0 and len(lines(out, "PASS")) == 4 and not lines(out, "SKIP") and not lines(out, "FAIL")
      and sum(c.startswith("apply") for c in calls) == 4 and cm.count("oa_restore_test_ok{") == 4, out[-500:])
# The point-in-time target must start after the NEWEST completed Backup, a manual one included (matched on spec.cluster.name, not on a label).
rcode, out, calls, left, cm, _ = flow("pitr", {n: cut for n in NAMES}, env_extra={"FAKE_BACKUPS": "mixed", "FAKE_BACKUP_STOP": str(time.time() - 300)})
check("PITR with an old scheduled Backup and a manual one of 5 minutes ago: no target before the manual one, SKIP, nothing restored",
      rcode == 0 and len(lines(out, "SKIP")) == 4 and "after the last base backup" in out and not any(c.startswith("apply") for c in calls), out[-500:])

print("=== dump load: no dump pushed yet (a fresh install before the first cnpg-dump) ===")
# Stage 2 (dump_load.py) on the /restore tree stage 1 (restic-dump.sh) leaves: an `absent` marker is a repository that does not exist
# (restic exit code 10, proven with a real restic in test-restore-test-dumps.sh), an `error` file any other failure. No PostgreSQL is
# needed: a skip or a missing dump ends before initdb. Then stage 3 (restore_cnpg.py --mode dump --publish) on what stage 2 wrote.
ABSENT = "Fatal: repository does not exist: unable to open config file: Stat: The specified key does not exist.\nIs there a repository at the following location?\n"


def dump_stage(layout):
    w = tempfile.mkdtemp()
    for store, how in layout.items():
        os.makedirs(os.path.join(w, "restore", store))
        if how == "absent":
            open(os.path.join(w, "restore", store, "absent"), "w").write(ABSENT)
        elif how:
            open(os.path.join(w, "restore", store, "error"), "w").write(how)
    out = os.path.join(w, "results", "dump.json")
    env = dict(os.environ, RESTORE_DIR=os.path.join(w, "restore"), RESULT=out, TMPDIR=w, NOW=str(NOW))
    r = subprocess.run([sys.executable, "-I", os.path.join(D, "dump_load.py")], env=env, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr, json.load(open(out))["results"]


def dump_publish(results, prev=None):
    w = tempfile.mkdtemp()
    os.makedirs(os.path.join(w, "state", "cm")), os.makedirs(os.path.join(w, "results"))
    json.dump({"results": results, "now": NOW}, open(os.path.join(w, "results", "dump.json"), "w"))
    if prev is not None:
        open(os.path.join(w, "state", "cm", "dump-load"), "w").write(prev)
    env = dict(os.environ, KUBECTL=FAKE, FAKE_STATE=os.path.join(w, "state"), RESULTS=os.path.join(w, "results"), NOW=str(NOW))
    r = subprocess.run([sys.executable, "-I", os.path.join(D, "restore_cnpg.py"), "--mode", "dump", "--publish"], env=env, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr, open(os.path.join(w, "state", "cm", "dump-load")).read()


def ok_lines(text):
    return [l for l in text.splitlines() if l.startswith("oa_restore_test_ok{")]


DL = {(c, st): rc.labels("dump", c, st, "latest") for st in rc.STORES for c in rc.CLUSTERS}
rcode, out, res = dump_stage({"primary": "absent", "replica": "absent"})
check("both repositories absent: the stage ends 0 and skips all four combinations, with the reason",
      rcode == 0 and [r["ok"] for r in res] == [None] * 4 and out.count("SKIP ") == 4 and "FAIL" not in out and "does not exist" in out, out)
rcode, out, res = dump_stage({"primary": "absent", "replica": "restic restore failed"})
check("the primary absent, the replica's restic restore failed: only the replica is a failure",
      [(r["labels"].split('store="')[1].split('"')[0], r["ok"]) for r in res] == [("primary", None)] * 2 + [("replica", False)] * 2
      and "FAIL zitadel-db/replica/dump: restic restore failed" in out and out.count("SKIP ") == 2, out)
rcode, out, res = dump_stage({"primary": "absent", "replica": None})
check("the primary absent, the replica restored nothing and left no error: still a failure (the dump is missing from the snapshot)",
      [r["ok"] for r in res] == [None, None, False, False] and "missing from the latest snapshot" in out, out)
rcode, out, res = dump_stage({"primary": "wrong password", "replica": "wrong password"})
check("an error file without the absent marker (unreadable repository, wrong password, refused key) is a failure on all four",
      [r["ok"] for r in res] == [False] * 4 and "SKIP" not in out, out)

rcode, out, res = dump_stage({"primary": "absent", "replica": "absent"})
rcode, out, cmv = dump_publish(res)
check("all four skipped on a fresh ConfigMap: the Job succeeds, no ok series, no last success, no FAIL (so no RestoreTestFailed)",
      rcode == 0 and not ok_lines(cmv) and "last_success_timestamp_seconds{" not in cmv and "FAIL" not in out, out + cmv)
old = "".join(f'oa_restore_test_ok{{{l}}} 0\noa_restore_test_last_run_timestamp_seconds{{{l}}} 1\n' for l in DL.values())
rcode, out, cmv = dump_publish(res, old)
check("the four ok=0 series a fresh install published before are gone after a skipping run (the alert resolves at once)",
      rcode == 0 and not ok_lines(cmv), cmv)
prev = rc.build_metrics([{"labels": DL[("zitadel-db", "primary")], "ok": True, "duration": 3, "recovery": 2, "lag": 9, "missing": None}], NOW - 86400, {})
rcode, out, cmv = dump_publish(res, prev)
check("a store that passed before and has NO repository now is a FAILURE (a dump that existed is gone), only that one",
      rcode == 1 and "FAIL zitadel-db/primary/dump" in out and "passed before" in out and out.count("FAIL") == 1
      and ok_lines(cmv) == [f"oa_restore_test_ok{{{DL[('zitadel-db', 'primary')]}}} 0"], out + cmv)
check("and its last success is kept, not moved to now",
      f"oa_restore_test_last_success_timestamp_seconds{{{DL[('zitadel-db', 'primary')]}}} {NOW - 86400:.0f}" in cmv, cmv)
rcode, out, res = dump_stage({"primary": "absent", "replica": "restic restore failed"})
rcode, out, cmv = dump_publish(res)
check("the primary absent and the replica broken: the Job fails and only the two replica combinations say ok=0",
      rcode == 1 and ok_lines(cmv) == [f"oa_restore_test_ok{{{DL[(c, 'replica')]}}} 0" for c in rc.CLUSTERS], out + cmv)

print("=== dump results are published ===")
w = tempfile.mkdtemp()
os.makedirs(os.path.join(w, "state")), os.makedirs(os.path.join(w, "results"))
labs = [rc.labels("dump", c, st, "latest") for st in rc.STORES for c in rc.CLUSTERS]
res = [{"labels": l, "ok": i != 2, "duration": 3, "recovery": 2, "lag": 100, "missing": None} for i, l in enumerate(labs)]
json.dump({"results": res, "now": NOW}, open(os.path.join(w, "results", "dump.json"), "w"))
env = dict(os.environ, KUBECTL=FAKE, FAKE_STATE=os.path.join(w, "state"), RESULTS=os.path.join(w, "results"))
r = subprocess.run([sys.executable, "-I", os.path.join(D, "restore_cnpg.py"), "--mode", "dump", "--publish"], env=env, capture_output=True, text=True)
cmv = open(os.path.join(w, "state", "cm", "dump-load")).read()
check("a failed dump load fails the publishing container (so the Job and kube-state-metrics see it)", r.returncode == 1, r.stdout + r.stderr)
check("the metrics say kind=dump, ok=0 for exactly the failed store and dataset",
      cmv.count('kind="dump"') >= 4 and [l for l in cmv.splitlines() if l.startswith("oa_restore_test_ok{") and l.endswith(" 0")] == [f"oa_restore_test_ok{{{labs[2]}}} 0"], cmv)

print("=== exporter ===")
sp = importlib.util.spec_from_file_location("sm", os.path.join(D, "serve_metrics.py"))
sm = importlib.util.module_from_spec(sp)
sp.loader.exec_module(sm)
with tempfile.TemporaryDirectory() as t:
    a, b = os.path.join(t, "a"), os.path.join(t, "b")
    os.makedirs(a), os.makedirs(b)
    open(os.path.join(a, "metrics"), "w").write("# HELP oa_x one\n# TYPE oa_x gauge\noa_x{d=\"1\"} 1\n# HELP oa_y y\n# TYPE oa_y gauge\noa_y 2\n")
    open(os.path.join(b, "k1"), "w").write("# HELP oa_x one\n# TYPE oa_x gauge\noa_x{d=\"2\"} 3\n")
    open(os.path.join(b, "k2"), "w").write("# no result yet\n")
    m = sm.merge([a, b, os.path.join(t, "absent")])
    check("the exporter merges the files: HELP and TYPE once, the samples of a metric together",
          m.count("# HELP oa_x") == 1 and m.count("# TYPE oa_x") == 1 and m.index('oa_x{d="2"}') < m.index("# HELP oa_y"), m)
    check("a comment-only file adds nothing", "no result yet" not in m)

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
