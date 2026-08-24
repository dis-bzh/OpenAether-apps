# Traps worth remembering

One line each; the detail lives in the referenced file or in the git history that
fixed it. These were paid for once — the point of writing them down is not paying
again.

Flux and Kustomize traps live in the `flux-dag` skill, next to the DAG rules they
belong to. What follows has no skill of its own yet.

## Network policy

- **`toServices: kubernetes` gets dropped.** The Service DNATs 443→6443 and Cilium
  enforces on the post-DNAT port. Use `toEntities: kube-apiserver`.
- **A CNP with no egress-to-S3 rule does not fail fast, it hangs.** CNPG's own
  instance pods need it for `barmanObjectStore`; a replica JOIN calling
  `restore_command` blocks for ever instead of erroring.

## Databases and secrets

- **Seed OpenBao app-DB secrets BEFORE the CNPG cluster's first `initdb`.** Seeding
  late leaves the live Postgres role out of sync with the Secret.

## CAPI

- **`clusterctl move --dry-run` does not check providers.** It passes, and the real
  move then fails.
- **`clusterctl init` does not install ORC.** CAPO v0.14 needs it; without it the
  network, load balancer and floating IPs get created and no server ever does.
- **CAPO reuses an Octavia load balancer BY NAME on the next deploy.** A leftover is
  not cruft — it silently breaks the redeploy. Re-verify against the provider
  directly after a Kubernetes-level cascade.

## Observability

- **`up == 0` only catches NODE-discovered targets.** A rule on a metric nobody
  produces never fires and never says so — that is what `task check-alerts` is for.
- **kube-state-metrics needs `honorLabels: true`**, or its labels arrive as
  `exported_*` and every rule selecting on `namespace`/`pod` matches nothing.
- **A component's metrics port is often its service port** — vmselect serves both
  queries and `/metrics` on 8481. Check `up == 0` after touching an observability
  CNP. And **a `VMRule` with no `VMAlert` is inert.**
- **`repeat_interval` must be strictly greater than `group_interval`.** With both at
  5m, Alertmanager sent one webhook in eleven minutes.
