#!/usr/bin/env bash
# Tests of the OpenBao escrow check (apps/base/foundation/vault/scripts/escrow-check.sh): it passes on a development
# cluster, within the grace period and with a sound attestation, and fails for each way the attestation can be missing or
# wrong. kubectl is a stub reading ConfigMap fixtures; the script is the one that ships. It runs on this host's bash and,
# when Docker and the image the CronJob uses are available, under that image too (busybox date, the real shell).
# Rung: mocked/static, nothing runs on a cluster.
#   Usage: scripts/dev/test-escrow-check.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$ROOT/apps/base/foundation/vault/scripts/escrow-check.sh"
IMAGE="$(grep -o 'docker.io/alpine/k8s:[0-9.]*' "$ROOT/apps/base/foundation/vault/escrow-check.yaml" | head -1)"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
PASS=0; FAIL=0
NOW=1792000000   # a fixed "now" (2026-10-14), so that the dates below are exact
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ; }
day() { date -u -d "@$1" +%F; }
ago() { echo $((NOW - $1 * 86400)); }   # epoch of N days ago

mkdir -p "$W/bin"
cat > "$W/bin/kubectl" <<'STUB'
#!/bin/bash
# kubectl -n NS get configmap NAME --ignore-not-found -o jsonpath={...}, answered from $FIX/NAME/
echo "$*" >> "$FIX/calls"
[ -e "$FIX/kubectl-down" ] && { echo "connection refused" >&2; exit 1; }
[ "$4" = configmap ] || { echo "stub: unexpected $*" >&2; exit 1; }
d="$FIX/$5"; [ -d "$d" ] || exit 0
case "${*: -1}" in
  *'{.metadata.name}'*) printf '%s' "$5" ;;
  *'{.metadata.creationTimestamp}'*) cat "$d/creationTimestamp" 2>/dev/null ;;
  *'{.data.'*) k="${*: -1}"; k="${k#*\{.data.}"; k="${k%\}}"; cat "$d/data.$k" 2>/dev/null ;;
esac
exit 0
STUB
chmod +x "$W/bin/kubectl"

fixture() { # prod|dev  INIT_AGE_DAYS|none  -> $FIX
  FIX="$W/fix"; rm -rf "$FIX"; mkdir -p "$FIX"; export FIX
  [ "$1" = prod ] && mkdir -p "$FIX/openbao-operator-keys"
  [ "$2" != none ] && { mkdir -p "$FIX/openbao-init-state"; iso "$(ago "$2")" > "$FIX/openbao-init-state/creationTimestamp"; }
  return 0
}
ack() { # AT SHA HOLDERS
  mkdir -p "$FIX/openbao-escrow-ack"
  [ -n "$1" ] && printf '%s' "$1" > "$FIX/openbao-escrow-ack/data.escrowed-at"
  [ -n "$2" ] && printf '%s' "$2" > "$FIX/openbao-escrow-ack/data.seal-key-sha256"
  [ -n "$3" ] && printf '%s' "$3" > "$FIX/openbao-escrow-ack/data.holders"
  return 0
}
SHA="$(printf 'k' | sha256sum | cut -d' ' -f1)"

# run MODE -> rc and $W/out
run() {
  if [ "$1" = host ]; then
    PATH="$W/bin:$PATH" ESCROW_NOW=$NOW bash "$SCRIPT" >"$W/out" 2>&1
  else
    docker run --rm --entrypoint /bin/bash -e FIX=/fix -e ESCROW_NOW=$NOW -e PATH="/stub:/usr/local/bin:/usr/bin:/bin" \
      -v "$W/bin:/stub:ro" -v "$FIX:/fix" -v "$SCRIPT:/s.sh:ro" "$IMAGE" /s.sh >"$W/out" 2>&1
  fi
}
check() { # MODE DESC WANT(pass|fail) [MESSAGE_SUBSTRING]
  local mode="$1" desc="$2" want="$3" sub="${4:-}"
  run "$mode"; local rc=$?
  if { [ "$want" = pass ] && [ $rc = 0 ]; } || { [ "$want" = fail ] && [ $rc != 0 ] && grep -q -- "$sub" "$W/out"; }; then
    PASS=$((PASS+1)); echo "  ok   [$mode] $desc"
  else
    FAIL=$((FAIL+1)); echo "  FAIL [$mode] $desc (rc=$rc: $(tail -2 "$W/out" | tr '\n' ' '))"
  fi
}

cases() {
  local m="$1"
  fixture dev 400;  check "$m" "a development cluster passes without any attestation" pass
  fixture prod none; check "$m" "a production cluster not initialised yet passes" pass
  fixture prod 3;   check "$m" "no attestation, 3 days old: within the grace period" pass
  fixture prod 30;  check "$m" "no attestation, 30 days old, fails and names the ConfigMap" fail "openbao-escrow-ack"
  fixture prod 30; ack "$(day "$(ago 10)")" "$SHA" 3; check "$m" "a sound attestation of 10 days ago passes" pass
  fixture prod 30; ack "$(day "$(ago 399)")" "$SHA" 2; check "$m" "an attestation of 399 days ago still passes" pass
  fixture prod 900; ack "$(day "$(ago 401)")" "$SHA" 3; check "$m" "an attestation of 401 days ago fails" fail "401 days ago"
  fixture prod 30; ack "yesterday" "$SHA" 3; check "$m" "a date that is not YYYY-MM-DD fails" fail "not YYYY-MM-DD"
  fixture prod 30; ack "2026-13-45" "$SHA" 3; check "$m" "an impossible date fails" fail "not a date"
  fixture prod 30; ack "$(day $((NOW + 10 * 86400)))" "$SHA" 3; check "$m" "a date in the future fails" fail "future"
  fixture prod 30; ack "$(day "$(ago 5)")" "" 3; check "$m" "no hash fails" fail "seal-key-sha256"
  fixture prod 30; ack "$(day "$(ago 5)")" "$(echo "$SHA" | tr a-f A-F)" 3; check "$m" "an upper-case hash fails" fail "seal-key-sha256"
  fixture prod 30; ack "$(day "$(ago 5)")" "${SHA%??}" 3; check "$m" "a short hash fails" fail "seal-key-sha256"
  fixture prod 30; ack "$(day "$(ago 5)")" "$SHA" 1; check "$m" "a single holder fails" fail "holders"
  fixture prod 30; ack "$(day "$(ago 5)")" "$SHA" many; check "$m" "a non-numeric holders fails" fail "holders"
  fixture prod 30; ack "$(day "$(ago 5)")" "$SHA" ""; check "$m" "no holders fails" fail "holders"
  fixture prod 30; ack "$(day "$(ago 5)")" "$SHA" 3; touch "$FIX/kubectl-down"; check "$m" "an unreachable API fails, never a silent pass" fail "refused"
  fixture prod 30; ack "$(day "$(ago 5)")" "$SHA" 3; : > "$FIX/calls"; run "$m"
  if grep -qi "secret" "$FIX/calls"; then FAIL=$((FAIL+1)); echo "  FAIL [$m] the script asked kubectl for a Secret"; else PASS=$((PASS+1)); echo "  ok   [$m] only ConfigMaps are read, never a Secret"; fi
}

# The manifest claims "get on three ConfigMaps, never the Secret": assert them on the file, not just in the script.
echo "=== escrow-check.yaml, the manifest ==="
MANIFEST_OUT="$(python3 -I - "$ROOT/apps/base/foundation/vault/escrow-check.yaml" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
role = next(d for d in docs if d["kind"] == "Role")
ok = (len(role["rules"]) == 1 and role["rules"][0]["apiGroups"] == [""] and role["rules"][0]["resources"] == ["configmaps"]
      and role["rules"][0]["verbs"] == ["get"]
      and sorted(role["rules"][0]["resourceNames"]) == ["openbao-escrow-ack", "openbao-init-state", "openbao-operator-keys"])
print(("ok   " if ok else "FAIL ") + "the Role grants get on the three named ConfigMaps and nothing else (no Secret, no list)")
pod = next(d for d in docs if d["kind"] == "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
c = pod["containers"][0]
tmp = [m for m in c.get("volumeMounts", []) if m["mountPath"] == "/tmp"]
ok = (c["securityContext"]["readOnlyRootFilesystem"] is True and len(tmp) == 1
      and {e["name"]: e.get("value") for e in c.get("env", [])}.get("HOME") == "/tmp"
      and any(v["name"] == tmp[0]["name"] and "emptyDir" in v for v in pod["volumes"]))
print(("ok   " if ok else "FAIL ") + "read-only root with an emptyDir /tmp as HOME (kubectl's discovery cache)")
PY
)" || MANIFEST_OUT="FAIL the manifest check did not run"
echo "$MANIFEST_OUT" | sed 's/^/  /'
PASS=$((PASS + $(grep -c '^ok' <<<"$(echo "$MANIFEST_OUT" | sed 's/^ *//')")))
FAIL=$((FAIL + $(grep -c '^FAIL' <<<"$(echo "$MANIFEST_OUT" | sed 's/^ *//')")))
echo "=== escrow-check.sh, host bash ==="
cases host
echo "=== escrow-check.sh, $IMAGE ==="
if command -v docker >/dev/null && { docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull -q "$IMAGE" >/dev/null 2>&1; }; then
  cases image
else
  echo "  SKIPPED: docker or $IMAGE is not available"
  if [ "${CI:-}" = true ]; then FAIL=$((FAIL+1)); echo "  FAIL not run but required in CI"; fi
fi
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
