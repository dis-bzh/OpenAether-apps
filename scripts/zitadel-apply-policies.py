#!/usr/bin/env python3
"""zitadel-apply-policies.py - apply the instance policies of the Helm values to a RUNNING Zitadel.

`DefaultInstance.*` in apps/base/identity/zitadel/helmrelease.yaml is read once, when the instance is created, so
an instance that already exists keeps Zitadel's upstream policies (8-character passwords, no lockout, no MFA,
open self-registration). This sends the same values through the Admin API, from the same file, so the two cannot
disagree. Nothing runs by itself: an operator runs it, reads the dry run first, and owns the lockout risk (5 wrong
passwords lock an account; docs/zitadel-hardening.md has the unlock).

Dry run by default: prints each policy, the values that differ, and exits 0. `--apply` sends them.
A login policy is replaced as a whole by Zitadel, so the current one is read and only our keys are changed.
Org-level custom policies, if any were created in the console, override the instance ones and are not touched.

Usage:  ZITADEL_PAT=<iam-admin PAT> python3 scripts/zitadel-apply-policies.py --url http://127.0.0.1:8080 \\
            --host zitadel.openaether.local [--apply]
  The PAT: kubectl -n services-identity get secret iam-admin-pat -o jsonpath='{.data.pat}' | base64 -d
  The URL: kubectl -n services-identity port-forward svc/zitadel 8080:8080 (the Host header selects the instance).
Dependencies: PyYAML.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
HELMRELEASE = REPO / "apps/base/identity/zitadel/helmrelease.yaml"

# policy -> (Admin API path, response key holding the object, {config key: API field}, fields PUT accepts)
# The lockout policy is read at /policies/lockout and written at /policies/password/lockout (admin.proto).
PUT_PATH = {"LockoutPolicy": "/admin/v1/policies/password/lockout"}
POLICIES = {
    "PasswordComplexityPolicy": ("/admin/v1/policies/password/complexity", "policy", {
        "MinLength": "minLength", "HasLowercase": "hasLowercase", "HasUppercase": "hasUppercase",
        "HasNumber": "hasNumber", "HasSymbol": "hasSymbol"},
        ["minLength", "hasUppercase", "hasLowercase", "hasNumber", "hasSymbol"]),
    "LockoutPolicy": ("/admin/v1/policies/lockout", "policy", {
        "MaxPasswordAttempts": "maxPasswordAttempts", "MaxOTPAttempts": "maxOtpAttempts"},
        ["maxPasswordAttempts", "maxOtpAttempts"]),
    "LoginPolicy": ("/admin/v1/policies/login", "policy", {
        "AllowRegister": "allowRegister", "ForceMFA": "forceMfa", "AllowExternalIDP": "allowExternalIdp",
        "PasswordCheckLifetime": "passwordCheckLifetime", "ExternalLoginCheckLifetime": "externalLoginCheckLifetime"},
        ["allowUsernamePassword", "allowRegister", "allowExternalIdp", "forceMfa", "passwordlessType",
         "hidePasswordReset", "ignoreUnknownUsernames", "defaultRedirectUri", "passwordCheckLifetime",
         "externalLoginCheckLifetime", "mfaInitSkipLifetime", "secondFactorCheckLifetime",
         "multiFactorCheckLifetime", "allowDomainDiscovery", "disableLoginWithEmail", "disableLoginWithPhone",
         "forceMfaLocalOnly"]),
    "OIDCSettings": ("/admin/v1/settings/oidc", "settings", {
        "AccessTokenLifetime": "accessTokenLifetime", "IdTokenLifetime": "idTokenLifetime",
        "RefreshTokenIdleExpiration": "refreshTokenIdleExpiration",
        "RefreshTokenExpiration": "refreshTokenExpiration"},
        ["accessTokenLifetime", "idTokenLifetime", "refreshTokenIdleExpiration", "refreshTokenExpiration"]),
}
DURATION = re.compile(r"^(\d+)(s|m|h)$")


def seconds(value):
    """'12h' -> '43200s', the form the Admin API speaks."""
    m = DURATION.match(str(value))
    if not m:
        raise ValueError(f"unsupported duration {value!r}: use <n>s, <n>m or <n>h")
    return f"{int(m.group(1)) * {'s': 1, 'm': 60, 'h': 3600}[m.group(2)]}s"


def desired(helmrelease=HELMRELEASE):
    """{policy name: {API field: value}} from DefaultInstance of the HelmRelease values; only mapped keys."""
    for doc in yaml.safe_load_all(helmrelease.read_text()):
        if doc and doc.get("kind") == "HelmRelease":
            cfg = doc["spec"]["values"]["zitadel"]["configmapConfig"].get("DefaultInstance") or {}
            break
    else:
        raise SystemExit(f"no HelmRelease in {helmrelease}")
    out = {}
    for name, block in cfg.items():
        if name not in POLICIES:
            continue  # a policy this script does not manage (labels, privacy links, SMTP...) is left alone
        fields = POLICIES[name][2]
        unknown = sorted(set(block) - set(fields))
        if unknown:
            raise SystemExit(f"DefaultInstance.{name}.{', '.join(unknown)} has no mapping to the Admin API in {__file__}")
        out[name] = {}
        for key, value in block.items():
            field = fields[key]
            out[name][field] = seconds(value) if field.endswith(("Lifetime", "Expiration")) else value
    return out


def same(current, want):
    """The Admin API returns int64 as strings and durations as '<n>s'; compare as strings."""
    return str(current) == str(want)


def plan(current, want):
    """[(field, current, wanted)] of the fields that differ."""
    return [(f, current.get(f, False if isinstance(v, bool) else None), v) for f, v in want.items()
            if not same(current.get(f, False if isinstance(v, bool) else None), v)]


def call(url, host, pat, method, path, body=None):
    req = urllib.request.Request(url.rstrip("/") + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json",
                                          **({"Host": host} if host else {})})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 503 and attempt < 4:  # a pod that is still starting answers 503 before its API is up
                time.sleep(2)
                continue
            return e.code, {"error": e.read().decode(errors="replace")[:300]}
        except OSError as e:
            raise SystemExit(f"{method} {path}: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--host", help="Host header: the instance's ExternalDomain")
    ap.add_argument("--apply", action="store_true", help="send the changes (default: print them)")
    ap.add_argument("--helmrelease", default=str(HELMRELEASE))
    a = ap.parse_args()
    pat = os.environ.get("ZITADEL_PAT")
    if not pat:
        sys.exit("ZITADEL_PAT is not set (see --help); it is never taken from the command line")
    want = desired(Path(a.helmrelease))
    changed = failed = 0
    for name, fields in want.items():
        path, key, _, accepted = POLICIES[name]
        status, body = call(a.url, a.host, pat, "GET", path)
        if status != 200:
            print(f"{name}: GET {path} answered HTTP {status} {body.get('error', '')}", file=sys.stderr)
            failed += 1
            continue
        current = body.get(key, {})
        diff = plan(current, fields)
        if not diff:
            print(f"{name}: already as wanted")
            continue
        for f, cur, new in diff:
            print(f"{name}.{f}: {cur} -> {new}")
        changed += 1
        if a.apply:
            put = {k: v for k, v in current.items() if k in accepted}
            put.update(fields)
            path = PUT_PATH.get(name, path)
            status, body = call(a.url, a.host, pat, "PUT", path, put)
            if status != 200:
                print(f"{name}: PUT {path} answered HTTP {status} {body.get('error', '')}", file=sys.stderr)
                failed += 1
            else:
                print(f"{name}: applied")
    if not a.apply and changed:
        print(f"dry run: {changed} policy(ies) differ; --apply sends them")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
