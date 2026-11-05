# What the Gateway serves, to whom

One Gateway (`openaether-gateway`, namespace `services-gateway`) fronts every UI. It is reached two ways: the private
LB-IPAM VIP of Istio's own Service (the admin path, through the bastion tunnel), and the fixed-NodePort Service
`openaether-gateway-nodeport` behind the cloud **app load balancer**, which exists only when OpenAether-infra's
`deploy_app_lb` is true. Both land on the same listener and the same routes.

**By default the app LB has no source ACL** (`app_lb_allowed_cidrs` is optional, see the end of this page). `admin_ip` guards the Kubernetes API frontend only, and the gateway sees the LB's address,
not the caller's, so it cannot filter by source either (`apps/base/istio/authz/authorizationpolicy-explicit-allows.yaml`).
So, when the app LB is deployed without that ACL, **every route on this Gateway answers anyone who sets its Host header.** What
protects a route is its own login, nothing before it. OpenAether-infra's `docs/admin-access.md` section 6 still says "nothing public":
that is wrong whenever `deploy_app_lb` is true (corrected on the infra side).

## The register

[`public-exposure.yaml`](public-exposure.yaml) lists each route, what authenticates its callers, whether it is served by
default and whether the opt-in below removes it. `scripts/check-gateway-routes.py` (CI, job "Flux DAG") fails when a route
is attached to the Gateway without a row, when a row has no authentication, when a route served by default states none, or
when the flags disagree with the manifests. A new UI therefore cannot reach the Gateway without someone writing down what
guards it (the metrics paths are a separate check, below). Longhorn is the example of the rule: its route has no login, so it is
not in the default kustomization.

## Metrics endpoints: refused at the Gateway

A proof lab on Scaleway (3 CP + 6 W) read `https://grafana.<domain>/metrics` through the public Gateway: HTTP 200, 4,516 lines
(2,525 `grafana_*` series: build version, user, organisation and alert-rule totals), with no login, at 0.4.0 and at the 0.4.1
head. That lab had `app_lb_allowed_cidrs` set to the lab host, so the reading came from an allowed source: the exposure is to
any caller that reaches the Gateway, which by default (no ACL) is anyone. Same lab: `/metrics` answered 404 on the OpenBao and
Zitadel routes, and 200 on the route of a test application. Zitadel serves its metrics at `/debug/metrics` (the scrape in
`vmscrapes-pipeline.yaml`); the first lab did not probe it. A third lab did, on a cluster at the 0.4.0 tags, before the policy:
it was open there too, HTTP 200 with a Prometheus body (11,573 bytes, 53 `# HELP` lines) on the Zitadel host, read from the lab
host like Grafana's. On the 216-probe sweep below, 10 probes returned a Prometheus body at 0.4.0: 7 on Grafana (`/metrics` and 6
spellings) and 3 on Zitadel (`/debug/metrics` and 2 spellings), all on HTTPS.
Only the Forgejo route had a rule of its own for `/metrics` (no backend; it answered 500 in an earlier proof lab).

Since 0.4.1, `apps/base/services-gateway/authorizationpolicy-deny-metrics.yaml` is an Istio AuthorizationPolicy (DENY) aimed at
the Gateway for `/metrics`, `/metrics/*`, `/debug/metrics` and `/debug/metrics/*`, any caller, host and route. The answer is
a 403 from the Gateway's Envoy (read in the second lab, below). One policy rather than a rule per route: it covers a route nobody
has written yet, and it is the mechanism `allow-gateway-ingress` already uses (`targetRefs` on the Gateway, DENY evaluated before
ALLOW). The Forgejo rule stays, as a second layer. These four paths are matched as they arrive: the slash spellings (`//metrics`)
were not covered at 0.4.1 and 0.4.2, and the forge's metrics answered them. Next section.

### Slash spellings: the forge answered `//metrics` at 0.4.1 and 0.4.2 (fixed in 0.4.3)

**What was found.** A later proof lab (Scaleway, 3 CP + 6 W; the released 0.4.2 tags with the forge brick, which is opt-in, turned on by
the documented live patch; Forgejo 15.0.10) ran the first `/metrics` sweep that included the forge host: 270 probes (27 spellings x 5
hosts x HTTPS and plain HTTP), 140 answered 403 and one returned a Prometheus body. `https://forge.<domain>//metrics` answered 200 with
Forgejo's metrics: 66 metric families, 11,151 bytes, including the build information (version 15.0.10), with no credential, from the lab host.
`///metrics`, `//metrics/` and `//metrics?x=1` did the same (200, 11,152 to 11,153 bytes, read in a second file of 20 probes, 10 per listener). `/metrics`,
`/metrics/` and `/debug/metrics` gave 403, and plain HTTP on :80 gave a 301 for the `//` spellings (the redirect, not the metrics).
What the body lists is the forge's counters and build information, not repository content or a credential. **Who is touched:** a
cluster that turned the forge brick on at 0.4.1 or 0.4.2. A cluster without the brick is not (no route, no Forgejo). On the four
other hosts of that sweep no spelling returned metrics.

**Why it was not seen before.** The policy lists its paths literally and Istio's default normalisation (BASE) removes dot segments
and decodes `%6D`, but does not merge slashes; Forgejo cleans `//metrics` to `/metrics` itself. The 216-probe sweeps behind the 0.4.1
and 0.4.2 notes had no forge host (the brick was not deployed in those labs), so their sentence "the variants reach the service, none
served metrics" was true of the hosts swept and false of the forge. It is corrected in the 0.4.3 notes.

**The fix, three layers, each of which stopped every spelling on its own on the rig below:**

1. `meshConfig.pathNormalization.normalization: MERGE_SLASHES` in the `istiod` HelmRelease (`apps/base/istio/istiod.yaml`): runs of
   slashes are merged before the policy, the routes and the service see the path, so `//metrics` is `/metrics` and the existing four
   paths refuse it. One line, closes the slash family for every host present and future. **Blast radius: mesh-wide.** Every Envoy
   istiod configures reads it: the Gateway pods (the only way in for customer requests) and the `istio-waypoint`, which no workload of
   this repository uses. ztunnel is L4 and has no path. The visible change: a request path with `//` in it reaches a service merged.
   A route of yours whose service gives a meaning to `//` (an object key, a URL embedded in a path, a signed path) changes behaviour:
   reasoned, not run. Nothing in this repository routes such a service through the Gateway. Istio pushes a changed `meshConfig` to the running proxies by xDS: on the rig the effect came in 2 s with no restart of
   istiod or of the Gateway pods. `DECODE_AND_MERGE_SLASHES` (Istio's recommendation) was measured valid and stricter, and is NOT
   shipped: its extra coverage is `%2F`, no host served metrics on a `%2F` spelling, and it forwards the decoded slash to the
   application, which no client of the Forgejo package registries (scoped npm names, container registry) was run against.
2. Two more paths in `deny-metrics-paths`: `//*` and `/debug//*`. A path that starts with `//`, or has `//` right after `/debug`, is
   refused, whatever the mesh does. Istio paths match only exactly, as a prefix, as a suffix or anything, so this is the shortest
   set. A suffix pattern (`*/metrics`) was rejected: on the rig it still left 4 of 35 spellings open (`///metrics//`, `//metrics//` and
   their `?x=1` forms) and it refused a legitimate repository named `metrics` (page 303 to 403, API 200 to 403). With layer 1 the two patterns
   are inert (the path arrives merged), which is their purpose: a second mechanism with another failure mode (istiod values against
   the `services-gateway` Kustomization).
3. A token on the forge's endpoint: `[metrics] TOKEN` (`GITEA__metrics__TOKEN`, from `secret/forgejo/metrics` through the
   ExternalSecret `forgejo-metrics`), and vmagent sends it (`authorization.credentials` of the `VMPodScrape forgejo`, from the twin
   Secret in `services-observability`). Without the right bearer Forgejo answers 401, whatever the path and whoever the caller.
   Details and the upgrade of an existing forge: `docs/forgejo.md`. A Gateway API rule on the forge route (`PathPrefix //metrics`, or
   a regular expression) was tried and REJECTED: the CRD refuses `//` in a prefix, and Istio 1.31.3 orders the regular-expression
   rule after the `/` rule, so it is never reached.

`scripts/check-gateway-routes.py` pins layers 1 and 2 separately (a route that serves `/metrics` must be refused for the
slash-merged spellings by the policy alone, and istiod must merge); layer 3 is pinned by `scripts/dev/test-forgejo-brick.py`.

**Measured: rung LOCAL RIG, not a cluster of the platform.** k3s in a throwaway Docker container (Kubernetes 1.36.5), Istio 1.31.3
(base and istiod with this repository's values, one istiod replica, no ambient CNI, ztunnel or waypoint), this repository's Gateway,
NodePort Service, HTTP redirect route, default-deny, `allow-gateway-ingress` and `deny-metrics-paths` files and the real HTTPRoutes of
the forge, Grafana 13.2.4, OpenBao 2.7.1 (dev mode) and Zitadel v4.20.1 (its `/debug/metrics` is not served in the rig: only its
routing answers compare), a Go echo application, Forgejo 15.0.11 from chart 17.3.2 (SQLite and `emptyDir` in place of CNPG and
Longhorn), VM operator 0.68.1 and vmagent v1.136.0.
- *Reproduced:* a sweep of 530 probes (53 spellings x 5 hosts x HTTPS and HTTP) returned 5 Prometheus bodies, all on the forge:
  `//metrics`, `///metrics`, `//metrics/`, `//metrics?x=1` and `/\metrics` (Envoy's BASE normalisation turns the backslash into a
  slash). A fuzz of 35 slash spellings of `/metrics` on the forge: 25 leaked (66 families, 11 KB). `/metrics` 403, plain HTTP 301.
- *Other hosts:* only the forge leaked. They do not fail the same way: Zitadel and the echo application answer a `//metrics` with a
  301 to `/metrics`, and the second request is refused; Grafana sends it to its single-page fallback (302 to the login; signed in as
  admin, a 404 page, no metrics); OpenBao answers 404. That is safe by each service's router, not by design, which is why the
  Gateway layers exist. The Longhorn opt-in route was not tested.
- *After the fix:* layer 1 alone, layer 2 alone and the token alone each took the fuzz from 25 leaking to 0 (the token: the 25 answer
  401); the final run, with the files rendered from this tree, returned 0 bodies of 530. With the right token, `//metrics` answers
  200, as it must (the token is the secret).
- *Side effects seen:* normal paths identical before and after (Grafana login and health, Zitadel `openid-configuration` with its
  issuer unchanged and its login page, OpenBao UI and health, the forge home page, API and login, a `git clone`, `git push` and
  second clone over HTTPS, the echo application, and the encoded-slash set `feat%2Fx` on the forge API and `a%2Fb` on OpenBao). 4
  probes changed to something other than 403 (`//Metrics` and `///METRICS`, whose merged path now reaches the service: 301 to 404 on
  Zitadel, 301 to 200 on the echo application; neither serves metrics). On :80 the `//` spellings went from 301 to 403: the deny runs
  before the redirect.
- *Still open after the fix:* `%2F` spellings (`/%2fmetrics`, `/metrics%2f`, `/%2F%2Fmetrics`, `/debug%2Fmetrics`) reach the service
  as written: forge 303, Grafana 302, OpenBao 404, Zitadel 301 or 404, none served metrics, but a service that decodes `%2F` would.
  Case (`/Metrics`) and `;param` (`/metrics;a=b`) variants reach the service too; no host served metrics on them.
- *Not shown:* a platform cluster at the 0.4.3 head. The checks the next lab must make: the sweep on all five hosts including the forge
  with these extra spellings on both listeners before and after the move; a vmagent scrape of the forge at `up` = 1 with the token; an
  existing forge, upgraded from 0.4.2, keeps its scrape (and how long the forge pod waits for its token); the Gateway pods and
  istiod without a restart on the move. The ExternalSecret and the OpenBao path were not run on the rig (its Secrets were made by hand).

**Upgrade note.** A route of yours that serves `/metrics` or `/debug/metrics` on purpose (an external uptime or Prometheus
check, say) gets the 403 too, and no ALLOW overrides a DENY. Serve it under another path, or turn the policy off for the whole
Gateway (Grafana's and Zitadel's own metrics are then open again): delete `AuthorizationPolicy/deny-metrics-paths` (namespace
`services-gateway`) with a `$patch: delete` in a `spec.patches` of the Flux Kustomization `services-gateway`, built like
`apps/flux/components/private-admin-routes`. The inner patch renders with `kubectl kustomize` on the brick; the Flux field and
an overlay doing it have not been run, UNVERIFIED on a cluster. The checker reads the manifests, not overlays: it stays green.

**On an upgrade the refusal arrives with Flux, not with the end of the move.** In the third lab (Scaleway, 3 CP + 6 W, durations
NOMINAL; a 0.4.0 cluster moved onto the branch of the release candidate at the lab-proven apps head, the tag not existing yet,
probed every 2 s from the lab host) Grafana's and Zitadel's metrics still answered with a body until `services-gateway` applied the
policy: 2 min 38 s after the apps ref moved (the ref move is read from a poll every 5 to 6 s, so the resolution is about 6 s), 71 s
after `cluster-up` returned. All 247 probes of each path before that got a 200 with a body; the next probe, 2 s later, got a 403, and
none got a body after the first 403.

**Istio warns when it applies the policy, and a TCP listener would be denied.** Flux applied `deny-metrics-paths` without error
and the admission answered a warning (kustomize-controller log, third lab): `configured AuthorizationPolicy will deny all traffic
to TCP ports under its scope due to the use of only HTTP attributes in a DENY rule; it is recommended to explicitly specify the
port`. The policy names no port, so it covers every listener of `openaether-gateway`. Harmless today: that Gateway has an HTTP
listener on 80 and an HTTPS listener on 443 (`gateway-openaether.yaml`) and `apps/` holds no `TCPRoute` or `TLSRoute` resource (the
live listeners were not read back in the lab). A TCP or TLS listener added to it later would, by Istio's own warning, be denied.
Narrow the policy to ports 80 and 443 before adding one (`ports` in the operation; not tried on a cluster, and
`scripts/check-gateway-routes.py` counts a policy narrowed by port as no policy, so the checker needs an edit with it).

**The scrapes are not affected**: vmagent reads `VMPodScrape` and `VMServiceScrape` targets pod to pod, and none crosses the
Gateway. `scripts/check-gateway-routes.py` (CI, job "Flux DAG") fails when a `VMStaticScrape` or `VMProbe` names a Gateway host
(a `VMScrapeConfig` or an inline scrape config of the VMAgent is not read), when the policy loses one of the four paths or the slash
spellings of them (the policy alone, as if the mesh did not merge), when istiod's `pathNormalization` is not a merging one, when
the policy is not listed in its brick, is narrowed (caller, host, port, condition), aims elsewhere, or refuses ordinary paths
(`/x/metrics`, a repository or a dashboard named `metrics`, included); on a missing policy it names the routes that would forward each
path. Its fixture cases, `main()`'s wiring included, are in `scripts/dev/test-check-gateway-routes.py` (62 cases; the 0.4.2 policy and
a tree without the setting fail it for the right reason). It reads manifests, not what a service does with a path: it could not have
seen the forge.

Rung: the checker is **mocked, offline** (the manifests and its fixture cases, the policy against the CRD schema CI uses); the policy
was **observed on a cluster** in a second proof lab and again in a third, on a cluster moved from 0.4.0 (Scaleway, 3 CP + 6 W,
NOMINAL, from the lab host, no credential):

1. **Shown, for four hosts.** 216 probes (27 spellings x 4 hosts x HTTPS and plain HTTP on :80, **the forge not among them**) returned 0 Prometheus bodies. `/metrics` and
   `/debug/metrics` answered 403 "RBAC: access denied" on all four hosts (Grafana, Zitadel, OpenBao and the lab's own test
   application), on both listeners: the plain-HTTP listener answers 403, not 301. `/`, `/login`, `/api/health`, `/ui/v2/login`
   and `/.well-known/openid-configuration` were still served. After a disaster-recovery rebuild: 36 probes, 30 of them 403, 0 bodies.
   The third lab's sweep after the move: 112 x 403 and 0 bodies, against 10 bodies on the same sweep at 0.4.0 (above).
2. **Shown.** `up` = 1 for Grafana (2 pods) and Zitadel (2 pods): in the third lab the VictoriaMetrics query
   `up{job=~".*zitadel.*|.*grafana.*"}` returned those 4 series, all 1, at 0.4.0 and twice after the policy (about 3 and 10 minutes
   after it), the raw answers kept with the lab's files. The forge was not deployed in those labs: **not shown** for the forge, and with
   the token (0.4.3) its scrape is **not shown on a cluster** either: on the rig `up` = 1 with the token, DOWN (401) without it.
3. **Spellings.** The policy matched `/metrics/`, `/metrics/x`, `/metrics?x=1`, `/%6Detrics`, `/%6detrics`, `/./metrics`,
   `/a/../metrics` and `/%2e/metrics` (403): the policy sees the path after Istio's normalisation. It did **not** match `//metrics`,
   `/debug//metrics`, `/Metrics`, `/METRICS`, `/metrics;a=b`, `/metrics.`, `/metrics%20`, `/metrics%2f` and `/%2fmetrics`, which reached
   the service: Grafana redirected to its login, Zitadel and OpenBao answered 404 or a redirect, and the test application answered
   200 with its own 60-byte page, not metrics. None served metrics **on those four hosts**; the forge was not among them, and it did
   (section above). A service that treats one of these as its metrics path would still leak; the answer is then at the source:
   Grafana's `[metrics] basic_auth_username` and `basic_auth_password` (a credential in OpenBao and in the scrape: a larger change) or
   `enabled = false` (no Grafana metrics). Since 0.4.3 the `//` and `/debug//` spellings are closed at the Gateway; the case,
   `;param` and `%2F` ones are not (section above).

Not covered here: the other paths those services answer without a login (Zitadel `/debug/healthz`, `/debug/ready`,
`/debug/validate`; Grafana `/api/health`), which were not read beyond the ones above. OpenBao's `/v1/sys/metrics` needs a token on the
API listener; its unauthenticated metrics listener is a separate port that no Service of the route exposes.

## Taking OpenBao and Grafana off the Gateway (opt-in)

Not the default, because it changes what a deployed cluster serves. A cluster that wants it adds the component in its own
overlay (not in a generated profile, which `pick.py` rewrites):

```yaml
# apps/flux/<cluster>/kustomization.yaml
resources:  [../workload]       # or the profile the cluster uses
components: [../components/private-admin-routes]
```

Flux then deletes the HTTPRoutes `foundation-vault/openbao` and `services-observability/grafana` (`prune` removes the
live ones at the next reconcile). Zitadel stays routed: it is the identity provider, and Grafana's server-side calls to it
use its public host name. **What stops working**, on the private path too, since the route is gone for both:

- the OpenBao UI and API through `vault.openaether.local`. Operators use `bao_admin` (vault README: it runs inside the pod);
  ESO, cert-manager and the Jobs use the in-cluster Service and are not affected;
- Grafana through `grafana.openaether.local`, hence **Grafana's Zitadel login** (its redirect URI is that host name). Reach
  Grafana with `kubectl -n services-observability port-forward svc/grafana 3000:80` and its local admin login.

Rollback: remove the `components:` line. `scripts/dev/test-private-admin-routes.py` renders the component and applies the
patches it carries to the real bricks (exactly those two routes disappear, nothing else); that Flux merges `spec.patches`
the way it documents has not been seen on a cluster (`LAB-CHECKLIST.md`).

## What is not done here

- **A source ACL on the app LB** (`app_lb_allowed_cidrs`, default empty = today's behaviour) is the control that actually
  closes the path, and it belongs to OpenAether-infra. Filtering at the LB works because the LB sees the caller; pods that
  call `zitadel.openaether.local` through the LB leave from the cluster's own egress addresses, which the allow-list must
  include or the OIDC back-channel breaks.
- **A second Gateway for public applications**, with the admin routes kept on the first, is the clean split, and untried: a
  second Gateway has a different canonical-name label and service account, and the Cilium and Istio policies, the SPIFFE
  principal of the explicit allows, the Kyverno exclusion and the `openbao` DestinationRule all name `openaether-gateway`.
  It is also a second LB-IPAM address and a second certificate reference. Do it with a cluster in front of you.
- Gating `service-nodeport.yaml` itself behind a component would take every UI off the Internet at once, Zitadel's SSO
  included. It is a decision for the day a real public application exists.
