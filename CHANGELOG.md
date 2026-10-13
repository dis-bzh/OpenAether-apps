# Changelog

Journal des changements notables d'`OpenAether-apps` (manifests communs + wiring Flux).

Format inspiré de [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Les changements d'infrastructure (OpenTofu, Talos, providers) vivent dans le
`CHANGELOG.md` d'`OpenAether-infra` — beaucoup de chantiers touchent les deux
dépôts, les entrées se répondent alors.

Ce fichier démarre au 2026-07-27 : les 200 commits antérieurs ne sont pas
rétro-documentés, l'historique git faisant foi. Les décisions structurantes de
cette période étaient consignées dans `OpenAether-infra/docs/backlog.md`, retiré
depuis (#86 côté infra) : ses tâches sont des issues GitHub, ses pièges sont dans
`docs/traps.md`.

---

## [Unreleased]

### Validated

- **The OpenBao redesign on a real cluster, 2026-10-13: Scaleway, management role, 3 control planes + 2 workers,
  `deploy_flux = true`, OpenAether-infra `claude/security-bootstrap` (production mode: 3 PGP recovery keys, threshold 2).**
  Observed: the init Job in production mode (shares encrypted to the operator keys, root token revoked, the audit trail showing the
  revocation, seal type `static`, three raft nodes); the configure Job logging in through the real Kubernetes auth method, creating 8
  policies, 8 roles, the KV and PKI engines and 7 application secrets without printing one, and a second run changing nothing;
  `seed-openbao.sh` (k8s-auth, create-only) writing five paths in 2.6 s and leaving all five alone on the second run, the replica
  carrying the replica cloud's keys; External Secrets, cert-manager and the snapshot job authenticating with their roles; the
  documented admin helper; the break-glass producing a root token from 2 of 3 PGP-decrypted shares; the PKI CSR signed by a throwaway
  root and imported with the admin identity (`set-signed` accepted the chain; the `openbao` ClusterIssuer was Ready, and no
  certificate was requested from it afterwards); a replaced pod unsealing itself in 22 s; the seal-key
  rotation procedure (two rolls of about 70 s each, run twice, once with `rollout restart`; every pod unsealing with the new key id); the Kyverno cleanup
  CronJobs finishing in 3 to 4 s (they hung for about 138 s); no secret in the logs of the `foundation-vault` pods (the three
  OpenBao pods and the re-run Jobs: recovery-share blob, seal key, root token, JWT, seven generated values, a seeded S3 key: no match). 35 of 35 Kustomizations Ready at the last snapshot.
  **Found there, and fixed in this release:** the init Job's hand-over proof used its own ServiceAccount, which the bootstrap role did
  not accept (the mocked harness replaces that login), leaving the root token valid; Loki's `spec.values` overrode the seeded S3
  destination (#53); the break-glass procedure lacked its nonce; two Kyverno exemptions named the wrong namespace or registry.
  **A second run on the hardened code (same day, a fresh cluster, no re-initialisation):** the first run completed unaided (seal key
  created, production mode, the break-glass role present from the root window, root token revoked); the Kubernetes auth config pinning no
  CA served the init Job's own hand-over proof, the configure Job, External Secrets, cert-manager and the admin helper; with the seal key
  Secret deleted on purpose the init Job refused to invent a new one, and passed again once the key was put back; the narrowed admin
  identity imported the signed intermediate and was refused a PKI role (403); the break-glass produced a root token from 2 of 3 shares
  (a different pair); the Kyverno reports showed 0 `restrict-privileged` and 0 `restrict-image-registries` violations (74
  `require-security-context` remain); Grafana came up (the first run's failure was the double re-initialisation); 35 of 35 Kustomizations
  Ready 132 s after the seeding began.
  **Not observed:** the disaster-recovery restore on a cluster (exercised on a real OpenBao container only), a rolling upgrade of the
  three replicas, Kyverno in Enforce mode, any log in Loki (it stores nothing on a cloud cluster, #53: its egress to the provider's S3 is
  blocked), the Loki side of the log search, and the 3-replica restore. **A lab artefact of the first run, not a defect:** re-initialising
  OpenBao twice regenerated the database password after the database had been created with the first one (Grafana failed to
  authenticate until the role was aligned), which is why the README says a re-init regenerates every generated secret.

### Security

- **Grafana's dashboards sidecar reads ConfigMaps only, not Secrets.** The chart's ClusterRole gave the sidecar `configmaps` and
  `secrets` in every namespace (`searchNamespace: ALL`). `rbac.create: false` drops the chart's roles and `rbac-grafana.yaml` ships
  `grafana-dashboard-reader` (core `configmaps`, get/list/watch), with `sidecar.dashboards.resource: configmap` so the sidecar does
  not list Secrets and take a 403. A new role name rather than `rbac.useExistingClusterRole`: a binding's `roleRef` is immutable,
  so repointing the chart's binding would fail the Helm upgrade. `scripts/dev/test-observability-rbac.sh` covers it. Addresses
  ANSSI PA-012 R19. Rung: chart render and kustomize build, not run on a cluster. Does not remove the accepted residual (ConfigMaps
  stay readable cluster-wide; moving the dashboards into `services-observability` and narrowing `searchNamespace` would). Lab check:
  the dashboards still load and the sidecar logs no 403.

- **Alloy no longer reads every Secret and ConfigMap in the cluster.** The chart's ClusterRole (`rbac.create: true`) also granted
  `secrets`, `configmaps`, `nodes/proxy`, events and replicasets for components this config does not use. It is now off, and
  `rbac-alloy.yaml` ships `alloy-pod-logs`: `pods` and `namespaces` (get/list/watch) and `pods/log` (get), which is what
  `loki.source.kubernetes` and `discovery.kubernetes role=pod` call (read in Alloy v1.6.1's `tailer.go`). Named differently from the
  chart's `alloy` so Helm's cleanup on upgrade cannot delete it. `scripts/dev/test-observability-rbac.sh` (new CI job) renders the
  chart and the brick and fails if any role of these service accounts reads Secrets or `nodes/proxy`. Addresses ANSSI PA-012 R19.
  Rung: chart render and kustomize build, not run on a cluster. Does not remove the accepted residual (Alloy reads pod specs and
  logs of every namespace), and does not prove that discovery still works without `namespaces`: lab check, watch the Alloy log for
  `forbidden` and that Loki still receives streams from every node.

- **Loki no longer reads ConfigMaps and Secrets cluster-wide.** The chart bound a ClusterRole on both to the Loki ServiceAccount for
  its rules sidecar (`loki_rule` ConfigMaps), which nothing here uses (no ruler). `sidecar.rules.enabled: false` and
  `rbac.namespaced: true` remove the sidecar container, the ClusterRole and its binding; `scripts/dev/test-loki-render.sh` now fails
  if either comes back. Addresses ANSSI PA-012 R19 (least privilege of the collection server) only. Rung: chart render against the
  pinned 6.25.0, not run on a cluster. The StatefulSet pod template changes, so the three replicas roll one at a time (PDB
  `minAvailable: 1`, RF 2); that roll and a Loki ruler loaded from ConfigMaps (none expected) are the lab checks.

- **vmagent no longer reads Secrets or `nodes/proxy` cluster-wide.** Its ClusterRole listed `secrets` and `nodes/proxy`; no scrape
  references a Secret (all use the pod's own `bearerTokenFile`) and the kubelet and cAdvisor are scraped on :10250 directly, which
  `nodes/metrics` authorizes. Addresses ANSSI NSA12-AUTH-02, NSA12-NET-08 and BP-028 R64. Rung: static (`kubectl kustomize`), not run on
  a cluster. Does not touch Alloy, Loki or Grafana (own entries) and cannot prove the scrapes stay up: that is the lab check
  (`up` per job before and after).

- **OpenBao bootstrap redesigned: no root token and no unseal key kept in the cluster.** The former design printed the five
  Shamir shares and the root token in the `openbao-init` log (shipped to Loki and kept), stored the shares and the root token
  in plain Secrets, and ran an unsealer that held the shares in memory. Now: `seal "static"` (a 32-byte key in the Secret
  `openbao-seal-key`, created once and never printed) unseals the pods by themselves; `sys/init` returns *recovery* shares,
  PGP-encrypted to the operators' keys in production (ConfigMap `openbao-operator-keys`) or a single labelled plaintext share in
  development; the root token lives seconds in memory and is **revoked** after the hand-over to a Kubernetes-auth bootstrap
  identity (15 minute tokens, no seal/rekey/raw/raft/audit/token-minting). The unsealer, `openbao-unseal-keys`,
  `openbao-recovery` and `bitwarden-unseal-creds` are gone. The audit device is declared in the configuration (stdout, values
  HMAC-ed). Application secrets are generated create-only (`cas=0`) and never printed. Eight least-privilege policies, with
  explicit denies; `openaether-admin` can no longer write policies, roles or mounts (git does) or mint tokens.
  Image `openbao:2.7.1` (the 2.5.4 it replaces predates published advisories: rate-limit bypass, k8s-auth renewals, PKI ACME).
  **Limit, stated in the README: the static key sits next to the data it protects**; escrow it (Day 0) or lose the data with the cluster.
  Tested against a real OpenBao by `scripts/dev/test-openbao-bootstrap.sh` (CI job `openbao-bootstrap`): both modes, root revocation,
  no secret in any output, idempotence, create-only, policy enforcement, PGP shares, break-glass, snapshot restore with the
  right and a wrong key, seal-key rotation. Not yet run on a real cluster at this entry: the Kubernetes-auth login, the 3-replica join.
- Found by the first run on a cluster (Scaleway, 2026-10-13) and fixed: the init Job proved the hand-over with its own
  ServiceAccount, which the bootstrap role did not accept, so a fresh cluster ended with the root token not revoked and the
  Job failing. The role now binds both ServiceAccounts, the calls of the root window are retried, and the root token is revoked
  on **any** exit from it. The mocked harness could not see this: it replaces the Kubernetes-auth login with a test seam.
- **Hardened after an independent review of the redesign.** A non-numeric `threshold` in the ConfigMap made the guard *error* instead of
  fail and `sys/init` ran with an empty body, leaving a live root token in a 0644 file: integers are now checked, the request is checked
  before it is sent, every file is created 0600, the response is deleted as soon as it is parsed, and a root token left by a killed
  container is revoked at the next start. The init Job refuses, before `sys/init`, a leftover recovery-shares Secret and a key that is not
  base64; it no longer invents a seal key on a cluster that was initialised (a marker ConfigMap and the pods' own answer say so); it asks
  every pod whether it is initialised and treats a failed request as "unknown", not "no". The break-glass role and policy are created in the
  root window, so they exist whenever the window did; the Kubernetes auth config pins no CA (a snapshot restored onto a rebuilt cluster,
  or a rotated CA, would otherwise lock out every login including the break-glass); the hand-over proof waits three minutes. The
  `openaether-admin` identity can no longer write PKI roles or touch the PKI root. The README no longer claims the bootstrap identity
  cannot escalate: it can, by design, and says who the real root of trust is. Harness: 124 checks.
- The `backup/openbao` snapshot job uses `openbao:2.7.1` (was 2.5.4).

### Fixed

- **Kyverno Audit policies flagged pods that merely lack optional keys, and short Docker Hub names (#47).** `initContainers` and
  `volumes` are now conditional anchors (`=(initContainers)`, `=(volumes)` with Kyverno's own `X(hostPath)`), the image
  allow-list accepts the `org/image` short form of the allowed organisations (and `prom`), and `deny-hostpath` exempts
  node-exporter, ztunnel and the Istio CNI agent by pod name rather than a whole namespace. Rung: static (the render and
  the patterns' shape); the violation count is to be re-measured on a cluster, and Enforce remains a separate decision (#37).
- **The Kyverno cleanup CronJobs downloaded kubectl at every run, which the egress policy does not allow, and could not
  fail (#48).** They now run `docker.io/alpine/k8s` (kubectl and bash, allow-listed, already run by `openbao-init`) with the
  upstream `set -euo pipefail` script untouched and nothing downloaded. Rung: static; the Job log of a run on a cluster is
  what closes the issue.
- **Three `dependsOn` edges were missing (#38).** `storage`, `cnpg` and `observability` now wait for
  `external-secrets-stores`; the Istio `DestinationRule` of the OpenBao UI moved from `foundation-vault`, which is applied
  before Istio and fails its dry-run on a missing CRD, to `services-gateway`, which depends on Istio.
- The two OpenBao Jobs run `alpine/k8s:1.36.5` (was 1.31.1, five minors behind the cluster's Kubernetes).
- **Loki can reach the provider's S3, enforces its retention and no longer rejects pushes when one pod is down (ANSSI hardening B02;
  addresses PA-012 R9, R10, R25 and hygiene M36; refs #53).** The Loki network policy gains `rules.dns` on its DNS rule (without it
  Cilium never learns the S3 addresses and the FQDN rule matches nothing), and the Loki-to-Loki egress it lacked (memberlist
  7946 tcp/udp, gRPC 9095, 3100; 9095 and 7946/udp were also missing on ingress): by reading, that alone keeps the ring from forming,
  which fits the "at least 2 live replicas required" error of 2026-10-13, but it was not observed. The hard-coded `s3.insecure: true`
  is gone (the seeded endpoint's scheme decides). The compactor is on (`retention_enabled`, `delete_request_store: s3`) with
  `retention_period: 8760h`, so nothing is deleted for a year; the 30d value was never applied. Three replicas with `replication_factor: 2`
  (was two with RF 2, where a single pod down rejects every push), a PodDisruptionBudget `minAvailable: 1`, and the canary turned off
  at the key the chart reads (`lokiCanary`, plus `test.enabled: false`, without which the chart refuses to render). A local
  overlay (`apps/base/observability-local`, selected by `apps/flux/local`) keeps one replica, RF 1, plain http and 168h.
  Rung: mocked/static (`scripts/dev/test-loki-render.sh` renders chart 6.25.0 with the shipped values and asserts the above; CI job
  `loki-render`), not run on a cluster. **It does not:** give Loki its own bucket and key (B16, infra #302: it still holds the backup
  bucket's key and the compactor can delete in it), set a per-stream or identity-log retention, add log-flow alerts or an Alloy buffer,
  or prove one compactor runs among three replicas. Anti-affinity stays preferred (clusters have 1 to 3 workers) and there is no
  topology spread (the chart exposes none for the single binary).
- **Comments and a README that contradicted the code now say what the code does (ANSSI hardening B01, text only).** The backup README
  no longer says the restic password is printed in a log (`configure.sh` prints nothing; the operator reads it once and escrows it),
  that the CNPG PITR block is commented (it is active) or that no `VMRule` covers backups (`vmrule-backup.yaml` does); the Loki
  header and ExternalSecret no longer claim a bucket separate from `backup/s3-primary` (the infra seed reuses it today, infra #302);
  the Kyverno namespace comment no longer claims `hostPID`; four seeding comments point at `configure.sh` instead of a step that no
  longer exists. Rung: static (`kubectl kustomize` output unchanged). It changes no behaviour and fixes none of the gaps it now states.
- **The daily database dump matches the server's Postgres major, and the monthly restore tests read what they restore (ANSSI hardening
  B03; addresses RW-SAUV-1, GH-M37-7, GH-M37-9 for the dump path, and PG-083 RecoPQTailleCleSym for Longhorn LUKS).**
  `cnpg-dump` ran `pg_dump` 16.3 against `zitadel-db` on 18.4; `pg_dump` refuses a newer server, so by reading the first dump failed
  under `set -eu` and nothing reached restic (not observed on a cluster; only the `BackupJobFailed` alert would have shown it). The dump
  init container and the new restore-test container run `postgresql:18.4`, Renovate now tracks them, and
  `scripts/dev/check-pg-dump-version.sh` (CI job `backup-checks`) fails when a backup client image is below any Cluster major. The
  databases restore-test restores both repositories in an init container, then runs `pg_restore --list` on every dump in a Postgres
  image; the OpenBao one unpacks the raft snapshot and runs `sha256sum -c` on its `SHA256SUMS`
  (`scripts/dev/test-backup-checks.sh`, run against fixtures and real `pg_dump -Fc` archives, includes damaged ones). A
  `RecurringJob oa-backup-daily` (backup, 14 kept) exists for Longhorn volumes but is **opt-in**: its group is `oa-backup`, which no
  volume carries, until a volume is labelled or `LONGHORN_BACKUP_GROUP=default` is set (a recurring backup with no target URL would only pile up
  snapshots; deviation from the plan's group `default`). The README now says metrics and logs are deliberately not backed up and
  documents a CNPG point-in-time recovery Cluster. The `longhorn-encryption` Secret carries `CRYPTO_KEY_SIZE 512`, `argon2id`, `sha256`
  and `aes-xts-plain64` explicitly (Longhorn's defaults are 256 and argon2i): applied at format time only, no platform volume uses an
  encrypted class yet. Rung: mocked/static (kustomize builds, the two scripts above), not run on a cluster. **It does not:** restore a
  dump into a database, an OpenBao snapshot into an OpenBao or a PITR on a cluster (the recovery manifest is written from the CNPG docs,
  untested), encrypt or re-prefix the barman archive (it would orphan the existing WAL chain), add CNPG archiver alerts, or give
  Longhorn volumes their own passphrase. Refs F-BKP-02, F-BKP-06, F-BKP-13, F-BKP-03, TLS-18 of the audit.
- **The `openbao` ClusterSecretStore serves only the platform namespaces (ANSSI hardening B04, stage 1; addresses B2-RecomDiffusion-1,
  HYG-M9, PG078-R13 and NSA12-AUTH-02 for the cross-namespace read).** Its OpenBao role reads every `secret/` path, so any
  ExternalSecret in any namespace could materialise the restic password, the Longhorn passphrase or the Zitadel master key. It now
  carries `spec.conditions[].namespaces` (`foundation-vault`, `foundation-databases`, `foundation-storage`, `longhorn-system`,
  `services-identity`, `services-observability`: explicit names, since a label is set by whoever can label a namespace; the field is
  checked against the vendored ESO v1.2.0 CRD). `scripts/check-externalsecret-namespaces.py` (CI, build job) builds every brick, the local
  overlay included, and fails on an ExternalSecret, PushSecret or SecretStore outside that list, on a ClusterExternalSecret or a second
  ClusterSecretStore, and when the store is not an explicit name list. A new brick `kyverno-policies-eso` (companion, needs Kyverno
  and ESO, own Kustomization because Kyverno rejects a policy naming a kind whose CRD is absent) adds an **Audit** policy for the same
  objects. Rung: mocked/static (kustomize builds, fixture tests of the check, `pick.py --validate/--check`), not run on a cluster. **It
  does not:** narrow the OpenBao policy (`external-secrets-read.hcl` still reads `secret/data/*` for the one role: stage 2, per-domain
  stores and path-scoped roles, waits for #54 and a lab), enforce anything in Kyverno, or grant or remove any RBAC (tenants should get none
  on ClusterExternalSecret, SecretStore or PushSecret; no tenant model exists in the repo). An ExternalSecret outside the list, for
  example in an application repository's namespace, stops refreshing until its namespace is added (its Secret stays).

### Removed

- `scripts/test-sprint0-openbao.sh` and `scripts/lib/test-helpers.sh`: an end-to-end test of the two-OpenBao design that no
  longer exists (`foundation-pki-root`).

---

## [0.1.0] — 2026-10-12

**The common platform bricks and the Flux wiring that `OpenAether-infra` can point a cluster at, validated statically and
not reconciled on a real cluster at this tag.** This is the first number the repository puts on something it measured, and
what it measured is static: `yamllint`, `pick.py --validate` (the DAG is healthy) and `pick.py --check` (the generated
profile matches it), a `kubectl kustomize` build of every brick with no object claimed by two bricks, and the Cilium
settings of the CAPI children agreeing with the foundation's (`task apps-validate` from `OpenAether-infra`). The runs on a
real cluster that this file records (July and August 2026, Scaleway and OVH) are on earlier commits. No run on a
real cluster is recorded since, and `OpenAether-infra` 0.2.0 validated its three clouds with Flux off. Read Known limits before
pulling this onto a cluster that matters.

`OpenAether-infra` 0.2.0 pins this tag in its `envs/*.tfvars.example` (`git_ref = "refs/tags/0.1.0"`). The tag replaces the withdrawn 1.0.0 below, whose entries are the
record of how the repository got here and are part of what this tag contains.

### Removed

- **`clusterctl-inventory`, a brick that claimed a fix it never delivered.** It
  writes `Provider` objects (`clusterctl.cluster.x-k8s.io/v1alpha3`), a CRD that
  `cluster-api-operator` does not install — only the imperative `clusterctl init`
  does. So it sat permanently `ReconciliationFailed` in every management
  deployment while `capi-bootstrap.md` recorded it as the fix for
  `clusterctl move` rejecting an operator-equipped target. Nothing depended on
  it, and it was the single Kustomization keeping a healthy cluster from ever
  reporting a converged DAG — which is how it was found, on 2026-08-14, by a
  check that waits for convergence instead of asserting it. Open since
  2026-07-30; dropped rather than carried as a permanent red reconcile.
  **Consequence: pivoting a management cluster onto itself with `clusterctl move`
  needs that inventory installed some other way first.**

### Fixed

- **`istiod` ran a single replica under a PodDisruptionBudget requiring one
  available, so a node could never be fully drained.** Two replicas now. Measured
  after the fix on a real Scaleway cluster: `kubectl drain` on a worker, exit 0,
  zero non-DaemonSet pods left. The six other zero-disruption budgets in the stack
  are correct and transient — this was the only one pinning a node. Infra's 1.0.0
  release note blamed all seven and has been corrected in place.
- **The release skill linked a file this repository does not have.**

### Known limits

Read these before deploying something that matters. Open items:
[the open issues](https://github.com/dis-bzh/OpenAether-apps/issues).

- **No profile has been reconciled on a real cluster at this tag.** The statements above name the gates that ran. A real
  reconcile of the `management` or `workload` profile, `deploy_flux = true` in `OpenAether-infra`, is the next thing to
  measure and is not recorded anywhere.
- **The pinned Flux source has never been exercised on a live cluster.** `ref.name: ${GIT_REF:=refs/heads/main}` carries a
  tag or a branch (entry 1.0.0, Changed); only static validation ran.
- **The CAPI child clusters in `apps/clusters` are scaffolding, and none is enabled by default.** Two management clusters
  must never read the same `apps/clusters` (README).
- **The public Gateway has no perimeter in front of it.** Nothing may be attached to it that does not authenticate its own
  callers; the Longhorn UI route is opt-in for that reason.
- **Pivoting a management cluster onto itself with `clusterctl move` needs the provider inventory installed some other
  way** (the `clusterctl-inventory` brick is gone, above).
- **Seven runbooks and READMEs are still French at an English-canonical name** (#40); the language check lists them in
  `.languageignore` and the list must not grow.

---

## 1.0.0 — 2026-08-13 — withdrawn, the tag no longer exists

**The first tag of this repository.** It moves in lockstep with
`OpenAether-infra`: infra 1.0.0 pins `refs/tags/1.0.0` here, and one version
identifies one deployable system. Until now infra tracked the `main` branch of
this repo, so a commit here could change a running cluster within the reconcile
interval, and no version named anything reproducible.

Drafted as 1.0.1, to match an infra 1.0.1 that was never tagged. Infra's 1.0.0 has
since been re-cut — it had been tagged before deploy, idempotency and upgrade were
ever exercised on the three cloud providers, which is what the number was meant to
certify — so both repositories are 1.0.0 and mean the same thing by it.

Written in English, as the repository's own rule requires. The entries below it stay
French, unrewritten.

### Changed

- **Relicensed from AGPLv3 to Apache 2.0.** The copyleft was deterring the one
  thing this project wants — people running it. A relaxation, so anything already
  obtained under AGPLv3 remains available under it.

### Security

- **The Longhorn UI is no longer published by default.** `apps/base/storage` shipped
  `httproute-longhorn.yaml` unconditionally, attaching a volume-administration UI with
  **no authentication of its own** to the public gateway, in the default `workload`
  profile. Both the route and the gateway's `AuthorizationPolicy` justified it by an
  app-LB ACL on `admin_ip` — that ACL does not exist: on Scaleway, OVH and Outscale
  alike only the k8s API frontend carries `admin_ip`, and the app LB listens on 80/443
  unrestricted (`modules/providers/{scw,ovh,outscale}/lb.tf`). Anyone setting the Host
  header could delete volumes, delete backups or repoint the BackupTarget. The route is
  now opt-in and both comments say what is actually true: there is no perimeter in
  front of that Gateway, so nothing may be attached to it that does not authenticate
  its own callers.

### Changed

- **The Flux source is pinned by ref, not by branch.** `apps/flux/base/gitrepository.yaml`
  reapplies Flux's own source from git (the gitception loop), and its hardcoded
  `branch: ${GIT_BRANCH:=main}` overwrote whatever infra laid down at bootstrap on the
  first reconcile — so a tag pinned on the infra side silently became `main` within a
  minute. It now uses `ref.name: ${GIT_REF:=refs/heads/main}`, one field that carries a
  tag or a branch, and `CHILD_BRANCH` becomes `CHILD_REF` for the child loop.
  Substituting the value rather than the YAML key is what keeps the manifest parseable
  at build time.
  Rung: statically validated — yamllint, `pick.py --validate` / `--check`. **Not
  exercised on a live cluster.**
- **No CAPI child cluster is enabled by default.** `apps/clusters/kustomization.yaml`
  listed `edge-2` (OVH) and `edge-3` (Outscale) — the DIS fleet, torn down since — so
  every management tracking this repo tried to reconcile them and inherited ten
  permanently failing Flux objects. Nothing provisioned in anyone's account (the
  non-optional `substituteFrom` fails first), but it was a broken out-of-the-box
  experience. Enable your own by renaming `example-scaleway.yaml.example`.

### Fixed

- **The documented way to regenerate the `workload` profile shrank it.** Three files
  told the reader to run `pick.py vault eso certs gateway`, four bricks, against a
  profile picked with ten — which would have pruned `cnpg`, `storage`, `identity`,
  `observability`, `kyverno` and more from what every spoke and every CAPI child
  reconciles, with `prune: true`. `--check` could not catch it: it re-derives the
  expected content from the file's own `# Pick:` header, which the stale command
  rewrites. The documented command now reproduces the committed profile byte for byte
  (verified).
- **Environment-specific values removed from tracked manifests** — a real backup bucket
  name in `apps/clusters/edge-{2,3}.yaml`, a real OMI id in `edge-3.yaml`, and a real
  Slack workspace and channel in the Alertmanager config. The last one is simply gone:
  an incoming webhook already posts to its own channel, so the override bought nothing.
- `apps/clusters` isolation guidance named `CHILD_BRANCH`, which selects what a child
  follows, not what a management reads.

### Added

- **Préfixe par cluster sur les dépôts restic** — le chemin devient
  `s3://<bucket>/<CLUSTER_NAME>/{openbao,cnpg}`. Sans lui, deux clusters
  partageant un bucket se bloquaient mutuellement : un dépôt restic n'est
  lisible qu'avec le `RESTIC_PASSWORD` de celui qui l'a créé, régénéré à chaque
  bootstrap d'OpenBao. `CLUSTER_NAME` vient du ConfigMap `cluster-identity` posé
  au bootstrap (parent) ou par `child-gitops` (enfants CAPI).
- **Alerting des sauvegardes** (`VMRule`, 3 règles). La plus utile est
  `BackupCronJobStale` : un CronJob qui ne se déclenche plus ne produit aucun
  Job en échec, donc aucun signal.
- **Test de restauration mensuel** sur les deux briques backup :
  `restic check --read-data-subset=5%` (relit et déchiffre réellement) puis
  `restic restore` vers un volume jetable, avec assertion que le résultat n'est
  pas vide — un restore « réussi » mais vide est un dépôt inexploitable.
- **PITR CNPG** (`barmanObjectStore` + `ScheduledBackup` quotidiens) : le RPO
  passe de 24 h (dump quotidien) à quelques minutes.
- **Cible de sauvegarde Longhorn** — les volumes n'en avaient aucune.
- **Service NodePort à ports figés** pour l'ingress public du Gateway.

### Changed

- **Loki** : credentials ET emplacement (endpoint + bucket) passent par son
  Secret via `valuesFrom`. Basculer du MinIO interne au S3 du provider ne demande
  plus aucun changement de code, seulement un reseed. Destination volontairement
  distincte de celle des sauvegardes — Loki n'a pas à pouvoir y écrire.
- **Kyverno** est vendoré (`kyverno-1.12.1.yaml`, sha256 en commentaire) au lieu
  d'être tiré d'une URL GitHub à chaque réconciliation.

### Fixed

- **`ipam.mode=kubernetes` rétabli sur les enfants CAPI.** Le défaut du chart
  (`cluster-pool`) ignore le `clusterNetwork.pods` déclaré par CAPI et taille les
  CIDR de pods dans `10.0.0.0/8` — le /8 où vivent les sous-réseaux de nœuds.
  C'était la cause de `apiserver → kubelet:10250` injoignable sur l'edge OVH.
- **`socketLB.hostNamespaceOnly` réaligné à `true`** sur les enfants : l'asymétrie
  parent/enfant supposée n'existait pas, le flag ne concerne que les netns
  non-root.
- **Sondes `cilium-health` autorisées** : le default-deny couvrait aussi ces
  endpoints, si bien que « Cluster health » affichait 1/N sur des clusters sains.
  Ce faux signal avait fait condamner un enfant à tort.
- **`HelmRepository cilium` sortie de `edge-1.yaml`** : ressource partagée par
  tous les enfants, elle rendait chacun dépendant de l'activation d'edge-1.
- **AuthorizationPolicies de `foundation-storage` scindées** en brique compagnon :
  appliquées dès qu'Istio était pioché, elles cassaient tout profil « istio sans
  storage » — celui de tous les enfants.
- **`CHILD_BRANCH` retiré** des enfants : la branche référencée n'existait plus.
- **`remediation` sur les HelmReleases du chemin critique** : sans elle, Flux ne
  retente jamais une install échouée (istio-cni est resté 3 h en échec après
  correction de sa cause).
- **Timeout Helm de Longhorn porté à 15 min** : le défaut de 5 min expirait en
  cloud, déclenchant un uninstall de remédiation.

### Notes d'exploitation

Plusieurs briques portent un `postBuild.substituteFrom` **volontairement isolé**
dans une Kustomization qui ne rend que des ConfigMaps ou des Settings. La
substitution Flux s'applique à TOUT le rendu d'une Kustomization : fusionnée avec
une brique contenant des scripts, elle viderait leurs variables shell
(`$PRIMARY_ENDPOINT`, `$VOL_DIR`, `${datasource}` des dashboards Grafana…). Ne
jamais fusionner ces ressources — c'est écrit sur place dans chaque fichier
concerné.
