#!/usr/bin/env python3
"""Fixture tests of scripts/check-vex.py: an expired statement, a missing justification or expiry fail; the shipped file passes.
No network. Usage: python3 scripts/dev/test-check-vex.py
"""
import copy
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("vex", HERE.parent / "check-vex.py")
vex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vex)

GOOD = {"vulnerability": {"name": "CVE-2026-1"}, "timestamp": "2026-10-22T00:00:00Z",
        "products": [{"@id": "pkg:oci/x?repository_url=docker.io/a/x"}], "status": "not_affected",
        "justification": "vulnerable_code_not_in_execute_path", "impact_statement": "never run",
        "action_statement": "bump x", "status_notes": "Expires: 2026-11-20"}
TODAY = dt.date(2026, 10, 22)
PASS = FAIL = 0


def check(name, ok):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if ok else (PASS, FAIL + 1)
    print(("ok   " if ok else "FAIL ") + name)


def run(mut=None, today=TODAY):
    s = copy.deepcopy(GOOD)
    if mut:
        mut(s)
    return vex.check({"statements": [s]}, today)


check("a complete statement passes", run() == [])
check("an empty list passes", vex.check({"statements": []}, TODAY) == [])
check("the day of the expiry still passes", run(today=dt.date(2026, 11, 20)) == [])
check("the day after the expiry fails", any("EXPIRED" in e for e in run(today=dt.date(2026, 11, 21))))
check("no justification fails", any("justification" in e for e in run(lambda s: s.pop("justification"))))
check("an invented justification fails", any("justification" in e for e in run(lambda s: s.update(justification="trust me"))))
check("no impact_statement fails", any("impact_statement" in e for e in run(lambda s: s.pop("impact_statement"))))
check("no action_statement fails", any("action_statement" in e for e in run(lambda s: s.pop("action_statement"))))
check("no expiry fails", any("Expires" in e for e in run(lambda s: s.pop("status_notes"))))
check("two expiries fail", any("exactly one" in e for e in run(lambda s: s.update(status_notes="Expires: 2026-11-01 Expires: 2026-11-02"))))
check("an expiry over 90 days out fails", any("within 90 days" in e for e in run(lambda s: s.update(status_notes="Expires: 2027-03-01"))))
check("no product fails", any("product" in e for e in run(lambda s: s.update(products=[]))))
check("an unknown status fails", any("status" in e for e in run(lambda s: s.update(status="maybe"))))
real = json.loads((HERE.parent.parent / ".vex/openaether-apps.openvex.json").read_text())
check("the shipped file passes on the day it was written", vex.check(real, dt.date(2026, 10, 22)) == [])
check("every shipped statement is not_affected with a justification", all(s.get("justification") for s in real["statements"]))
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
