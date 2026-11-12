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

# test-backup-verify.sh runs in CI shards (VERIFY_SHARD=1..4): a scenario block assigned to no shard, or to two, or a pass outside every
# block (it would run in each shard, or only in the unsharded run), is a test that silently stops running. Static: no docker.
V="$ROOT/scripts/dev/test-backup-verify.sh"
table="$(VERIFY_LIST=1 bash "$V")"
gates="$(sed -n 's/^if want \(.*\); then$/\1/p' "$V")"
if [ -n "$gates" ] && [ -z "$(sort <<<"$gates" | uniq -d)" ] && [ "$(sort <<<"$gates")" = "$(awk '{print $2}' <<<"$table" | sort)" ]; then
  echo "ok   each of the $(wc -l <<<"$gates") scenario blocks of the verifier test is assigned to exactly one shard, none is orphaned"
else echo "FAIL scenario blocks and SHARD_OF differ. blocks: $(tr '\n' ' ' <<<"$gates")| table: $(awk '{print $2}' <<<"$table" | sort | tr '\n' ' ')"; rc=1; fi
shards="$(awk '{print $1}' <<<"$table" | sort -u | paste -sd,)"
if [ "$shards" = "1,2,3,4" ] && [ "$(sed -n 's/^ *shard: \[\(.*\)\]$/\1/p' "$ROOT/.github/workflows/ci.yml" | tr -d ' ')" = "$shards" ]; then
  echo "ok   the shards 1..4 all hold a scenario and are the ones ci.yml runs"
else echo "FAIL the shards of SHARD_OF ($shards) are not 1,2,3,4 or not the matrix of ci.yml"; rc=1; fi
loose="$(awk '/^if want .*; then$/ {b=1; next} /^fi$/ {b=0; next} !b && !/^#/ && !/^run_all\(\)/ && /(^|[^_a-z])run_all($|[^_a-z(])/ {n++} END {print n+0}' "$V")"
if [ "$loose" = 1 ]; then echo "ok   one pass (the healthy baseline) sits outside the scenario blocks, so it runs in every shard"
else echo "FAIL $loose passes outside the scenario blocks of the verifier test, expected 1 (the healthy baseline): each runs in every shard"; rc=1; fi
exit $rc
