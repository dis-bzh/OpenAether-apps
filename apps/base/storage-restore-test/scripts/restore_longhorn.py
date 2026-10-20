#!/usr/bin/env python3
"""Longhorn restore test (0.4.0 M3): restore the newest backup of the canary volume into a scratch volume, check its files, clean up.

  1. the newest Completed backup of the volume behind PVC foundation-storage/oa-canary (BackupVolume.status.lastBackupName);
  2. a Longhorn Volume `oa-restore-test-canary` (longhorn-system) with fromBackup = that backup, ONE replica; wait until restored;
  3. a pod in backup-restore-test mounts it read-only through the static PV/PVC (restore-canary) and runs canary_volume.py verify:
     every file holds what its generation number says (recomputed, not read from the live volume), the manifest agrees, the
     newest generation is the backup's;
  4. the pod and the Longhorn Volume are deleted, whatever happened. The PV and PVC are static and stay, bound to a name.
Publishes oa_restore_test_* {kind="longhorn",dataset="canary",store="primary",mode="volume"} into the shared ConfigMap. Exit 1 on a
failure. Skips (no series, exit 0) when the volume has no backup yet (no backup target, or the first backup has not run).
"""
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time

KUBECTL = os.environ.get("KUBECTL", "kubectl")
LH_NS = "longhorn-system"
SRC_NS, SRC_PVC = "foundation-storage", "oa-canary"
SCRATCH_NS, SCRATCH_PVC = "backup-restore-test", "restore-canary"
VOLUME = "oa-restore-test-canary"      # the Longhorn Volume the static PV points at
POD = "rt-longhorn-verify"
METRICS_NS, CM, KEY = "foundation-databases", "oa-restore-test-metrics", "longhorn-volume"
LABELS = 'kind="longhorn",dataset="canary",store="primary",mode="volume"'
RESTORE_TIMEOUT = float(os.environ.get("RESTORE_TIMEOUT", "900"))
POD_TIMEOUT = float(os.environ.get("POD_TIMEOUT", "300"))
DELETE_TIMEOUT = float(os.environ.get("DELETE_TIMEOUT", "300"))
POLL = float(os.environ.get("POLL", "5"))
BACKUP_MAX_AGE = 27 * 3600.0   # a daily backup: older than this and the backups stopped


class Skip(Exception):
    pass


class Terminated(BaseException):
    pass


def kube(*args, stdin=None, timeout=120, check=True):
    r = subprocess.run([KUBECTL, *args], input=stdin, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {r.stderr.strip()[:300]}")
    return r


def kj(*args, **kw):
    return json.loads(kube(*args, "-o", "json", **kw).stdout or "{}")


def ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()


def guard(ns, kind, name):
    """Only these objects, only here: the restore test never touches a live volume."""
    ok = (kind, ns, name) in {("volumes.longhorn.io", LH_NS, VOLUME), ("pod", SCRATCH_NS, POD)}
    if not ok:
        raise RuntimeError(f"refusing to touch {kind} {ns}/{name}")


def latest_backup(now):
    """-> dict(name, url, created, size). Skip when there is nothing to restore."""
    r = kube("get", "pvc", SRC_PVC, "-n", SRC_NS, "-o", "jsonpath={.spec.volumeName}", check=False)
    vol = r.stdout.strip()
    if r.returncode != 0 or not vol:
        raise Skip(f"PVC {SRC_NS}/{SRC_PVC} is not bound (the canary volume does not exist yet)")
    bv = kube("get", "backupvolumes.longhorn.io", vol, "-n", LH_NS, "-o", "json", check=False)
    last = json.loads(bv.stdout or "{}").get("status", {}).get("lastBackupName", "") if bv.returncode == 0 else ""
    if not last:
        raise Skip(f"volume {vol} has no backup yet (no backup target, or the first backup has not run)")
    b = kj("get", "backups.longhorn.io", last, "-n", LH_NS)
    st = b.get("status", {})
    if st.get("state") != "Completed" or st.get("error") or not st.get("url"):
        raise RuntimeError(f"newest backup {last} of {vol} is not usable: state={st.get('state')} error={st.get('error', '')[:80]}")
    created = ts(st.get("backupCreatedAt") or st["snapshotCreatedAt"])
    if now - created > BACKUP_MAX_AGE:
        raise RuntimeError(f"newest backup {last} of {vol} is {(now - created) / 3600:.0f} h old: the daily backups stopped")
    return {"name": last, "url": st["url"], "created": created, "size": str(st.get("volumeSize") or st.get("size") or "1073741824")}


def volume_manifest(b):
    guard(LH_NS, "volumes.longhorn.io", VOLUME)
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Volume",
            "metadata": {"name": VOLUME, "namespace": LH_NS, "labels": {"openaether.io/restore-test": "true"}},
            "spec": {"size": b["size"], "numberOfReplicas": 1, "fromBackup": b["url"], "frontend": "blockdev",
                     "accessMode": "rwo", "dataEngine": "v1"}}


def pod_manifest(image, created):
    guard(SCRATCH_NS, "pod", POD)
    sc = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}, "readOnlyRootFilesystem": True,
          "runAsNonRoot": True, "runAsUser": 1000, "seccompProfile": {"type": "RuntimeDefault"}}
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": POD, "namespace": SCRATCH_NS, "labels": {"openaether.io/restore-test": "true", "app": POD}},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False, "activeDeadlineSeconds": int(POD_TIMEOUT),
                     "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                                         "seccompProfile": {"type": "RuntimeDefault"}},
                     "containers": [{"name": "verify", "image": image, "securityContext": sc,
                                     "command": ["python3", "/scripts/canary_volume.py", "verify", "/data", "--backup-created", f"{created:.0f}"],
                                     "volumeMounts": [{"name": "data", "mountPath": "/data", "readOnly": True},
                                                      {"name": "scripts", "mountPath": "/scripts", "readOnly": True}],
                                     "resources": {"requests": {"cpu": "20m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}}}],
                     "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": SCRATCH_PVC, "readOnly": True}},
                                 {"name": "scripts", "configMap": {"name": "longhorn-restore-scripts"}}]}}


def gone(*what):
    return kube("get", *what, "-o", "name", check=False).returncode != 0


def cleanup():
    """Delete the verify pod and the restored Longhorn Volume. Never raises; returns True when both are gone."""
    try:
        guard(SCRATCH_NS, "pod", POD)
        guard(LH_NS, "volumes.longhorn.io", VOLUME)
        kube("delete", "pod", POD, "-n", SCRATCH_NS, "--wait=false", "--ignore-not-found", check=False)
        deadline = time.time() + DELETE_TIMEOUT
        while time.time() < deadline and not gone("pod", POD, "-n", SCRATCH_NS):
            time.sleep(POLL)
        kube("delete", "volumes.longhorn.io", VOLUME, "-n", LH_NS, "--wait=false", "--ignore-not-found", check=False)
        while time.time() < deadline and not gone("volumes.longhorn.io", VOLUME, "-n", LH_NS):
            time.sleep(POLL)
        if not gone("volumes.longhorn.io", VOLUME, "-n", LH_NS):  # stuck on a finalizer
            kube("patch", "volumes.longhorn.io", VOLUME, "-n", LH_NS, "--type", "merge", "-p", '{"metadata":{"finalizers":null}}', check=False)
            kube("delete", "volumes.longhorn.io", VOLUME, "-n", LH_NS, "--wait=false", "--ignore-not-found", check=False)
        return gone("pod", POD, "-n", SCRATCH_NS) and gone("volumes.longhorn.io", VOLUME, "-n", LH_NS)
    except Exception as e:  # noqa: BLE001
        print(f"WARN: cleanup: {e}")
        return False


def wait_restored():
    deadline = time.time() + RESTORE_TIMEOUT
    last = ""
    while time.time() < deadline:
        r = kube("get", "volumes.longhorn.io", VOLUME, "-n", LH_NS, "-o", "json", check=False)
        st = json.loads(r.stdout or "{}").get("status", {}) if r.returncode == 0 else {}
        last = f"state={st.get('state')} restoreRequired={st.get('restoreRequired')} robustness={st.get('robustness')}"
        if st.get("state") == "detached" and st.get("restoreRequired") is False:
            return
        if st.get("state") == "faulted" or st.get("robustness") == "faulted":
            raise RuntimeError(f"the restored volume is faulted ({last})")
        time.sleep(POLL)
    raise RuntimeError(f"not restored after {RESTORE_TIMEOUT:.0f} s ({last})")


def run_verify(image, created):
    kube("apply", "-n", SCRATCH_NS, "-f", "-", stdin=json.dumps(pod_manifest(image, created)))
    deadline = time.time() + POD_TIMEOUT
    phase = ""
    while time.time() < deadline:
        phase = kj("get", "pod", POD, "-n", SCRATCH_NS, check=False).get("status", {}).get("phase", "")
        if phase in ("Succeeded", "Failed"):
            break
        time.sleep(POLL)
    out = kube("logs", POD, "-n", SCRATCH_NS, check=False).stdout.strip().splitlines()
    verdict = json.loads(out[-1]) if out and out[-1].startswith("{") else {}
    if phase not in ("Succeeded", "Failed"):
        raise RuntimeError(f"the verify pod did not finish ({phase or 'no phase'})")
    return phase == "Succeeded" and verdict.get("ok") is True, verdict.get("reasons") or [f"pod {phase}, no verdict in its log"], verdict


def build_metrics(res, now, previous_ok_ts):
    n = {"ok": "oa_restore_test_ok", "last": "oa_restore_test_last_success_timestamp_seconds", "run": "oa_restore_test_last_run_timestamp_seconds",
         "dur": "oa_restore_test_duration_seconds", "rec": "oa_restore_test_recovery_seconds"}
    h = {"ok": "1 when the last restore test passed, 0 when it failed (absent: never run or skipped)",
         "last": "Unix time of the last restore test that passed", "run": "Unix time the restore test last finished (pass or fail)",
         "dur": "Seconds the last restore test took, scratch cleanup included",
         "rec": "Seconds from the scratch Cluster's creation to its healthy state (restore RTO of the data)"}
    lines = {k: [] for k in n}
    if res["ok"] is not None:
        lines["ok"].append(f"{n['ok']}{{{LABELS}}} {int(res['ok'])}")
        lines["run"].append(f"{n['run']}{{{LABELS}}} {now:.0f}")
    last = now if res["ok"] else previous_ok_ts
    if last is not None:
        lines["last"].append(f"{n['last']}{{{LABELS}}} {last:.0f}")
    for k, key in (("dur", "duration"), ("rec", "recovery")):
        if res.get(key) is not None:
            lines[k].append(f"{n[k]}{{{LABELS}}} {res[key]:.0f}")
    return "".join(f"# HELP {n[k]} {h[k]}\n# TYPE {n[k]} gauge\n" + "".join(l + "\n" for l in lines[k]) for k in n)


def run(image, now_fn=time.time):
    res = {"ok": False, "duration": None, "recovery": None}
    t0 = now_fn()
    created = False
    try:
        b = latest_backup(now_fn())
        cleanup()   # a leftover of a killed run is not an input
        created = True
        kube("apply", "-f", "-", stdin=json.dumps(volume_manifest(b)))
        t1 = now_fn()
        wait_restored()
        ok, why, verdict = run_verify(image, b["created"])
        res.update(ok=ok, recovery=now_fn() - t1)
        print(("PASS " if ok else "FAIL ") + f"longhorn/{b['name']}: " + ("; ".join(why) if not ok else
              f"restored and verified in {res['recovery']:.0f} s: generation {verdict.get('counter')}, {verdict.get('files')} files identical to what they must hold"))
    except Skip as e:
        res["ok"] = None
        print(f"SKIP longhorn: {e}")
    except Terminated:
        print("FAIL longhorn: terminated (deadline)")
        cleanup()
        raise
    except Exception as e:  # noqa: BLE001
        print(f"FAIL longhorn: {e}")
    finally:
        if created and not cleanup():
            print("ERROR: could not remove the restored volume or the verify pod; the next run removes them")
            res["ok"] = False
    res["duration"] = now_fn() - t0
    return res


def main():
    image = os.environ.get("VERIFY_IMAGE", "docker.io/alpine/k8s:1.36.5")
    def term(*_):
        raise Terminated()
    signal.signal(signal.SIGTERM, term)
    r = kube("get", "configmap", CM, "-n", METRICS_NS, "-o", "jsonpath={.data." + KEY + "}", check=False)
    m = re.search(r"last_success_timestamp_seconds\{[^}]*\} ([0-9.e+]+)", r.stdout)
    prev = float(m.group(1)) if m else None
    try:
        res = run(image)
    except Terminated:
        res = {"ok": False, "duration": None, "recovery": None}
    text = build_metrics(res, time.time(), prev)
    p = kube("patch", "configmap", CM, "-n", METRICS_NS, "--type", "merge", "-p", json.dumps({"data": {KEY: text}}), check=False)
    if p.returncode != 0:
        print("ERROR: cannot publish the metrics:", p.stderr.strip()[:300])
        return 1
    return 0 if res["ok"] is not False else 1


if __name__ == "__main__":
    sys.exit(main())
