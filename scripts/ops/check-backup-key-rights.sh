#!/usr/bin/env bash
# Does a DEDICATED backup key pair hold every S3 right the backup stack uses? Run it from the operator's machine with the
# pair that was seeded at backup/s3-primary (or s3-replica); it needs no cluster. Lab 11 (Scaleway): the pair could list and
# write, restic and Longhorn worked, and CNPG reported ContinuousArchiving=False because barman-cloud starts with HeadBucket
# (403), a right that only a bucket-level IAM permission set carries on that provider. The list below is the contract in
# apps/base/backup/README.md ("S3 rights of a dedicated key pair").
#
#   S3_ENDPOINT=https://s3.fr-par.scw.cloud S3_BUCKET=<bucket> S3_REGION=fr-par \
#   AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... scripts/ops/check-backup-key-rights.sh
#
# Probes (all under the prefix .oa-rights-probe/, removed at the end): HeadBucket, ListObjectsV2, PutObject, HeadObject+GetObject,
# PutObject with the AES256 server-side-encryption request, CreateMultipartUpload + ListMultipartUploads + AbortMultipartUpload,
# DeleteObject. Exit 0 every right held; 1 one is missing
# (named); 2 usage. Rung: tested against a stub aws (scripts/dev/test-backup-key-rights.sh), not against a store.
set -uo pipefail
: "${S3_ENDPOINT:?S3_ENDPOINT required}" "${S3_BUCKET:?S3_BUCKET required}"
: "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID required}" "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY required}"
command -v aws >/dev/null 2>&1 || { echo "aws CLI required" >&2; exit 2; }
export AWS_DEFAULT_REGION="${S3_REGION:-us-east-1}" AWS_PAGER=""
key=".oa-rights-probe/$(date +%s)-$$"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
echo oa-rights-probe >"$tmp/obj"
fails=0
s3api() { aws --endpoint-url "$S3_ENDPOINT" s3api "$@"; }
probe() { # DESC CMD... : ok when the command succeeds
  local desc="$1"; shift
  if out="$("$@" 2>&1)"; then echo "  ok   $desc"; else echo "  FAIL $desc -> $(tail -1 <<<"$out" | cut -c1-160)"; fails=$((fails + 1)); fi
}
probe "HeadBucket (barman-cloud starts with it)" s3api head-bucket --bucket "$S3_BUCKET"
probe "ListObjectsV2" s3api list-objects-v2 --bucket "$S3_BUCKET" --max-keys 1
probe "PutObject" s3api put-object --bucket "$S3_BUCKET" --key "$key" --body "$tmp/obj"
# The ObjectStores and the replica copy ask the store for server-side encryption (x-amz-server-side-encryption: AES256) on every object.
# A store that refuses the header would fail every WAL archive: better to learn it here than from ContinuousArchiving=False.
probe "PutObject with the AES256 server-side-encryption request" s3api put-object --bucket "$S3_BUCKET" --key "$key-sse" --body "$tmp/obj" --server-side-encryption AES256
probe "HeadObject" s3api head-object --bucket "$S3_BUCKET" --key "$key"
probe "GetObject" s3api get-object --bucket "$S3_BUCKET" --key "$key" "$tmp/back"
probe "ListMultipartUploads" s3api list-multipart-uploads --bucket "$S3_BUCKET" --prefix .oa-rights-probe/
mp="$(s3api create-multipart-upload --bucket "$S3_BUCKET" --key "$key-mp" --query UploadId --output text 2>&1)" && mpok=1 || mpok=0
if [ "$mpok" = 1 ]; then
  echo "  ok   CreateMultipartUpload"
  probe "AbortMultipartUpload" s3api abort-multipart-upload --bucket "$S3_BUCKET" --key "$key-mp" --upload-id "$mp"
else
  echo "  FAIL CreateMultipartUpload -> $(tail -1 <<<"$mp" | cut -c1-160)"; fails=$((fails + 1))
fi
probe "DeleteObject" s3api delete-object --bucket "$S3_BUCKET" --key "$key"
probe "DeleteObject (the SSE probe object)" s3api delete-object --bucket "$S3_BUCKET" --key "$key-sse"
if [ "$fails" -gt 0 ]; then echo "$fails right(s) missing: see apps/base/backup/README.md, S3 rights of a dedicated key pair" >&2; exit 1; fi
echo "every right held"
