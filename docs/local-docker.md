# Local Docker lane (rung 2): partial bricks, not a substitute for the cloud

`.github/workflows/local-docker.yml` runs `scripts/dev/local-docker-lane.sh` on a GitHub-hosted runner. It needs a runner
because Talos 1.14 wants a kernel >= 6.15 and several GB of RAM; a small VPS or a laptop on an older kernel cannot host it.

## What it does

1. Checks out OpenAether-infra at the `infra_ref` input (default: a pinned commit recorded in the workflow) and runs its own
   `task local-up`: Talos in Docker, **1 control plane + 1 worker**, Cilium, no Flux, no apps.
2. `flux install`, then applies one `GitRepository` (this repository, the **commit under test**, not a branch tip) and a
   fixed set of the real Flux Kustomizations from `apps/flux/base`. Flux itself reconciles them from GitHub, so
   `dependsOn`, `wait` and the health gates are exercised, not bypassed with `kubectl apply -k`.
3. Default set (`plan` refuses a set that is not closed under `dependsOn`): `namespaces`, `platform-priority-classes`,
   `platform-gateway-api`, `platform-local-path-provisioner`, `external-secrets`, `external-secrets-stores`, `cert-manager`,
   `vault-ca`, `foundation-vault`, `cert-manager-issuers`, `kyverno`, `kyverno-policies`.
4. Waits with a bounded budget, then asserts (read-only): both nodes Ready, Cilium ready on each, every selected
   Kustomization `Ready=True`, the OpenBao init Job and bootstrap Job `Complete`, the init Job having chosen the requested
   mode (`dev`: only `openbao-recovery-dev`; `prod`: only `openbao-recovery-shares`, from three throwaway PGP public keys),
   and no secret value (seal key, recovery shares, a token shape) in any collected pod log.
5. Always collects `kubectl get/describe` and pod logs as an artifact (Secret names only, never values; a log file that
   holds a secret is deleted from the artifact and fails the lane), then tears the cluster down.

## What it proves

OpenBao's real Kubernetes-auth login and 3-replica raft join on local-path volumes, the order of the two bootstrap Jobs,
External Secrets and cert-manager starting and the `openbao` ClusterSecretStore/ClusterIssuer becoming Ready, Kyverno and its
policies installing, and a cold-start `dependsOn` ordering on a cluster that is not a cloud one.

## What it does NOT prove

Longhorn (the Docker image has no `iscsi-tools`), anything needing a routable LoadBalancer (the pool is the Docker bridge),
provider S3 or egress rules, Zitadel behind the TLS gateway, Istio ambient, observability or Loki at this size, CloudNativePG
(needs the `cluster-identity` ConfigMap that only the cloud bootstrap creates), the Cluster API providers, node rolls and PDB
behaviour, the prod-mode break-glass (the throwaway private keys are discarded), and anything about a cloud provider. A green
run is "local Docker, partial bricks"; the cloud rung is still required before a release.

## Running it

```bash
gh workflow run local-docker.yml --ref <branch> -f infra_ref=<infra-sha> -f openbao_mode=dev
gh workflow run local-docker.yml --ref <branch> -f openbao_mode=prod
```

It also runs on a pull request that touches the workflow or the script, so a change to the lane is tested from its own
branch. `task local-test` in OpenAether-infra is **not** this lane: it installs Flux and applies `apps/flux/local` with every
Kustomization suspended, so it proves the controllers start, not that a brick deploys.

Statically (anywhere): `scripts/dev/test-local-docker-lane.sh`, run by CI.
