#!/usr/bin/env python3
"""Every CNPG Cluster sets `spec.stopDelay` to 600 s or less (0.5.0 M4, lab 52).

The instance manager of an evicted primary hung for the whole default stopDelay (1800 s) and `cluster-roll` gave up draining after 900 s.
A pod that cannot end must be killed before the drain timeout, whatever the next CNPG release does. Needs PyYAML.
Run: python3 -I scripts/dev/test-cnpg-stop-delay.py"""
import sys, pathlib, yaml
R = pathlib.Path(__file__).resolve().parents[2]
MAX = 600
fail = 0
for p in sorted(R.glob("apps/base/**/*.yaml")):
    if p.name.startswith("cnpg-1."): continue
    try: docs = [d for d in yaml.safe_load_all(open(p)) if isinstance(d, dict)]
    except Exception: continue
    for d in docs:
        if d.get("kind") != "Cluster" or "postgresql.cnpg.io" not in str(d.get("apiVersion", "")): continue
        sd = d.get("spec", {}).get("stopDelay")
        name = f"{p.relative_to(R)} ({d['metadata']['name']})"
        if isinstance(sd, int) and 0 < sd <= MAX: print(f"  ✓ {name}: stopDelay {sd} s")
        else:
            fail += 1; print(f"  ✗ {name}: stopDelay {sd!r}, must be set and <= {MAX} s (the drain timeout is 900 s)")
print(f"{'FAIL' if fail else 'OK'}: {fail} failure(s)")
sys.exit(1 if fail else 0)
