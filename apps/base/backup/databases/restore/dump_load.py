#!/usr/bin/env python3
"""Stage 2 of the dump restore test (PostgreSQL image, user postgres): load each restored dump into a throwaway server and
check what it holds. The dump is the one copy that does not depend on the CNPG archive or on a CNPG version, so this is the
recovery path of last resort: it must LOAD, not just list.

For each store and database: initdb in a temporary directory, start a private server (no network, unix socket only),
pg_restore --exit-on-error, run snapshot.sql, judge with restore_cnpg.check_dump (tables, canary chain, age of the newest
canary row), stop and delete the server. Writes the results as JSON for the publishing stage; never fails the Job itself.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import restore_cnpg as rc  # noqa: E402

RESTORE = os.environ.get("RESTORE_DIR", "/restore")
OUT = os.environ.get("RESULT", "/results/dump.json")
TMP = os.environ.get("TMPDIR", "/tmp")
SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshot.sql")


def binary(name):
    p = shutil.which(name)
    if p:
        return p
    found = sorted(glob.glob(f"/usr/lib/postgresql/*/bin/{name}"))
    if not found:
        raise RuntimeError(f"{name} not found")
    return found[-1]


def run(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 900), **kw)


def load_one(store, cluster, now):
    db = rc.CLUSTERS[cluster]
    lab = rc.labels("dump", cluster, store, "latest")
    res = {"labels": lab, "ok": False, "duration": None, "recovery": None, "lag": None, "missing": None}
    t0 = time.time()
    d = os.path.join(TMP, f"pgdata-{store}-{cluster}")
    sock = os.path.join(TMP, f"pgsock-{store}-{cluster}")
    started = False
    try:
        err = os.path.join(RESTORE, store, "error")
        if os.path.exists(err):
            raise RuntimeError(open(err).read().strip()[:200] or "restic restore failed")
        files = glob.glob(os.path.join(RESTORE, store, "**", f"{cluster}.dump"), recursive=True)
        if not files:
            raise RuntimeError(f"{cluster}.dump is missing from the latest snapshot of the {store} repository")
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(sock, ignore_errors=True)
        os.makedirs(sock)
        r = run(binary("initdb"), "-D", d, "-U", "postgres", "-A", "trust", "--no-sync")
        if r.returncode:
            raise RuntimeError("initdb: " + r.stderr.strip()[:200])
        opts = f"-c listen_addresses='' -c unix_socket_directories={sock} -c fsync=off -c max_connections=20 -c shared_buffers=32MB"
        r = run(binary("pg_ctl"), "-D", d, "-o", opts, "-l", os.path.join(d, "log"), "-w", "-t", "120", "start")
        if r.returncode:
            raise RuntimeError("pg_ctl start: " + r.stderr.strip()[:200])
        started = True
        base = ["-h", sock, "-U", "postgres"]
        r = run(binary("createdb"), *base, db)
        if r.returncode:
            raise RuntimeError("createdb: " + r.stderr.strip()[:200])
        t1 = time.time()
        r = run(binary("pg_restore"), *base, "-d", db, "--no-owner", "--no-acl", "--exit-on-error", files[0])
        if r.returncode:
            raise RuntimeError("pg_restore: " + r.stderr.strip()[:300])
        res["recovery"] = time.time() - t1
        r = run(binary("psql"), "-X", "-At", "-F", "\t", *base, "-d", db, "-f", SQL)
        ok, why, info = rc.check_dump(rc.parse_snapshot(r.stdout), now)
        res.update(ok=ok, lag=info["lag"])
        print(("PASS " if ok else "FAIL ") + f"{cluster}/{store}/dump: " + ("; ".join(why) if why else
              f"loaded, canary chain intact, newest row {info['lag']:.0f} s old, {info['tables']} tables"))
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {cluster}/{store}/dump: {e}")
    finally:
        if started:
            run(binary("pg_ctl"), "-D", d, "-m", "immediate", "-w", "stop")
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(sock, ignore_errors=True)
    res["duration"] = time.time() - t0
    return res


def main():
    now = float(os.environ.get("NOW") or time.time())
    results = [load_one(s, c, now) for s in rc.STORES for c in rc.CLUSTERS]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"results": results, "now": now}, open(OUT, "w"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
