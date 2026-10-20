#!/usr/bin/env python3
"""CNPG restore test (0.4.0 M3): recover the archive into a scratch Cluster, compare it with the live database, clean up.

For each Cluster (zitadel-db, grafana-db) and each store (primary, replica) the test creates ONE single-instance Cluster in
the scratch namespace, bootstrapped by `recovery` from the ObjectStore of that store, waits until it is healthy, runs
snapshot.sql inside it and compares the result with the snapshot stage 1 took of the live database (source-snapshot.sh).
It then deletes the scratch Cluster, whatever happened, and publishes the result as metrics (oa_restore_test_*).

What is compared (the canary: oa_canary, a counter plus a hash chain written every 5 minutes by canary-write.sh):
  latest  the restored chain is intact, its newest row is in the live chain with the SAME hash, and it is not older than
          the lag budget of the store (the RPO of that store, see LAG_BUDGET); the restored database has tables.
  pitr    the target is the time of one canary row older than 2 hours and newer than the last base backup: the restored
          chain must end at exactly that row, with the hash the live chain holds for it.
Table list and row counts are exported and reported; they are NOT exact (the live side keeps moving), so only a restored
table that is empty while the live one holds more than 1000 rows fails the test.

The guard: every Cluster write names the scratch namespace and an object name that starts with `rt-`; the only write in
foundation-databases is the metrics ConfigMap. The ServiceAccount's RBAC says the same (restore-test-rbac.yaml) and a Kyverno
policy refuses any restore-test Cluster outside the scratch namespace (restore-test-scratch-only).
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time

KUBECTL = os.environ.get("KUBECTL", "kubectl")
SCRATCH_NS = "backup-restore-test"
LIVE_NS = "foundation-databases"
PREFIX = "rt-"
CM = "oa-restore-test-metrics"
CLUSTERS = {"zitadel-db": "zitadel", "grafana-db": "grafana"}  # cluster -> database
STORES = ("primary", "replica")
# Seconds the restored copy may trail the live database. primary: WAL is archived at least every 5 min (archive_timeout) plus
# upload; replica: the copy job runs every 15 min plus its own duration. Margins included. docs: backup-cnpg.md.
LAG_BUDGET = {"primary": 1200.0, "replica": 3000.0}
HEALTHY_TIMEOUT = float(os.environ.get("HEALTHY_TIMEOUT", "900"))
DELETE_TIMEOUT = float(os.environ.get("DELETE_TIMEOUT", "300"))
POLL = float(os.environ.get("POLL", "5"))
PITR_MIN_AGE = float(os.environ.get("PITR_MIN_AGE", "7200"))  # the target row is at least this old, so its WAL is archived on both stores
PITR_AFTER_BACKUP = 300.0   # and at least this much after the end of the base backup it starts from


class Skip(Exception):
    """Nothing to compare yet (a fresh cluster): not a failure, not a success."""


class Terminated(BaseException):
    pass


# ── pure parts (tested without a cluster) ─────────────────────────────────────────────────────────────────────────────
def parse_snapshot(text):
    """The tagged blocks of snapshot.sql -> {tables: set, rows: {t: int}, canary: [(counter, ts, sha)]}."""
    out = {"tables": set(), "rows": {}, "canary": []}
    block = None
    for line in text.splitlines():
        if line.startswith("##"):
            block = line[2:]
            continue
        if not line.strip() or block is None:
            continue
        f = line.split("\t")
        if block == "tables":
            out["tables"].add(f[0])
        elif block == "rows" and len(f) == 2 and f[1].isdigit():
            out["rows"][f[0]] = int(f[1])
        elif block == "canary" and len(f) == 3 and f[0].isdigit():
            out["canary"].append((int(f[0]), f[1], f[2]))
    return out


def chain_sha(prev_sha, counter, ts):
    return hashlib.sha256(f"{prev_sha}|{counter}|{ts}".encode()).hexdigest()


def chain_errors(rows):
    """rows sorted by counter. The first row has no predecessor in the window, so it is only checked for being a row."""
    errs = []
    for i in range(1, len(rows)):
        (pc, _, ps), (c, ts, sha) = rows[i - 1], rows[i]
        if c != pc + 1:
            errs.append(f"counter jumps from {pc} to {c}")
        elif sha != chain_sha(ps, c, ts):
            errs.append(f"hash of row {c} does not follow row {pc}")
        if len(errs) >= 3:
            break
    return errs


def epoch(ts):
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=datetime.timezone.utc).timestamp()


def pick_pitr_target(src_canary, backup_stop, now):
    """The newest canary row that is old enough and after the base backup. Returns (counter, ts) or None."""
    best = None
    for c, ts, _ in src_canary:
        t = epoch(ts)
        if t <= now - PITR_MIN_AGE and (backup_stop is None or t >= backup_stop + PITR_AFTER_BACKUP):
            best = (c, ts)
    return best


def target_time(ts):
    """Postgres recovery_target_time for a row written at ts: the row's own second plus 2 s (the commit follows the
    clock_timestamp() of the insert by milliseconds; the next row is 5 minutes later)."""
    t = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f") + datetime.timedelta(seconds=2)
    return t.strftime("%Y-%m-%d %H:%M:%S+00")


def compare(src, res, store, mode, pitr_row=None):
    """-> (ok, reasons, info). src/res are parse_snapshot results. Raises Skip when the live side has no canary yet."""
    info = {"lag": None, "missing": len(src["tables"] - res["tables"])}
    if not src["canary"]:
        raise Skip("the live database has no canary row yet (writer not run)")
    why = []
    if not res["tables"]:
        why.append("the restored database has no table")
    if not res["canary"]:
        why.append("the restored database has no canary row")
        return False, why, info
    why += [f"restored canary: {e}" for e in chain_errors(res["canary"])]
    live = {c: (ts, sha) for c, ts, sha in src["canary"]}
    c, ts, sha = res["canary"][-1]
    if c in live and live[c][1] != sha:
        why.append(f"restored canary row {c} has another hash than the live one")
    elif c not in live and c < src["canary"][0][0]:
        why.append(f"restored canary row {c} is older than the whole live window")
    elif c not in live:
        why.append(f"restored canary row {c} is newer than anything live: not from this database")
    if c in live:
        info["lag"] = epoch(src["canary"][-1][1]) - epoch(live[c][0])
    if mode == "pitr":
        want = pitr_row[0]
        if c != want:
            why.append(f"point in time recovery ended at canary row {c}, wanted exactly {want}")
    elif info["lag"] is not None and info["lag"] > LAG_BUDGET[store]:
        why.append(f"the restored copy trails the live database by {info['lag']:.0f} s (budget {LAG_BUDGET[store]:.0f} s)")
    for t, n in src["rows"].items():
        if n > 1000 and res["rows"].get(t) == 0:
            why.append(f"table {t} is empty after the restore, {n} rows live")
    return not why, why, info


DUMP_MAX_AGE = 26 * 3600.0  # the dump job runs daily (RPO 24 h): the newest canary row of the latest dump is at most this old


def check_dump(res, now):
    """A dump loaded into a private server has no live side to compare with (nothing in the job holds a database credential):
    the canary proves it from the inside. -> (ok, reasons, info)."""
    why = []
    info = {"tables": len(res["tables"]), "lag": None}
    if not res["tables"]:
        why.append("the loaded dump has no table")
    if not res["canary"]:
        why.append("the loaded dump has no canary row")
        return False, why, info
    why += [f"canary: {e}" for e in chain_errors(res["canary"])]
    info["lag"] = now - epoch(res["canary"][-1][1])
    if info["lag"] > DUMP_MAX_AGE:
        why.append(f"the newest canary row is {info['lag'] / 3600:.1f} h old: the latest dump is stale (budget {DUMP_MAX_AGE / 3600:.0f} h)")
    return not why, why, info


def scratch_name(cluster, store, mode):
    return f"{PREFIX}{cluster}-{store}-{mode}"


def guard(ns, name):
    """Never write outside the scratch namespace or to an object that is not a scratch one."""
    if ns != SCRATCH_NS or not name.startswith(PREFIX) or name in CLUSTERS:
        raise RuntimeError(f"refusing to touch {ns}/{name}: restore tests only act on {SCRATCH_NS}/{PREFIX}*")


def recovery_manifest(cluster, store, mode, source, target=None):
    """The scratch Cluster. No `plugins` entry with isWALArchiver: the copy never writes to a store."""
    name = scratch_name(cluster, store, mode)
    guard(SCRATCH_NS, name)
    spec = source.get("spec", {})
    params = dict((spec.get("postgresql") or {}).get("parameters") or {})  # max_connections must not be lower than the source's
    rec = {"source": "origin"}
    if target:
        rec["recoveryTarget"] = {"targetTime": target}
    return {
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
        "metadata": {"name": name, "namespace": SCRATCH_NS, "labels": {
            "openaether.io/managed": "true", "openaether.io/component": "restore-test",
            "openaether.io/restore-test": "true"}},
        "spec": {
            "instances": 1,
            "imageName": spec["imageName"],
            "enableSuperuserAccess": False,
            "storage": {"size": spec.get("storage", {}).get("size", "5Gi"), "storageClass": "local-path"},
            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "768Mi"}},
            "postgresql": {"parameters": params} if params else {},
            "bootstrap": {"recovery": rec},
            "externalClusters": [{"name": "origin", "plugin": {
                "name": "barman-cloud.cloudnative-pg.io",
                "parameters": {"barmanObjectName": f"{cluster}-{store}", "serverName": cluster}}}],
        },
    }


LABELS = ("kind", "dataset", "store", "mode")


def labels(kind, dataset, store, mode):
    return f'kind="{kind}",dataset="{dataset}",store="{store}",mode="{mode}"'


def parse_previous(text):
    prev = {}
    for m in re.finditer(r"^oa_restore_test_last_success_timestamp_seconds\{([^}]*)\} ([0-9.e+]+)$", text or "", re.M):
        prev[m.group(1)] = float(m.group(2))
    return prev


def build_metrics(results, now, previous):
    """results: [{labels, ok (True/False/None=skipped), duration, recovery, lag, missing}] -> Prometheus text."""
    names = {
        "oa_restore_test_ok": "1 when the last restore test passed, 0 when it failed (absent: never run or skipped)",
        "oa_restore_test_last_success_timestamp_seconds": "Unix time of the last restore test that passed",
        "oa_restore_test_last_run_timestamp_seconds": "Unix time the restore test last finished (pass or fail)",
        "oa_restore_test_duration_seconds": "Seconds the last restore test took, scratch cleanup included",
        "oa_restore_test_recovery_seconds": "Seconds from the scratch Cluster's creation to its healthy state (restore RTO of the data)",
        "oa_restore_test_data_lag_seconds": "How far the restored data trailed the live database (canary clock)",
        "oa_restore_test_tables_missing": "Live tables the restored database lacks (informative: the live side moves)",
    }
    lines = {n: [] for n in names}
    seen = set()
    for r in results:
        lab = r["labels"]
        seen.add(lab)
        if r["ok"] is not None:
            lines["oa_restore_test_ok"].append(f"oa_restore_test_ok{{{lab}}} {int(r['ok'])}")
            lines["oa_restore_test_last_run_timestamp_seconds"].append(f"oa_restore_test_last_run_timestamp_seconds{{{lab}}} {now:.0f}")
        last = now if r["ok"] else previous.get(lab)
        if last is not None:
            lines["oa_restore_test_last_success_timestamp_seconds"].append(f"oa_restore_test_last_success_timestamp_seconds{{{lab}}} {last:.0f}")
        for key, name in (("duration", "oa_restore_test_duration_seconds"), ("recovery", "oa_restore_test_recovery_seconds"),
                          ("lag", "oa_restore_test_data_lag_seconds"), ("missing", "oa_restore_test_tables_missing")):
            if r.get(key) is not None:
                lines[name].append(f"{name}{{{lab}}} {r[key]:.0f}")
    for lab, t in previous.items():  # a dataset this run did not reach keeps its last success
        if lab not in seen:
            lines["oa_restore_test_last_success_timestamp_seconds"].append(f"oa_restore_test_last_success_timestamp_seconds{{{lab}}} {t:.0f}")
    out = []
    for n, h in names.items():
        out.append(f"# HELP {n} {h}\n# TYPE {n} gauge")
        out.extend(lines[n])
    return "\n".join(out) + "\n"


# ── cluster side ──────────────────────────────────────────────────────────────────────────────────────────────────────
def kube(*args, stdin=None, timeout=120, check=True):
    r = subprocess.run([KUBECTL, *args], input=stdin, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {r.stderr.strip()[:300]}")
    return r


def kube_json(*args, **kw):
    return json.loads(kube(*args, "-o", "json", **kw).stdout or "{}")


def exists(name):
    return kube("get", "cluster.postgresql.cnpg.io", name, "-n", SCRATCH_NS, "-o", "name", check=False).returncode == 0


def cleanup(name):
    """Delete the scratch Cluster and what it leaves, finalizer-safe. Never raises."""
    try:
        guard(SCRATCH_NS, name)
        kube("delete", "cluster.postgresql.cnpg.io", name, "-n", SCRATCH_NS, "--wait=false", "--ignore-not-found", check=False)
        deadline = time.time() + DELETE_TIMEOUT
        while time.time() < deadline and exists(name):
            time.sleep(POLL)
        if exists(name):  # stuck on a finalizer: drop it, the objects below go with the sweep
            kube("patch", "cluster.postgresql.cnpg.io", name, "-n", SCRATCH_NS, "--type", "merge", "-p",
                 '{"metadata":{"finalizers":null}}', check=False)
            kube("delete", "cluster.postgresql.cnpg.io", name, "-n", SCRATCH_NS, "--wait=false", "--ignore-not-found", check=False)
        for kind in ("job", "pod", "pvc"):
            kube("delete", kind, "-n", SCRATCH_NS, "-l", f"cnpg.io/cluster={name}", "--wait=false", "--ignore-not-found", check=False)
        return not exists(name)
    except Exception as e:  # noqa: BLE001 - cleanup must not hide the result
        print(f"WARN: cleanup of {name}: {e}")
        return False


def sweep_leftovers():
    """A previous run that was killed leaves scratch Clusters: remove those older than the Job deadline."""
    r = kube("get", "cluster.postgresql.cnpg.io", "-n", SCRATCH_NS, "-o", "json", check=False)
    if r.returncode != 0:
        return
    for c in json.loads(r.stdout or "{}").get("items", []):
        n = c["metadata"]["name"]
        age = time.time() - datetime.datetime.strptime(c["metadata"]["creationTimestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
        if n.startswith(PREFIX) and age > 3600:
            print(f"sweep: removing the leftover {n} ({age:.0f} s old)")
            cleanup(n)


def wait_healthy(name):
    deadline = time.time() + HEALTHY_TIMEOUT
    last = ""
    while time.time() < deadline:
        c = kube_json("get", "cluster.postgresql.cnpg.io", name, "-n", SCRATCH_NS, check=False) if exists(name) else {}
        st = c.get("status", {})
        last = st.get("phase", "")
        if st.get("readyInstances") == 1 and last == "Cluster in healthy state":
            return st["currentPrimary"]
        time.sleep(POLL)
    raise RuntimeError(f"not healthy after {HEALTHY_TIMEOUT} s (phase: {last or 'none'})")


def last_backup_stop(cluster):
    """End of the newest completed base backup of the live cluster (epoch) or None."""
    items = kube_json("get", "backups.postgresql.cnpg.io", "-n", LIVE_NS, "-l", f"cnpg.io/cluster={cluster}").get("items", [])
    stops = []
    for b in items:
        st = b.get("status", {})
        if st.get("phase") == "completed" and st.get("stoppedAt"):
            stops.append(datetime.datetime.strptime(st["stoppedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp())
    return max(stops) if stops else None


def run_one(cluster, store, mode, work, now_fn=time.time):
    """-> result dict. Never leaves the scratch Cluster behind."""
    lab = labels("cnpg", cluster, store, mode)
    res = {"labels": lab, "ok": False, "duration": None, "recovery": None, "lag": None, "missing": None}
    name = scratch_name(cluster, store, mode)
    t0 = now_fn()
    created = False
    try:
        guard(SCRATCH_NS, name)
        src_file = os.path.join(work, f"{cluster}.src")
        if not os.path.exists(src_file):
            raise RuntimeError("stage 1 could not read the live database (see its log)")
        src = parse_snapshot(open(src_file).read())
        if not src["canary"]:
            raise Skip("the live database has no canary row yet (writer not run)")
        source = kube_json("get", "cluster.postgresql.cnpg.io", cluster, "-n", LIVE_NS)
        target, pitr_row = None, None
        if mode == "pitr":
            pitr_row = pick_pitr_target(src["canary"], last_backup_stop(cluster), now_fn())
            if not pitr_row:
                raise Skip("no canary row is both older than 2 h and after the last base backup")
            target = target_time(pitr_row[1])
        if exists(name):  # a stale object of the same name is a leftover, not an input
            cleanup(name)
        created = True
        kube("apply", "-n", SCRATCH_NS, "-f", "-", stdin=json.dumps(recovery_manifest(cluster, store, mode, source, target)))
        t1 = now_fn()
        pod = wait_healthy(name)
        res["recovery"] = now_fn() - t1
        sql = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshot.sql")).read()
        out = kube("exec", "-i", "-n", SCRATCH_NS, pod, "-c", "postgres", "--", "psql", "-X", "-At", "-F", "\t",
                   "-U", "postgres", "-d", CLUSTERS[cluster], stdin=sql, timeout=300).stdout
        ok, why, info = compare(src, parse_snapshot(out), store, mode, pitr_row)
        res.update(ok=ok, lag=info["lag"], missing=info["missing"])
        print(("PASS " if ok else "FAIL ") + f"{cluster}/{store}/{mode}: " + ("; ".join(why) if why else
              f"canary identical to the live chain (lag {info['lag']:.0f} s), recovered healthy in {res['recovery']:.0f} s"))
    except Skip as e:
        res["ok"] = None
        print(f"SKIP {cluster}/{store}/{mode}: {e}")
    except Terminated:
        print(f"FAIL {cluster}/{store}/{mode}: terminated (deadline)")
        cleanup(name)
        raise
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {cluster}/{store}/{mode}: {e}")
    finally:
        if created and not cleanup(name):
            print(f"ERROR: could not remove {SCRATCH_NS}/{name}; the leftover alert and the next run's sweep handle it")
            res["ok"] = False
    res["duration"] = now_fn() - t0
    return res


def publish(key, text):
    r = kube("patch", "configmap", CM, "-n", LIVE_NS, "--type", "merge", "-p", json.dumps({"data": {key: text}}), check=False)
    if r.returncode != 0:
        print("ERROR: cannot publish the metrics:", r.stderr.strip()[:300])
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("latest", "pitr", "dump"), required=True,
                    help="dump: publish the results dump_load.py left in $RESULTS (the previous stage of that Job)")
    ap.add_argument("--work", default=os.environ.get("WORK", "/work"))
    ap.add_argument("--out", default="")
    ap.add_argument("--publish", action="store_true")
    a = ap.parse_args()
    key = "dump-load" if a.mode == "dump" else "cnpg-" + a.mode

    def term(*_):
        raise Terminated()
    signal.signal(signal.SIGTERM, term)
    previous = {}
    if a.publish:
        r = kube("get", "configmap", CM, "-n", LIVE_NS, "-o", "jsonpath={.data." + key + "}", check=False)
        previous = parse_previous(r.stdout)
    results = []
    if a.mode == "dump":
        results = json.load(open(os.path.join(os.environ.get("RESULTS", "/results"), "dump.json")))["results"]
    else:
        try:
            sweep_leftovers()
            for cluster in CLUSTERS:
                for store in STORES:
                    results.append(run_one(cluster, store, a.mode, a.work))
        except Terminated:
            pass
    text = build_metrics(results, float(os.environ.get("NOW") or time.time()), previous)
    if a.out:
        open(a.out, "w").write(text)
    rc = 0 if all(r["ok"] is not False for r in results) and len(results) == len(CLUSTERS) * len(STORES) else 1
    if a.publish and not publish(key, text):
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
