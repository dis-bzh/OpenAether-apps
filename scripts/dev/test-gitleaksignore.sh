#!/usr/bin/env bash
# .gitleaksignore silences ONE known false positive and nothing else: the upstream Barman Cloud plugin Secret whose data is the base64 of the
# sidecar IMAGE NAME (apps/base/cnpg/barman-cloud-plugin-0.15.1.yaml, line 967). Runs gitleaks, with the rules the CI jobs use (the defaults,
# and the repository's .gitleaks-envdata.toml), over a throwaway repository holding:
#   - a copy of that real file at its real path: must NOT be reported;
#   - the same Secret one line lower in the same file (a different line), another path with the same rule and a high-entropy value: MUST be reported.
# A self-test of the self-test: without the .gitleaksignore entry the real file is reported (so the probe can see the finding at all).
set -euo pipefail
cd "$(dirname "$0")/../.."
command -v gitleaks >/dev/null || { echo "✗ gitleaks not on PATH" >&2; exit 1; }
REAL=apps/base/cnpg/barman-cloud-plugin-0.15.1.yaml
[ -f "$REAL" ] || { echo "✗ $REAL is gone: update the entry of .gitleaksignore (and this test) for the new file" >&2; exit 1; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
git init -q "$tmp"
mkdir -p "$tmp/$(dirname "$REAL")" "$tmp/other"
cp "$REAL" "$tmp/$REAL"
# the same file with one extra first line: the finding moves to line 968
{ echo "# shifted"; cat "$REAL"; } > "$tmp/apps/base/cnpg/shifted.yaml"
# a Secret of another path with a high-entropy value
printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: x\ndata:\n  token: |\n    %s\n' \
  "$(head -c 33 /dev/urandom | base64 -w0)" > "$tmp/other/secret.yaml"
git -C "$tmp" add -A
git -C "$tmp" -c user.email=t@t -c user.name=t commit -q -m probe

scan() { # <ignorefile or ""> -> findings as "file:line" lines
  rm -f "$tmp/.gitleaksignore" "$tmp/out.json"
  [ -z "$1" ] || cp "$1" "$tmp/.gitleaksignore"
  (cd "$tmp" && gitleaks git -c "$PWD_REPO/.gitleaks-envdata.toml" --report-format json --report-path out.json >/dev/null 2>&1) || true
  python3 -I -c 'import json,sys; [print(f["File"]+":"+str(f["StartLine"])) for f in json.load(open(sys.argv[1]))]' "$tmp/out.json" | sort
}
PWD_REPO="$PWD"
bad=0
without="$(scan "")"
grep -qx "$REAL:967" <<<"$without" || { echo "✗ the probe cannot see the false positive without the ignore file (the test would prove nothing): $without"; bad=1; }
with="$(scan "$PWD_REPO/.gitleaksignore")"
! grep -qx "$REAL:967" <<<"$with" && echo "✓ the known false positive ($REAL:967) is silenced" || { echo "✗ the known false positive is still reported"; bad=1; }
grep -qx "apps/base/cnpg/shifted.yaml:968" <<<"$with" && echo "✓ the same Secret on another line of another file is still reported" || { echo "✗ a shifted copy is silenced: the entry is too wide: $with"; bad=1; }
grep -q "^other/secret.yaml:" <<<"$with" && echo "✓ a high-entropy Secret elsewhere is still reported" || { echo "✗ another Secret is silenced: $with"; bad=1; }
[ "$bad" = 0 ]
