#!/usr/bin/env python3
"""audit-sink-check (apps/base/observability-audit-sink/scripts/check.py) against a fake `aws`: the series the alerts read.

What it proves (infra #318): per node, the age comes from the LastModified of the newest SNAPSHOT object of the newest two days (not from a
data pack or another prefix); a node that stops delivering keeps its last timestamp, so its age keeps growing; a failing list call is
oa_audit_sink_ok 0 and does not erase what was known; an empty sink has oa_audit_sink_nodes 0 with ok 1; a second sink is reported under its own
label; the credentials come from files and reach `aws` only as environment; no password is needed. Static + a fake binary: no object store.
"""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "apps/base/observability-audit-sink/scripts/check.py"
failures = 0


def check(cond, msg):
    global failures
    print(("ok   " if cond else "FAIL ") + msg)
    failures += 0 if cond else 1


def main():
    tmp = Path(tempfile.mkdtemp())
    now = time.time()

    def iso(age):
        return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - age))

    # The sink: audit/cp-1/{D1,D2}/snapshots/*, audit/cp-2/D2/snapshots/*; cp-1's newest snapshot is 120 s old, cp-2's 3000 s old.
    world = {
        "audit/": {"CommonPrefixes": [{"Prefix": "audit/cp-1/"}, {"Prefix": "audit/cp-2/"}]},
        "audit/cp-1/": {"CommonPrefixes": [{"Prefix": "audit/cp-1/2026-10-20/"}, {"Prefix": "audit/cp-1/2026-10-21/"}, {"Prefix": "audit/cp-1/2026-10-22/"}]},
        "audit/cp-2/": {"CommonPrefixes": [{"Prefix": "audit/cp-2/2026-10-22/"}]},
        "audit/cp-1/2026-10-22/snapshots/": {"Contents": [{"Key": "a", "LastModified": iso(900)}, {"Key": "b", "LastModified": iso(120)}]},
        "audit/cp-1/2026-10-21/snapshots/": {"Contents": [{"Key": "c", "LastModified": iso(80000)}]},
        "audit/cp-1/2026-10-20/snapshots/": {"Contents": [{"Key": "old", "LastModified": iso(1)}]},   # three days old repository: never read
        "audit/cp-2/2026-10-22/snapshots/": {"Contents": [{"Key": "d", "LastModified": iso(3000)}]},
    }
    (tmp / "world.json").write_text(json.dumps(world))
    fake = tmp / "aws"
    fake.write_text(f"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
prefix = a[a.index("--prefix") + 1]
open("{tmp}/calls.log", "a").write(json.dumps({{"prefix": prefix, "ak": os.environ.get("AWS_ACCESS_KEY_ID"), "region": os.environ.get("AWS_DEFAULT_REGION"),
                                              "endpoint": a[a.index("--endpoint-url") + 1], "argv_has_secret": any("SECRET" in x for x in a)}}) + "\\n")
if os.path.exists("{tmp}/down"):
    sys.stderr.write("An error occurred (AccessDenied)"); sys.exit(254)
w = json.load(open("{tmp}/world.json"))
print(json.dumps(w.get(prefix, {{}})))
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    for sink, sub in (("primary", "p"), ("secondary", "s")):
        d = tmp / "creds" / sink
        d.mkdir(parents=True)
        for k, v in {"ENDPOINT": "https://s3.example.invalid", "BUCKET": f"bucket-{sub}", "PREFIX": "audit", "AWS_DEFAULT_REGION": "gra",
                     "AWS_ACCESS_KEY_ID": f"AK-{sub}", "AWS_SECRET_ACCESS_KEY": "SECRET-VALUE"}.items():
            (d / k).write_text(v + "\n")
    os.environ.update(CREDS_DIR=str(tmp / "creds"), AWS_BIN=str(fake))
    spec = importlib.util.spec_from_file_location("check_mod", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    check(mod.sinks() == ["primary", "secondary"], "both sinks are found from their credential files")
    res = mod.check("primary")
    check(res["ok"] == 1 and set(res["nodes"]) == {"cp-1", "cp-2"}, f"ok 1 and two nodes: {res}")
    check(abs((now - res["nodes"]["cp-1"]) - 120) < 5, "cp-1: the newest snapshot object of the newest day, 120 s old")
    check(abs((now - res["nodes"]["cp-2"]) - 3000) < 5, "cp-2: 3000 s old")
    calls = [json.loads(line) for line in (tmp / "calls.log").read_text().splitlines()]
    check(not any(c["prefix"] == "audit/cp-1/2026-10-20/snapshots/" for c in calls), "the third-newest day is not read (bounded listing)")
    check(all(c["ak"] == "AK-p" and c["region"] == "gra" and c["endpoint"] == "https://s3.example.invalid" for c in calls), "keys and region reach aws as environment, from the files")
    check(not any(c["argv_has_secret"] for c in calls), "the secret key is never on a command line")
    check(all("RESTIC_PASSWORD" not in os.environ for _ in [0]), "no repository password is needed or present")

    mod.STATE["primary"] = {"ok": res["ok"], "checked": res["checked"], "known": dict(res["nodes"])}
    text = mod.metrics()
    check('oa_audit_sink_ok{sink="primary"} 1' in text, "oa_audit_sink_ok 1")
    check('oa_audit_sink_nodes{sink="primary"} 2' in text, "two nodes")
    line = [x for x in text.splitlines() if x.startswith('oa_audit_sink_last_object_age_seconds{sink="primary",node="cp-2"}')][0]
    check(2990 <= float(line.split()[-1]) <= 3020, f"the age of cp-2 is read at scrape time: {line}")

    # the store goes down: ok 0, and what was known stays (its age keeps growing)
    (tmp / "down").write_text("")
    down = mod.check("primary")
    check(down["ok"] == 0 and down["nodes"] is None, "a failing list call: ok 0")
    check(mod.read("primary", "AWS_ACCESS_KEY_ID") == "AK-p", "files still read")
    (tmp / "down").unlink()

    # one day listing fails midway: ok 0 but the other nodes are still reported
    orig = mod.aws_list

    def flaky(sink, prefix, delimiter=True):
        return None if prefix == "audit/cp-2/" else orig(sink, prefix, delimiter)
    mod.aws_list = flaky
    part = mod.check("primary")
    mod.aws_list = orig
    check(part["ok"] == 0 and "cp-1" in part["nodes"] and "cp-2" not in part["nodes"], "a partial failure is ok 0 and keeps the nodes it could read")

    # an empty sink
    (tmp / "world.json").write_text(json.dumps({"audit/": {}}))
    empty = mod.check("secondary")
    mod.STATE["secondary"] = {"ok": empty["ok"], "checked": empty["checked"], "known": {}}
    text = mod.metrics()
    check('oa_audit_sink_ok{sink="secondary"} 1' in text and 'oa_audit_sink_nodes{sink="secondary"} 0' in text, "an empty sink: ok 1, nodes 0 (AuditSinkEmpty)")
    check('oa_audit_sink_ok{sink="primary"}' in text, "the second sink is a label, not a replacement")

    # the same names the alert rules read
    rules = (REPO / "apps/base/observability-audit-sink/vmrule.yaml").read_text()
    for name in ("oa_audit_sink_ok", "oa_audit_sink_last_check_timestamp_seconds", "oa_audit_sink_last_object_age_seconds", "oa_audit_sink_nodes"):
        check(name in rules and name in text, f"{name} is exported and read by a rule")
    print(f"{failures} failed" if failures else "0 failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
