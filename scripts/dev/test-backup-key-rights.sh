#!/usr/bin/env bash
# Test of scripts/ops/check-backup-key-rights.sh with a stub `aws` that refuses the actions named in $DENY: the probe must
# pass when nothing is refused and name the missing right when one is (HeadBucket first: the lab 11 case). Rung: mocked/static.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
cat >"$W/aws" <<'STUB'
#!/usr/bin/env bash
# aws --endpoint-url URL s3api <op> ...
op="$4"
case " $DENY " in *" $op "*) echo "An error occurred (403) when calling the $op operation: Forbidden" >&2; exit 254;; esac
[ "$op" = create-multipart-upload ] && echo upload-id
exit 0
STUB
chmod +x "$W/aws"
export PATH="$W:$PATH" S3_ENDPOINT=https://s3.example.invalid S3_BUCKET=b AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=y
pass=0; fail=0
t() { # DESC DENY WANT_RC WANT_TEXT
  out="$(DENY="$2" "$ROOT/scripts/ops/check-backup-key-rights.sh" 2>&1)"; rc=$?
  if [ "$rc" = "$3" ] && { [ -z "$4" ] || grep -q "$4" <<<"$out"; }; then pass=$((pass+1)); echo "  ok   $1"
  else fail=$((fail+1)); echo "  FAIL $1 (rc=$rc)"; echo "$out" | tail -4; fi
}
t "every right held" "" 0 "every right held"
t "HeadBucket refused (the lab 11 case) is named" "head-bucket" 1 "FAIL HeadBucket"
t "multipart create refused is named" "create-multipart-upload" 1 "FAIL CreateMultipartUpload"
t "delete refused is named" "delete-object" 1 "FAIL DeleteObject"
t "two missing rights are both counted" "head-bucket list-multipart-uploads" 1 "2 right(s) missing"
echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
