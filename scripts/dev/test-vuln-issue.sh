#!/usr/bin/env bash
# scripts/ops/vuln-issue.sh against a stub `gh` that keeps issues in a file: one issue per key, a second failure comments,
# a pass closes, a near-match title is not taken for the key, a missing or empty report opens and comments on nothing,
# a transient API error (503) is retried a bounded number of times (#209).
# No network. Usage: scripts/dev/test-vuln-issue.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
PASS=0; FAIL=0
ok()  { echo "ok   $*"; PASS=$((PASS + 1)); }
bad() { echo "FAIL $*"; FAIL=$((FAIL + 1)); }
mkdir -p "$W/bin"; echo '[]' > "$W/issues.json"; : > "$W/log"
cat > "$W/bin/gh" <<'STUB'
#!/usr/bin/env bash
# stub: issue list | create | comment | close
db="$STUB_DIR/issues.json"
# failure injection (#209): a file fail.<word>.<word> holding N makes the next N calls of that subcommand answer 503
f="$STUB_DIR/fail.$1.$2"
if [ -s "$f" ] && [ "$(cat "$f")" -gt 0 ]; then
  echo $(( $(cat "$f") - 1 )) > "$f"; echo "$1 $2" >> "$STUB_DIR/fails"; echo "HTTP 503: Service Unavailable" >&2; exit 1
fi
case "$1 $2" in
  "issue list") jq -c '[.[] | select(.state == "open") | {number, title}]' "$db" ;;
  "issue create") t=""; while [ $# -gt 0 ]; do [ "$1" = --title ] && t="$2"; shift; done
    n=$(( $(jq length "$db") + 1 )); jq --arg t "$t" --argjson n "$n" '. + [{number:$n,title:$t,state:"open"}]' "$db" > "$db.n" && mv "$db.n" "$db"; echo "create $t" >> "$STUB_DIR/log" ;;
  "issue comment") echo "comment $3" >> "$STUB_DIR/log" ;;
  "issue close") jq --argjson n "$3" 'map(if .number == $n then .state = "closed" else . end)' "$db" > "$db.n" && mv "$db.n" "$db"; echo "close $3" >> "$STUB_DIR/log" ;;
esac
STUB
chmod +x "$W/bin/gh"
export STUB_DIR="$W" PATH="$W/bin:$PATH" GH_RETRY_SLEEP=0
echo "table" > "$W/body"
S="$ROOT/scripts/ops/vuln-issue.sh"
"$S" open docker.io/restic/restic:0.19.1 "$W/body" http://run/1 >/dev/null
[ "$(grep -c '^create \[vuln\] docker.io/restic/restic:0.19.1$' "$W/log")" = 1 ] && ok "the first failure opens the issue, titled [vuln] <image>" || bad "no issue opened"
"$S" open docker.io/restic/restic:0.19.1 "$W/body" http://run/2 >/dev/null
[ "$(grep -c '^create' "$W/log")" = 1 ] && grep -q '^comment 1$' "$W/log" && ok "the second failure comments, no duplicate" || bad "duplicate or no comment"
"$S" open docker.io/restic/restic:0.19.10 "$W/body" http://run/3 >/dev/null
[ "$(grep -c '^create' "$W/log")" = 2 ] && ok "another image (a near-match title) gets its own issue" || bad "near-match taken for the same image"
"$S" close docker.io/restic/restic:0.19.1 http://run/4 >/dev/null
grep -q '^close 1$' "$W/log" && [ "$(jq -r '.[1].state' "$W/issues.json")" = open ] && ok "a passing scan closes only its own issue" || bad "close"
"$S" close docker.io/other/never:1 http://run/5 >/dev/null && ok "closing an image with no issue is a no-op" || bad "close of nothing failed"
# #197: a scan that wrote no report (file missing or empty) opens no issue and comments on none, and says why. Each case
# takes its own `before`, so one failing guard does not fail the other case as well.
: > "$W/empty"; before="$(cat "$W/log")"
"$S" open docker.io/restic/restic:0.19.10 "$W/empty" http://run/6 >/dev/null 2>"$W/err"; rc1=$?
[ "$rc1" != 0 ] && [ "$(cat "$W/log")" = "$before" ] && grep -q 'no report in' "$W/err" && ok "an empty report comments on nothing and fails" || bad "empty report: rc $rc1, log changed or no message"
before="$(cat "$W/log")"
"$S" open docker.io/other/never:1 "$W/missing" http://run/7 >/dev/null 2>"$W/err"; rc2=$?
[ "$rc2" != 0 ] && [ "$(cat "$W/log")" = "$before" ] && grep -q 'no report in' "$W/err" && ok "a missing report opens nothing and fails" || bad "missing report: rc $rc2, log changed or no message"
# #209: a transient 503 on a gh call is retried, a permanent one still fails after a bounded number of attempts.
: > "$W/fails"; echo 1 > "$W/fail.issue.close"
"$S" close docker.io/restic/restic:0.19.10 http://run/8 >/dev/null 2>&1; rc3=$?
[ "$rc3" = 0 ] && [ "$(jq -r '.[1].state' "$W/issues.json")" = closed ] && [ "$(grep -c '^issue close$' "$W/fails")" = 1 ] && ok "a 503 on issue close is retried, the issue is closed, the step is green" || bad "503 on close: rc $rc3, state $(jq -r '.[1].state' "$W/issues.json")"
: > "$W/fails"; echo 1 > "$W/fail.issue.list"; echo 1 > "$W/fail.issue.create"
"$S" open docker.io/new/img:1 "$W/body" http://run/9 >/dev/null 2>&1; rc4=$?
[ "$rc4" = 0 ] && [ "$(grep -c '^create \[vuln\] docker.io/new/img:1$' "$W/log")" = 1 ] && [ "$(wc -l < "$W/fails")" = 2 ] && ok "a 503 on issue list and on issue create is retried, one issue is opened" || bad "503 on open: rc $rc4"
: > "$W/fails"; echo 1 > "$W/fail.issue.comment"; before="$(grep -c '^comment' "$W/log")"
"$S" open docker.io/new/img:1 "$W/body" http://run/9b >/dev/null 2>&1; rc4b=$?
[ "$rc4b" = 0 ] && [ "$(grep -c '^comment' "$W/log")" = $((before + 1)) ] && [ "$(cat "$W/fails")" = "issue comment" ] && ok "a 503 on issue comment is retried, one comment is added" || bad "503 on comment: rc $rc4b"
: > "$W/fails"; echo 99 > "$W/fail.issue.close"; before="$(cat "$W/log")"
"$S" close docker.io/new/img:1 http://run/10 >/dev/null 2>&1; rc5=$?
[ "$rc5" != 0 ] && [ "$(wc -l < "$W/fails")" = 3 ] && [ "$(cat "$W/log")" = "$before" ] && ok "a 503 that never clears fails after three attempts, not more" || bad "permanent 503: rc $rc5, $(wc -l < "$W/fails") attempts"
echo "$PASS passed, $FAIL failed"; [ "$FAIL" = 0 ]
