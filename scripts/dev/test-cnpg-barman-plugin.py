#!/usr/bin/env python3
"""CNPG archives through the Barman Cloud plugin, and the objects match the vendored CRDs (apps #114).

The in-tree `barmanObjectStore` is removed in CNPG 1.31. Renders apps/base/cnpg with `kubectl kustomize` and checks:
no Cluster or backup spec still uses `barmanObjectStore`; each Cluster names a WAL-archiving plugin ObjectStore that exists in
its namespace; each ScheduledBackup uses `method: plugin`; every ObjectStore validates against the CRD in the plugin dump
(unknown field, enum, required, type); the sidecar of every ObjectStore carries the Outscale checksum fix (apps #132) and a
memory limit; the plugin Deployment runs with bounds and info logging; with the Kyverno CLI ($KYVERNO or PATH) the
rendered workloads pass the policies. Static: it proves the manifests, not a store.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLUGIN = "barman-cloud.cloudnative-pg.io"
CHECKSUM_ENV = {"AWS_REQUEST_CHECKSUM_CALCULATION": "when_required", "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required"}


def render(path="apps/base/cnpg"):
    out = subprocess.run(["kubectl", "kustomize", str(REPO / path)], check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def check_schema(node, schema, path, errs):
    """A small OpenAPI v3 walk: type, enum, required and unknown fields (what a typo or a removed field trips on)."""
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        if not isinstance(node, dict):
            errs.append(f"{path}: want object"); return
        props = schema.get("properties", {})
        for r in schema.get("required", []):
            if r not in node:
                errs.append(f"{path}: missing required {r}")
        for k, v in node.items():
            if k in props:
                check_schema(v, props[k], f"{path}.{k}", errs)
            elif "additionalProperties" in schema and isinstance(schema["additionalProperties"], dict):
                check_schema(v, schema["additionalProperties"], f"{path}.{k}", errs)
            elif not schema.get("x-kubernetes-preserve-unknown-fields") and "additionalProperties" not in schema:
                errs.append(f"{path}: unknown field {k}")
    elif t == "array":
        if not isinstance(node, list):
            errs.append(f"{path}: want array"); return
        for i, v in enumerate(node):
            check_schema(v, schema.get("items", {}), f"{path}[{i}]", errs)
    elif t == "string":
        if not isinstance(node, str):
            errs.append(f"{path}: want string, got {node!r}")
        elif "enum" in schema and node not in schema["enum"]:
            errs.append(f"{path}: {node!r} not in {schema['enum']}")
    elif t == "integer" and not isinstance(node, int):
        errs.append(f"{path}: want integer")
    elif t == "boolean" and not isinstance(node, bool):
        errs.append(f"{path}: want boolean")


def crd_schema(docs, kind):
    for d in docs:
        if d["kind"] == "CustomResourceDefinition" and d["spec"]["names"]["kind"] == kind:
            return d["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    raise SystemExit(f"no CRD {kind} in the render")


def main(path="apps/base/cnpg"):
    docs = render(path)
    errs = []
    stores = {(d["metadata"]["namespace"], d["metadata"]["name"]): d for d in docs if d["kind"] == "ObjectStore"}
    clusters = [d for d in docs if d["kind"] == "Cluster"]
    if not clusters:
        errs.append("no Cluster rendered")
    for c in clusters:
        n = c["metadata"]["name"]
        if "barmanObjectStore" in (c["spec"].get("backup") or {}):
            errs.append(f"Cluster {n}: still uses spec.backup.barmanObjectStore")
        arch = [p for p in c["spec"].get("plugins", []) if p.get("name") == PLUGIN and p.get("isWALArchiver")]
        if len(arch) != 1:
            errs.append(f"Cluster {n}: want exactly one {PLUGIN} plugin with isWALArchiver, got {len(arch)}"); continue
        ref = arch[0].get("parameters", {}).get("barmanObjectName")
        if (c["metadata"]["namespace"], ref) not in stores:
            errs.append(f"Cluster {n}: barmanObjectName {ref!r} is not an ObjectStore of its namespace"); continue
        # The replica store: its own ObjectStore, its own key pair, its own destination (infra #368).
        rep = stores.get((c["metadata"]["namespace"], f"{n}-replica"))
        if rep is None:
            errs.append(f"Cluster {n}: no ObjectStore {n}-replica for the second store"); continue
        pc, rc = stores[(c["metadata"]["namespace"], ref)]["spec"]["configuration"], rep["spec"]["configuration"]
        if pc["s3Credentials"]["accessKeyId"]["name"] == rc["s3Credentials"]["accessKeyId"]["name"]:
            errs.append(f"{n}: the replica ObjectStore reads the primary key pair")
        for f in ("destinationPath", "endpointURL"):
            if "REPLICA" not in rc[f] or "REPLICA" in pc[f]:
                errs.append(f"{n}: {f} of the replica must come from BACKUP_REPLICA_S3_*, the primary's from BACKUP_S3_*")
        if rc["destinationPath"].split("/", 3)[-1] != pc["destinationPath"].split("/", 3)[-1]:
            errs.append(f"{n}: the replica path inside the bucket differs from the primary's (the copy keeps the layout)")
    for d in docs:
        if d["kind"] == "ScheduledBackup":
            if d["spec"].get("method") != "plugin" or d["spec"].get("pluginConfiguration", {}).get("name") != PLUGIN:
                errs.append(f"ScheduledBackup {d['metadata']['name']}: want method plugin with {PLUGIN}")
    os_schema = crd_schema(docs, "ObjectStore")["properties"]["spec"]
    for (ns, name), d in stores.items():
        e = []
        check_schema(d["spec"], os_schema, f"ObjectStore {name}.spec", e)
        errs += e
        env = {x["name"]: x.get("value") for x in d["spec"].get("instanceSidecarConfiguration", {}).get("env", [])}
        if any(env.get(k) != v for k, v in CHECKSUM_ENV.items()):
            errs.append(f"ObjectStore {name}: sidecar env lacks the checksum fix (apps #132): {env}")
        if "memory" not in d["spec"].get("instanceSidecarConfiguration", {}).get("resources", {}).get("limits", {}):
            errs.append(f"ObjectStore {name}: the sidecar has no memory limit")
        for part in ("wal", "data"):
            if d["spec"]["configuration"].get(part, {}).get("encryption") != "AES256":
                errs.append(f"ObjectStore {name}: {part}.encryption must request server-side AES256")
    plugin = [d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "barman-cloud"]
    if len(plugin) != 1:
        errs.append("the barman-cloud plugin Deployment is missing")
    else:
        c = plugin[0]["spec"]["template"]["spec"]["containers"][0]
        if "--log-level=info" not in c["args"] or "--log-level=debug" in c["args"]:
            errs.append("plugin: log level must be info")
        if "memory" not in c.get("resources", {}).get("limits", {}):
            errs.append("plugin: no memory limit")
        if c["securityContext"].get("runAsNonRoot") is not True:
            errs.append("plugin: container runAsNonRoot must be true")
        # lab 25 (D3): the operator asks the plugin at every Cluster reconcile and never reaches the failover while it is
        # unreachable; one pod in a lost zone held every database without a primary for 388-402 s.
        spec = plugin[0]["spec"]
        pod = spec["template"]["spec"]
        if spec.get("replicas", 1) < 2:
            errs.append("plugin: needs 2 replicas, a failover waits for it (lab 25, D3)")
        if spec.get("strategy", {}).get("type") != "RollingUpdate":
            errs.append("plugin: a Recreate rollout takes both pods away; use RollingUpdate")
        keys = {t["topologyKey"] for t in pod.get("topologySpreadConstraints", [])}
        if keys != {"kubernetes.io/hostname", "topology.kubernetes.io/zone"}:
            errs.append(f"plugin: spread over hostname and zone, got {sorted(keys)}")
        secs = {t["key"]: t.get("tolerationSeconds") for t in pod.get("tolerations", [])}
        if secs.get("node.kubernetes.io/unreachable") != 30 or secs.get("node.kubernetes.io/not-ready") != 30:
            errs.append("plugin: 30 s tolerations on unreachable and not-ready, like the database pods")
        pdb = [d for d in docs if d["kind"] == "PodDisruptionBudget" and d["metadata"]["name"] == "barman-cloud"]
        if not pdb or pdb[0]["spec"].get("maxUnavailable") != 1 or pdb[0]["spec"]["selector"]["matchLabels"] != {"app": "barman-cloud"}:
            errs.append("plugin: a PodDisruptionBudget allowing one disruption and selecting app=barman-cloud")
    ky = os.environ.get("KYVERNO") or shutil.which("kyverno")
    if ky and not errs:
        with tempfile.TemporaryDirectory() as t:
            wl = Path(t) / "workloads.yaml"
            wl.write_text(yaml.safe_dump_all([d for d in docs if d["kind"] in ("Deployment", "CronJob", "Job")]))
            r = subprocess.run([ky, "apply", str(REPO / "apps/base/kyverno-policies"), "--resource", str(wl)],
                               capture_output=True, text=True)
            last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:]
            if r.returncode != 0 or "fail: 0" not in last or "error: 0" not in last:
                errs.append(f"Kyverno policies report on the rendered workloads: {last}")
    for e in errs:
        print("FAIL", e)
    print("ok" if not errs else f"{len(errs)} failure(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
