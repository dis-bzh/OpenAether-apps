#!/usr/bin/env python3
"""check-vex.py - every VEX statement says why, says until when, and is not past that date.

Trivy applies .vex/openaether-apps.openvex.json to the image scans (sbom.yml) and knows nothing of time: a statement
written as a 30-day waiver would hide a finding forever. So each statement must carry
  - a known status; for not_affected a valid OpenVEX justification and an impact_statement;
  - an action_statement (what ends the waiver);
  - exactly one `Expires: YYYY-MM-DD` in status_notes, not before its timestamp and at most 90 days after it;
  - at least one product.
It fails on a statement whose date is before today (UTC). Renew by re-reading the finding, not by moving the date.

The Trivy ignore file .github/trivy-ignore.yaml (accepted findings the VEX matching misses) is held to the same rule:
every entry needs a `statement` and an `expired_at` within 90 days from today. Trivy enforces that date itself.

Usage: python3 scripts/check-vex.py [--file PATH] [--ignore-file PATH] [--today YYYY-MM-DD]
Exit: 0 ok, 1 a statement is wrong or expired, 2 the file cannot be read.
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUSES = {"not_affected", "affected", "fixed", "under_investigation"}
JUSTIFICATIONS = {"component_not_present", "vulnerable_code_not_present", "vulnerable_code_cannot_be_controlled_by_adversary",
                  "vulnerable_code_not_in_execute_path", "inline_mitigations_already_exist"}
EXPIRES = re.compile(r"\bExpires:\s*(\d{4}-\d{2}-\d{2})\b")
MAX_DAYS = 90


def check(doc, today):
    errors = []
    stmts = doc.get("statements")
    if not isinstance(stmts, list):
        return ["'statements' is missing or not a list"]
    for i, s in enumerate(stmts):
        name = (s.get("vulnerability") or {}).get("name") or "?"
        who = f"statement {i + 1} ({name})"
        if not (s.get("vulnerability") or {}).get("name"):
            errors.append(f"{who}: no vulnerability name")
        if not s.get("products"):
            errors.append(f"{who}: no product")
        status = s.get("status")
        if status not in STATUSES:
            errors.append(f"{who}: unknown status {status!r}")
        if status == "not_affected":
            if s.get("justification") not in JUSTIFICATIONS:
                errors.append(f"{who}: not_affected needs an OpenVEX justification, got {s.get('justification')!r}")
            if not str(s.get("impact_statement", "")).strip():
                errors.append(f"{who}: not_affected needs an impact_statement (why it is not reachable)")
        if not str(s.get("action_statement", "")).strip():
            errors.append(f"{who}: no action_statement (what ends this waiver)")
        dates = EXPIRES.findall(str(s.get("status_notes", "")))
        if len(dates) != 1:
            errors.append(f"{who}: status_notes must hold exactly one 'Expires: YYYY-MM-DD' (found {len(dates)})")
            continue
        try:
            exp = dt.date.fromisoformat(dates[0])
            start = dt.date.fromisoformat(str(s.get("timestamp", ""))[:10])
        except ValueError:
            errors.append(f"{who}: the expiry or the timestamp is not a date")
            continue
        if exp < today:
            errors.append(f"{who}: EXPIRED on {exp} - fix the image or re-read the finding and renew it")
        elif exp < start or (exp - start).days > MAX_DAYS:
            errors.append(f"{who}: expiry {exp} must be within {MAX_DAYS} days after the timestamp {start}")
    return errors


def check_ignore(doc, today):
    """Entries of a Trivy ignore file: a statement and an expiry that is not past and not too far."""
    errors = []
    for kind, entries in (doc or {}).items():
        for e in entries or []:
            who = f"ignore entry {kind}:{e.get('id', '?')}"
            if not str(e.get("statement", "")).strip():
                errors.append(f"{who}: no statement (why it is accepted)")
            exp = e.get("expired_at")
            if not isinstance(exp, dt.date):
                errors.append(f"{who}: no expired_at date")
            elif exp < today:
                errors.append(f"{who}: EXPIRED on {exp} - Trivy no longer ignores it; fix the image or re-read the finding")
            elif (exp - today).days > MAX_DAYS:
                errors.append(f"{who}: expired_at {exp} is more than {MAX_DAYS} days away")
    return errors


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", default=str(ROOT / ".vex/openaether-apps.openvex.json"))
    ap.add_argument("--ignore-file", default=str(ROOT / ".github/trivy-ignore.yaml"))
    ap.add_argument("--today", default=None)
    a = ap.parse_args()
    try:
        doc = json.loads(Path(a.file).read_text())
        today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(dt.timezone.utc).date()
    except (OSError, ValueError) as e:
        print(f"cannot read: {e}", file=sys.stderr)
        return 2
    errors = check(doc, today)
    ig = Path(a.ignore_file)
    if ig.exists():
        try:
            import yaml
            errors += check_ignore(yaml.safe_load(ig.read_text()), today)
        except (ImportError, ValueError, yaml.YAMLError) as e:  # PyYAML is in scripts/ci-requirements.txt
            print(f"cannot read {ig}: {e}", file=sys.stderr)
            return 2
    for e in errors:
        print(f"ERROR {e}")
    n = len(doc.get("statements") or [])
    if errors:
        return 1
    soonest = min((EXPIRES.search(str(s.get("status_notes", ""))).group(1) for s in doc["statements"]), default="-")
    print(f"OK - {n} VEX statement(s), soonest expiry {soonest}" + (f"; {ig.name} checked" if ig.exists() else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
