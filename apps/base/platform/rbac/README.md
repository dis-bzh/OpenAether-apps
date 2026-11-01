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

## Named identities in 0.4.0: what is decided

Decision (maintainer, 2026-10-21, issue #87):

- **Short-lived client certificates are the identity mechanism of 0.4.0.** `task kubeconfig-new PROVIDER=... NAME=<operator>` (OpenAether-infra) files a
  CertificateSigningRequest and writes a kubeconfig for `CN=<operator>` with the group `oa:readers` (default) or `oa:admins`; the lifetime is a few
  hours (default 8 h, from 10 minutes to 24 hours). The private key never leaves the operator's machine.
- **There is no individual revocation.** Kubernetes has no certificate revocation list for client certificates: a certificate stays valid until it
  expires. The control is the lifetime, nothing else. If a laptop is lost, the exposure is what the certificate may do until `notAfter`.
- **Deleting a binding cuts a whole group, at once.** Removing `oa:readers` or `oa:admins` (or the `platform-rbac` Kustomization) makes every
  certificate carrying that group useless immediately (the certificates stay valid but are refused), for every holder, not for one person. To
  cut one person only, bind that user by name in a separate, reviewed binding; the review script of OpenAether-infra (`rbac-review`) flags named
  subjects so they are recorded and removed.
- **Not covered:** a per-operator audit trail beyond the API server audit log, MFA, and single sign-on. They need an identity provider the API
  server can reach, which Zitadel inside the cluster cannot be at bootstrap (circular).

Plan for 1.0.0 (retargeted from 0.4.0): an **optional, user-supplied external OIDC issuer** (the operator brings their own, outside the cluster):
the API server then trusts it (groups with a prefix, so an OIDC group can never be `system:*`), and an OIDC session replaces the certificate for
people who have one. The certificate flow stays as the default for those who do not, and as the fallback when the issuer is down. Not built:
nothing in this repository configures an OIDC issuer for the API server today.

Known conflict (review of B26): `OpenAether-infra` `scripts/ops/cluster-security-tests.sh` (check 3.7, no custom cluster-admin
binding) and `scripts/ops/rbac-review.sh` allow only `cluster-admin` and `cluster-reconciler`, so they fail on the `oa-admins`
binding until infra allows it by name. The brick stays in the baseline (inert without a credential); the infra change is tracked
on the infra side.

Rollback: delete the `platform-rbac` Kustomization (pruned with its two bindings); no other component references them.
