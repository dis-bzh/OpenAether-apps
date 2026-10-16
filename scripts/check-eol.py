#!/usr/bin/env python3
"""check-eol.py - every pinned component has a row in docs/eol-calendar.yaml, and the row is current.

A pin that nobody tracks against upstream's support window is how Istio 1.24 and cert-manager 1.15
ended up past end of life (ANSSI HYG-R34/-R35). This reads the pinned version out of each file a row
names and compares it with the row, so a Renovate bump that leaves the calendar stale fails here.

Two kinds of finding, because only one of them is about the diff:
  errors    (exit 1)  a row whose pin cannot be read or differs from the calendar, a HelmRelease or a
                      vendored dump (renovate.json5 ignorePaths) with no row, an unreadable date.
  warnings  (exit 0)  a component past its EOL without a live waiver, an expired waiver, an overdue
                      review, an EOL within 90 days, a stale snapshot. These change with the calendar, not
                      with a PR, so they must not turn an unrelated PR red; `--strict` makes them errors.

Usage: python3 scripts/check-eol.py [--strict] [--today YYYY-MM-DD] [--root DIR]
       python3 scripts/check-eol.py --markdown      print the table docs/eol-calendar.md embeds
       python3 scripts/check-eol.py --check-doc     fail if docs/eol-calendar.md embeds a different table
No network: the dates are a checked-in snapshot, refreshed by hand (the doc says how).
Dependency: PyYAML.
"""
import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SPECIAL_EOL = {"not-yet", "unsupported", "unknown"}
BEGIN, END = "<!-- eol-table:begin -->", "<!-- eol-table:end -->"
SOON = 90
STALE_SNAPSHOT_DAYS = 120


def parse_date(s):
    return dt.date.fromisoformat(str(s))


def read_pin(root, row):
    path = root / row["file"]
    if not path.is_file():
        return None, f"{row['file']} does not exist"
    m = re.search(row["pattern"], path.read_text(), re.M)
    if not m:
        return None, f"{row['file']}: pattern does not match, the pin moved or changed shape"
    return m.group(1), None


def helmreleases(root):
    """(file, chart) of every HelmRelease under apps/, parsed, not grepped."""
    found = set()
    for path in sorted((root / "apps").rglob("*.yaml")):
        text = path.read_text()
        if "kind: HelmRelease" not in text:
            continue
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            continue
        for d in docs:
            if isinstance(d, dict) and d.get("kind") == "HelmRelease":
                chart = ((d.get("spec") or {}).get("chart") or {}).get("spec", {}).get("chart")
                if chart:
                    found.add((str(path.relative_to(root)), chart))
    return found


def vendored_paths(root):
    """The files renovate.json5 lists in ignorePaths: full upstream dumps nobody can bump by editing a field."""
    text = (root / "renovate.json5").read_text()
    m = re.search(r"ignorePaths:\s*\[(.*?)\]", text, re.S)
    return set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()


def check(root, today, calendar=None):
    cal = yaml.safe_load((calendar or root / "docs/eol-calendar.yaml").read_text())
    rows = cal["components"]
    errors, warnings = [], []
    for r in rows:
        label = r["id"]
        pin, err = read_pin(root, r)
        if err:
            errors.append(f"{label}: {err}")
            continue
        if str(r.get("pin")) != pin:
            errors.append(f"{label}: the file pins {pin} but the calendar row says {r.get('pin')}: "
                          f"re-read the upstream support dates for the new version, then update the row")
            continue
        eol = str(r["eol"])
        if eol not in SPECIAL_EOL:
            try:
                parse_date(eol)
            except ValueError:
                errors.append(f"{label}: eol {eol!r} is neither a date nor one of {sorted(SPECIAL_EOL)}")
                continue
        if r.get("cycle") and not any(ch.isdigit() for ch in r["cycle"]):
            errors.append(f"{label}: cycle {r['cycle']!r} names no version")

        waiver = r.get("waiver")
        waiver_ok = False
        if waiver:
            try:
                waiver_ok = parse_date(waiver["until"]) >= today
            except (KeyError, ValueError):
                errors.append(f"{label}: waiver needs `until` (a date) and `reason`")
            if not waiver.get("reason"):
                errors.append(f"{label}: waiver without a reason")
            if not waiver_ok and "until" in waiver:
                warnings.append(f"{label}: waiver expired {waiver['until']}: upgrade or renew it with a reason")
        past = eol == "unsupported" or (eol not in SPECIAL_EOL and parse_date(eol) <= today)
        if past and not waiver_ok:
            warnings.append(f"{label} {pin} ({r['cycle']}): past upstream support ({eol}) with no live waiver")
        elif eol not in SPECIAL_EOL and not past and (parse_date(eol) - today).days <= SOON:
            warnings.append(f"{label} {pin} ({r['cycle']}): upstream support ends {eol}, plan the upgrade")
        try:
            if parse_date(r["next_review"]) < today:
                warnings.append(f"{label}: review overdue since {r['next_review']}")
        except (KeyError, ValueError):
            errors.append(f"{label}: next_review is missing or not a date")

    for file, chart in sorted(helmreleases(root)):
        if not any(r["file"] == file and re.search(rf"\b{re.escape(chart)}\b", r["pattern"]) for r in rows):
            errors.append(f"HelmRelease chart {chart} ({file}) has no row in docs/eol-calendar.yaml")
    rowfiles = {r["file"] for r in rows}
    for p in sorted(vendored_paths(root)):
        if p not in rowfiles:
            errors.append(f"vendored dump {p} (renovate.json5 ignorePaths) has no row in docs/eol-calendar.yaml")
    ids = [r["id"] for r in rows]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        errors.append(f"duplicate row id {dup}")
    try:
        if (today - parse_date(cal["snapshot"])).days > STALE_SNAPSHOT_DAYS:
            warnings.append(f"the snapshot of {cal['snapshot']} is over {STALE_SNAPSHOT_DAYS} days old: refresh the dates")
    except (KeyError, ValueError):
        errors.append("snapshot is missing or not a date")
    return rows, errors, warnings


def markdown(rows):
    out = ["| Component | Pinned | Upstream line | End of support | Waiver until | Review |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        w = (r.get("waiver") or {}).get("until", "")
        out.append(f"| {r['component']} (`{r['brick']}`) | {r['pin']} | {r['cycle']} | {r['eol']} | {w} | "
                   f"{r['next_review']} |")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--today", default=None)
    ap.add_argument("--root", default=str(REPO))
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--check-doc", action="store_true")
    a = ap.parse_args()
    root = Path(a.root)
    today = parse_date(a.today) if a.today else dt.date.today()
    rows, errors, warnings = check(root, today)

    if a.markdown:
        print(markdown(rows))
        return 1 if errors else 0
    if a.check_doc:
        doc = (root / "docs/eol-calendar.md").read_text()
        m = re.search(re.escape(BEGIN) + r"\n(.*?)\n" + re.escape(END), doc, re.S)
        if not m or m.group(1).strip() != markdown(rows).strip():
            errors.append("docs/eol-calendar.md does not embed the current table: "
                          "python3 scripts/check-eol.py --markdown, paste between the markers")
    gha = os.environ.get("GITHUB_ACTIONS") == "true"
    for w in warnings:
        print(f"::warning::{w}" if gha else f"warning: {w}")
    for e in errors:
        print(f"::error::{e}" if gha else f"error: {e}", file=sys.stderr)
    if errors or (a.strict and warnings):
        sys.exit(1)
    print(f"OK - {len(rows)} components in the calendar, {len(warnings)} warning(s).")


if __name__ == "__main__":
    main()
