#!/usr/bin/env bash
# scripts/ops/vuln-issue.sh against a stub `gh` that keeps issues in a file: one issue per key, a second failure comments,
# a pass closes, a near-match title is not taken for the key. No network. Usage: scripts/dev/test-vuln-issue.sh
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
case "$1 $2" in
  "issue list") jq -c '[.[] | select(.state == "open") | {number, title}]' "$db" ;;
  "issue create") t=""; while [ $# -gt 0 ]; do [ "$1" = --title ] && t="$2"; shift; done
    n=$(( $(jq length "$db") + 1 )); jq --arg t "$t" --argjson n "$n" '. + [{number:$n,title:$t,state:"open"}]' "$db" > "$db.n" && mv "$db.n" "$db"; echo "create $t" >> "$STUB_DIR/log" ;;
  "issue comment") echo "comment $3" >> "$STUB_DIR/log" ;;
  "issue close") jq --argjson n "$3" 'map(if .number == $n then .state = "closed" else . end)' "$db" > "$db.n" && mv "$db.n" "$db"; echo "close $3" >> "$STUB_DIR/log" ;;
esac
STUB
chmod +x "$W/bin/gh"
export STUB_DIR="$W" PATH="$W/bin:$PATH"
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
echo "$PASS passed, $FAIL failed"; [ "$FAIL" = 0 ]
