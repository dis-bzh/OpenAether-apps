#!/usr/bin/env python3
"""A cluster rebuilt by the DR chain archives under a NEW serverName and everything follows it (apps #153), offline.

Lab 20: the rebuilt zitadel-db archived into the dead cluster's WAL path (barman: "Expected empty archive"), so the archive stopped at
the old cluster's last segment and the verifier failed with "trails the live database". The fix is a generation suffix: the Cluster
archives under serverName `<cluster>${CNPG_ARCHIVE_SUFFIX:=}` (Flux substitutes it from the optional ConfigMap cnpg-archive-generation
that `task dr-restore-plan` writes), the older path stays as the recovery source. This test shows, without a cluster:
  * render: with no ConfigMap the serverName is the Cluster name and the ObjectStore paths do not move; with the suffix the Cluster
    archives under the new name while the ObjectStore destinationPath is untouched (the plugin appends the serverName);
  * Flux: the cnpg brick and the backup-databases-identity brick read the ConfigMap as OPTIONAL (a fresh install has none), the
    foundation-databases copy of cluster-identity carries the suffix, the verifier container reads it;
  * the verifier (barman_check.py) against an in-memory store holding an OLD archive (complete, the dead cluster's) and a NEW one:
    it follows the current generation, still reads the old path when no suffix is set, and does not call the new archive healthy
    because the old one is;
  * the restore tests build their recovery source from the live Cluster's serverName (current generation), default unchanged.
Run: python3 -I scripts/dev/test-cnpg-archive-generation.py
"""
import datetime
import gzip
import importlib.util
import io
import os
import re
import subprocess
import sys
import tarfile
from contextlib import redirect_stdout
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def flux_subst(text, env):
    """What Flux's postBuild does to a rendered manifest: ${VAR}, ${VAR:=default}, ${VAR:-default}."""
    def one(m):
        var, op, default = m.group(1), m.group(2), m.group(3) or ""
        v = env.get(var)
        return v if v else (default if op else "")
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:=|:-)([^}]*))?\}", one, text)


# `cnpg` and `forgejo-db` are two Flux Kustomizations that read the same ConfigMaps: both are rendered and substituted.
PATHS = ("apps/base/cnpg", "apps/base/forgejo-db")
CLUSTERS = ("zitadel-db", "grafana-db", "forgejo-db")


def render(env):
    out = "\n---\n".join(subprocess.run(["kubectl", "kustomize", str(REPO / p)], check=True, capture_output=True, text=True).stdout
                         for p in PATHS)
    return [d for d in yaml.safe_load_all(flux_subst(out, env)) if d]


def archiver(cluster):
    return [p for p in cluster["spec"]["plugins"] if p.get("isWALArchiver")][0]


print("=== render: the Cluster's serverName ===")
for env, want in (({}, ""), ({"CNPG_ARCHIVE_SUFFIX": ""}, ""), ({"CNPG_ARCHIVE_SUFFIX": "-g2"}, "-g2")):
    docs = render(env)
    cl = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Cluster"}
    stores = {d["metadata"]["name"]: d for d in docs if d["kind"] == "ObjectStore"}
    label = f"suffix {env.get('CNPG_ARCHIVE_SUFFIX', 'unset')!r}"
    check(f"{label}: both Clusters archive under <cluster>{want}",
          all(archiver(cl[c])["parameters"].get("serverName") == c + want for c in CLUSTERS),
          str({c: archiver(cl[c])["parameters"] for c in cl}))
    check(f"{label}: the ObjectStore paths do not move (the plugin appends the serverName)",
          all(stores[f"{c}-{s}"]["spec"]["configuration"]["destinationPath"].endswith(f"/cnpg/{c}")
              for c in CLUSTERS for s in ("primary", "replica")))
    check(f"{label}: the ObjectStores carry no serverName (the CRD forbids it there)",
          all("serverName" not in s["spec"]["configuration"] for s in stores.values()))
raw = subprocess.run(["kubectl", "kustomize", str(REPO / "apps/base/cnpg")], check=True, capture_output=True, text=True).stdout
check("the unsubstituted render names the variable (the value is not hard-coded in git)",
      raw.count("${CNPG_ARCHIVE_SUFFIX:=}") == 2)

print("=== Flux: who reads the ConfigMap ===")


def kustomizations(path):
    return {d["metadata"]["name"]: d for d in yaml.safe_load_all((REPO / path).read_text()) if d and d.get("kind") == "Kustomization"}


def reads_optional(ks):
    refs = (ks["spec"].get("postBuild") or {}).get("substituteFrom") or []
    return [r for r in refs if r.get("name") == "cnpg-archive-generation"]


for path, name in (("apps/flux/base/20-cnpg.yaml", "cnpg"), ("apps/flux/base/22a-backup-identity.yaml", "backup-databases-identity")):
    r = reads_optional(kustomizations(path)[name])
    check(f"{name} substitutes from cnpg-archive-generation, OPTIONAL (a fresh install has none)",
          len(r) == 1 and r[0].get("optional") is True and r[0].get("kind") == "ConfigMap", str(r))
    refs = kustomizations(path)[name]["spec"]["postBuild"]["substituteFrom"]
    check(f"{name} still reads cluster-identity first", refs[0]["name"] == "cluster-identity")
cm = next(d for d in yaml.safe_load_all((REPO / "apps/base/backup/databases-identity/configmap.yaml").read_text()) if d)
check("the foundation-databases copy of cluster-identity carries CNPG_ARCHIVE_SUFFIX defaulting to empty",
      cm["data"].get("CNPG_ARCHIVE_SUFFIX") == "${CNPG_ARCHIVE_SUFFIX:=}")
check("...and Flux turns it into the suffix, or into nothing",
      yaml.safe_load(flux_subst(yaml.safe_dump(cm), {"CLUSTER_NAME": "x", "CNPG_ARCHIVE_SUFFIX": "-g3"}))["data"]["CNPG_ARCHIVE_SUFFIX"] == "-g3"
      and yaml.safe_load(flux_subst(yaml.safe_dump(cm), {"CLUSTER_NAME": "x"}))["data"]["CNPG_ARCHIVE_SUFFIX"] in ("", None))
cj = next(d for d in yaml.safe_load_all((REPO / "apps/base/backup/databases/verify-cronjob.yaml").read_text()) if d and d["kind"] == "CronJob")
ic = {c["name"]: c for c in cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["initContainers"]}
check("the verifier container (barman-stage) reads cluster-identity, where the suffix is",
      {"configMapRef": {"name": "cluster-identity"}} in ic["barman-stage"].get("envFrom", []))

print("=== the verifier follows the current archive ===")
bc = load("barman_check", "apps/base/backup/databases/verify/barman_check.py")
NOW = datetime.datetime(2026, 10, 21, 12, 0, tzinfo=datetime.timezone.utc).timestamp()


def seg(n, tl=1):
    return f"{tl:08X}{n // 256:08X}{n % 256:08X}"


def archive(objs, prefix, first=1, last=3, ended=NOW - 3600, stamp=None):
    """A complete base backup (backup.info, a tiny data.tar.gz) and the WAL first..last under prefix = cnpg/<c>/<serverName>/."""
    bid = "20261021T100000"
    iso = datetime.datetime.fromtimestamp(ended, datetime.timezone.utc).isoformat()
    info = (f"backup_id={bid}\nstatus=DONE\ntimeline=1\nbegin_wal={seg(first)}\nend_wal={seg(first)}\nend_time={iso}\n"
            f"xlog_segment_size=16777216\n")
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w") as t:
        data = b"x" * 600
        ti = tarfile.TarInfo("PG_VERSION"); ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    objs[f"{prefix}base/{bid}/backup.info"] = info.encode()
    objs[f"{prefix}base/{bid}/data.tar.gz"] = gzip.compress(tar.getvalue())
    for n in range(first, last + 1):
        objs[f"{prefix}wals/0000000100000000/{seg(n)}.gz"] = gzip.compress(b"w" * 100)


class FakeS3:
    """list_objects_v2 / get_object over a dict, enough for barman_check.Store."""
    def __init__(self, objs, mtime=NOW - 7200):
        self.objs, self.mtime = objs, mtime

    def list_objects_v2(self, Bucket, Prefix, Delimiter=None, ContinuationToken=None):
        keys = sorted(k for k in self.objs if k.startswith(Prefix))
        if Delimiter:
            pre = sorted({Prefix + k[len(Prefix):].split(Delimiter)[0] + Delimiter for k in keys if Delimiter in k[len(Prefix):]})
            return {"CommonPrefixes": [{"Prefix": p} for p in pre], "Contents": [], "IsTruncated": False}
        return {"Contents": [{"Key": k, "Size": len(self.objs[k]), "LastModified": datetime.datetime.fromtimestamp(self.mtime, datetime.timezone.utc)}
                             for k in keys], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objs[Key])}


def run_main(objs, suffix=None, clusters="zitadel-db"):
    env = {"NOW": str(NOW), "PRIMARY_ENDPOINT": "http://p", "PRIMARY_BUCKET": "b", "PRIMARY_ACCESS_KEY": "a", "PRIMARY_SECRET_KEY": "s",
           "CLUSTERS": clusters}
    if suffix is not None:
        env["CNPG_ARCHIVE_SUFFIX"] = suffix
    saved = (dict(os.environ), bc.client)
    os.environ.clear(); os.environ.update(env)
    bc.client = lambda *a, **k: FakeS3(objs)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            bc.main()
    finally:
        os.environ.clear(); os.environ.update(saved[0]); bc.client = saved[1]
    return [l.split("\t") for l in buf.getvalue().splitlines()]


OLD = "cnpg/zitadel-db/zitadel-db/"
NEW = "cnpg/zitadel-db/zitadel-db-g2/"
only_old = {}
archive(only_old, OLD)
rows = run_main(only_old)
check("no suffix (a fresh install, an existing one): the historical path is read and passes", rows[0][2] == "1", str(rows))
rows = run_main(only_old, suffix="")
check("an empty suffix is the same", rows[0][2] == "1", str(rows))
# the rebuilt cluster: the dead cluster's complete archive, and nothing under the new name yet
rows = run_main(only_old, suffix="-g2")
check("suffix -g2 with only the OLD archive present: the old archive does NOT make the new one healthy (skip, empty)",
      rows[0][2] == "skip" and "[archive zitadel-db-g2]" in rows[0][4], str(rows))
both = dict(only_old)
archive(both, NEW, first=7, last=9)
rows = run_main(both, suffix="-g2")
check("suffix -g2 with both archives: the NEW one is verified (WAL 7..9), the old one is not what is reported",
      rows[0][2] == "1" and "WAL 7..9" in rows[0][4], str(rows))
rows = run_main(both)
check("without the suffix the same stores report the OLD archive (WAL 1..3): the suffix is what moves the verifier", rows[0][2] == "1" and "WAL 1..3" in rows[0][4], str(rows))
broken = dict(both)
del broken[f"{NEW}wals/0000000100000000/{seg(8)}.gz"]
rows = run_main(broken, suffix="-g2")
check("a hole in the NEW archive fails the verifier although the old archive is intact", rows[0][2] == "0" and "WAL gap" in rows[0][4], str(rows))
rows = run_main(broken)
check("...and the same hole is invisible when the verifier still reads the old path (what lab 20 had: the stale path was read)", rows[0][2] == "1", str(rows))

print("=== the restore tests recover from the current archive ===")
rc = load("restore_cnpg", "apps/base/backup/databases/restore/restore_cnpg.py")
base = {"spec": {"imageName": "img", "storage": {"size": "5Gi"},
                 "plugins": [{"name": "barman-cloud.cloudnative-pg.io", "isWALArchiver": True, "parameters": {"barmanObjectName": "zitadel-db-primary"}}]}}
gen2 = {"spec": {**base["spec"], "plugins": [{"name": "barman-cloud.cloudnative-pg.io", "isWALArchiver": True,
                                               "parameters": {"barmanObjectName": "zitadel-db-primary", "serverName": "zitadel-db-g2"}}]}}


def server_of(src, store="replica"):
    m = rc.recovery_manifest("zitadel-db", store, "latest", src)
    p = m["spec"]["externalClusters"][0]["plugin"]["parameters"]
    return p["serverName"], p["barmanObjectName"]


check("a live Cluster without serverName (every existing install): the Cluster name, as before", server_of(base) == ("zitadel-db", "zitadel-db-replica"), str(server_of(base)))
check("a live Cluster archiving under -g2: the scratch recovery reads -g2, from the same ObjectStore", server_of(gen2) == ("zitadel-db-g2", "zitadel-db-replica"), str(server_of(gen2)))
check("PITR (a target time) follows the same archive",
      rc.recovery_manifest("zitadel-db", "primary", "pitr", gen2, "2026-10-21 10:00:00+00")["spec"]["externalClusters"][0]["plugin"]["parameters"]["serverName"] == "zitadel-db-g2")
check("a Cluster object with no plugins at all (a malformed read) falls back to the Cluster name", rc.archive_server({"spec": {}}, "grafana-db") == "grafana-db")

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
