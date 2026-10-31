#!/usr/bin/env python3
"""Zitadel and Grafana recycle their database connections in minutes, so a failover is not followed by a pool of dead ones.

A CNPG failover (or a lost node) leaves connections half-open; a client that keeps them for 30 minutes (Zitadel default) or 4 hours
(Grafana default) answers errors long after the new primary is up. Renders both charts (`helm template`, network for the charts)
and reads the setting where the application reads it: the Zitadel config ConfigMap, the Grafana grafana.ini. Static: it proves the
settings reach the rendered config, not how each application behaves during a failover (docs/database-failover-clients.md).
Run: python3 -I scripts/dev/test-db-client-settings.py
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "apps/base"
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond


def seconds(v):
    n = {"s": 1, "m": 60, "h": 3600}
    m = re.fullmatch(r"(\d+)([smh])", str(v))
    return int(m.group(1)) * n[m.group(2)] if m else None


def repo_url(name):
    for f in BASE.rglob("*.yaml"):
        try:
            for d in yaml.safe_load_all(f.read_text()):
                if d and d.get("kind") == "HelmRepository" and d["metadata"]["name"] == name:
                    return d["spec"]["url"]
        except yaml.YAMLError:
            continue
    sys.exit(f"no HelmRepository {name}")


def render(rel):
    hr = next(d for d in yaml.safe_load_all((BASE / rel).read_text()) if d and d["kind"] == "HelmRelease")
    spec = hr["spec"]["chart"]["spec"]
    with tempfile.TemporaryDirectory() as tmp:
        v = Path(tmp) / "values.yaml"
        v.write_text(yaml.safe_dump(hr["spec"].get("values", {})))
        r = subprocess.run(["helm", "template", hr["metadata"]["name"], spec["chart"], "--repo", repo_url(spec["sourceRef"]["name"]),
                            "--version", spec["version"], "-n", hr["metadata"]["namespace"], "-f", str(v)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"helm template {rel}: {r.stderr[-300:]}")
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def configmap_text(docs, key_part):
    return "\n".join(str(v) for d in docs if d["kind"] == "ConfigMap" for k, v in (d.get("data") or {}).items() if key_part in k)


# Zitadel: the config YAML in the chart's ConfigMap
cfg = yaml.safe_load(configmap_text(render("identity/zitadel/helmrelease.yaml"), "zitadel-config-yaml"))
pg = cfg["Database"]["Postgres"]
check(seconds(pg.get("MaxConnLifetime")) is not None and seconds(pg["MaxConnLifetime"]) <= 600,
      f"Zitadel recycles a connection within 10 minutes (MaxConnLifetime {pg.get('MaxConnLifetime')}, default 30m)")
check(seconds(pg.get("MaxConnIdleTime")) is not None and seconds(pg["MaxConnIdleTime"]) <= 300,
      f"Zitadel drops an idle connection within 5 minutes (MaxConnIdleTime {pg.get('MaxConnIdleTime')})")
check(0 < pg.get("MaxIdleConns", 0) <= pg.get("MaxOpenConns", 0), "Zitadel's pool is bounded (MaxIdleConns <= MaxOpenConns)")

# Grafana: grafana.ini in the chart's ConfigMap
ini = configmap_text(render("observability/grafana.yaml"), "grafana.ini")
sec = re.search(r"^\[database\]\n(.*?)(?=^\[|\Z)", ini, re.M | re.S)
db = dict(re.findall(r"^(\w+)\s*=\s*(.*)$", sec.group(1), re.M)) if sec else {}
check(0 < int(db.get("conn_max_lifetime", 0)) <= 600, f"Grafana recycles a connection within 10 minutes (conn_max_lifetime {db.get('conn_max_lifetime')}, default 14400)")
check(0 < int(db.get("max_open_conn", 0)) and int(db.get("max_idle_conn", 0)) <= int(db.get("max_open_conn", 0)),
      "Grafana's pool is bounded (max_idle_conn <= max_open_conn)")
print(f"{fails} failed")
sys.exit(1 if fails else 0)
