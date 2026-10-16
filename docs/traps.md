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

- **The `openbao` ClusterSecretStore serves only the namespaces it lists.** An
  ExternalSecret in any other one stays NotReady with a "denied by ClusterSecretStore
  conditions" error (its existing Secret stays, but stops refreshing). A new brick
  adds its namespace to `stores/clustersecretstore.yaml` and to the Kyverno audit
  exclusion; `scripts/check-externalsecret-namespaces.py` fails CI otherwise.
- **Seed OpenBao app-DB secrets BEFORE the CNPG cluster's first `initdb`.** Seeding
  late leaves the live Postgres role out of sync with the Secret.
- **Do not trust `kubectl cnpg promote` output; read the Cluster.** Measured once on a real cluster against the 1.23.1 operator
  (PostgreSQL 18 `zitadel-db`, 3 instances, Scaleway, 2026-10-14), three promotions in a row, one per plugin, no repeat: plugin
  **1.30.0** promoted the replica; about a minute later plugin **1.23.6** printed "will be promoted" and changed nothing
  (`.status.targetPrimary` never moved), and the operator had just logged "Cluster is not healthy"; plugin
  **1.23.1**, run later, promoted and the Cluster followed (#36). Two readings fit: a fault inside plugin 1.23.6, or a promote
  ignored right after a switchover. They were not separated (no repeat of 1.23.6 on a settled cluster), so nothing here pins or
  bans a plugin version. "Install a plugin at least as new as the operator" is not a remedy (1.23.6 is newer than the operator).
  In every case check `.status.targetPrimary`, `.status.currentPrimary`, the conditions, that the target replica is streaming and
  not lagging, and the `cnpg-system` operator log, and let the cluster settle between two switchovers. For 1.30.0 the two status
  fields printed empty before and after, so that promotion is evidenced by the pod roles only. Operator 1.23 is end of support and
  does not list PostgreSQL 18, which `zitadel-db` runs (`grafana-db` runs 16.3).

- **A cluster rebuilt on a bucket that still holds the old cluster's WAL archive does not archive.** CNPG's
  `barman-cloud-check-wal-archive` refuses a non-empty archive for a new cluster ("WAL archive check failed for server ...:
  Expected empty archive"), the `ContinuousArchiving` condition stays `False` with `ContinuousArchivingFailing`, and no WAL
  leaves the cluster. It happens when a lab or a rebuild reuses the bucket (and the `cnpg/<cluster>` path) of an earlier cluster.
  Either bootstrap the new cluster from that archive (recovery), or, once you are sure you do not need it, remove that path
  from the bucket. Seen on a Scaleway lab that reused its bucket (105 objects under `cnpg/`).

- **A CloudNativePG instance does not see an S3 Secret that appeared after its pod started.** `ContinuousArchiving` stays False with
  `failed to get envs: cache miss` in the instance log, although the ExternalSecret is synced. Annotate the Cluster
  (`kubectl -n foundation-databases annotate cluster.postgresql.cnpg.io <name> cnpg.io/reloadedAt="$(date -u +%FT%TZ)" --overwrite`): archiving
  came back in about a minute. OpenAether-infra's `seed-openbao.sh` does it after seeding. Seen on Scaleway when the Day-1 seeding came 30 minutes
  after the database pods.

## CAPI

- **`clusterctl move --dry-run` does not check providers.** It passes, and the real
  move then fails.
- **`clusterctl init` does not install ORC.** CAPO v0.14 needs it; without it the
  network, load balancer and floating IPs get created and no server ever does.
- **CAPO reuses an Octavia load balancer BY NAME on the next deploy.** A leftover is
  not cruft — it silently breaks the redeploy. Re-verify against the provider
  directly after a Kubernetes-level cascade.

## Observability

- **The config-reloader of a vmagent pod reads its own Secret.** An RBAC that forbids every Secret read crash-loops its init container
  (`secrets "vmagent-vmagent" is forbidden`). The namespaced Role `vmagent-config` allows that one name, read-only, and the RBAC test checks it
  is the only Secret read.
- **The Loki chart's default pod anti-affinity is `required`.** Helm merges maps, so adding a preferred rule does not remove it: three replicas on
  two workers leave the third Pending. The values clear the required list.

- **`up == 0` only catches NODE-discovered targets.** A rule on a metric nobody
  produces never fires and never says so — that is what `task check-alerts` is for.
- **kube-state-metrics needs `honorLabels: true`**, or its labels arrive as
  `exported_*` and every rule selecting on `namespace`/`pod` matches nothing.
- **A component's metrics port is often its service port** — vmselect serves both
  queries and `/metrics` on 8481. Check `up == 0` after touching an observability
  CNP. And **a `VMRule` with no `VMAlert` is inert.**
- **`repeat_interval` must be strictly greater than `group_interval`.** With both at
  5m, Alertmanager sent one webhook in eleven minutes.

## Pod security (Kyverno `require-security-context`)

- **The policy reads the CONTAINER, not the pod.** A chart that sets `runAsNonRoot` and `seccompProfile` in the pod
  `securityContext` and nothing on its containers is a violation. Measured on the 0.1.1 candidate: 82 results across the observability
  stack, cert-manager, the CNPG operator and istiod; set the container-level values per chart (`containerSecurityContext`
  or the chart's own name for it) and read the policy report again.
- **The VictoriaMetrics CRs take `spec.securityContext`, with pod and container fields in one flat block.**
  `containerSecurityContext` is not a field: the CRD keeps unknown fields, the API server accepts it, the operator ignores it,
  and nothing says so. `scripts/dev/test-container-security-context.py` fails on it.
- **`runAsNonRoot: true` alone breaks an image that runs as root.** The kubelet refuses with `CreateContainerConfigError`
  ("container has runAsNonRoot and image will run as root"). Give a numeric `runAsUser` (VictoriaMetrics: 65534).
- **Moving a workload from root to non-root leaves its local-path volume root-owned.** `fsGroup` does nothing on those volumes.
  `vmstorage` and `vmselect` then crash on their lock file / cache (apps 0.2.0 Upgrade notes, #71).
- **`Gateway.spec.infrastructure` needs Gateway API v1.2.** On the v1.1.0 CRDs this repository vendors, the dry-run of the `istio`
  Kustomization fails and everything behind it stays not Ready until the change is reverted (#70). The vendored CRDs are v1.2.1 since the 0.3.0 upgrade campaign; v1.5.x is NOT an option while Istio is 1.24 (see docs/patching.md).
- **A policy report keeps results for pods a changed `exclude` no longer covers**, at least for the first hour on a cluster
  moved from an earlier tree; a fresh cluster does not show them (#37).
