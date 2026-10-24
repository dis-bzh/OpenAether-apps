#!/usr/bin/env python3
"""A kubectl double for test-restore-test-longhorn.py: the objects restore_longhorn.py reads and writes, with a state directory.

FAKE_STATE   directory (calls.log, obj/<kind>_<name>.json, cm/<key>)
FAKE_BACKUP  JSON file: the Backup status (state, url, backupCreatedAt, volumeSize); absent file = no backup volume
FAKE_DATA    directory the verify pod "sees" as /data (the restored volume); the pod's log is the real canary_volume.py verdict
FAKE_MODE    ok (default) | never-restored | faulted | stuck-volume
"""
import json
import os
import subprocess
import sys
import tempfile
import time

S = os.environ["FAKE_STATE"]
MODE = os.environ.get("FAKE_MODE", "ok")
HERE = os.path.dirname(os.path.abspath(__file__))
CANARY = os.environ["FAKE_CANARY_SCRIPT"]
a = sys.argv[1:]
open(os.path.join(S, "calls.log"), "a").write(" ".join(a) + "\n")
os.makedirs(os.path.join(S, "obj"), exist_ok=True)
os.makedirs(os.path.join(S, "cm"), exist_ok=True)


def opt(f):
    return a[a.index(f) + 1] if f in a else None


def path(kind, name):
    return os.path.join(S, "obj", f"{kind}_{name}.json")


def fail(m):
    sys.stderr.write(m + "\n")
    sys.exit(1)


def say(o):
    print(o if isinstance(o, str) else json.dumps(o))
    sys.exit(0)


verb, what = a[0], (a[1] if len(a) > 1 else "")
stdin = sys.stdin.read() if verb == "apply" else ""
if verb == "get" and what == "pvc":
    if not os.environ.get("FAKE_PVC"):
        fail('Error from server (NotFound): persistentvolumeclaims "oa-canary" not found')
    say(os.environ["FAKE_PVC"])
elif verb == "get" and what == "backupvolumes.longhorn.io":
    # like Longhorn: named "<volume>-<suffix>", found by the label backup-volume=<volume>, never by the bare volume name
    if len(a) > 2 and not a[2].startswith("-"):
        fail('Error from server (NotFound): backupvolumes.longhorn.io "%s" not found' % a[2])
    if not os.environ.get("FAKE_LAST"):
        say({"items": []})
    say({"items": [{"metadata": {"name": "vol-6a055fab", "labels": {"backup-volume": "vol"}},
                    "status": {"lastBackupName": os.environ["FAKE_LAST"], "lastBackupAt": "2026-10-20T01:00:00Z"}}]})
elif verb == "get" and what == "backups.longhorn.io":
    say({"status": json.load(open(os.environ["FAKE_BACKUP"]))})
elif verb == "get" and what in ("volumes.longhorn.io", "pod"):
    kind, name = ("volume" if what.startswith("volumes") else "pod"), a[2]
    p = path(kind, name)
    if not os.path.exists(p):
        fail(f'Error from server (NotFound): {what} "{name}" not found')
    o = json.load(open(p))
    if opt("-o") == "name":
        say(what + "/" + name)
    age = time.time() - o["_created"]
    if kind == "volume":
        if MODE == "never-restored":
            o["status"] = {"state": "attaching", "restoreRequired": True, "robustness": "unknown"}
        elif MODE == "faulted":
            o["status"] = {"state": "detached", "restoreRequired": True, "robustness": "faulted"}
        else:
            ready = age >= 0.05
            o["status"] = {"state": "detached" if ready else "attaching", "restoreRequired": not ready, "robustness": "healthy"}
    else:
        o["status"] = {"phase": "Succeeded" if age >= 0.05 else "Pending"}
    say(o)
elif verb == "apply":
    o = json.loads(stdin)
    o["_created"] = time.time()
    kind = "volume" if o["kind"] == "Volume" else "pod"
    json.dump(o, open(path(kind, o["metadata"]["name"]), "w"))
    open(os.path.join(S, "applied.jsonl"), "a").write(json.dumps(o) + "\n")
elif verb == "logs":
    o = json.load(open(path("pod", a[1])))
    cmd = o["spec"]["containers"][0]["command"]
    # The writer puts the files in <volume>/canary (canary.yaml: write /data/canary); the verify pod mounts the volume at /data. FAKE_DATA
    # is that canary directory, and any other path the pod is told to verify holds nothing (lab 20: the pod verified /data and
    # reported "no MANIFEST on the restored volume" on a good backup).
    vdir = cmd[cmd.index("verify") + 1]
    data = os.environ["FAKE_DATA"] if vdir == "/data/canary" else tempfile.mkdtemp()
    r = subprocess.run([sys.executable, CANARY, "verify", data, "--backup-created", cmd[cmd.index("--backup-created") + 1]],
                       capture_output=True, text=True)
    say(r.stdout.strip())
elif verb == "delete" and what in ("pod", "volumes.longhorn.io"):
    kind = "volume" if what.startswith("volumes") else "pod"
    p = path(kind, a[2])
    if os.path.exists(p) and not (kind == "volume" and MODE == "stuck-volume" and not os.path.exists(p + ".unstuck")):
        os.remove(p)
elif verb == "patch" and what == "volumes.longhorn.io":
    open(path("volume", a[2]) + ".unstuck", "w").write("1")
elif verb == "get" and what == "configmap":
    key = a[-1].split(".data.")[-1].rstrip("}")
    p = os.path.join(S, "cm", key)
    print(open(p).read() if os.path.exists(p) else "", end="")
elif verb == "patch" and what == "configmap":
    for k, v in json.loads(opt("-p"))["data"].items():
        open(os.path.join(S, "cm", k), "w").write(v)
else:
    fail("fake kubectl: unhandled " + " ".join(a))
