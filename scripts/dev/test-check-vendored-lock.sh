#!/usr/bin/env bash
# Test of check-vendored-lock.sh: passes on the repository, fails on a changed hash and on a missing lock entry.
# Rung: mocked/static (the lock is copied, no dump is touched).
set -uo pipefail
cd "$(dirname "$0")/../.."
S=scripts/dev/check-vendored-lock.sh
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
P=0; F=0
t() { if [[ "$1" == "$2" ]]; then echo "  ok   $3"; P=$((P+1)); else echo "  FAIL $3 (got rc=$1, want $2)"; F=$((F+1)); fi; }
$S >/dev/null 2>&1; t $? 0 "the repository matches its lock"
sed '1s/^./0/' apps/vendored-dumps.sha256 > "$W/bad"; [[ "$(head -c1 apps/vendored-dumps.sha256)" == 0 ]] && sed -i '1s/^./1/' "$W/bad"
VENDORED_LOCK="$W/bad" $S >/dev/null 2>&1; t $? 1 "a changed hash fails"
sed '1d' apps/vendored-dumps.sha256 > "$W/short"
VENDORED_LOCK="$W/short" $S >/dev/null 2>&1; t $? 1 "a dump missing from the lock fails"
VENDORED_LOCK="$W/none" $S >/dev/null 2>&1; t $? 1 "a missing lock fails"
echo "$P passed, $F failed"; exit $((F > 0))
