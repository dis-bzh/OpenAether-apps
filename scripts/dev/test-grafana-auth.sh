#!/usr/bin/env bash
# Grafana's authentication settings, on what the manifests really produce: the base brick, the SSO-only overlay
# (default-off, docs/zitadel-hardening.md) and the chart rendered with both. The base must keep the local login
# and the permissive role mapping (nothing changes for a cluster that does not opt in); the overlay must flip
# exactly those, and the role expression must say what it means on sample claims.
# Needs kubectl, python3 with PyYAML and jmespath; helm and the network for the chart render (skipped with
# GRAFANA_AUTH_NO_HELM=1). Rung: mocked/static.   Usage: scripts/dev/test-grafana-auth.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for bin in kubectl python3; do command -v "$bin" >/dev/null || { echo "$bin is required" >&2; exit 2; }; done
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
kubectl kustomize "$ROOT/apps/base/observability" > "$WORK/base.yaml"
kubectl kustomize "$ROOT/apps/base/observability-sso-only" > "$WORK/only.yaml"

python3 -I - "$WORK" "${GRAFANA_AUTH_NO_HELM:-0}" <<'PY'
import copy, subprocess, sys, yaml
try:
    import jmespath
except ImportError:
    sys.exit("jmespath is required (pip install -r scripts/ci-requirements.txt)")
work, no_helm = sys.argv[1], sys.argv[2] == "1"
fails = 0
def check(label, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + label)
    fails += 0 if ok else 1

def release(env):
    for d in yaml.safe_load_all(open(f"{work}/{env}.yaml")):
        if d and d.get("kind") == "HelmRelease" and d["metadata"]["name"] == "grafana":
            return d
    sys.exit("no grafana HelmRelease in " + env)

base, only = release("base"), release("only")
bi, oi = base["spec"]["values"]["grafana.ini"], only["spec"]["values"]["grafana.ini"]
bo, oo = bi["auth.generic_oauth"], oi["auth.generic_oauth"]

print("--- the base brick (what runs today, plus the session and log settings)")
check("sessions: 2h idle, 12h in all", bi["auth"] == {"login_maximum_inactive_lifetime_duration": "2h",
                                                      "login_maximum_lifetime_duration": "12h"})
check("the local login form is NOT hidden", "disable_login_form" not in bi["auth"])
check("the role mapping is permissive: strict false and a Viewer fallback",
      bo["role_attribute_strict"] is False and "|| 'Viewer'" in bo["role_attribute_path"])
check("every HTTP request is logged, as JSON", bi["server"].get("router_logging") is True and bi["log.console"]["format"] == "json")
check("SSO still creates the user at first login", bo["allow_sign_up"] is True and bo["use_pkce"] is True)
check("the server root_url and the database survive the section move",
      bi["server"]["root_url"] == "https://grafana.openaether.local" and bi["database"]["type"] == "postgres")

print("--- the SSO-only overlay (off by default)")
check("it hides the local login form", oi["auth"].get("disable_login_form") is True)
check("it keeps the session lifetimes", oi["auth"]["login_maximum_lifetime_duration"] == "12h")
check("the role mapping is strict", oo["role_attribute_strict"] is True)
path = oo["role_attribute_path"]
roles = lambda *names: {"urn:zitadel:iam:org:project:roles": {n: {"org1": "d"} for n in names}}
ev = lambda claims: jmespath.search(path, claims)
check("grafana-admin gives Admin", ev(roles("grafana-admin")) == "Admin")
check("grafana-viewer gives Viewer", ev(roles("grafana-viewer")) == "Viewer")
check("both: Admin wins", ev(roles("grafana-viewer", "grafana-admin")) == "Admin")
check("any other role gives nothing (strict then denies)", not ev(roles("something-else")))
try:
    none = ev({"sub": "x"})
except Exception:
    none = None
check("no roles claim at all gives nothing", not none)
rest_b = copy.deepcopy(base); rest_o = copy.deepcopy(only)
for r in (rest_b, rest_o):
    r["spec"]["values"]["grafana.ini"]["auth"].pop("disable_login_form", None)
    r["spec"]["values"]["grafana.ini"]["auth.generic_oauth"].pop("role_attribute_path")
    r["spec"]["values"]["grafana.ini"]["auth.generic_oauth"].pop("role_attribute_strict")
check("nothing else differs between the base and the overlay", rest_b == rest_o)

if no_helm:
    print("skip chart render: GRAFANA_AUTH_NO_HELM=1")
else:
    print("--- the chart rendered with each (grafana.ini as Grafana will read it)")
    for env, rel in (("base", base), ("only", only)):
        spec = rel["spec"]["chart"]["spec"]
        yaml.safe_dump(rel["spec"]["values"], open(f"{work}/{env}-values.yaml", "w"))
        out = subprocess.run(["helm", "template", "grafana", spec["chart"], "--repo", "https://grafana.github.io/helm-charts",
                              "--version", spec["version"], "-n", "services-observability", "-f", f"{work}/{env}-values.yaml"],
                             capture_output=True, text=True)
        if out.returncode:
            check(f"helm template ({env}) failed: {out.stderr.strip()[-200:]}", False)
            continue
        ini = ""
        for d in yaml.safe_load_all(out.stdout):
            if d and d.get("kind") == "ConfigMap" and "grafana.ini" in (d.get("data") or {}):
                ini = d["data"]["grafana.ini"]
        check(f"{env}: [auth] carries the lifetimes", "login_maximum_inactive_lifetime_duration = 2h" in ini
              and "login_maximum_lifetime_duration = 12h" in ini)
        check(f"{env}: [log.console] format = json and router_logging = true", "[log.console]\nformat = json" in ini
              and "router_logging = true" in ini)
        check(f"{env}: disable_login_form is " + ("set" if env == "only" else "absent"),
              ("disable_login_form = true" in ini) == (env == "only"))
        check(f"{env}: role_attribute_strict = " + ("true" if env == "only" else "false"),
              f"role_attribute_strict = {'true' if env == 'only' else 'false'}" in ini)
print(f"{fails} failed")
sys.exit(1 if fails else 0)
PY
