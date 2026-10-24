#!/bin/sh
# Stage 1 of the dump restore test (restic image): restore the latest cnpg snapshot of EACH repository (primary, replica) into
# /restore/<store>/. The data is what the dump job pushed; restic decrypts it with the escrowed password, so a pass here also
# proves that password. A repository that cannot be read leaves an `error` file instead of a restore: dump_load.py reports it.
set -u
: "${CLUSTER_NAME:?}"
export RESTIC_CACHE_DIR="${RESTIC_CACHE_DIR:-/tmp/restic-cache}"
one() { # store endpoint bucket ak sk
  export RESTIC_REPOSITORY="s3:${2}/${3}/${CLUSTER_NAME}/cnpg" AWS_ACCESS_KEY_ID="$4" AWS_SECRET_ACCESS_KEY="$5"
  mkdir -p "/restore/$1"
  if ! timeout 120 restic cat config >/dev/null 2>"/restore/$1/error"; then
    echo "ERROR: repository $1 unreadable: $(tail -1 "/restore/$1/error")"; return
  fi
  if restic restore latest --host cnpg --retry-lock 3m --target "/restore/$1" >"/restore/$1/restic.log" 2>&1; then
    rm -f "/restore/$1/error"
    # the dump job ran as another uid: let the next stage (the postgres user) read what it restored
    chmod -R a+rX "/restore/$1"
    echo "restored the latest snapshot of repository $1"
  else
    echo "ERROR: restore from repository $1 failed: $(tail -1 "/restore/$1/restic.log")" | tee "/restore/$1/error"
  fi
}
one primary "$PRIMARY_ENDPOINT" "$PRIMARY_BUCKET" "$PRIMARY_ACCESS_KEY" "$PRIMARY_SECRET_KEY"
one replica "$REPLICA_ENDPOINT" "$REPLICA_BUCKET" "$REPLICA_ACCESS_KEY" "$REPLICA_SECRET_KEY"
exit 0
