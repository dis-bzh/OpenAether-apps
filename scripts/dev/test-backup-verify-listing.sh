#!/usr/bin/env bash
# restic-stage.sh must not turn a FAILED `restic snapshots` into "no snapshot yet" (a skip leaves no series and no alert).
# Lab 19: the daily openbao-snapshot held the repository lock while the verifier listed it; the dataset was a skip, not a failure.
# A fake `restic` stands in: no docker, no network. Static+mocked.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
mkdir -p "$W/bin" "$W/res" "$W/work"
cat >"$W/bin/restic" <<'R'
#!/bin/sh
case "$1" in
  cat) exit 0 ;;
  snapshots) if [ "$FAKE" = locked ]; then echo "Fatal: unable to create lock in backend: repository is already locked exclusively by PID 7" >&2; exit 1
             elif [ "$FAKE" = empty ]; then echo '[]'; exit 0
             else echo '[{"time":"2026-10-20T01:00:00Z"}]'; exit 0; fi ;;
  check|restore) exit 0 ;;
  ls) echo /x.dump; exit 0 ;;
esac
R
chmod +x "$W/bin/restic"
run() { FAKE="$1" PATH="$W/bin:$PATH" RESULTS="$W/res" WORK="$W/work" CLUSTER_NAME=c RESTIC_SUBSET_PARTS=10 \
  PRIMARY_ENDPOINT=http://x PRIMARY_BUCKET=b PRIMARY_ACCESS_KEY=a PRIMARY_SECRET_KEY=s REPLICA_ENDPOINT=http://x REPLICA_BUCKET=b REPLICA_ACCESS_KEY=a REPLICA_SECRET_KEY=s \
  sh "$ROOT/apps/base/backup/databases/verify/restic-stage.sh" >/dev/null 2>&1; cat "$W/res/restic.tsv"; }
rc=0
out="$(run locked)"; if grep -q "^openbao	primary	0	.*snapshots unreadable" <<<"$out" && ! grep -q skip <<<"$out"; then echo "ok   a locked repository is a failure (0), not a skip"; else echo "FAIL locked listing: $out"; rc=1; fi
out="$(run empty)";  if grep -q "^openbao	primary	skip	.*no snapshot yet" <<<"$out"; then echo "ok   an empty repository is still a skip"; else echo "FAIL empty listing: $out"; rc=1; fi
out="$(run good)";   if grep -q "^openbao	primary	1	" <<<"$out"; then echo "ok   a readable repository passes"; else echo "FAIL good: $out"; rc=1; fi
exit $rc
