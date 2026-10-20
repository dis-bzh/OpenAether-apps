#!/usr/bin/env python3
"""The Longhorn restore test (apps/base/storage-restore-test), offline: the canary file set (write, verify, each damage), the
restore flow against a kubectl double (newest backup -> Longhorn Volume fromBackup -> verify pod -> cleanup) for each outcome, and the
shape of the manifests (rights, token, class, claim, schedule). The flow runs the real canary_volume.py on the "restored" directory.
Rung: mocked cluster, real scripts. Not run against Longhorn: the Volume CR fields and the static PV are UNVERIFIED until a lab.
Run: python3 -I scripts/dev/test-restore-test-longhorn.py   (kubectl and PyYAML for the manifest part)
"""
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
B = os.path.join(ROOT, "apps/base/storage-restore-test")
CANARY = os.path.join(B, "scripts/canary_volume.py")
FLOW = os.path.join(B, "scripts/restore_longhorn.py")
FAKE = os.path.join(ROOT, "scripts/dev/testdata/fake-kubectl-longhorn.py")
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += bool(cond)
    FAIL += not cond
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f" -> {detail}"))


def load(path, name):
    s = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


cv = load(CANARY, "cv")
print("=== canary file set ===")
d = tempfile.mkdtemp()
for i in range(30):
    cv.write(d, now=1000.0 + i * 3600)
files = sorted(f for f in os.listdir(d) if f.startswith("gen-"))
check("30 writes keep the last 24 generations and a manifest", len(files) == 24 and "gen-30.bin" in files and "gen-6.bin" not in files and os.path.exists(d + "/MANIFEST"), files)
newest = 1000.0 + 29 * 3600
ok, why, info = cv.verify(d, newest + 600, newest + 7200)
check("an intact set passes", ok, why)
check("a file of one generation is the function of its number alone", cv.content(7) == cv.content(7) and cv.content(7) != cv.content(8))
def damaged(fn):
    t = tempfile.mkdtemp()
    shutil.copytree(d, t, dirs_exist_ok=True)
    fn(t)
    return cv.verify(t, newest + 600, newest + 7200)
ok, why, _ = damaged(lambda t: open(t + "/gen-20.bin", "r+b").write(b"\0"))
check("a flipped byte in a file is found", not ok and any("gen-20" in w for w in why), why)
ok, why, _ = damaged(lambda t: os.remove(t + "/gen-12.bin"))
check("a missing file is found", not ok and any("missing" in w for w in why), why)
def rewrite(t):
    m = json.load(open(t + "/MANIFEST")); m["files"]["gen-20.bin"] = "0" * 64; json.dump(m, open(t + "/MANIFEST", "w"))
ok, why, _ = damaged(rewrite)
check("a manifest that disagrees with the file is found", not ok, why)
ok, why, _ = damaged(lambda t: open(t + "/gen-99.bin", "wb").write(b"x"))
check("a stray file is found", not ok and any("does not know" in w for w in why), why)
ok, why, _ = damaged(lambda t: open(t + "/gen-31.bin", "wb").write(cv.content(31)))
check("one generation ahead of the manifest (a snapshot between the two writes) is tolerated", ok, why)
ok, why, _ = damaged(lambda t: os.remove(t + "/MANIFEST"))
check("no manifest is a failure", not ok)
ok, why, _ = cv.verify(d, newest - 7200, newest)
check("a backup older than the newest generation is refused", not ok and any("newer than the backup" in w for w in why), why)
ok, why, _ = cv.verify(d, newest + 5 * 3600, newest + 6 * 3600)
check("a backup taken 5 h after the last write means the writer stopped", not ok and any("older than the backup" in w for w in why), why)

print("=== restore flow against a kubectl double ===")
NOW = time.time()
BACKUP = {"state": "Completed", "url": "s3://b@r/p?backup=backup-1&volume=pvc-1", "backupCreatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 4 * 3600)), "volumeSize": "1073741824"}


def data_dir(created, bad=None):
    t = tempfile.mkdtemp()
    for i in range(30):
        cv.write(t, now=created - 600 - (29 - i) * 3600)
    if bad:
        bad(t)
    return t


def flow(mode="ok", backup=BACKUP, pvc="pvc-1", last="backup-1", bad=None, sigterm_after=None, env_extra=None):
    w = tempfile.mkdtemp()
    st = os.path.join(w, "state")
    os.makedirs(st)
    bf = os.path.join(w, "backup.json")
    json.dump(backup, open(bf, "w"))
    created = NOW - 4 * 3600
    env = dict(os.environ, KUBECTL=FAKE, FAKE_STATE=st, FAKE_MODE=mode, FAKE_BACKUP=bf, FAKE_PVC=pvc, FAKE_LAST=last,
               FAKE_DATA=data_dir(created, bad), FAKE_CANARY_SCRIPT=CANARY, POLL="0.01", RESTORE_TIMEOUT="5", POD_TIMEOUT="5", DELETE_TIMEOUT="1")
    env.update(env_extra or {})
    p = subprocess.Popen([sys.executable, "-I", FLOW], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if sigterm_after:
        time.sleep(sigterm_after)
        p.send_signal(signal.SIGTERM)
    out = p.communicate(timeout=60)[0]
    left = [f for f in os.listdir(os.path.join(st, "obj")) if f.endswith(".json")] if os.path.isdir(os.path.join(st, "obj")) else []
    cmf = os.path.join(st, "cm", "longhorn-volume")
    return p.returncode, out, open(os.path.join(st, "calls.log")).read().splitlines(), left, (open(cmf).read() if os.path.exists(cmf) else ""), st


def writes_ok(calls):
    for c in calls:
        w = c.split()
        if w[0] in ("apply", "delete", "patch"):
            if w[1] == "configmap":
                if "foundation-databases" not in w or "oa-restore-test-metrics" not in w:
                    return False, c
            elif w[0] == "apply":
                if not ("-n" in w and w[w.index("-n") + 1] == "backup-restore-test") and w[1:3] != ["-f", "-"]:
                    return False, c
            elif not ((w[1], w[2]) in {("pod", "rt-longhorn-verify"), ("volumes.longhorn.io", "oa-restore-test-canary")}):
                return False, c
    return True, ""


rcode, out, calls, left, cm, st = flow()
check("a backup that restores and verifies: exit 0, ok=1", rcode == 0 and 'oa_restore_test_ok{kind="longhorn",dataset="canary",store="primary",mode="volume"} 1' in cm, out[-300:])
check("the scratch Volume and the verify pod are gone", left == [], left)
applied = [json.loads(l) for l in open(st + "/applied.jsonl")]
vol = [o for o in applied if o["kind"] == "Volume"][0]
check("the Volume is the one the static PV names, restored from the newest backup, with one replica", vol["metadata"]["name"] == "oa-restore-test-canary" and vol["spec"]["fromBackup"] == BACKUP["url"] and vol["spec"]["numberOfReplicas"] == 1)
pod = [o for o in applied if o["kind"] == "Pod"][0]
check("the verify pod mounts the claim read-only and is restricted-compliant",
      pod["spec"]["volumes"][0]["persistentVolumeClaim"] == {"claimName": "restore-canary", "readOnly": True}
      and pod["spec"]["containers"][0]["securityContext"]["readOnlyRootFilesystem"] and pod["spec"]["securityContext"]["runAsNonRoot"] and "limits" in pod["spec"]["containers"][0]["resources"])
check("nothing was written but the two scratch objects and the metrics ConfigMap", writes_ok(calls)[0], writes_ok(calls)[1])
check("the recovery time is exported", "oa_restore_test_recovery_seconds{" in cm and "last_success_timestamp_seconds{" in cm)

def flip(t):
    f = t + "/gen-20.bin"
    b = bytearray(open(f, "rb").read()); b[100] ^= 1; open(f, "wb").write(bytes(b))
rcode, out, calls, left, cm, _ = flow(bad=flip)
check("a corrupted file in the restored volume fails: exit 1, ok=0, everything removed", rcode == 1 and 'mode="volume"} 0' in cm and left == [] and "gen-20" in out, out[-300:])
rcode, out, calls, left, cm, _ = flow(backup=dict(BACKUP, backupCreatedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 40 * 3600))))
check("a newest backup older than 27 h fails (the daily backups stopped)", rcode == 1 and "stopped" in out and not any(c.startswith("apply") for c in calls), out[-200:])
rcode, out, calls, left, cm, _ = flow(backup=dict(BACKUP, state="Error", error="boom"))
check("a backup in error fails", rcode == 1 and "not usable" in out)
rcode, out, calls, left, cm, _ = flow(last="")
check("no backup yet: skipped, nothing created, no ok series, exit 0", rcode == 0 and "SKIP" in out and not any(c.startswith("apply") for c in calls) and "oa_restore_test_ok{" not in cm, out[-200:])
rcode, out, calls, left, cm, _ = flow(pvc="")
check("no canary volume yet: skipped", rcode == 0 and "SKIP" in out)
rcode, out, calls, left, cm, _ = flow(mode="never-restored", env_extra={"RESTORE_TIMEOUT": "0.3"})
check("a volume that is never restored fails and is removed", rcode == 1 and left == [] and "not restored" in out, out[-300:])
rcode, out, calls, left, cm, _ = flow(mode="faulted")
check("a faulted restored volume fails fast and is removed", rcode == 1 and left == [] and "faulted" in out)
rcode, out, calls, left, cm, _ = flow(mode="stuck-volume")
check("a Volume stuck on a finalizer is released", rcode == 0 and left == [] and any(c.startswith("patch volumes.longhorn.io") for c in calls), (left, out[-200:]))
rcode, out, calls, left, cm, _ = flow(mode="never-restored", env_extra={"RESTORE_TIMEOUT": "60"}, sigterm_after=1.5)
check("a Job killed at its deadline removes the scratch Volume first", left == [] and "terminated" in out, (left, out[-300:]))

print("=== manifests ===")
r = subprocess.run(["kubectl", "kustomize", B], capture_output=True, text=True)
docs = [x for x in yaml.safe_load_all(r.stdout) if x]
get = lambda kind, name=None, ns=None: [x for x in docs if x["kind"] == kind and (name is None or x["metadata"]["name"] == name) and (ns is None or x["metadata"].get("namespace") == ns)]  # noqa: E731
roles = get("Role", "longhorn-restore-test")
check("four Roles, no ClusterRole", len(roles) == 4 and not get("ClusterRole") and not get("ClusterRoleBinding"))
lh = [x for x in roles if x["metadata"]["namespace"] == "longhorn-system"][0]["rules"]
check("in longhorn-system the SA reads backups and may create, patch, delete Volumes; not list or watch them", {tuple(r["verbs"]) for r in lh if r["resources"] == ["volumes"]} == {("get", "create", "patch", "delete")} and all(set(r["verbs"]) <= {"get", "list"} for r in lh if r["resources"] != ["volumes"]))
check("in the scratch namespace it handles pods only (no exec, no secrets)", [x for x in roles if x["metadata"]["namespace"] == "backup-restore-test"][0]["rules"][0]["resources"] == ["pods"])
check("the canary PVC is the only claim it can read", [x for x in roles if x["metadata"]["namespace"] == "foundation-storage"][0]["rules"][0]["resourceNames"] == ["oa-canary"])
cj = {x["metadata"]["name"]: x for x in get("CronJob")}
check("the Job is named *-restore-test (M1's alert watches that), weekly, with a deadline and no retry",
      "longhorn-restore-test" in cj and cj["longhorn-restore-test"]["spec"]["schedule"].split()[-1] == "0" and cj["longhorn-restore-test"]["spec"]["jobTemplate"]["spec"]["backoffLimit"] == 0
      and cj["longhorn-restore-test"]["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] > 0)
check("the writer holds no ServiceAccount token and mounts the canary claim", cj["longhorn-canary"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
      and cj["longhorn-canary"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "oa-canary")
sc = get("StorageClass")[0]
check("the canary class takes the backup group of the recurring job", "${LONGHORN_BACKUP_GROUP:=oa-backup}" in sc["parameters"]["recurringJobSelector"] and sc["provisioner"] == "driver.longhorn.io")
rj = open(os.path.join(ROOT, "apps/base/storage/backup-target/recurringjob.yaml")).read()
check("that group is the one the recurring job uses", '"${LONGHORN_BACKUP_GROUP:=oa-backup}"' in rj)
pv, pvc = get("PersistentVolume")[0], get("PersistentVolumeClaim", "restore-canary")[0]
check("the static PV is claimed by the scratch PVC only and names the Volume the flow creates",
      pv["spec"]["claimRef"] == {"name": "restore-canary", "namespace": "backup-restore-test"} and pv["spec"]["csi"]["volumeHandle"] == "oa-restore-test-canary" and pvc["spec"]["volumeName"] == pv["metadata"]["name"])
fl = load(FLOW, "fl")
check("the flow and the manifests agree on the names", fl.VOLUME == pv["spec"]["csi"]["volumeHandle"] and fl.SCRATCH_PVC == pvc["metadata"]["name"] and fl.POD.startswith("rt-"))
cms = get("ConfigMap", "longhorn-restore-scripts")
check("the scripts are in foundation-storage and (the verifier only) in the scratch namespace",
      {c["metadata"]["namespace"]: set(c["data"]) for c in cms} == {"foundation-storage": {"canary_volume.py", "restore_longhorn.py"}, "backup-restore-test": {"canary_volume.py"}})
comp = yaml.safe_load(open(os.path.join(ROOT, "apps/flux/components/longhorn-restore-test/storage-restore-test.yaml")))
check("the Flux Kustomization depends on the backup target and on the backup brick", {d["name"] for d in comp["spec"]["dependsOn"]} == {"storage-backup-target", "backup-databases"})
rc = load(os.path.join(ROOT, "apps/base/backup/databases/restore/restore_cnpg.py"), "rc")
check("the metric names are the CNPG test's", all(n in fl.build_metrics({"ok": True, "duration": 1, "recovery": 1}, 1.0, None) for n in ("oa_restore_test_ok", "oa_restore_test_last_success_timestamp_seconds", "oa_restore_test_last_run_timestamp_seconds", "oa_restore_test_duration_seconds", "oa_restore_test_recovery_seconds")))
check("and the same publishing key space of the exporter", fl.CM == rc.CM and fl.METRICS_NS == rc.LIVE_NS)

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
