#!/usr/bin/env python3
"""A failed backup Job keeps the pod and the log that say why (lab 27 part B, D13).

`restartPolicy: OnFailure` makes the Job controller delete the pod when the backoff limit is reached: the nightly cnpg-dump failed on a
table its role could not read and left no pod and no log to read. Every CronJob of the backup brick that runs a Job is checked: its pod
template says `restartPolicy: Never`, the Job retries a bounded number of times, and the history keeps failed Jobs.
Static: it reads the manifests. Run: python3 -I scripts/dev/test-backup-job-logs.py
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
bad = 0


def check(desc, cond, detail=""):
    global bad
    print(("  ok   " if cond else "  FAIL ") + desc + ("" if cond else f" ({detail})"))
    bad += 0 if cond else 1


def problems(cj):
    spec = cj["spec"]
    job = spec["jobTemplate"]["spec"]
    errs = []
    rp = job["template"]["spec"].get("restartPolicy")
    if rp != "Never":
        errs.append(f"restartPolicy is {rp!r}: the pod and its log are deleted when the Job fails")
    if not isinstance(job.get("backoffLimit"), int) or job["backoffLimit"] > 3:
        errs.append(f"backoffLimit {job.get('backoffLimit')!r}: with Never every retry is a new pod, keep it small")
    if spec.get("failedJobsHistoryLimit", 1) < 1:
        errs.append("failedJobsHistoryLimit 0 keeps no failed Job")
    return errs


files = sorted((REPO / "apps/base/backup").rglob("*.yaml"))
cronjobs = [(f, d) for f in files for d in yaml.safe_load_all(f.read_text()) if d and d.get("kind") == "CronJob"]
check("the backup brick has CronJobs to look at", len(cronjobs) >= 8, len(cronjobs))
for f, cj in cronjobs:
    errs = problems(cj)
    check(f"{cj['metadata']['name']} ({f.relative_to(REPO)}) keeps a failed run's pod and log", not errs, "; ".join(errs))

# the checker itself: each defect it names must be seen
good = next(cj for _, cj in cronjobs if cj["metadata"]["name"] == "cnpg-dump")
for name, edit in (("OnFailure", lambda j: j["spec"]["jobTemplate"]["spec"]["template"]["spec"].update(restartPolicy="OnFailure")),
                   ("no restartPolicy", lambda j: j["spec"]["jobTemplate"]["spec"]["template"]["spec"].pop("restartPolicy")),
                   ("a backoffLimit of 10", lambda j: j["spec"]["jobTemplate"]["spec"].update(backoffLimit=10)),
                   ("no failed history", lambda j: j["spec"].update(failedJobsHistoryLimit=0))):
    import copy
    m = copy.deepcopy(good)
    edit(m)
    check(f"the check catches {name}", bool(problems(m)))
print("%s" % ("ok" if not bad else f"{bad} failed"))
sys.exit(1 if bad else 0)
