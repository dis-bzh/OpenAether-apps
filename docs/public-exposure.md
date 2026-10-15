# What the Gateway serves, to whom

One Gateway (`openaether-gateway`, namespace `services-gateway`) fronts every UI. It is reached two ways: the private
LB-IPAM VIP of Istio's own Service (the admin path, through the bastion tunnel), and the fixed-NodePort Service
`openaether-gateway-nodeport` behind the cloud **app load balancer**, which exists only when OpenAether-infra's
`deploy_app_lb` is true. Both land on the same listener and the same routes.

**The app LB has no source ACL.** `admin_ip` guards the Kubernetes API frontend only, and the gateway sees the LB's address,
not the caller's, so it cannot filter by source either (`apps/base/istio/authz/authorizationpolicy-explicit-allows.yaml`).
So, when the app LB is deployed, **every route on this Gateway answers anyone who sets its Host header.** What protects a
route is its own login, nothing before it. OpenAether-infra's `docs/admin-access.md` section 6 still says "nothing public":
that is wrong whenever `deploy_app_lb` is true (corrected on the infra side).

## The register

[`public-exposure.yaml`](public-exposure.yaml) lists each route, what authenticates its callers, whether it is served by
default and whether the opt-in below removes it. `scripts/check-gateway-routes.py` (CI, job "Flux DAG") fails when a route
is attached to the Gateway without a row, when a row has no authentication, when a route served by default states none, or
when the flags disagree with the manifests. A new UI therefore cannot reach the Gateway without someone writing down what
guards it. Longhorn is the example of the rule: its route has no login, so it is not in the default kustomization.

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
