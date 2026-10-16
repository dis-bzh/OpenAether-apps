# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for a security vulnerability. Use
[GitHub's private vulnerability reporting](https://github.com/dis-bzh/OpenAether-apps/security/advisories/new)
for this repository instead — it's enabled and reaches the maintainers directly.

We'll acknowledge within a few days and keep you updated while we investigate
and fix.

## Scope

This repo holds the common Kubernetes/Flux GitOps manifests reconciled onto
clusters provisioned by `OpenAether-infra`. Findings of interest: secrets or
credentials committed to the repo, supply-chain issues in pinned dependencies
(GitHub Actions, vendored manifests, container images), and manifest
misconfigurations (RBAC, NetworkPolicy, PodSecurity) that would weaken a
deployed cluster.

## Operating a security incident

Reporting a vulnerability (above) is not operating an incident. For a suspected compromise of a cluster (a leaked kubeconfig or
secret, a departed operator, a suspicious pod), the ordered procedure is in `OpenAether-infra/docs/incident-response.md`; what to
replace on the application side, key by key, is in [`docs/key-inventory.md`](docs/key-inventory.md), and the OpenBao break-glass and
disaster-recovery runbooks are in [`apps/base/foundation/vault/README.md`](apps/base/foundation/vault/README.md).
