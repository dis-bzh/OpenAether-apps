#!/usr/bin/env python3
"""The provider-S3 egress rules must admit every region of a provider, not one hard-coded host (apps #53).

Six files of CiliumNetworkPolicies (observability, backup/openbao, backup/databases, cnpg, storage) let a workload reach the provider's object storage on :443 through `toFQDNs`.
They used to name ONE host per provider (`s3.eu-west-par.io.cloud.ovh.net`), so a real OVH config with
`s3.gra.io.cloud.ovh.net` had Loki, the CNPG backups and the OpenBao snapshot job dropped by Cilium
("Policy denied", measured on a real OVH cluster). This test reads those rules and checks them against
the hosts the infra tfvars can name, with Cilium's matching rule (`*` stays inside one DNS label).

Static: it proves the rules, not Cilium. It also proves it would have caught the old rule.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FILES = [
    "apps/base/observability/networkpolicy.yaml",
    "apps/base/backup/openbao/networkpolicy.yaml",
    "apps/base/backup/databases/networkpolicy.yaml",   # the dump, the store replication and the verifier: one block each
    "apps/base/cnpg/networkpolicy-db-restricted.yaml",
    "apps/base/storage/networkpolicy.yaml",
]
MUST_MATCH = [
    "s3.fr-par.scw.cloud", "s3.nl-ams.scw.cloud", "s3.pl-waw.scw.cloud",
    "s3.eu-west-par.io.cloud.ovh.net", "s3.gra.io.cloud.ovh.net", "s3.rbx.io.cloud.ovh.net",
    "oos.eu-west-2.outscale.com", "oos.cloudgouv-eu-west-1.outscale.com",
]
MUST_NOT_MATCH = [
    "s3.amazonaws.com", "evil.example.com", "scw.cloud", "s3.fr-par.scw.cloud.evil.example",
    "attacker.io.cloud.ovh.net", "s3.gra.io.cloud.ovh.net.evil.example", "oos.eu-west-2.outscale.com.evil.example",
    "x.s3.fr-par.scw.cloud", "api.scaleway.com",
]
OLD_RULES = [("matchName", "s3.fr-par.scw.cloud"), ("matchName", "s3.eu-west-par.io.cloud.ovh.net"),
             ("matchName", "oos.eu-west-2.outscale.com")]

ENTRY = re.compile(r"^(\s*)- match(Name|Pattern):\s*\"?([^\s\"#]+)\"?")
failures = 0


def fail(msg):
    global failures
    failures += 1
    print("FAIL:", msg)


def to_regex(kind, value):
    if kind == "Name":
        return re.compile(re.escape(value) + r"\Z", re.I)
    # Cilium: `*` matches zero or more DNS characters, never a dot.
    return re.compile("".join("[-a-z0-9_]*" if c == "*" else re.escape(c) for c in value) + r"\Z", re.I)


def matches(rules, host):
    return any(to_regex(k, v).match(host) for k, v in rules)


def s3_rules(path):
    """The (kind, value) entries of every toFQDNs block that names an object-storage provider."""
    lines = (REPO / path).read_text().splitlines()
    out, i = [], 0
    while i < len(lines):
        m = re.match(r"^(\s*)- toFQDNs:\s*$", lines[i]) or re.match(r"^(\s*)toFQDNs:\s*$", lines[i])
        i += 1
        if not m:
            continue
        indent = len(m.group(1))
        block = []
        while i < len(lines):
            e = ENTRY.match(lines[i])
            if e:
                block.append((e.group(2), e.group(3)))
            elif lines[i].strip() and not lines[i].strip().startswith("#") and \
                    len(lines[i]) - len(lines[i].lstrip()) <= indent + 2 and not lines[i].lstrip().startswith("- match"):
                break
            i += 1
        if any(re.search(r"scw\.cloud|ovh\.net|outscale\.com", v) and re.search(r"s3|oos", v) for _, v in block):
            ports = []
            j = i
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent):
                m2 = re.match(r"^\s*- port:\s*\"?(\d+)\"?", lines[j]) or re.match(r"^\s*port:\s*\"?(\d+)\"?", lines[j])
                if m2:
                    ports.append(m2.group(1))
                j += 1
            out.append((block, sorted(set(ports))))
    return out


# 0. The test must be able to fail: the former rule really missed an OVH region other than eu-west-par.
if matches(OLD_RULES, "s3.gra.io.cloud.ovh.net"):
    fail("self-check: the former single-host rules should NOT match s3.gra.io.cloud.ovh.net")
if not matches(OLD_RULES, "s3.eu-west-par.io.cloud.ovh.net"):
    fail("self-check: the former rules should match their own host")

sets = {}
for f in FILES:
    blocks = s3_rules(f)
    if not blocks:
        fail(f"{f}: no provider-S3 toFQDNs block found")
        continue
    for n, (rules, ports) in enumerate(blocks):
        where = f"{f} (block {n + 1} of {len(blocks)})"
        if ports != ["443"]:
            fail(f"{where}: the provider-S3 rule must open exactly 443/TCP, found ports {ports}")
        sets[where] = sorted(rules)
        for host in MUST_MATCH:
            if not matches(rules, host):
                fail(f"{where}: does not admit {host}")
        for host in MUST_NOT_MATCH:
            if matches(rules, host):
                fail(f"{where}: admits {host}, which it must not")

if len({tuple(v) for v in sets.values()}) > 1:
    fail("the files carry different provider-S3 rules: " + "; ".join(f"{k}={v}" for k, v in sets.items()))

print(f"{failures} failed" if failures else f"ok: the {len(sets)} provider-S3 egress rules admit every region of Scaleway, OVH and Outscale on 443 only, "
      "refuse the look-alike hosts and are identical; the former single-host rule is shown to miss s3.gra.io.cloud.ovh.net")
sys.exit(1 if failures else 0)
