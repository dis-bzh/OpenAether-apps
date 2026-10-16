#!/usr/bin/env bash
# Runs the Kyverno policies of apps/base/kyverno-policies against fixture pods with the Kyverno CLI of the vendored
# version (1.19.1): what each Audit rule reports, and that a `names` exception spares one pod, not its namespace.
# The CLI is found on PATH or in $KYVERNO; CI downloads it with a pinned checksum. Static: no cluster.
set -euo pipefail
cd "$(dirname "$0")/../.."
kyverno="${KYVERNO:-$(command -v kyverno || true)}"
[[ -x "$kyverno" ]] || { echo "kyverno CLI 1.19.1 not found (PATH or \$KYVERNO)" >&2; exit 2; }
[[ "$("$kyverno" version 2>&1)" == *"Version: 1.19."* ]] || { echo "kyverno CLI is not 1.19.x: the fixtures use its test schema (no namespace in results)" >&2; exit 2; }
out="$("$kyverno" test scripts/dev/testdata/kyverno-policies 2>&1)" || { echo "$out" | tail -25; exit 1; }
# The CLI's own summary has been seen to count a "Want fail, got pass" row as passed: read the rows too.
if grep -q 'Want ' <<<"$out"; then grep 'Want ' <<<"$out"; exit 1; fi
grep 'Test Summary' <<<"$out"

# The CLI cannot evaluate a `Pod/ephemeralcontainers` match (every row comes back "Excluded"), so the two ephemeral policies are
# run on a copy whose match is rewritten to Pod: this tests the deny conditions; that the real match registers CREATE and UPDATE
# on the subresource is checked statically by test-kyverno-enforcement.py.
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
cp -r scripts/dev/testdata/kyverno-policies-ephemeral/. "$work/"; mkdir "$work/policies"
python3 -I - "$work/policies" <<'PY'
import sys, yaml
for n in ("restrict-capabilities-ephemeral", "restrict-privileged-ephemeral"):
    pol = yaml.safe_load(open(f"apps/base/kyverno-policies/{n}.yaml"))
    for rule in pol["spec"]["rules"]:
        for blk in rule["match"]["any"]:
            assert blk["resources"]["kinds"] == ["Pod/ephemeralcontainers"], n
            blk["resources"] = {"kinds": ["Pod"]}
    pol["spec"]["background"] = True
    yaml.safe_dump(pol, open(f"{sys.argv[1]}/{n}.yaml", "w"))
PY
out="$("$kyverno" test "$work" 2>&1)" || { echo "$out" | tail -25; exit 1; }
if grep -q 'Want ' <<<"$out"; then grep 'Want ' <<<"$out"; exit 1; fi
grep -q Excluded <<<"$out" && grep -v -E 'kube-system' <<<"$out" | grep -q 'Excluded' && { echo "ephemeral rows were not evaluated" >&2; exit 1; }
grep 'Test Summary' <<<"$out"
