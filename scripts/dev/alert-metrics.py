"""Metric names read by the alert expressions (shared by test-alert-metrics.py)."""
import re
from pathlib import Path

import yaml

KEYWORDS = {"and", "or", "unless", "by", "without", "on", "ignoring", "group_left", "group_right", "bool", "offset",
            "inf", "nan", "keep_metric_names", "with"}
RULE_FILES = ["apps/base/observability/vm-customresources/vmrule-*.yaml", "apps/base/observability-openbao/vmrule.yaml",
              "apps/base/observability-alert-probe/vmrule.yaml", "apps/base/observability-audit-sink/vmrule.yaml"]


def rules(repo):
    out = {}
    for pattern in RULE_FILES:
        for f in sorted(Path(repo).glob(pattern)):
            for d in yaml.safe_load_all(f.read_text()):
                if d and d.get("kind") == "VMRule":
                    for g in d["spec"]["groups"]:
                        for r in g["rules"]:
                            out[r["alert"]] = r["expr"]
    return out


def metrics(expr):
    e = re.sub(r'"(?:[^"\\]|\\.)*"|`[^`]*`', '""', expr)          # strings
    e = re.sub(r"\{[^{}]*\}", "", e)                              # label matchers
    e = re.sub(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", " ", e)
    e = re.sub(r"\[[^\]]*\]", " ", e)                             # ranges
    e = re.sub(r"\b\d+(?:\.\d+)?(?:[smhdwy])?\b", " ", e)         # numbers and durations
    names = set()
    for m in re.finditer(r"[A-Za-z_:][A-Za-z0-9_:]*", e):
        tail = e[m.end():].lstrip()
        if tail.startswith("(") or m.group(0) in KEYWORDS:
            continue
        names.add(m.group(0))
    return names
