#!/usr/bin/env python3
"""A kubectl double for test-restore-test-cnpg.py: just enough of the CNPG restore test's calls, with a state directory.

FAKE_STATE   directory (calls.log, scratch/<name>.json, cm/<key>)
FAKE_SNAPS   directory with <scratch cluster name>.snap: what `exec psql` prints in that scratch Cluster
FAKE_MODE    healthy (default) | never-healthy | stuck-finalizer
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
        say({"spec": {"imageName": "ghcr.io/cloudnative-pg/postgresql:18.6", "storage": {"size": "5Gi"},
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
    stop = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(os.environ.get("FAKE_BACKUP_STOP", time.time() - 90000))))
    say({"items": [{"status": {"phase": "completed", "stoppedAt": stop}}]})
elif verb == "get" and a[1] == "configmap":
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
