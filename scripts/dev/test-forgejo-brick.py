#!/usr/bin/env python3
"""The Forgejo git forge brick (apps/base/forgejo, apps/base/forgejo-db) keeps the shape that lets a DEFAULT install converge.

Each rule exists because the first version of the brick (PR #173, written to the 0.3.x patterns, never run on a cluster)
broke it:
  1. every OpenBao path an ExternalSecret of the brick reads (forgejo/db, forgejo/admin, forgejo/oidc, with its property) is
     created by the vault configure Job (configure.sh): otherwise the ExternalSecret stays SecretSyncedError, the CNPG
     bootstrap Secret never exists and the database never starts;
  2. the admin Secret carries `username` AND `password`: the chart reads both keys of gitea.admin.existingSecret, and the pod
     stays in CreateContainerConfigError with only the password;
  3. the HelmRepository is the OCI one (the https address of the first version has no index.yaml: HTTP 404), and Flux's
     egress policy admits its host;
  4. the HTTPRoute does not hand /metrics (unauthenticated, enabled for vmagent) to the Internet: a rule for it with no backend,
     before the catch-all;
  5. forgejo-db is its OWN Flux Kustomization (wait: true, the cluster-identity substitution), `forgejo` depends on it, and
     the `cnpg` directory holds no forgejo object: a profile without the forge must not install three database pods, and a
     slow forgejo-db must not hold `cnpg` (wait: true) or identity back.
  6. self-registration is off and sign-in is required to view (the route answers anyone who sets the Host header; Forgejo's
     defaults allow registration);
  7. the Gateway's egress policy admits every namespace a route sends traffic to (the first version routed to the forge and forgot
     the rule: the route would have timed out);
  8. the brick is OPT-IN (0.4.0 release gate): `apps/flux/management` and `apps/flux/workload` do not render forgejo and forgejo-db,
     `apps/flux/management-forge` renders exactly those two more, and the catalogue keeps the `forge` alias;
  9. the repositories' volume has a DEFAULT daily Longhorn backup: the volume is created ALREADY in the group, through the
     StorageClass `longhorn-x2-rwo-forgejo` (longhorn-x2-rwo plus `recurringJobSelector` naming the group; a label on the PVC does
     not reach the Longhorn volume, lab 27 FG), the chart's PVC uses that class (checked on the rendered PVC when helm is
     available), a RecurringJob of type backup selects that group, runs daily
     at a time no other job uses and keeps a retention, and sits in longhorn-system (the namespace Longhorn reads);
  10. git fsyncs loose objects (FK, lab 27, DATA LOSS): `[git.config] core.fsync = all` is in the chart values and in the app.ini
     sources the chart renders (when helm is available), and core.fsyncMethod is not set to a mode that gives part of it back;
  11. the pod is rescheduled 30 s after its node is NotReady or unreachable, not after Kubernetes' default 300 s (F-FORGE-TOL, lab 27:
     the forge was away about 8 minutes): `tolerations` in the chart values carries both keys with tolerationSeconds <= 30, and the
     Deployment the chart renders carries them (when helm is available);
  12. forgejo-db has a base backup at enablement, not only at 03:40 (F-FORGEDB-BASE, lab 27: WAL and no base backup for up to a day, so no
     PITR): its ScheduledBackup sets `immediate: true`, the field exists in the vendored CRD, and it is the ONLY ScheduledBackup of
     forgejo-db (no second object that would double the daily backup);
  13. (with the Kyverno CLI, $KYVERNO or PATH, and helm with network for the chart) the pod the chart renders passes the platform
     policies except the one read-only-root-filesystem finding that is known and left open (every one is Audit).
  14. the forge volume is mounted with `dirsync` (lab 27 FK2, an acknowledged ref update lost at the instant of a power-off): the
     StorageClass longhorn-x2-rwo-forgejo carries `mountOptions: [dirsync]` (and Longhorn formats ext4, its default fsType);
  15. stale git locks are removed before the app starts (lab 27 FK2/s5q, F-LOCKAGE): an init container `clean-stale-git-locks`, FIRST in the
     pod, ANY age (LOCK_MIN_AGE_MIN 0: the Deployment is Recreate and the volume RWO, so no git process runs on it), and a sidecar
     `sweep-stale-git-locks` that looks again every 60 s for locks older than 10 minutes (a restart of the main container does not run
     an init container). Both on the main container's image, non-root, restricted securityContext, requests and limits, mounting only
     the data volume; both are the SAME script, RUN against a temporary tree (old and fresh locks, ref locks and not, index.lock,
     symlinks, other directories): the init removes every ref/packed-refs/HEAD/config/shallow lock, the sweeper only the old ones; both
     log paths and never contents, exit 0, and the sweeper stops on TERM at once and refuses an age that is not a number.
Static: it proves the wiring, not a cluster. Run: python3 -I scripts/dev/test-forgejo-brick.py [--selftest]
"""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CONFIGURE = "apps/base/foundation/vault/scripts/configure.sh"


def docs(rel):
    return [d for d in yaml.safe_load_all((REPO / rel).read_text()) if d]


def seeded(configure_text):
    """{openbao path: set of properties} from the `kv_create <path>` lines of configure.sh (the jq / JSON literal on the same line)."""
    out = {}
    for line in configure_text.splitlines():
        m = re.search(r"kv_create (\S+) D\s*$", line)
        if not m:
            continue
        props = set(re.findall(r"[{,]\s*\"?([A-Za-z_]+)\"?\s*:", line.split("kv_create")[0]))
        out[m.group(1)] = props
    return out


def route_namespaces():
    out = {}
    for f in sorted((REPO / "apps/base").rglob("*.yaml")):
        try:
            for d in yaml.safe_load_all(f.read_text()):
                if isinstance(d, dict) and d.get("kind") == "HTTPRoute":
                    for r in d["spec"]["rules"]:
                        for b in r.get("backendRefs", []):
                            out.setdefault(d["metadata"]["namespace"], set()).add(b["name"])
        except yaml.YAMLError:
            continue
    return out


def kustomization_names(profile):
    """The names of the Flux Kustomizations a profile (a kustomize overlay under apps/flux) renders."""
    sys.path.insert(0, str(REPO / "scripts"))
    import ha_render
    return {d["metadata"]["name"] for d in ha_render.kustomize(REPO / "apps/flux" / profile) if d.get("kind") == "Kustomization"}


BRICK = {"forgejo", "forgejo-db"}
GROUP = "oa-forgejo"
LABEL = f"recurring-job-group.longhorn.io/{GROUP}"
SC = "longhorn-x2-rwo-forgejo"


def crd_has_immediate():
    """True when the vendored CNPG CRD of ScheduledBackup has spec.immediate (a boolean)."""
    for d in docs("apps/base/cnpg/cnpg-1.30.1.yaml"):
        if d.get("kind") == "CustomResourceDefinition" and d["metadata"]["name"] == "scheduledbackups.postgresql.cnpg.io":
            props = d["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
            return props.get("immediate", {}).get("type") == "boolean"
    return False


def basebackup_problems(sbs, crd_ok):
    errs = []
    mine = [d for d in sbs if (d.get("spec", {}).get("cluster") or {}).get("name") == "forgejo-db"]
    if len(mine) != 1:
        errs.append(f"forgejo-db must have exactly one ScheduledBackup (found {len(mine)}): a second one doubles the daily base backup")
    elif mine[0]["spec"].get("immediate") is not True:
        errs.append("the forgejo-db ScheduledBackup must set `immediate: true`: without it the database has WAL and no base backup until 03:40, so no point-in-time recovery for up to a day (lab 27, F-FORGEDB-BASE)")
    if not crd_ok:
        errs.append("the vendored CNPG CRD has no boolean spec.immediate on ScheduledBackup: the field would be rejected or ignored")
    return errs


def tolerations_problems(tols, where):
    errs = []
    for key in ("node.kubernetes.io/unreachable", "node.kubernetes.io/not-ready"):
        t = [x for x in (tols or []) if x.get("key") == key and x.get("effect") in (None, "NoExecute")]
        if len(t) != 1 or t[0].get("operator", "Equal") != "Exists" or not isinstance(t[0].get("tolerationSeconds"), int) or t[0]["tolerationSeconds"] > 30:
            errs.append(f"{where}: a NoExecute toleration of {key} with tolerationSeconds <= 30 is missing (Kubernetes' default is 300 s: the forge was away about 8 minutes after a node loss, lab 27 F-FORGE-TOL), found {t}")
    return errs


CLEANER = "clean-stale-git-locks"
SWEEPER = "sweep-stale-git-locks"
OLD_LOCKS = ["alice/r.git/refs/heads/old.lock", "alice/r.git/refs/heads/feat/deep/old.lock", "alice/r.git/refs/tags/t.lock",
             "alice/r.git/packed-refs.lock", "alice/r.git/HEAD.lock", "alice/r.git/config.lock", "alice/r.git/shallow.lock"]
FRESH_LOCK = "alice/r.git/refs/heads/fresh.lock"   # younger than the sweeper's limit: a live git process may hold it
KEPT = ["alice/r.git/index.lock",                  # not a bare repository's lock
        "alice/r.git/objects/pack/tmp.lock",       # objects are not in the list
        "alice/r.git/hooks/x.lock", "alice/r.git/refs/heads/main", "alice/r.git/packed-refs",
        "stray.lock", "alice/stray.lock", "bob/wt/index.lock"]


def cleaner_container(hr, name=CLEANER):
    for pr in hr["spec"].get("postRenderers", []):
        for p in pr.get("kustomize", {}).get("patches", []):
            if f"name: {name}" in p.get("patch", ""):
                ops = yaml.safe_load(p["patch"])
                return p, ops
    return None, None


def cleaner_problems(hr, rendered=None, flux_forgejo=None):
    """rendered: (initContainers, main image, containers, strategy type) of the Deployment the chart renders with the post-renderer applied (needs helm)."""
    errs = []
    scripts = {}
    for name, path, kind in ((CLEANER, "/spec/template/spec/initContainers/0", "init"), (SWEEPER, "/spec/template/spec/containers/-", "sweep")):
        p, ops = cleaner_container(hr, name)
        if not ops:
            errs.append(f"no post-renderer patch adds the {'init container' if kind == 'init' else 'sidecar'} {name}: stale git locks block a branch (lab 27 FK2, F-LOCKAGE)")
            continue
        if p.get("target", {}).get("kind") != "Deployment":
            errs.append(f"{name}: the patch must target the Deployment")
        op = ops[0]
        c = op.get("value") or {}
        if op.get("op") != "add" or op.get("path") != path:
            errs.append(f"{name}: must be added at {path}, not {op.get('path')!r}" + (" (FIRST in initContainers)" if kind == "init" else ""))
        if c.get("name") != name:
            errs.append(f"the container must be named {name}")
        sc = c.get("securityContext") or {}
        if sc.get("allowPrivilegeEscalation") is not False or sc.get("privileged") is not False or (sc.get("capabilities") or {}).get("drop") != ["ALL"]:
            errs.append(f"{name}: securityContext must be allowPrivilegeEscalation false, privileged false, drop ALL (as the chart's init containers)")
        if sc.get("runAsUser") in (0, "0") or sc.get("runAsNonRoot") is False:
            errs.append(f"{name}: must not run as root")
        res = c.get("resources") or {}
        for part in ("requests", "limits"):
            for k in ("cpu", "memory"):
                if not (res.get(part) or {}).get(k):
                    errs.append(f"{name}: resources.{part}.{k} is missing (Kyverno require-limits reads initContainers)")
        mounts = c.get("volumeMounts") or []
        if [(m.get("name"), m.get("mountPath")) for m in mounts] != [("data", "/data")] or any(m.get("readOnly") for m in mounts):
            errs.append(f"{name}: must mount exactly the data volume at /data, writable (and nothing else)")
        env = {e["name"]: e.get("value") for e in c.get("env") or []}
        if env.get("REPOS_ROOT") != "/data/git/gitea-repositories":
            errs.append(f"{name}: REPOS_ROOT must be the [repository] ROOT the chart renders (/data/git/gitea-repositories), not {env.get('REPOS_ROOT')!r}")
        if kind == "init":
            if env.get("LOCK_MIN_AGE_MIN") != "0" or env.get("SWEEP_EVERY_S") not in (None, "0"):
                errs.append(f"{name}: LOCK_MIN_AGE_MIN must be \"0\" and it must not loop: at pod start no git process runs on the volume (Recreate, RWO), so a lock of any age is stale; a 5 minute limit left a branch blocked for 9 minutes (lab 27 sitting 4)")
        else:
            if not (env.get("LOCK_MIN_AGE_MIN", "").isdigit() and int(env["LOCK_MIN_AGE_MIN"]) >= 10):
                errs.append(f"{name}: LOCK_MIN_AGE_MIN must be a number of minutes >= 10: the forge is RUNNING, and a lock younger than Forgejo's git timeout (360 s) may be a live push")
            if not (env.get("SWEEP_EVERY_S", "").isdigit() and 10 <= int(env["SWEEP_EVERY_S"]) <= 600):
                errs.append(f"{name}: SWEEP_EVERY_S must be 10 to 600 seconds")
            if env.get("CLEANER_NAME") != SWEEPER:
                errs.append(f"{name}: CLEANER_NAME must be {SWEEPER} so its log lines say which cleaner removed a lock")
        if c.get("image") is None:
            errs.append(f"{name} has no image")
        scripts[kind] = c
    if flux_forgejo is not None and (flux_forgejo.get("spec") or {}).get("postBuild"):
        errs.append("the forgejo Kustomization has a postBuild: Flux would substitute the ${...} of the cleaners' shell script")
    if len(scripts) == 2 and scripts["init"].get("args") != scripts["sweep"].get("args"):
        errs.append("the init container and the sidecar must carry the SAME script (the age and the loop are their environment)")
    if rendered is not None:
        inits, main_image, containers, strategy = rendered
        if not inits or inits[0].get("name") != CLEANER:
            errs.append(f"the Deployment the chart renders does not start with the init container {CLEANER}")
        elif inits[0].get("image") != main_image:
            errs.append(f"{CLEANER} runs {inits[0].get('image')}, the main container {main_image}: the same image (one pull, same user and filesystem)")
        sw = [x for x in containers if x.get("name") == SWEEPER]
        if len(sw) != 1:
            errs.append(f"the Deployment the chart renders has {len(sw)} containers named {SWEEPER}, want 1")
        elif sw[0].get("image") != main_image:
            errs.append(f"{SWEEPER} runs {sw[0].get('image')}, the main container {main_image}: the same image")
        if strategy != "Recreate":
            errs.append(f"the Deployment strategy is {strategy!r}: {CLEANER} removes locks of ANY age because Recreate stops the old pod before the new one exists; with another strategy a live push could hold one")
    for kind, c in scripts.items():
        errs += cleaner_script_problems(c, kind)
    return errs


def cleaner_script_problems(c, kind):
    """RUN the script on a temporary tree. kind init: every lock goes; kind sweep: only the old ones, and it loops."""
    import time
    errs = []
    name = c.get("name")
    args = c.get("args") or []
    if c.get("command") != ["/bin/sh", "-c"] or len(args) != 1:
        return [f"{name}: command must be [/bin/sh, -c] with the script as the one argument"]
    env0 = {e["name"]: e.get("value") for e in c.get("env") or []}
    once = dict(env0, SWEEP_EVERY_S="0")   # one pass, whatever the loop setting
    with tempfile.TemporaryDirectory() as t:
        root = Path(t) / "repos"
        old = time.time() - 1200
        for rel in OLD_LOCKS + KEPT + [FRESH_LOCK]:
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("0123456789abcdef-SECRET-CONTENT\n")
            if rel != FRESH_LOCK:
                os.utime(f, (old, old))
        soon = time.time() - 180   # three minutes: stale at pod start, possibly live in a running forge
        os.utime(root / FRESH_LOCK, (soon, soon))
        (root / "alice/r.git/refs/heads/link.lock").symlink_to(root / "alice/r.git/refs/heads/main")
        (root / "alice/r.git/refs/heads/dir.lock").mkdir()
        os.utime(root / "alice/r.git/refs/heads/dir.lock", (old, old))
        env = dict(os.environ, REPOS_ROOT=str(root), **{k: v for k, v in once.items() if k != "REPOS_ROOT"})
        r = subprocess.run(["/bin/sh", "-c", args[0]], capture_output=True, text=True, env=env, timeout=30)
        out = r.stdout + r.stderr
        if r.returncode:
            errs.append(f"{name} script exits {r.returncode}, it must always exit 0")
        for rel in OLD_LOCKS:
            if (root / rel).exists():
                errs.append(f"{name} left the old lock {rel}")
            elif rel not in out:
                errs.append(f"{name} removed {rel} without logging it")
        if kind == "init" and (root / FRESH_LOCK).exists():
            errs.append(f"{name} left a 3 minute old lock: at pod start nothing holds it, and the 5 minute limit blocked a branch for 9 minutes (lab 27 sitting 4)")
        if kind == "sweep" and not (root / FRESH_LOCK).exists():
            errs.append(f"{name} removed a 3 minute old lock in a RUNNING forge: it may be a live push")
        for rel in KEPT:
            if not (root / rel).exists():
                errs.append(f"{name} removed {rel}, which it must keep")
        if not (root / "alice/r.git/refs/heads/link.lock").is_symlink():
            errs.append(f"{name} removed a symlink named *.lock")
        if not (root / "alice/r.git/refs/heads/dir.lock").is_dir():
            errs.append(f"{name} removed a directory named *.lock")
        if "SECRET-CONTENT" in out:
            errs.append(f"{name} printed the contents of a file")
        # an age that is not a number removes nothing (a garbled setting must not turn a sweeper into a deleter)
        (root / FRESH_LOCK).write_text("x")
        r = subprocess.run(["/bin/sh", "-c", args[0]], capture_output=True, text=True, env=dict(env, LOCK_MIN_AGE_MIN="ten"), timeout=30)
        if r.returncode or not (root / FRESH_LOCK).exists():
            errs.append(f"{name}: an age that is not a number must remove nothing and exit 0 (rc {r.returncode})")
        # a missing repositories directory and a directory it cannot write to both end with exit 0
        r = subprocess.run(["/bin/sh", "-c", args[0]], capture_output=True, text=True, env=dict(env, REPOS_ROOT=str(Path(t) / "absent")), timeout=30)
        if r.returncode:
            errs.append(f"{name} script exits {r.returncode} when the repositories directory does not exist (first start)")
        for rel in OLD_LOCKS:
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
            os.utime(f, (old, old))
        locked = root / "alice/r.git/refs/heads"
        locked.chmod(0o555)
        try:
            if os.geteuid() != 0:
                r = subprocess.run(["/bin/sh", "-c", args[0]], capture_output=True, text=True, env=env, timeout=30)
                if r.returncode:
                    errs.append(f"{name} script exits {r.returncode} when a lock cannot be removed: a cleanup that cannot run must not stop the forge")
        finally:
            locked.chmod(0o755)
        if kind == "sweep":
            # the loop: a second pass finds a lock that turned old since the first, and TERM ends it at once (a pod must not wait out the sleep)
            (root / "later.git").mkdir()
            proc = subprocess.Popen(["/bin/sh", "-c", args[0]], env=dict(env, SWEEP_EVERY_S="1"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            time.sleep(0.5)
            lk = root / "alice/r.git/refs/heads/late.lock"
            lk.write_text("x")
            os.utime(lk, (old, old))
            deadline = time.time() + 6
            while lk.exists() and time.time() < deadline:
                time.sleep(0.2)
            if lk.exists():
                errs.append(f"{name} did not look again: a lock that turned old after the first pass is still there")
            t0 = time.time()
            proc.terminate()
            try:
                proc.wait(timeout=5)
                if proc.returncode != 0 or time.time() - t0 > 3:
                    errs.append(f"{name} ended with {proc.returncode} {time.time() - t0:.1f} s after TERM: it must exit 0 at once")
            except subprocess.TimeoutExpired:
                proc.kill()
                errs.append(f"{name} ignores TERM: the pod would wait out its termination grace period")
    return errs


def optin_problems(profiles, catalog, hr, jobs, other_crons, pvc_class=None, sc=None, base_sc=None, kust=None, rendered_cfg=None, rendered_tols=None, sbs=None, crd_ok=True, rendered_inits=None, flux_forgejo=None):
    """profiles: {name: set of Kustomization names}; jobs: the RecurringJobs of the brick; other_crons: the schedules of the other jobs."""
    errs = []
    for name in ("management", "workload"):
        if profiles[name] & BRICK:
            errs.append(f"apps/flux/{name} renders {sorted(profiles[name] & BRICK)}: the Forgejo brick is OPT-IN, off by default")
    if profiles["management-forge"] - profiles["management"] != BRICK:
        errs.append("apps/flux/management-forge must render exactly forgejo and forgejo-db more than apps/flux/management")
    if profiles["management"] - profiles["management-forge"]:
        errs.append("apps/flux/management-forge must not remove anything apps/flux/management renders")
    if (catalog.get("aliases") or {}).get("forge") != "forgejo":
        errs.append("the `forge` alias of bricks.yaml (the picker's way to include the forge) is gone")
    gc = (((hr["spec"]["values"].get("gitea") or {}).get("config") or {}).get("git.config")) or {}
    if str(gc.get("core.fsync")) != "all":
        errs.append(f"[git.config] core.fsync must be 'all' (gitea.config.git.config in the HelmRelease), not {gc.get('core.fsync')!r}: git does not fsync loose objects by default and a node loss loses acknowledged pushes (lab 27, FK)")
    if str(gc.get("core.fsyncMethod", "fsync")) != "fsync":
        errs.append(f"core.fsyncMethod {gc.get('core.fsyncMethod')!r} gives back part of the guarantee: leave git's default (fsync)")
    if rendered_cfg is not None and "core.fsync=all" not in rendered_cfg.get("git.config", ""):
        errs.append("the app.ini sources the chart renders have no [git.config] core.fsync=all")
    errs += basebackup_problems(sbs if sbs is not None else [], crd_ok)
    errs += tolerations_problems(hr["spec"]["values"].get("tolerations"), "the HelmRelease values")
    if rendered_tols is not None:
        errs += tolerations_problems(rendered_tols, "the Deployment the chart renders")
    pers = hr["spec"]["values"].get("persistence") or {}
    if pers.get("storageClass") != SC:
        errs.append(f"the Forgejo PVC must use the StorageClass {SC} (persistence.storageClass), not {pers.get('storageClass')!r}: only that class puts the volume in the backup group")
    if (pers.get("labels") or {}).get(LABEL):
        errs.append("persistence.labels still relies on a PVC label for the backup group: Longhorn does not copy it to the volume (lab 27, FG)")
    if pvc_class is not None and pvc_class != SC:
        errs.append(f"the PVC the chart renders has storageClassName {pvc_class!r}, want {SC}")
    if kust is not None and "storageclass.yaml" not in (kust.get("resources") or []):
        errs.append("apps/base/forgejo/kustomization.yaml does not list storageclass.yaml: the class would not exist")
    if sc is None or sc.get("metadata", {}).get("name") != SC:
        errs.append(f"apps/base/forgejo/storageclass.yaml has no StorageClass {SC}")
    else:
        params = dict(sc.get("parameters") or {})
        try:
            sel_json = json.loads(params.pop("recurringJobSelector", "null"))
        except ValueError:
            sel_json = None
        if sel_json != [{"name": GROUP, "isGroup": True}]:
            errs.append(f"the StorageClass recurringJobSelector must be exactly [{{\"name\": \"{GROUP}\", \"isGroup\": true}}], not {sel_json!r}")
        if sc.get("provisioner") != "driver.longhorn.io":
            errs.append("the StorageClass provisioner must be driver.longhorn.io")
        if base_sc is not None:
            if params != (base_sc.get("parameters") or {}):
                errs.append(f"the StorageClass parameters (besides recurringJobSelector) must be those of longhorn-x2-rwo: {params} vs {base_sc.get('parameters')}")
            for k in ("reclaimPolicy", "allowVolumeExpansion", "volumeBindingMode"):
                if sc.get(k) != base_sc.get(k):
                    errs.append(f"the StorageClass {k} must be that of longhorn-x2-rwo ({base_sc.get(k)!r}), not {sc.get(k)!r}")
        if "dirsync" not in (sc.get("mountOptions") or []):
            errs.append("the StorageClass longhorn-x2-rwo-forgejo must carry mountOptions: [dirsync] (the ref rename of a push must reach the disk, lab 27 FK2)")
        elif len(sc["mountOptions"]) != 1:
            errs.append(f"the StorageClass mountOptions must be exactly [dirsync], not {sc['mountOptions']!r}: every other option is untested here")
        if (sc.get("parameters") or {}).get("fsType", "ext4") != "ext4":
            errs.append("dirsync is an ext4 mount option here: the StorageClass fsType must stay ext4 (Longhorn's default)")
    errs += cleaner_problems(hr, rendered_inits, flux_forgejo)
    sel = [j for j in jobs if j["spec"].get("task") == "backup" and GROUP in (j["spec"].get("groups") or [])]
    if len(sel) != 1:
        errs.append(f"exactly one RecurringJob of type backup must select the group {GROUP} (found {len(sel)})")
    for j in sel:
        sp = j["spec"]
        if j["metadata"].get("namespace") != "longhorn-system":
            errs.append("the RecurringJob must be in longhorn-system")
        f = sp.get("cron", "").split()
        if len(f) != 5 or f[2:] != ["*", "*", "*"] or not (f[0].isdigit() and f[1].isdigit()):
            errs.append(f"the RecurringJob must run daily (a fixed minute and hour), not {sp.get('cron')!r}")
        elif sp["cron"] in other_crons:
            errs.append(f"the RecurringJob schedule {sp['cron']!r} collides with another job's")
        if not isinstance(sp.get("retain"), int) or sp["retain"] < 1:
            errs.append("the RecurringJob must keep a retention (retain >= 1)")
    return errs


def other_schedules():
    """Every cron of the other jobs: the Longhorn RecurringJobs (cron), the Kubernetes CronJobs and CNPG ScheduledBackups (schedule,
    the CNPG one with a leading seconds field, put in the 5-field form)."""
    out = set()
    for f in sorted((REPO / "apps/base").rglob("*.yaml")):
        if "forgejo" in f.parts[-2] or f.stat().st_size > 400000:
            continue
        try:
            for d in yaml.safe_load_all(f.read_text()):
                if not isinstance(d, dict):
                    continue
                if d.get("kind") == "RecurringJob":
                    out.add(d["spec"]["cron"])
                elif d.get("kind") == "CronJob":
                    out.add(d["spec"]["schedule"])
                elif d.get("kind") == "ScheduledBackup":
                    out.add(" ".join(d["spec"]["schedule"].split()[1:]))
        except yaml.YAMLError:
            continue
    return out


def gateway_egress_namespaces(gateway_cnp):
    return {e["matchLabels"].get("io.kubernetes.pod.namespace") for r in gateway_cnp["spec"]["egress"] for e in r.get("toEndpoints", [])}


def problems(configure_text, externalsecrets, helmrelease, repos, egress_text, flux, cnpg_files, route, gw_ns, routed):
    errs = []
    for ns in sorted(set(routed) - gw_ns):
        errs.append(f"the Gateway's egress policy (istio/networkpolicy.yaml) does not admit namespace {ns}, which an HTTPRoute sends traffic to")
    rules = route["spec"]["rules"]
    idx = [i for i, r in enumerate(rules) if any(m.get("path", {}).get("value") == "/metrics" for m in r.get("matches", []))]
    if not idx or rules[idx[0]].get("backendRefs") or idx[0] != 0:
        errs.append("the HTTPRoute must start with a /metrics rule that has no backendRefs (the endpoint is for vmagent only)")
    seed = seeded(configure_text)
    names = set()
    for es in externalsecrets:
        for ref in es["spec"].get("data", []):
            r = ref["remoteRef"]
            names.add(r["key"])
            if r["property"] not in seed.get(r["key"], set()):
                errs.append(f"ExternalSecret {es['metadata']['name']}: secret/{r['key']} property {r['property']} is not created by configure.sh")
    if not names:
        errs.append("no forgejo ExternalSecret was read: the test is blind")
    for es in externalsecrets:
        if es["metadata"]["name"] == "forgejo-admin":
            keys = set(((es["spec"].get("target") or {}).get("template") or {}).get("data", {})) | {r["secretKey"] for r in es["spec"].get("data", [])}
            if not {"username", "password"} <= keys:
                errs.append(f"forgejo-admin carries {sorted(keys)}: the chart reads `username` and `password`")
            tpl = es["spec"]["target"]["template"]["data"]["username"] if "username" in keys and "template" in (es["spec"].get("target") or {}) else None
            if tpl is not None and tpl != helmrelease["spec"]["values"]["gitea"]["admin"]["username"]:
                errs.append("forgejo-admin username differs from gitea.admin.username in the HelmRelease")
    svc = (((helmrelease["spec"]["values"].get("gitea") or {}).get("config") or {}).get("service")) or {}
    if svc.get("DISABLE_REGISTRATION") is not True or svc.get("REQUIRE_SIGNIN_VIEW") is not True:
        errs.append("gitea.config.service must set DISABLE_REGISTRATION and REQUIRE_SIGNIN_VIEW to true (the forge is on the public route)")
    ref = helmrelease["spec"]["chart"]["spec"]["sourceRef"]["name"]
    repo = repos.get(ref)
    if not repo:
        errs.append(f"HelmRepository {ref} not found")
    else:
        if repo["spec"].get("type") != "oci" or not repo["spec"]["url"].startswith("oci://code.forgejo.org/"):
            errs.append("the Forgejo chart is published as an OCI artifact on code.forgejo.org only: HelmRepository type oci and an oci:// url")
        host = re.sub(r"^\w+://", "", repo["spec"]["url"]).split("/")[0]
        if f"matchName: {host}" not in egress_text:
            errs.append(f"Flux's egress policy does not admit {host}: the chart cannot be fetched")
    kdb, kfg = flux.get("forgejo-db"), flux.get("forgejo")
    if not kdb or not kfg:
        errs.append("the Flux Kustomizations forgejo-db and forgejo must both exist")
    else:
        if kdb["spec"].get("wait") is not True:
            errs.append("forgejo-db must be wait: true (the chart is installed against a database that exists)")
        subs = [s.get("name") for s in (kdb["spec"].get("postBuild") or {}).get("substituteFrom", [])]
        if "cluster-identity" not in subs:
            errs.append("forgejo-db must substitute from cluster-identity (the S3 destination of its ObjectStores)")
        if "forgejo-db" not in [d["name"] for d in kfg["spec"].get("dependsOn", [])]:
            errs.append("forgejo must depend on forgejo-db")
        if kdb["spec"]["path"] != "./apps/base/forgejo-db":
            errs.append("forgejo-db path must be ./apps/base/forgejo-db")
    for f in cnpg_files:
        if "forgejo" in f:
            errs.append(f"{f}: a forgejo object in the cnpg directory ties the forge's database to every profile that picks cnpg")
    return errs


# The Audit finding that stays: the chart's init containers and the forge write under / (never proven read-only here). A NEW
# failing rule, or this one on a container that used to pass, is a regression.
KNOWN_KYVERNO = {"require-ro-rootfs"}


def kyverno_failures(ky, pod):
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "pod.yaml"
        f.write_text(yaml.safe_dump(pod))
        r = subprocess.run([ky, "apply", str(REPO / "apps/base/kyverno-policies"), "--resource", str(f)], capture_output=True, text=True)
    return set(re.findall(r"policy (\S+) -> resource \S+ failed:", r.stdout)), r.stdout


def rendered_pod():
    sys.path.insert(0, str(REPO / "scripts"))
    import ha_render
    objs = ha_render.kustomize(REPO / "apps/base/forgejo")
    repos = {d["metadata"]["name"]: d["spec"]["url"] for d in objs if d["kind"] == "HelmRepository"}
    hr = next(d for d in objs if d["kind"] == "HelmRelease")
    dep = next(d for d in ha_render.helm_release(hr, repos) if d["kind"] == "Deployment")
    t = dep["spec"]["template"]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "forgejo-x", "namespace": dep["metadata"]["namespace"],
                                                              "labels": t["metadata"]["labels"]}, "spec": t["spec"]}


def kyverno_problems():
    ky = os.environ.get("KYVERNO") or shutil.which("kyverno")
    if not ky or not shutil.which("helm"):
        print("skip the Kyverno check: no Kyverno CLI or no helm (set KYVERNO)")
        return [], None
    pod = rendered_pod()
    got, _ = kyverno_failures(ky, pod)
    errs = [f"Kyverno policy {p} fails on the rendered Forgejo pod" for p in sorted(got - KNOWN_KYVERNO)]
    return errs, (ky, pod)


def load():
    es = [d for rel in ("apps/base/forgejo/externalsecrets.yaml", "apps/base/forgejo-db/externalsecret.yaml") for d in docs(rel)
          if d["kind"] == "ExternalSecret"]
    hr = next(d for d in docs("apps/base/forgejo/helmrelease.yaml") if d["kind"] == "HelmRelease")
    repos = {d["metadata"]["name"]: d for d in docs("apps/base/forgejo/helmrelease.yaml") if d["kind"] == "HelmRepository"}
    flux = {d["metadata"]["name"]: d for rel in ("apps/flux/base/36-forgejo-db.yaml", "apps/flux/base/37-forgejo.yaml") for d in docs(rel)}
    cnpg = [str(p.relative_to(REPO)) for p in (REPO / "apps/base/cnpg").glob("*forgejo*")]
    cnpg += [str(REPO / "apps/base/cnpg/kustomization.yaml") for _ in [0] if "forgejo" in (REPO / "apps/base/cnpg/kustomization.yaml").read_text()]
    route = next(d for d in docs("apps/base/forgejo/httproute.yaml") if d["kind"] == "HTTPRoute")
    gw = next(d for d in docs("apps/base/istio/networkpolicy.yaml") if d["metadata"]["name"] == "openaether-gateway")
    return ((REPO / CONFIGURE).read_text(), es, hr, repos, (REPO / "apps/base/flux/flux-egress-restricted.yaml").read_text(), flux, cnpg, route,
            gateway_egress_namespaces(gw), route_namespaces())


def selftest(args):
    """Each mutation of the real inputs must be caught; an unmutated copy must pass."""
    conf, es, hr, repos, egress, flux, cnpg, route, gw_ns, routed = args
    muts = {
        "a missing forgejo/db seed": dict(conf=re.sub(r".*kv_create forgejo/db D\n", "", conf)),
        "a missing forgejo/admin seed": dict(conf=re.sub(r".*kv_create forgejo/admin D\n", "", conf)),
        "a missing forgejo/oidc seed": dict(conf=re.sub(r".*kv_create forgejo/oidc D\n", "", conf)),
        "an https chart repository": dict(repos={k: dict(v, spec=dict(v["spec"], type="default", url="https://forgejo-contrib.codeberg.page/forgejo-helm/")) for k, v in repos.items()}),
        "a chart host Flux cannot reach": dict(egress=egress.replace("code.forgejo.org", "elsewhere.example")),
        "a forgejo object in the cnpg directory": dict(cnpg=["apps/base/cnpg/forgejo-db-cluster.yaml"]),
    }
    a = copy.deepcopy(es)
    for e in a:
        if e["metadata"]["name"] == "forgejo-admin":
            e["spec"]["target"].pop("template", None)
    muts["an admin Secret without username"] = dict(es=a)
    f = copy.deepcopy(flux); f["forgejo-db"]["spec"]["wait"] = False
    muts["forgejo-db without wait"] = dict(flux=f)
    f = copy.deepcopy(flux); f["forgejo"]["spec"]["dependsOn"] = [{"name": "cnpg"}]
    muts["forgejo not depending on forgejo-db"] = dict(flux=f)
    f = copy.deepcopy(flux); f["forgejo-db"]["spec"]["postBuild"] = {}
    muts["forgejo-db without the cluster-identity substitution"] = dict(flux=f)
    r = copy.deepcopy(route); r["spec"]["rules"][0]["backendRefs"] = [{"name": "forgejo-http", "port": 3000}]
    muts["/metrics routed to the backend"] = dict(route=r)
    r = copy.deepcopy(route); r["spec"]["rules"] = r["spec"]["rules"][1:]
    muts["no /metrics rule"] = dict(route=r)
    h = copy.deepcopy(hr); h["spec"]["values"]["gitea"]["config"].pop("service")
    muts["self-registration left open"] = dict(hr=h)
    muts["a routed namespace the Gateway may not reach"] = dict(gw_ns=gw_ns - {"services-git"})
    ok = not problems(*args)
    print(("ok   " if ok else "FAIL ") + "the real inputs pass")
    for name, m in muts.items():
        a2 = dict(conf=conf, es=es, hr=hr, repos=repos, egress=egress, flux=flux, cnpg=cnpg, route=route, gw_ns=gw_ns, routed=routed)
        a2.update(m)
        caught = bool(problems(a2["conf"], a2["es"], a2["hr"], a2["repos"], a2["egress"], a2["flux"], a2["cnpg"], a2["route"], a2["gw_ns"], a2["routed"]))
        print(("ok   " if caught else "FAIL ") + f"caught: {name}")
        ok = ok and caught
    return ok


def kyverno_selftest(ky, pod):
    ok = True
    for name, mut in {"no runAsNonRoot": lambda p: p["spec"]["securityContext"].pop("runAsNonRoot"),
                      "no limit on an init container": lambda p: p["spec"]["initContainers"][0]["resources"].pop("limits"),
                      "an image from another registry": lambda p: p["spec"]["containers"][0].update(image="docker.io/library/nginx:1")}.items():
        p = copy.deepcopy(pod)
        mut(p)
        caught = bool(kyverno_failures(ky, p)[0] - KNOWN_KYVERNO)
        print(("ok   " if caught else "FAIL ") + f"caught by Kyverno: {name}")
        ok = ok and caught
    return ok


def optin_inputs():
    profiles = {n: kustomization_names(n) for n in ("management", "management-forge", "workload")}
    catalog = yaml.safe_load((REPO / "apps/flux/bricks.yaml").read_text())
    hr = next(d for d in docs("apps/base/forgejo/helmrelease.yaml") if d["kind"] == "HelmRelease")
    jobs = [d for d in docs("apps/base/forgejo/recurringjob.yaml") if d["kind"] == "RecurringJob"]
    sc = next((d for d in docs("apps/base/forgejo/storageclass.yaml") if d["kind"] == "StorageClass"), None)
    base_sc = next(d for d in docs("apps/base/storage/storageclasses.yaml") if d["kind"] == "StorageClass" and d["metadata"]["name"] == "longhorn-x2-rwo")
    kust = yaml.safe_load((REPO / "apps/base/forgejo/kustomization.yaml").read_text())
    pvc = None
    cfg = None
    tols = None
    inits = None
    if shutil.which("helm"):
        sys.path.insert(0, str(REPO / "scripts"))
        import ha_render
        objs = ha_render.kustomize(REPO / "apps/base/forgejo")
        repos = {d["metadata"]["name"]: d["spec"]["url"] for d in objs if d["kind"] == "HelmRepository"}
        rel = next(d for d in objs if d["kind"] == "HelmRelease")
        rendered = ha_render.helm_release(rel, repos)
        pvc = next(d for d in rendered if d["kind"] == "PersistentVolumeClaim")["spec"].get("storageClassName")
        cfg = next(d for d in rendered if d["kind"] == "Secret" and d["metadata"]["name"].endswith("inline-config")).get("stringData", {})
        tols = next(d for d in rendered if d["kind"] == "Deployment")["spec"]["template"]["spec"].get("tolerations") or []
        dep = next(d for d in rendered if d["kind"] == "Deployment")["spec"]["template"]["spec"]
        inits = (dep.get("initContainers") or [], dep["containers"][0]["image"], dep["containers"], (next(d for d in rendered if d["kind"] == "Deployment")["spec"].get("strategy") or {}).get("type"))
    sbs = [d for d in docs("apps/base/forgejo-db/scheduledbackup.yaml") if d["kind"] == "ScheduledBackup"]
    ffj = next(d for d in docs("apps/flux/base/37-forgejo.yaml") if d["kind"] == "Kustomization")
    return profiles, catalog, hr, jobs, other_schedules(), pvc, sc, base_sc, kust, cfg, tols, sbs, crd_has_immediate(), inits, ffj


def optin_selftest(inp):
    profiles, catalog, hr, jobs, crons, pvc, sc, base_sc, kust, cfg, tols, sbs, crd_ok, inits, ffj = inp
    muts = {
        "the forge back in the default management profile": dict(profiles=dict(profiles, management=profiles["management"] | BRICK)),
        "the forge in the workload profile": dict(profiles=dict(profiles, workload=profiles["workload"] | {"forgejo"})),
        "an opt-in profile that renders nothing more": dict(profiles=dict(profiles, **{"management-forge": profiles["management"]})),
        "no forge alias": dict(catalog={"aliases": {}}),
        "the PVC back on the plain class (no group)": dict(hr=_with_class(hr, "longhorn-x2-rwo")),
        "a rendered PVC on another class": dict(pvc_class="longhorn-x2-rwo"),
        "the backup group asked of a PVC label again": dict(hr=_with_label(hr)),
        "a StorageClass without the selector": dict(sc=_sc(sc, drop="recurringJobSelector")),
        "a selector on another group": dict(sc=_sc(sc, recurringJobSelector='[{"name":"other","isGroup":true}]')),
        "a selector that names a job, not a group": dict(sc=_sc(sc, recurringJobSelector='[{"name":"oa-forgejo","isGroup":false}]')),
        "a StorageClass with one Longhorn replica": dict(sc=_sc(sc, numberOfReplicas="1")),
        "a StorageClass left out of the kustomization": dict(kust={"resources": ["namespace.yaml"]}),
        "no StorageClass at all": dict(sc=None),
        "no git fsync of loose objects": dict(hr=_with_gitcfg(hr, None)),
        "core.fsync left at the committed set": dict(hr=_with_gitcfg(hr, {"core.fsync": "committed"})),
        "core.fsync all but writeout-only": dict(hr=_with_gitcfg(hr, {"core.fsync": "all", "core.fsyncMethod": "writeout-only"})),
        "a rendered app.ini without core.fsync": dict(rendered_cfg={"git.config": ""}) if cfg is not None else dict(hr=_with_gitcfg(hr, None)),
        "no toleration: Kubernetes' 300 s default": dict(hr=_with_tols(hr, None)),
        "tolerations kept at 300 s": dict(hr=_with_tols(hr, [dict(key=k, operator="Exists", effect="NoExecute", tolerationSeconds=300) for k in ("node.kubernetes.io/unreachable", "node.kubernetes.io/not-ready")])),
        "only the unreachable toleration": dict(hr=_with_tols(hr, [dict(key="node.kubernetes.io/unreachable", operator="Exists", effect="NoExecute", tolerationSeconds=30)])),
        "a rendered Deployment without the tolerations": dict(rendered_tols=[]) if tols is not None else dict(hr=_with_tols(hr, None)),
        "no immediate base backup at enablement": dict(sbs=[_sb(sbs, immediate=False)]),
        "immediate left out of the ScheduledBackup": dict(sbs=[_sb(sbs, drop="immediate")]),
        "a second ScheduledBackup for forgejo-db": dict(sbs=[sbs[0], _sb(sbs, name="forgejo-db-second")]),
        "a CRD without spec.immediate": dict(crd_ok=False),
        "no dirsync mount option": dict(sc=_sc_mount(sc, None)),
        "mount options sync instead of dirsync": dict(sc=_sc_mount(sc, ["sync"])),
        "dirsync with another option": dict(sc=_sc_mount(sc, ["dirsync", "noatime"])),
        "a StorageClass on xfs": dict(sc=_sc(sc, fsType="xfs")),
        "no lock cleaner": dict(hr=_cleaner(hr, drop=True)),
        "no sweeper sidecar": dict(hr=_cleaner(hr, drop=True, name=SWEEPER)),
        "a lock cleaner added last, not first": dict(hr=_cleaner(hr, path="/spec/template/spec/initContainers/-")),
        "a sweeper added as an init container": dict(hr=_cleaner(hr, name=SWEEPER, path="/spec/template/spec/initContainers/0")),
        "a lock cleaner without resource limits": dict(hr=_cleaner(hr, set_=("resources", {"requests": {"cpu": "1m", "memory": "1Mi"}}))),
        "a sweeper without resource limits": dict(hr=_cleaner(hr, name=SWEEPER, set_=("resources", {"requests": {"cpu": "1m", "memory": "1Mi"}}))),
        "a lock cleaner running as root": dict(hr=_cleaner(hr, set_=("securityContext", {"allowPrivilegeEscalation": False, "privileged": False, "capabilities": {"drop": ["ALL"]}, "runAsUser": 0}))),
        "a sweeper that may escalate": dict(hr=_cleaner(hr, name=SWEEPER, set_=("securityContext", {"allowPrivilegeEscalation": True, "privileged": False, "capabilities": {"drop": ["ALL"]}}))),
        "a lock cleaner on the wrong directory": dict(hr=_cleaner(hr, set_=("env", [{"name": "REPOS_ROOT", "value": "/data"}, {"name": "LOCK_MIN_AGE_MIN", "value": "0"}]))),
        "an init cleaner back at the 5 minute limit (F-LOCKAGE)": dict(hr=_cleaner(hr, set_=("env", _env("5")))),
        "an init cleaner that loops": dict(hr=_cleaner(hr, set_=("env", _env("0", SWEEP_EVERY_S="60")))),
        "a sweeper with a 2 minute limit": dict(hr=_cleaner(hr, name=SWEEPER, set_=("env", _env("2", CLEANER_NAME=SWEEPER, SWEEP_EVERY_S="60")))),
        "a sweeper with no age limit": dict(hr=_cleaner(hr, name=SWEEPER, set_=("env", _env("0", CLEANER_NAME=SWEEPER, SWEEP_EVERY_S="60")))),
        "a sweeper that does not loop": dict(hr=_cleaner(hr, name=SWEEPER, set_=("env", _env("10", CLEANER_NAME=SWEEPER)))),
        "a sweeper that loops every second": dict(hr=_cleaner(hr, name=SWEEPER, set_=("env", _env("10", CLEANER_NAME=SWEEPER, SWEEP_EVERY_S="1")))),
        "a sweeper that logs under the init container's name": dict(hr=_cleaner(hr, name=SWEEPER, set_=("env", _env("10", SWEEP_EVERY_S="60")))),
        "the two scripts drifting apart": dict(hr=_cleaner(hr, name=SWEEPER, replace=("exit 0\n", "exit 0\n# drift\n"))),
        "a lock cleaner with no age limit for the sweeper": dict(hr=_both(hr, replace=(' [ "$MIN" -gt 0 ] &&', " false &&"))),
        "a lock cleaner that removes every lock": dict(hr=_both(hr, replace=("*) continue ;;", "*) ;;"))),
        "a lock cleaner that removes index.lock too": dict(hr=_both(hr, replace=("*) continue ;;", "*/*.git/index.lock) ;;\n*) continue ;;"))),
        "a lock cleaner that ignores packed-refs.lock": dict(hr=_both(hr, replace=("*/*.git/packed-refs.lock | ", ""))),
        "a lock cleaner that follows symlinks": dict(hr=_both(hr, replace=("find \"$ROOT\" -type f", "find -L \"$ROOT\""))),
        "a lock cleaner that stays silent": dict(hr=_both(hr, replace=('echo "$NAME: removed $rel (age limit $MIN min)"', ":"))),
        "a lock cleaner that prints the contents": dict(hr=_both(hr, replace=('if rm -f -- "$f"; then', 'cat "$f"; if rm -f -- "$f"; then'))),
        "a lock cleaner that fails the start": dict(hr=_both(hr, replace=("sweep\nif", "sweep\nexit 1\nif"))),
        "a lock cleaner that takes a garbled age for zero": dict(hr=_both(hr, replace=('"" | *[!0-9]*)', "x)"))),
        "a sweeper that ignores TERM": dict(hr=_both(hr, replace=("trap 'exit 0' TERM INT", "trap '' TERM INT"))),
        "a sweeper that sleeps in the foreground": dict(hr=_both(hr, replace=('sleep "$EVERY" &\n', 'sleep "$EVERY"\n'))),
        "a lock cleaner on a postBuild Kustomization": dict(flux_forgejo={"spec": {"postBuild": {"substituteFrom": []}}}),
        "a lock cleaner on another image than the app": dict(rendered_inits=([dict(inits[0][0], image="docker.io/library/busybox:1")] + inits[0][1:],) + inits[1:]) if inits else dict(hr=_cleaner(hr, drop=True)),
        "a sweeper on another image than the app": dict(rendered_inits=(inits[0], inits[1], [dict(c, image="docker.io/library/busybox:1") if c["name"] == SWEEPER else c for c in inits[2]], inits[3])) if inits else dict(hr=_cleaner(hr, drop=True, name=SWEEPER)),
        "a Deployment on a rolling update, where an old pod may still push": dict(rendered_inits=inits[:3] + ("RollingUpdate",)) if inits else dict(hr=_cleaner(hr, drop=True)),
        "no RecurringJob": dict(jobs=[]),
        "a RecurringJob on another group": dict(jobs=[_job(jobs, groups=["other"])]),
        "a RecurringJob that is a snapshot, not a backup": dict(jobs=[_job(jobs, task="snapshot")]),
        "an hourly RecurringJob": dict(jobs=[_job(jobs, cron="55 * * * *")]),
        "a RecurringJob at the time of oa-backup-daily": dict(jobs=[_job(jobs, cron="37 3 * * *")]),
        "a RecurringJob with no retention": dict(jobs=[_job(jobs, retain=0)]),
    }
    ok = not optin_problems(*inp)
    print(("ok   " if ok else "FAIL ") + "the real opt-in inputs pass")
    for name, m in muts.items():
        a = dict(profiles=profiles, catalog=catalog, hr=hr, jobs=jobs, other_crons=crons, pvc_class=pvc, sc=sc, base_sc=base_sc, kust=kust, rendered_cfg=cfg, rendered_tols=tols, sbs=sbs, crd_ok=crd_ok, rendered_inits=inits, flux_forgejo=ffj)
        a.update(m)
        caught = bool(optin_problems(**a))
        print(("ok   " if caught else "FAIL ") + f"caught: {name}")
        ok = ok and caught
    return ok


def _job(jobs, **spec):
    j = copy.deepcopy(jobs[0])
    j["spec"].update(spec)
    return j


def _with_class(hr, name):
    h = copy.deepcopy(hr)
    h["spec"]["values"]["persistence"]["storageClass"] = name
    return h


def _with_gitcfg(hr, cfg):
    h = copy.deepcopy(hr)
    c = h["spec"]["values"]["gitea"]["config"]
    if cfg is None:
        c.pop("git.config", None)
    else:
        c["git.config"] = cfg
    return h


def _sb(sbs, drop=None, name=None, **spec):
    d = copy.deepcopy(sbs[0])
    if drop:
        d["spec"].pop(drop, None)
    if name:
        d["metadata"]["name"] = name
    d["spec"].update(spec)
    return d


def _with_tols(hr, tols):
    h = copy.deepcopy(hr)
    if tols is None:
        h["spec"]["values"].pop("tolerations", None)
    else:
        h["spec"]["values"]["tolerations"] = tols
    return h


def _with_label(hr):
    h = copy.deepcopy(hr)
    h["spec"]["values"]["persistence"]["labels"] = {LABEL: "enabled"}
    return h


def _sc_mount(sc, opts):
    c = copy.deepcopy(sc)
    if opts is None:
        c.pop("mountOptions", None)
    else:
        c["mountOptions"] = opts
    return c


def _cleaner(hr, drop=False, path=None, set_=None, replace=None, name=CLEANER):
    h = copy.deepcopy(hr)
    pats = h["spec"]["postRenderers"][0]["kustomize"]["patches"]
    i = next(k for k, p in enumerate(pats) if f"name: {name}" in p["patch"])
    if drop:
        pats.pop(i)
        return h
    ops = yaml.safe_load(pats[i]["patch"])
    if path:
        ops[0]["path"] = path
    if set_:
        ops[0]["value"][set_[0]] = set_[1]
    if replace:
        a = ops[0]["value"]["args"][0]
        assert replace[0] in a, replace[0]
        ops[0]["value"]["args"][0] = a.replace(*replace)
    pats[i]["patch"] = yaml.safe_dump(ops)
    return h


def _env(init_min="0", **extra):
    e = [{"name": "REPOS_ROOT", "value": "/data/git/gitea-repositories"}, {"name": "LOCK_MIN_AGE_MIN", "value": init_min}]
    return e + [{"name": k, "value": v} for k, v in extra.items()]


def _both(hr, **kw):
    """The same change to the script of BOTH cleaners, which the test insists are one text."""
    return _cleaner(_cleaner(hr, name=SWEEPER, **kw), **kw)


def _sc(sc, drop=None, **params):
    c = copy.deepcopy(sc)
    if drop:
        c["parameters"].pop(drop)
    c["parameters"].update(params)
    return c


def main():
    args = load()
    inp = optin_inputs()
    kerrs, kctx = kyverno_problems()
    if "--selftest" in sys.argv:
        ok = selftest(args)
        ok = optin_selftest(inp) and ok
        if kctx:
            ok = kyverno_selftest(*kctx) and ok
        sys.exit(0 if ok else 1)
    errs = problems(*args) + optin_problems(*inp) + kerrs
    for e in errs:
        print("FAIL " + e)
    print(f"{len(errs)} failed" if errs else "ok   the Forgejo brick is wired to its secrets, its chart source and its own database Kustomization")
    sys.exit(1 if errs else 0)


main()
