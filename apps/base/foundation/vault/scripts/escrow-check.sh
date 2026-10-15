#!/bin/bash
# Fails when a PRODUCTION OpenBao has no operator attestation of the off-cluster escrow of its seal key (or the attestation is
# stale): losing that key loses every secret and every raft snapshot, and nothing else notices a forgotten escrow.
#
# The attestation is the ConfigMap `openbao-escrow-ack`, written by the operator after the Day 0 escrow (vault README):
#   escrowed-at       YYYY-MM-DD, when the escrow was last made and its restore tested
#   seal-key-sha256   sha256 of the 32 key bytes (not secret: it only lets an operator compare a reconstructed key)
#   holders           how many distinct people/places hold an independent part of the escrow (>= 2)
# This job never reads the Secret openbao-seal-key: it only has `get` on three ConfigMaps. So it can tell "never attested" and
# "attested long ago", not "attested for an OLD key" (compare the hash by hand after a rotation, README).
#
# Development clusters (no openbao-operator-keys) are not production and pass. A new cluster has ESCROW_GRACE_DAYS to do it.
set -euo pipefail

NS="${VAULT_NS:-foundation-vault}"
GRACE_DAYS="${ESCROW_GRACE_DAYS:-7}"
MAX_AGE_DAYS="${ESCROW_MAX_AGE_DAYS:-400}"   # yearly re-attestation plus a margin; the seal-key period is proposed yearly
NOW="${ESCROW_NOW:-$(date -u +%s)}"          # overridable for the test only

field() { kubectl -n "$NS" get configmap "$1" --ignore-not-found -o "jsonpath=$2"; }
# RFC 3339 or YYYY-MM-DD to epoch seconds; busybox and GNU date both take "YYYY-MM-DD hh:mm:ss".
epoch() { local t="${1/T/ }"; t="${t%Z}"; [[ "$t" == *:* ]] || t="$t 00:00:00"; date -u -d "$t" +%s; }
fail() { echo "ESCROW CHECK FAILED: $*" >&2; exit 1; }

# Each lookup is assigned on its own: inside a test or `||`, a kubectl failure (API down) would not stop the script
# under `set -e` and would read as "absent", i.e. as a development cluster, i.e. as a pass.
keys="$(field openbao-operator-keys '{.metadata.name}')"
[ -n "$keys" ] || { echo "development bootstrap (no ConfigMap openbao-operator-keys): no escrow to attest"; exit 0; }
born="$(field openbao-init-state '{.metadata.creationTimestamp}')"
[ -n "$born" ] || { echo "OpenBao is not initialised yet: no escrow to attest"; exit 0; }
age_days=$(( (NOW - $(epoch "$born")) / 86400 ))

at="$(field openbao-escrow-ack '{.data.escrowed-at}')"
if [ -z "$at" ]; then
  [ "$age_days" -le "$GRACE_DAYS" ] \
    && { echo "no attestation yet; the cluster is $age_days day(s) old, grace is $GRACE_DAYS"; exit 0; }
  fail "no ConfigMap openbao-escrow-ack ($age_days days after initialisation). Escrow the seal key, then attest it: vault README, Day 0"
fi
[[ "$at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || fail "escrowed-at '$at' is not YYYY-MM-DD"
at_s="$(epoch "$at")" || fail "escrowed-at '$at' is not a date"
[ "$at_s" -le $((NOW + 86400)) ] || fail "escrowed-at '$at' is in the future"
sha="$(field openbao-escrow-ack '{.data.seal-key-sha256}')"
[[ "$sha" =~ ^[0-9a-f]{64}$ ]] || fail "seal-key-sha256 is missing or not 64 lowercase hex characters"
holders="$(field openbao-escrow-ack '{.data.holders}')"
[[ "$holders" =~ ^[0-9]+$ ]] && [ "$holders" -ge 2 ] || fail "holders must be an integer >= 2 (a single custodian is a single point of loss)"
att_days=$(( (NOW - at_s) / 86400 ))
[ "$att_days" -le "$MAX_AGE_DAYS" ] || fail "the escrow was attested $att_days days ago (limit $MAX_AGE_DAYS): redo it and the restore test"
echo "escrow attested $att_days day(s) ago by $holders holders"
