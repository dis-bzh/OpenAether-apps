#!/usr/bin/env python3
"""A kubectl double for test-restore-test-cnpg.py: just enough of the CNPG restore test's calls, with a state directory.

FAKE_STATE   directory (calls.log, scratch/<name>.json, cm/<key>)
FAKE_SNAPS   directory with <scratch cluster name>.snap: what `exec psql` prints in that scratch Cluster
FAKE_MODE    healthy (default) | never-healthy | stuck-finalizer
FAKE_BACKUPS the Backups of the namespace: completed (default: one per live Cluster, made by a ScheduledBackup, so labelled
             cnpg.io/cluster, stopped at FAKE_BACKUP_STOP) | manual (one per live Cluster, made by hand like the DR chain's
             dr-base-<cluster>: NO label, only spec.cluster.name) | mixed (the manual one at FAKE_BACKUP_STOP and a scheduled one
             25 h old) | none | running (one, not completed) | no-stop (one completed, no stoppedAt) | error (the listing fails).
             Every list also holds a completed Backup of ANOTHER cluster (other-db), which must never count.
             The double honours `-l key=value` on metadata.labels, as the API server does.
FAKE_CLUSTER_AGE  seconds since the live Cluster was created (default 10 days)
FAKE_CM_GET  fail: every `get configmap` exits 1 (an API error while the publishing stage reads what it published before)
"""
import json
import os
import sys
import time

S = os.environ["FAKE_STATE"]
MODE = os.environ.get("FAKE_MODE", "healthy")
a = sys.argv[1:]
open(os.path.join(S, "calls.log"), "a").write(" ".join(a) + "\n")
os.makedirs(os.path.join(S, "scratch"), exist_ok=True)
os.makedirs(os.path.join(S, "cm"), exist_ok=True)


def opt(flag):
    return a[a.index(flag) + 1] if flag in a else None


ns = opt("-n")
out = "-o" in a and opt("-o")
stdin = sys.stdin.read() if a[0] in ("apply", "exec") else ""


def path(n):
    return os.path.join(S, "scratch", n + ".json")


def say(o):
    print(json.dumps(o) if not isinstance(o, str) else o)
    sys.exit(0)


def fail(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(1)


verb = a[0]
if verb == "get" and a[1].startswith("cluster.postgresql"):
    if ns == "foundation-databases":
        say({"metadata": {"creationTimestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - float(os.environ.get("FAKE_CLUSTER_AGE", "864000"))))},
             "spec": {"imageName": "ghcr.io/cloudnative-pg/postgresql:18.6", "storage": {"size": "5Gi"},
                      "postgresql": {"parameters": {"max_connections": "200"}}}})
    names = [f[:-5] for f in sorted(os.listdir(os.path.join(S, "scratch"))) if f.endswith(".json")]
    if a[2].startswith("-"):  # list
        say({"items": [json.load(open(path(n))) for n in names]})
    n = a[2]
    if n not in names:
        fail(f'Error from server (NotFound): clusters "{n}" not found')
    o = json.load(open(path(n)))
    if out == "name":
        say("cluster.postgresql.cnpg.io/" + n)
    age = time.time() - o["_created"]
    healthy = MODE != "never-healthy" and age >= 0.05
    o["status"] = {"phase": "Cluster in healthy state" if healthy else "Setting up primary",
                   "readyInstances": 1 if healthy else 0, "currentPrimary": n + "-1"}
    say(o)
elif verb == "get" and a[1].startswith("backups"):
    ts = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
    stop = ts(float(os.environ.get("FAKE_BACKUP_STOP", time.time() - 90000)))
    kind = os.environ.get("FAKE_BACKUPS", "completed")
    if kind == "error":
        fail("Error from server (Forbidden): backups.postgresql.cnpg.io is forbidden")

    def backup(name, cluster, phase, stopped=None, labelled=True):
        st = {"phase": phase}
        if stopped:
            st["stoppedAt"] = stopped
        return {"metadata": {"name": name, "labels": {"cnpg.io/cluster": cluster} if labelled else {}},
                "spec": {"cluster": {"name": cluster}}, "status": st}

    items = [backup("other-db-daily", "other-db", "completed", ts(time.time() - 400000))]
    for c in ("zitadel-db", "grafana-db"):
        items += {"completed": [backup(c + "-daily", c, "completed", stop)],
                  "manual": [backup("dr-base-" + c, c, "completed", stop, labelled=False)],
                  "mixed": [backup(c + "-daily", c, "completed", ts(time.time() - 90000)),
                            backup("dr-base-" + c, c, "completed", stop, labelled=False)],
                  "none": [], "running": [backup(c + "-daily", c, "started")],
                  "no-stop": [backup(c + "-daily", c, "completed")]}[kind]
    sel = opt("-l")
    if sel:
        k, _, v = sel.partition("=")
        items = [b for b in items if b["metadata"]["labels"].get(k) == v]
    say({"items": items})
elif verb == "get" and a[1] == "configmap":
    if os.environ.get("FAKE_CM_GET") == "fail":
        fail("Error from server (ServiceUnavailable): the server is currently unable to handle the request")
    key = a[-1].split(".data.")[-1].rstrip("}")
    p = os.path.join(S, "cm", key)
    print(open(p).read() if os.path.exists(p) else "", end="")
elif verb == "apply":
    o = json.loads(stdin)
    o["_created"] = time.time()
    json.dump(o, open(path(o["metadata"]["name"]), "w"))
    open(os.path.join(S, "applied.jsonl"), "a").write(json.dumps(o) + "\n")
elif verb == "delete" and a[1].startswith("cluster.postgresql"):
    n = a[2]
    if os.path.exists(path(n)) and not (MODE == "stuck-finalizer" and not os.path.exists(path(n) + ".unstuck")):
        os.remove(path(n))
elif verb == "patch" and a[1].startswith("cluster.postgresql"):
    open(path(a[2]) + ".unstuck", "w").write("1")
elif verb == "delete":
    pass
elif verb == "patch" and a[1] == "configmap":
    for k, v in json.loads(opt("-p"))["data"].items():
        open(os.path.join(S, "cm", k), "w").write(v)
elif verb == "exec":
    pod = a[a.index("-c") - 1]
    f = os.path.join(os.environ["FAKE_SNAPS"], pod[:-2] + ".snap")
    if not os.path.exists(f):
        fail("no snapshot for " + pod)
    print(open(f).read(), end="")
else:
    fail("fake kubectl: unhandled " + " ".join(a))
