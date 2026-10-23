#!/usr/bin/env python3
"""A missing or empty cluster-identity key must not turn the database layer NotReady (lab 25, D9), offline.

Lab 25 moved a cluster built by 0.3.2 to the 0.4.0 content. Its `cluster-identity` ConfigMap had no BACKUP_REPLICA_S3_* keys, the
ObjectStores wrote `endpointURL: "${BACKUP_REPLICA_S3_ENDPOINT:=}"`, and the `cnpg` Kustomization (with 8 dependants) stayed
NotReady: `spec.configuration.endpointURL must be of type string: "null"`. The mechanism, reproduced with `flux build` 2.9.3:
kustomize prints the field WITHOUT its quotes (`endpointURL: ${VAR:=}`), Flux substitutes after that, and an empty result is a
YAML null. An empty default is therefore the defect; a field that is only a substitution needs a NON-EMPTY default.

This test shows, without a cluster:
  * the rendered ObjectStores (apps/base/cnpg and the example database profiles, which use the replica component) keep every
    replica field as a string when BACKUP_REPLICA_S3_BUCKET / _ENDPOINT are ABSENT or EMPTY, and the value is the documented
    placeholder, not a store;
  * with the keys PRESENT the values are exactly theirs (the placeholder never wins);
  * no manifest of apps/ has a field that is only `${VAR:=}` / `${VAR:-}` (an empty default), except the ones this release
    lists as known and tracked (BACKUP_TARGET_URL of the Longhorn target; the primary store, whose keys exist on every cluster
    and whose absence is meant to be loud);
  * the audit sink host defaults are non-empty hosts;
  * when the flux CLI is on PATH, the same render is made by `flux build kustomization` (the real substitution engine).
Run: python3 -I scripts/dev/test-cnpg-identity-defaults.py
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PASS = FAIL = 0
PLACEHOLDER_ENDPOINT = "https://replica-not-set.invalid"
PLACEHOLDER_BUCKET = "replica-not-set"
RENDERS = ("apps/base/cnpg", "apps/components/cnpg-profile-examples/ha")
# Fields that are ONLY a substitution with an empty default today. Each is a separate decision, not a silent pass.
KNOWN_EMPTY_DEFAULTS = {
    ("apps/base/storage/backup-target/settings.yaml", "BACKUP_TARGET_URL"),   # Longhorn target; tracked, see CHANGELOG
    ("apps/base/cnpg/objectstores.yaml", "BACKUP_S3_ENDPOINT"),               # primary store: every cluster has it, loud on purpose
    ("apps/components/cnpg-profile-backup/objectstores.yaml", "BACKUP_S3_ENDPOINT"),
    ("apps/base/cnpg/restore-scratch.yaml", "BACKUP_S3_ENDPOINT"),             # the scratch copy of the primary store
    ("apps/base/backup/databases-identity/configmap.yaml", "CNPG_ARCHIVE_SUFFIX"),  # a ConfigMap value, empty by design (no generation suffix)
}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


SUBST = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:=|:-)((?:[^}$]|\$\{[^}]*\})*))?\}")


def flux_subst(text, env):
    """Flux postBuild on the manifest text: ${VAR}, ${VAR:=default}, ${VAR:-default}; unset or empty takes the default."""
    def one(m):
        var, op, default = m.group(1), m.group(2), m.group(3) or ""
        v = env.get(var)
        return v if v else (flux_subst(default, env) if op else "")
    return SUBST.sub(one, text)


def render_text(path):
    return subprocess.run(["kubectl", "kustomize", str(REPO / path)], check=True, capture_output=True, text=True).stdout


def render(path, env):
    """The objects after substitution, with the YAML null an empty whole-field substitution becomes."""
    return [d for d in yaml.safe_load_all(flux_subst(render_text(path), env)) if d]


def replica_fields(docs):
    out = {}
    for d in docs:
        if d.get("kind") == "ObjectStore" and d["metadata"]["name"].endswith("-replica"):
            c = d["spec"]["configuration"]
            out[d["metadata"]["name"]] = (c.get("destinationPath"), c.get("endpointURL"))
    return out


print("=== the replica ObjectStores with the keys ABSENT or EMPTY ===")
for path in RENDERS:
    for label, env in (("absent", {}), ("empty", {"BACKUP_REPLICA_S3_BUCKET": "", "BACKUP_REPLICA_S3_ENDPOINT": ""})):
        fields = replica_fields(render(path, env))
        check(f"{path}: {label}: there are replica ObjectStores", len(fields) >= 1, str(fields))
        check(f"{path}: {label}: destinationPath and endpointURL are strings (no null)",
              all(isinstance(a, str) and isinstance(b, str) and b for a, b in fields.values()), str(fields))
        check(f"{path}: {label}: the placeholders, not a store",
              all(a.startswith(f"s3://{PLACEHOLDER_BUCKET}/cnpg/") and b == PLACEHOLDER_ENDPOINT for a, b in fields.values()), str(fields))

print("=== the same keys PRESENT: their values, exactly ===")
env = {"BACKUP_REPLICA_S3_BUCKET": "data-bkt-backup", "BACKUP_REPLICA_S3_ENDPOINT": "https://s3r.example.invalid"}
for path in RENDERS:
    fields = replica_fields(render(path, env))
    check(f"{path}: present: bucket and endpoint are the cluster's",
          fields and all(a.startswith("s3://data-bkt-backup/cnpg/") and b == "https://s3r.example.invalid" for a, b in fields.values()), str(fields))
    check(f"{path}: present: the placeholder is nowhere", "not-set" not in str(fields), str(fields))

print("=== the primary store and its path keep their meaning ===")
env2 = {"BACKUP_S3_BUCKET": "data-bkt", "BACKUP_S3_ENDPOINT": "https://s3.example.invalid"}
prim = [d for d in render("apps/base/cnpg", env2) if d.get("kind") == "ObjectStore" and d["metadata"]["name"].endswith("-primary")]
check("the primary ObjectStores read BACKUP_S3_*", prim and all(
    d["spec"]["configuration"]["endpointURL"] == "https://s3.example.invalid" and d["spec"]["configuration"]["destinationPath"].startswith("s3://data-bkt/cnpg/") for d in prim))

print("=== no manifest has a field that is only an empty default ===")
FIELD = re.compile(r'^\s*[-\w."/ ]+:\s*"?\$\{([A-Za-z_][A-Za-z0-9_]*):[=-]\}"?\s*$')
found, unknown = set(), []
for f in sorted((REPO / "apps").rglob("*.yaml")):
    rel = str(f.relative_to(REPO))
    for line in f.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = FIELD.match(line)
        if m:
            found.add((rel, m.group(1)))
            if (rel, m.group(1)) not in KNOWN_EMPTY_DEFAULTS:
                unknown.append(f"{rel}: {line.strip()}")
check("every whole-field `${VAR:=}` is a known, tracked one (an unlisted one renders YAML null when the key is missing)", not unknown, "\n".join(unknown))
check("the known list has no stale entry", KNOWN_EMPTY_DEFAULTS <= found, str(sorted(KNOWN_EMPTY_DEFAULTS - found)))

print("=== the audit sink hosts default to a non-empty host ===")
text = (REPO / "apps/base/observability-audit-sink-policy/networkpolicy.yaml").read_text()
defaults = re.findall(r"\$\{AUDIT_SINK(?:_SECONDARY)?_HOST:=([^}]*)\}", text)
check("AUDIT_SINK_HOST and _SECONDARY_HOST have a non-empty .invalid default", len(defaults) >= 2 and all(d.endswith(".invalid") and len(d) > 8 for d in defaults), str(defaults))

print("=== the real substitution engine, when the flux CLI is here ===")
flux = shutil.which("flux")
if not flux:
    print("  skip no flux CLI on PATH (the emulation above is the CI check)")
else:
    with tempfile.TemporaryDirectory() as tmp:
        ks = Path(tmp) / "ks.yaml"
        ks.write_text("apiVersion: kustomize.toolkit.fluxcd.io/v1\nkind: Kustomization\nmetadata: {name: t, namespace: flux-system}\n"
                      "spec: {interval: 1m, path: ./x, prune: false, sourceRef: {kind: GitRepository, name: x}, postBuild: {substitute: {UNRELATED: x}}}\n")
        r = subprocess.run([flux, "build", "kustomization", "t", "--dry-run", "--kustomization-file", str(ks), "--path",
                            str(REPO / "apps/base/cnpg")], capture_output=True, text=True)
        docs = [d for d in yaml.safe_load_all(r.stdout) if d] if r.returncode == 0 else []
        fields = replica_fields(docs)
        check("flux build: the keys absent, the replica ObjectStores are strings with the placeholders",
              r.returncode == 0 and fields and all(isinstance(b, str) and b == PLACEHOLDER_ENDPOINT for a, b in fields.values()),
              (r.stderr or str(fields))[:300])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
