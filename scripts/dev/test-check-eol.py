#!/usr/bin/env python3
"""Fixture tests of scripts/check-eol.py: errors for what a diff can break, warnings for what only time breaks.
No network, no cluster. Usage: python3 scripts/dev/test-check-eol.py
"""
import datetime as dt
import importlib.util
import tempfile
import textwrap
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("chk", HERE.parent / "check-eol.py")
chk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chk)

TODAY = dt.date(2026, 10, 14)
HR = textwrap.dedent("""\
    apiVersion: helm.toolkit.fluxcd.io/v2
    kind: HelmRelease
    metadata: {name: foo}
    spec:
      chart:
        spec:
          chart: foo
          version: "1.2.3"
    """)
ROW = {"id": "foo", "brick": "b", "component": "Foo", "pin": "1.2.3", "file": "apps/base/foo.yaml",
       "pattern": r'chart:\s*foo\s*\n\s*version:\s*"?([^"\s]+)', "cycle": "Foo 1.2", "eol": "2028-01-01",
       "owner": "@o", "next_review": "2026-12-01"}
PASS = FAIL = 0


def run(rows, hr=HR, vendored=(), snapshot="2026-10-01"):
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "apps/base").mkdir(parents=True)
        (root / "docs").mkdir()
        (root / "apps/base/foo.yaml").write_text(hr)
        for v in vendored:
            (root / v).parent.mkdir(parents=True, exist_ok=True)
            (root / v).write_text("x: 1\n")
        (root / "renovate.json5").write_text("{ ignorePaths: [" + ",".join(f'"{v}"' for v in vendored) + "] }")
        (root / "docs/eol-calendar.yaml").write_text(yaml.safe_dump({"snapshot": snapshot, "components": rows}))
        _, errors, warnings = chk.check(root, TODAY)
        return errors, warnings


def expect(name, got, want_substr):
    global PASS, FAIL
    ok = any(want_substr in x for x in got) if want_substr else not got
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  (got {got})"))
    PASS += ok
    FAIL += not ok


def row(**kw):
    r = dict(ROW)
    r.update(kw)
    return [r]


e, w = run(row())
expect("a consistent calendar has no error", e, None)
expect("a supported component has no warning", w, None)
e, w = run(row(pin="1.2.2"))
expect("a bumped pin with a stale row is an error", e, "the file pins 1.2.3 but the calendar row says 1.2.2")
e, w = run(row(pattern=r"nothing-matches"))
expect("a pattern that no longer matches is an error", e, "pattern does not match")
e, w = run([])
expect("a HelmRelease without a row is an error", e, "has no row")
e, w = run(row(), vendored=("apps/base/dump.yaml",))
expect("a vendored dump without a row is an error", e, "vendored dump apps/base/dump.yaml")
e, w = run(row(eol="soon"))
expect("an unreadable EOL is an error", e, "neither a date")
e, w = run(row(waiver={"until": "2027-01-01"}, eol="2025-01-01"))
expect("a waiver without a reason is an error", e, "waiver without a reason")
e, w = run(row(eol="2025-01-01"))
expect("past EOL without a waiver is a warning, not an error", w, "past upstream support")
expect("...and not an error", e, None)
e, w = run(row(eol="2025-01-01", waiver={"until": "2026-12-31", "reason": "r"}))
expect("past EOL under a live waiver is quiet", w, None)
e, w = run(row(eol="2025-01-01", waiver={"until": "2026-09-30", "reason": "r"}))
expect("an expired waiver warns", w, "waiver expired")
e, w = run(row(eol="unsupported"))
expect("an unsupported line without a waiver warns", w, "past upstream support")
e, w = run(row(eol="2026-11-27"))
expect("an EOL within 90 days warns", w, "plan the upgrade")
e, w = run(row(next_review="2026-10-01"))
expect("an overdue review warns", w, "review overdue")
e, w = run(row(), snapshot="2026-01-01")
expect("a stale snapshot warns", w, "days old")
e, w = run(row(eol="unknown"))
expect("an unknown EOL is not assumed either way", w, None)
expect("...nor an error", e, None)
e, w = run(row() + row())
expect("a duplicate id is an error", e, "duplicate row id")

# The shipped calendar must be consistent with the tree it ships in.
_, errors, _ = chk.check(chk.REPO, TODAY)
expect("the shipped calendar matches the tree", errors, None)

print(f"{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
