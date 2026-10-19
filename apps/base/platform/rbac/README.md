# platform/rbac: named operator identities

Two `ClusterRoleBinding`s for human operators, bound to **groups** (ANSSI PA022 R38, R40):

| Group | Role | Use |
|---|---|---|
| `oa:admins` | `cluster-admin` | explicit, short-lived administration |
| `oa:readers` | `view` | routine inspection (no Secrets, no RBAC objects) |

The bindings are inert until a credential carrying one of these groups exists. Today that credential is a client
certificate issued through the CertificateSigning API (`CN=<operator>`, `O=oa:readers` or `O=oa:admins`, a few hours
`expirationSeconds`); the tool that requests it lives in `OpenAether-infra`, which is also where the break-glass
`system:masters` kubeconfig stays documented. OIDC groups are a later step (an issuer outside the cluster is a prerequisite).

What this does **not** do: a client certificate cannot be revoked, and a group binding cannot single out one holder, so the
control is the short lifetime. Removing a binding cuts every certificate of that group at once. Group names must never start
with `system:`.

Known conflict (review of B26): `OpenAether-infra` `scripts/ops/cluster-security-tests.sh` (check 3.7, no custom cluster-admin
binding) and `scripts/ops/rbac-review.sh` allow only `cluster-admin` and `cluster-reconciler`, so they fail on the `oa-admins`
binding until infra allows it by name. The brick stays in the baseline (inert without a credential); the infra change is tracked
on the infra side.

Rollback: delete the `platform-rbac` Kustomization (pruned with its two bindings); no other component references them.
