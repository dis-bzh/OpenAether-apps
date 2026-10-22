#!/usr/bin/env python3
"""docs/database-profiles.md keeps its promises: the numbers table has the columns db-bench.sh prints and no invented figure, every file
it names exists, its security checklist agrees with the rendered manifests, its capacity table agrees with check-ha.py, and it makes
none of the claims the repository forbids. Static. Run: python3 -I scripts/dev/test-database-profiles-doc.py
"""
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DOC = (REPO / "docs/database-profiles.md").read_text()
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def table_after(title):
    """The rows (cells) of the first Markdown table after a heading or a line containing `title`."""
    i = DOC.index(title)
    rows = []
    for line in DOC[i:].splitlines()[1:]:
        if line.startswith("|"):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
        elif rows:
            break
    return rows


parse = load(REPO / "apps/components/cnpg-profile-bench/db-bench-parse.py", "dbp")
num = table_after("Filled by the lab")
check([c.replace(" ms", "_ms") for c in num[0]] == [c.replace("tps", "TPS") if c == "tps" else c for c in parse.COLS],
      "the numbers table has exactly the columns db-bench.sh prints")
body = num[2:]
check(len(body) == 8 and all(c == "UNMEASURED" for r in body for c in r[6:]),
      "every measured cell is UNMEASURED: no figure is written before the lab measured it")
check({r[0] for r in body} == {"light", "standard", "ha"} and {(r[0], r[1]) for r in body if r[0] == "ha"} == {("ha", "direct"), ("ha", "pooler")},
      "the table covers the three profiles, and ha both direct and through the pooler")

# every path the page names exists
named = set(re.findall(r"`((?:apps|scripts|docs)/[A-Za-z0-9_./<>*-]+)`", DOC))
missing = sorted(p for p in named if "<" not in p and "*" not in p and not (REPO / p.rstrip("/")).exists())
check(missing == [], "every file the page names exists %s" % missing)

# the security checklist against the manifests
ha = load(REPO / "scripts/check-ha.py", "hacheck")
r = {}
for p in ("light", "standard", "ha"):
    out = subprocess.run(["kubectl", "kustomize", str(REPO / "apps/components/cnpg-profile-examples" / p)], capture_output=True, text=True).stdout
    r[p] = [d for d in yaml.safe_load_all(out) if d]
rows = [row for row in (l.strip().strip("|").split("|") for l in DOC.splitlines() if l.startswith("| ") and "how it is checked" not in l) if len(row) == 5]
sec = {row[0].strip(): [c.strip() for c in row[1:4]] for row in rows}
def row(prefix):
    return next(v for k, v in sec.items() if k.startswith(prefix))
kinds = lambda p, k: [d for d in r[p] if d["kind"] == k]
cluster = lambda p: kinds(p, "Cluster")[0]["spec"]
for p, i in (("light", 0), ("standard", 1), ("ha", 2)):
    check(row("Network policy per database")[i] == "yes" and kinds(p, "CiliumNetworkPolicy"), "%s: the checklist says a database policy and there is one" % p)
    check(row("TLS and SCRAM")[i] == "yes" and cluster(p)["postgresql"]["pg_hba"][0].startswith("hostnossl") and
          "scram-sha-256" in cluster(p)["postgresql"]["pg_hba"][-1], "%s: the checklist says TLS and SCRAM only and pg_hba says so" % p)
    check(row("No superuser")[i] == "yes" and cluster(p)["enableSuperuserAccess"] is False, "%s: the checklist says no superuser and the Cluster agrees" % p)
    check(row("Owner's password")[i] == "yes" and kinds(p, "ExternalSecret"), "%s: the checklist says an ExternalSecret and there is one" % p)
    has_backup = bool(kinds(p, "ObjectStore"))
    check((row("Backup keys per application")[i] == "yes") == has_backup, "%s: the checklist's backup rows agree with the ObjectStores (%s)" % (p, has_backup))
check(row("Audit logging")[0:3] == ["not enabled"] * 3 and not any("pgaudit" in yaml.safe_dump(c["spec"]) for p in r for c in kinds(p, "Cluster")),
      "pgaudit is not enabled in any profile, as the page says")

# the capacity table against check-ha.py
cap = {x[0]: x[2] for x in table_after("Requests of the shipped example values")[2:]}
check(ha.profile_cpu(r["light"]) == 100 and ha.profile_cpu(r["standard"]) == 600 and ha.profile_cpu(r["ha"]) == 1100
      and "100 m" in cap["light"] and "= 600 m" in cap["standard"] and "= 1100 m" in cap["ha"],
      "the capacity table's CPU figures are the ones check-ha.py computes from the examples")

# what the repository forbids
low = DOC.lower()
check("automatic and autonomous" not in low and not re.search(r"\banssi\b.{0,40}(compl|conform|certif)", low),
      "no forbidden claim (automatic and autonomous, ANSSI compliance)")
check(not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", DOC) and "sk-" not in DOC and "claude" not in low,
      "no address, key or model identifier in the page")
check(DOC.count("UNVERIFIED") >= 6 and "Not done" in DOC, "the page lists what is unverified and what is not done")
print("%d failed" % fails)
sys.exit(1 if fails else 0)
