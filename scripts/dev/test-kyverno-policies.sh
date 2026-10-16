#!/usr/bin/env bash
# Runs the Kyverno policies of apps/base/kyverno-policies against fixture pods with the Kyverno CLI of the vendored
# version (1.12.1): what each Audit rule reports, and that a `names` exception spares one pod, not its namespace.
# The CLI is found on PATH or in $KYVERNO; CI downloads it with a pinned checksum. Static: no cluster.
set -euo pipefail
cd "$(dirname "$0")/../.."
kyverno="${KYVERNO:-$(command -v kyverno || true)}"
[[ -x "$kyverno" ]] || { echo "kyverno CLI 1.12.1 not found (PATH or \$KYVERNO)" >&2; exit 2; }
[[ "$("$kyverno" version 2>&1)" == *"Version: 1.12."* ]] || { echo "kyverno CLI is not 1.12.x: the fixtures use its v1alpha1 test schema" >&2; exit 2; }
out="$("$kyverno" test scripts/dev/testdata/kyverno-policies 2>&1)" || { echo "$out" | tail -25; exit 1; }
# The CLI's own summary has been seen to count a "Want fail, got pass" row as passed: read the rows too.
if grep -q 'Want ' <<<"$out"; then grep 'Want ' <<<"$out"; exit 1; fi
grep 'Test Summary' <<<"$out"
