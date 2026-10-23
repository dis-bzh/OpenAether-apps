#!/usr/bin/env bash
# One GitHub issue per failing image, found by its title `[vuln] <key>` (sbom.yml). A scan that fails again comments on
# the open issue instead of opening a second one; a scan that passes closes it. Uses the `gh` CLI with GH_TOKEN
# (the workflow token, `issues: write`) and GH_REPO or --repo from the checkout.
#   vuln-issue.sh open  KEY BODYFILE [RUN_URL]   open the issue, or comment on the open one
#   vuln-issue.sh close KEY [RUN_URL]            close the open issue with a comment (nothing to do if none)
set -euo pipefail
cmd="${1:?open|close}"; key="${2:?key}"
title="[vuln] $key"
# Exact title match: `gh issue list --search` is a text search and returns near matches.
number="$(gh issue list --state open --search "\"$title\" in:title" --json number,title --limit 50 \
  | jq -r --arg t "$title" '[.[] | select(.title == $t)][0].number // empty')"
case "$cmd" in
  open)
    body="${3:?body file}"; url="${4:-}"
    text="$(printf 'Scan failed again: %s\n\n%s\n' "$url" "$(head -c 20000 "$body")")"
    if [ -n "$number" ]; then
      gh issue comment "$number" --body "$text" >/dev/null
      echo "commented on #$number"
    else
      gh issue create --title "$title" --body "$(printf 'The image scan (CRITICAL with a fix upstream) fails for `%s`.\n\nRun: %s\n\nSeverity clock: docs/patching.md. Fix the image, or record a reasoned, dated waiver in `.vex/openaether-apps.openvex.json` (`scripts/check-vex.py` fails on an expired one).\n\n```\n%s\n```\n' "$key" "$url" "$(head -c 20000 "$body")")" >/dev/null
      echo "opened an issue for $key"
    fi ;;
  close)
    url="${3:-}"
    if [ -n "$number" ]; then
      gh issue close "$number" --comment "The scan passes again: $url" >/dev/null
      echo "closed #$number"
    fi ;;
  *) echo "usage: $0 open|close KEY ..." >&2; exit 2 ;;
esac
