# Kyverno: flipping a policy to Enforce, one namespace at a time

> Addresses ANSSI FT082 R1, R4, R8 and R15 in part, NSA12 POD-01 and POD-05, SSDLC-CONT-REC04. **Everything here ships in Audit.**
> Nothing in this repository has been run on a cluster for it: the mechanism is checked by `scripts/dev/test-kyverno-enforcement.py`
> and the Kyverno CLI fixtures, the rest is in the maintainers' lab checklist. This is not a claim of compliance with any ANSSI document.

## The switch

Five policies can be enforced where, and only where, a namespace carries a label (Kyverno 1.12.1 `spec.validationFailureActionOverrides`;
`validate.failureActionOverrides` is 1.13+ and the vendored version rejects it). No namespace in this repository has one, so the
default is what ran before: Audit everywhere.

| Policy | Label (value `"true"`) | What it denies |
|---|---|---|
| `restrict-privileged` | `kyverno.openaether.io/enforce-restrict-privileged` | privileged, hostNetwork/PID/IPC, hostPath (init containers included) |
| `restrict-capabilities` (+ `restrict-capabilities-ephemeral`) | `kyverno.openaether.io/enforce-restrict-capabilities` (one label flips both) | the sensitive `capabilities.add` list, ephemeral containers included (their own policy: Kyverno 1.12.1 refuses a subresource match with background scanning on) |
| `require-security-context` | `kyverno.openaether.io/enforce-require-security-context` | no drop ALL, no `allowPrivilegeEscalation: false`, no runAsNonRoot, no seccomp |
| `restrict-image-registries` | `kyverno.openaether.io/enforce-restrict-image-registries` | an image outside the organisation list |
| `require-limits` | `kyverno.openaether.io/enforce-require-limits` | no memory limit |

```
kubectl label ns <namespace> kyverno.openaether.io/enforce-restrict-privileged=true     # flip
kubectl label ns <namespace> kyverno.openaether.io/enforce-restrict-privileged-         # rollback, takes effect at once
```

To keep it in git, put the label on the Namespace manifest. Rollback is then one commit. `disallow-root-user` and
`disallow-default-namespace` already enforce and are not touched; `require-ro-rootfs` stays informational.

Rules that can be enforced match the Pod at **CREATE only**, so an update of a pod that already runs (a CNPG label patch, a finalizer) is
never denied. The consequence in Audit: a pod that changes after creation is no longer re-reported at admission (the background scan
still covers it). The ephemeral-container rule (`restrict-capabilities-ephemeral`) is the exception: `kubectl debug` is an UPDATE by nature.

## When to flip, in this order

The gate for each (policy, namespace) pair, from the audit plan: **zero `fail` results for it in the PolicyReports after one full
release AND after a node reboot or a Talos upgrade**. PSA and Kyverno judge a pod when it is created, so a pod admitted long ago is
only judged again when it is rescheduled: the External Secrets failure of 2026-08 appeared on the second scheduling, not the first.

```
kubectl get polr -A -o json | jq -r '.items[] | .metadata.namespace as $n | .results[]? | select(.result=="fail")
  | [$n, .policy, .rule, .resources[0].name] | @tsv' | sort | uniq -c
```

1. Policy order: `restrict-privileged` (highest value, cheapest), `restrict-capabilities`, `require-security-context`,
   `restrict-image-registries`, `require-limits` last (a limit chosen blind OOM-kills the workload).
2. Namespace order, newest and least shared first: `services-observability-audit`, `services-observability-node`, `services-identity`,
   `foundation-pki-management`, `foundation-databases`, `foundation-vault`, `cnpg-system`, `cert-manager`, `services-observability`,
   `services-gateway` last (istiod generates its Envoy Deployment: two proxies failed `require-security-context` on a fresh OVH
   cluster, they need Gateway API v1.2, see CHANGELOG 0.2.0).
3. Never label `kube-system`, `kyverno`, `flux-system`, `longhorn-system`, `local-path-storage`, `istio-system`, `foundation-service-mesh`,
   `foundation-networking`, the `capi-*` and `orc-system` namespaces: they hold infrastructure the policies already exempt by namespace
   or by name, and a Kyverno outage must never be able to block them. `restrict-image-registries` in particular is for **application
   namespaces only**.

The exceptions are named, never a whole namespace where avoidable: node-exporter (`services-observability-node`), `alloy-audit-*`
(`services-observability-audit`), `ztunnel-*` and `istio-cni-node-*` (`istio-system`, `foundation-service-mesh`). Cilium, Longhorn
and local-path are exempt through their namespaces (`kube-system`, `longhorn-system`, `local-path-storage`). A new exception is a
reviewed edit of the policy, with the reason in the comment.

## A Kyverno outage

The webhook of a policy that matches Pods is `failurePolicy: Fail` (the Kyverno default), and `disallow-root-user` already enforces on Pods,
so enforcing a second policy adds no new way to be locked out: with the admission controller down, pod creation is refused in every
namespace the webhook matches, `kube-system` and `kyverno` excepted (`resourceFilters`). `KyvernoAdmissionDown` alerts. The vendored
install runs one admission replica: **Kyverno HA is not done here** and belongs with the Kyverno upgrade. Until then, flip no more than one
policy per release. Note `KyvernoPolicyViolation` ignores `require-security-context`, so a denial by that policy does not page: watch
`kyverno_policy_results_total{rule_result="fail"}` and the Events of the namespace by hand after its flip.

## Pod Security Admission

The namespaces that still enforce baseline can move to restricted with the opt-in component `apps/flux/components/psa-restricted`
(foundation-networking, foundation-policy-engine, services-identity, capi-system, capi-operator-system, capi-outscale-system), after
their `warn`/`audit` = restricted lines are clean, with the same reboot gate. PSA covers init containers, ephemeral containers,
hostPorts and capabilities without a webhook. `kyverno` stays at baseline for the reasons written on its Namespace manifest.

Four namespaces enforce `privileged` as released although nothing privileged runs in them: services-observability,
services-gateway, foundation-service-mesh and foundation-storage. The opt-in component `apps/flux/components/psa-baseline` lowers
them to baseline (`warn`/`audit` restricted are already set, so their logs are readable before the flip). Gate: a server-side dry run,
`kubectl label --dry-run=server --overwrite ns <ns> pod-security.kubernetes.io/enforce=baseline`, for each, then a staging cluster
and a node reboot. Rollback: remove the component, or relabel `privileged`.

## Not done

`verifyImages` and image signatures (docs/supply-chain.md: the pinned Kyverno and CNPG releases are not signed), digest pins, `LimitRange` and
`ResourceQuota`, Kyverno HA, and any flip to Enforce on a measured cluster.
