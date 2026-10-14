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

**The 0.1.1 candidate: the fixes the first real Flux runs asked for, for fresh clusters.** Read *Upgrade notes* (a cluster
initialised by 0.1.0 cannot be moved in place) and *Not fixed* before pulling it, and the rung written under each entry: this file
distinguishes static checks, CI, local Docker, and real clouds.

**Pairing with OpenAether-infra.** Infra tags pin an apps tag and cannot be moved, so this is a table of what was proven:

| OpenAether-infra | apps | Status |
|---|---|---|
| 0.2.0 | 0.1.0 | The pair validated on 2026-10-12 (Scaleway, Flux off; one hand-driven Flux reconcile of 0.1.0 on 2026-10-13). |
| 0.2.0 | 0.1.1 | **Not supported.** Infra 0.2.0 has no operator PGP keys (OpenBao would start in development mode) and its `seed-openbao.sh` waits for a root-token Secret that 0.1.1 never creates. |
| 0.2.1 (not yet cut) | 0.1.1 | The intended pair. Validated with OpenAether-infra `f127a26` (a commit on main, not a tag), on the three clouds below. |
| main or 0.2.1 | 0.1.0 | Not supported: the seed script and `cluster-security` refuse it by design. |

### Added

- **A local Docker validation lane** (`.github/workflows/local-docker.yml`, `scripts/dev/local-docker-lane.sh`,
  `docs/local-docker.md`): on a GitHub-hosted runner, infra's own `task local-up` (1 control plane + 1 worker), Flux, then a
  conservative set of bricks reconciled from the commit under test (namespaces, platform prerequisites, External Secrets,
  cert-manager, OpenBao in dev or prod mode, Kyverno), bounded waits, read-only assertions, a pod-log leak scan, artifacts and
  an `if: always()` teardown. Rung reached: local Docker, **dev mode only**, run twice on a GitHub-hosted runner (the pull request of
  this candidate, green both times: 19 assertions after a bounded wait). **Not run:** prod mode (`openbao_mode=prod`) and the
  `workflow_dispatch` route with its inputs; they are written and statically checked. Static checks in CI: `plan` refusals,
  the workflow's shape (pinned actions, minimal permissions, step order, an always-run teardown, no `secrets.*`), and the leak scan
  against planted secrets. It is local Docker with partial bricks: it does not prove Longhorn, routable LoadBalancers, provider
  S3, Zitadel, Istio, observability or CloudNativePG, nor the `openbao` ClusterSecretStore or ClusterIssuer (no assertion reads them),
  nor the default-deny network baseline; it does not replace the cloud rung.
- Corrected the `apps/flux/local` header, which said `task local-test` "drives the whole order", and the README, which gave an
  equally wrong picture of it: for the apps it only installs Flux and applies the suspended overlay (it also checks the cluster
  itself: etcd quorum, nodes, Cilium).

### Validated

- **The 0.1.1 candidate on three clouds, 2026-10-14 (apps `883d888`, then `4405794`, then the later fix commits of this block;
  OpenAether-infra `f127a26`, a commit on main, not a tag; management role, 3 control planes + 2 workers, Kubernetes v1.37.1,
  `deploy_flux = true`, OpenBao in production mode with 3 throwaway PGP recovery keys, threshold 2).** Rung: real cloud, **once per
  cloud**, driven by hand; nothing is repeated or unattended, and `cluster-up` still ends 201 on the documented `flux-system exists`
  line until the Day-1 seeding has run.
  - **Scaleway, full DAG:** 35 of 35 Kustomizations Ready (34 apps Kustomizations plus infra's root) after the seeding. Observed:
    the PKI CSR stored in KV (`created`), `ttlSecondsAfterFinished` 3600 and `activeDeadlineSeconds` 3600 on the configure Job,
    both Jobs removed by their ttl about an hour after they ended and re-created by Flux about 1.5 minutes later, as no-ops
    (Complete in 5 to 6 s); `OpenBaoBootstrapJobFailed` **firing** for a deliberately failed `openbao-init` (with `openaether-platform` and
    `foundation-vault` suspended: suspending the child alone is reverted by its parent) and clearing after the resume (#34); the
    `CiliumLoadBalancerIPPool` at `cilium.io/v2` with 0 deprecation warnings (#49); the Kyverno cleanup Jobs in 4 s with
    `(0) reports found, no clean up needed` (#48); `loki-s3-credentials` synced with region `fr-par`, the `namespace` label, a LogQL
    marker line returned, the S3 host in Cilium's FQDN cache, Loki objects in the bucket and no `i/o timeout` (#53; two Alloy
    `status=500` lines at start-up, none after); no secret in the `foundation-vault` logs; the CNPG `promote` matrix (#36, see
    Documented). Kyverno reports on this candidate: 82 `require-security-context` (45 + 37 autogen), 3 `restrict-image-registries`
    (1 + 2 autogen, images not yet attributed) and no `restrict-privileged` (#37, #47).
  - **OVH, full DAG, Paris region with the Gravelines S3 endpoint (`s3.gra.io.cloud.ovh.net`):** 35 of 35 Ready, the same Job and
    CSR observations, `cilium.io/v2` with 0 warnings, cleanup Jobs in 3 s. The first lab of the day never got that far: every node
    lost its egress (no DNS, no NTP) behind a router whose centralized SNAT port stayed DOWN, so no apps object was ever deployed
    there; a lab with a fresh router came up in under 10 minutes. This run **found a real defect that no earlier gate could**: the
    provider-S3 egress rules named one OVH host, so Cilium dropped Loki, the CNPG backups and Longhorn's backup target (see Fixed,
    #53); after the fix Loki wrote its objects, `zitadel-db` and `grafana-db` reported `Continuous archiving is working`, and Loki
    logged no timeout.
  - **Outscale, REDUCED profile** (`vault eso certs kyverno platform-cilium-lb-ipam`, 18 Kustomizations): the shipped Outscale quota
    (20 vCPU, 40 GB) cannot hold the full DAG, so the full DAG was **not** run on Outscale. All 18 Ready, the same OpenBao and CSR
    observations, the pool at `v2`, cleanup Jobs in 6 s. Loki and the databases were not part of that profile.
  - **The stored CSR on all three clouds:** `openaether-admin` reads it (200) and is refused (exit code 2) an overwrite, a delete, a
    destroy and a metadata wipe through the `bao` CLI. The CLI strips a trailing slash, so those checks missed a raw HTTP
    `DELETE .../intermediate-csr/`, which the first version of the rule let through (found by the independent review on the pinned
    image, fixed by a prefix rule, below); the harness now sends it.
  - **CI on the pull request:** all required jobs green on the heads named above, including `openbao-bootstrap` against a real
    OpenBao (136 checks), `loki-render`, the S3 egress test, and the local Docker lane (dev mode, 19 assertions).
  - **Not observed, on any cloud:** the disaster-recovery restore on a cluster, a rolling upgrade of the OpenBao replicas, Kyverno in
    Enforce mode, the #39 CAPI child on-ramp on a real child, #38's "store not found" window (the events had expired), prod-mode
    local Docker, the `workflow_dispatch` route of the lane, an in-place upgrade of a cluster initialised by 0.1.0 (it cannot be done,
    see Upgrade notes), a Longhorn backup or restore (its target was only seen available on OVH), and anything repeated or unattended.
- **The OpenBao redesign on a real cluster, 2026-10-13: Scaleway, management role, 3 control planes + 2 workers,
  `deploy_flux = true`, an OpenAether-infra branch that is not public (production mode: 3 PGP recovery keys, threshold 2).**
  Observed: the init Job in production mode (shares encrypted to the operator keys, root token revoked, the audit trail showing the
  revocation, seal type `static`, three raft nodes); the configure Job logging in through the real Kubernetes auth method, creating 8
  policies, 8 roles, the KV and PKI engines and 7 application secrets without printing one, and a second run changing nothing;
  `seed-openbao.sh` (k8s-auth, create-only) writing five paths in 2.6 s and leaving all five alone on the second run, the replica
  carrying the replica cloud's keys; External Secrets, cert-manager and the snapshot job authenticating with their roles; the
  documented admin helper; the break-glass producing a root token from 2 of 3 PGP-decrypted shares; the PKI CSR signed by a throwaway
  root and imported with the admin identity (`set-signed` accepted the chain; the `openbao` ClusterIssuer was Ready, and no
  certificate was requested from it afterwards); a replaced pod unsealing itself in 22 s; the seal-key
  rotation procedure (two rolls of about 70 s each, run twice, once with `rollout restart`; every pod unsealing with the new key id); the Kyverno cleanup
  CronJobs finishing in 3 to 4 s (they hung for about 2m19s); no secret in the logs of the `foundation-vault` pods (the three
  OpenBao pods and the re-run Jobs: recovery-share blob, seal key, root token, JWT, seven generated values, a seeded S3 key: no match). 35 of 35 Kustomizations Ready (34 apps Kustomizations plus infra's root) at the last snapshot.
  **Found there, and fixed in this release:** the init Job's hand-over proof used its own ServiceAccount, which the bootstrap role did
  not accept (the mocked harness replaces that login), leaving the root token valid; Loki's `spec.values` overrode the seeded S3
  destination (#53); the break-glass procedure lacked its nonce; two Kyverno exemptions named the wrong namespace or registry.
  **A second run on the hardened code (same day, a fresh cluster, no re-initialisation):** the first run completed unaided (seal key
  created, production mode, the break-glass role present from the root window, root token revoked); the Kubernetes auth config pinning no
  CA served the init Job's own hand-over proof, the configure Job, External Secrets, cert-manager and the admin helper; with the seal key
  Secret deleted on purpose the init Job refused to invent a new one, and passed again once the key was put back; the narrowed admin
  identity imported the signed intermediate and was refused a PKI role (403); the break-glass produced a root token from 2 of 3 shares
  (a different pair); that day the Kyverno reports showed 0 `restrict-privileged`, 0 `restrict-image-registries` and 74
  `require-security-context` violations (the candidate run above measured 0, 3 and 82: use those); Grafana came up (the first run's
  failure was the double re-initialisation); 35 of 35 Kustomizations Ready 132 s after the seeding began (34 apps Kustomizations
  plus infra's root).
  **Not observed:** the disaster-recovery restore on a cluster (exercised on a real OpenBao container only), a rolling upgrade of the
  three replicas, Kyverno in Enforce mode, any log in Loki (it stores nothing on a cloud cluster, #53: its egress to the provider's S3 is
  blocked), the Loki side of the log search, and the 3-replica restore. **A lab artefact of the first run, not a defect:** re-initialising
  OpenBao twice regenerated the database password after the database had been created with the first one (Grafana failed to
  authenticate until the role was aligned), which is why the README says a re-init regenerates every generated secret.

### Security

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
  right and a wrong key, seal-key rotation. The Kubernetes-auth login and the 3-replica join were run on real clusters afterwards (Validated).
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
  cannot escalate: it can, by design, and says who the real root of trust is.
- The `backup/openbao` snapshot job uses `openbao:2.7.1` (was 2.5.4).
- **The stored PKI intermediate CSR is read-only for the admin identity (found on the Scaleway lab, 2026-10-14; hardened again after
  the independent review).** `configure.sh` keeps the CSR in KV at `secret/pki/intermediate-csr` (create only, so it outlives the
  Job log). `openaether-admin` holds `secret/*`, so the first version of that entry could be overwritten, deleted or destroyed by it,
  and a swapped CSR would get another key signed by the offline root. The admin policy now lets it read the entry and its metadata
  only, and the five KV paths (data, metadata, delete, undelete, destroy) are **prefix** rules: the first version matched the exact
  path, and a raw HTTP `DELETE .../intermediate-csr/` (trailing slash) fell through to `secret/*` (204, entry gone; the `bao` CLI
  strips the slash, so no cluster check saw it). The harness (136 checks) sends that request, the soft-delete and undelete
  endpoints, and reads the entry back. Rung: the harness against a real OpenBao image, then the CLI refusals (overwrite, delete,
  destroy, metadata wipe) on Scaleway, OVH and Outscale, and the raw requests (trailing-slash `DELETE` of the data and metadata paths,
  the delete and undelete endpoints) on OVH: all 403, the entry still readable. The entry is the CSR of the first generation: for a
  renewal generate a new one (runbook section 5).

### Fixed

- **Documentation statements the first real Flux run contradicted, and comments pointing at the wrong place (#50).** The 0.1.0
  lead and Known limits corrected in place (they said no real-cluster run existed); the transient `ClusterSecretStore`
  phase is not specific to an ad-hoc pick; `cnpg` is `wait: true` and reads the operator-seeded `backup/s3-primary`; the list of
  Kustomizations without a health gate is in `apps/flux/README.md` and pinned by `scripts/dev/test-flux-health-gates.sh`; the
  Job-recreation comments (a finished Job is reported unchanged unless its spec changes; `openbao-init` also expires after
  its TTL); the backup README (bucket names and who creates which bucket, the restic password is generated and never printed);
  the Flux 2.8/2.9 wording of one alert. Rung: static (docs and comments, no manifest behaviour changes). The text of the
  published 0.1.0 release notes is not changed by this commit: that remains an owner action on GitHub.
- **The `CiliumLoadBalancerIPPool` is `cilium.io/v2` (was `v2alpha1`, which the API server flags as deprecated from Cilium 1.19) (#49).**
  v2 is served since Cilium 1.18 and every pinned version is at or above it. The `CiliumL2AnnouncementPolicy` stays `v2alpha1`
  (upstream has no v2 type for it). Rung: static in CI (`scripts/dev/test-cilium-lb-ipam-api.sh` pins the apiVersion in the base and the local overlay), then real
  cloud on Scaleway, OVH and Outscale: `cilium.io/v2`, 0 deprecation warnings in the kustomize-controller log, the pool kept its blocks.
- **Loki could not push to the provider's S3 on a cloud cluster (#53, second half; the first, `spec.values` overriding the seeded
  destination, was fixed earlier).** The Loki CiliumNetworkPolicy now has `rules.dns` on its DNS rule (without it Cilium never
  learns the addresses behind the S3 FQDN rule), egress to the Loki peers and ingress on 9095 and 7946/UDP (the ring and the
  replicated write), and Loki signs with the provider's region: `us-east-1` left `spec.values`, and the ExternalSecret builds
  `region` (seeded value, else read from a Scaleway, OVH or Outscale endpoint host, else `us-east-1`). Hunks ported from a
  hardening branch that is not published. New `scripts/dev/test-loki-render.sh` and CI job `loki-render` (values through the pinned
  chart, the policy, the template's regex on fixtures). Rung: static in CI, then real cloud on Scaleway (`fr-par`) and OVH (`gra`): ESO evaluated the
  template (the region is derived from the endpoint host), Loki returned its labels and a marker line and wrote its objects to the bucket.
  On OVH this worked only after the next entry. It does NOT: give Loki its own bucket and key (the infra seed reuses the backup ones, infra #302; the comments now say
  so), change `insecure: true` with an `https` endpoint, disable the rules
  sidecar (no API egress), or fix the 2 replicas with replication factor 2 (one pod down still rejects pushes).
- **The provider-S3 egress rules named one host per provider, so a real OVH configuration (`s3.gra.io.cloud.ovh.net`) had Loki, the
  CNPG backups and Longhorn's backup target dropped by Cilium (#53, found on a real OVH cluster, not by any earlier gate).** The four
  `toFQDNs` rules (`observability`, `backup/openbao`, `backup/databases`, the `cnpg` restricted policy) allowed only
  `s3.fr-par.scw.cloud`, `s3.eu-west-par.io.cloud.ovh.net` and `oos.eu-west-2.outscale.com`, while the owner's OVH configuration
  uses the Gravelines endpoint: Cilium answered "Policy denied" to `loki-0`, `zitadel-db-1` and `longhorn-manager` (935 Loki
  `i/o timeout` lines in an hour). They now use one `matchPattern` per provider (`s3.*.scw.cloud`, `s3.*.io.cloud.ovh.net`,
  `oos.*.outscale.com`; `*` never crosses a dot), and the `longhorn` policy, which had **no** S3 rule at all (its BackupTarget sat at
  `available: false`), gets the same rule plus `rules.dns` on its DNS rule. New `scripts/dev/test-s3-egress-fqdns.py` (in CI) checks
  the five files against the regions the tfvars can name and against look-alike hosts that must stay refused, and shows that the
  former rule missed `s3.gra.io.cloud.ovh.net`. Rung: static in CI, then real cloud once on OVH: the drops from Loki and the
  database pod stopped when Flux applied the rule, Loki wrote its chunks to the bucket within seconds, both CNPG clusters reported
  `Continuous archiving is working`. It does NOT: cover OVH's high-performance class (`*.perf.cloud.ovh.net`) or an endpoint outside
  the three providers' names; Longhorn's BackupTarget turned `available: true` on OVH once the rule landed (it had been `false` since the cluster
  came up); it was not checked on Scaleway or Outscale, and no backup was taken or restored through it.
- **A Failed OpenBao configure Job never came back, its deadline did not fit its waits, and nothing alerted on it (#34).**
  `openbao-vault-bootstrap` now has `ttlSecondsAfterFinished: 3600` (like `openbao-init`) and `activeDeadlineSeconds: 3600` (its two
  15 minute waits filled the old 1800); the comments that said both Jobs are re-created on every sync now say what is true (after
  the ttl plus one sync, or when a script changes). New `OpenBaoBootstrapJobFailed` VMRule (`kube_job_failed`, 15 minutes).
  `scripts/dev/check-bootstrap-jobs.sh` (CI, with a self-test) fails on a Job with a deadline and no ttl, a deadline not above the
  waits, or an alert not naming both Jobs. Rung: static in CI, then real cloud on Scaleway: Flux re-created the removed Jobs about 1.5 minutes after their ttl, the
  `kube_job_failed` series exists, and the alert fired for a failed `openbao-init` and cleared. It does NOT make an interrupted first
  run recover: after a failure inside the root window, `openbao-init` fails again on every re-creation, and the alert can NOT show
  it on a first install (the VMRule is deployed by `observability`, which waits for OpenBao-fed secrets): read
  `kubectl -n foundation-vault get jobs`; the fix stays the README's re-init. **Side effect, handled:**
  the PKI CSR printed by the first run is in a log that disappears an hour after the Job ends, so `configure.sh` now also stores it
  (public data, create only) at `secret/pki/intermediate-csr`; read it with `bao_admin kv get -field=csr secret/pki/intermediate-csr`
  (README, runbook; the harness proves the entry exists and a second run leaves it at version 1; observed on all three clouds). A CSR generated by 0.1.0 is not
  there (it can not be read back from the pending key): generate a new one. OpenAether-infra's `docs/admin-access*.md` still say
  to read it from the Job log: an infra docs change for infra 0.2.1. `prometheusrule.yaml` was
  never in the vault kustomization: its three rules are not deployed, and the README now says so (not fixed here).
- **Kyverno Audit policies flagged pods that merely lack optional keys, and short Docker Hub names (#47).** `initContainers` and
  `volumes` are now conditional anchors (`=(initContainers)`, `=(volumes)` with Kyverno's own `X(hostPath)`), the image
  allow-list accepts the `org/image` short form of the allowed organisations (and `prom`), and `deny-hostpath` exempts
  node-exporter, ztunnel and the Istio CNI agent by pod name rather than a whole namespace. Rung: static (the render and
  the patterns' shape), then measured on the Scaleway candidate: 82 `require-security-context` and 3 `restrict-image-registries`
  results remain (images not attributed), 0 `restrict-privileged`; the policies stay in Audit and Enforce is a separate decision (#37).
  The Outscale registry (`registry.hub.docker.com/outscale`) joined the allow-list.
- **The Kyverno cleanup CronJobs downloaded kubectl at every run, which the egress policy does not allow, and could not
  fail (#48).** They now run `docker.io/alpine/k8s` (kubectl and bash, allow-listed, already run by `openbao-init`) with the
  upstream `set -euo pipefail` script untouched and nothing downloaded. Rung: real cloud, hand-run (Scaleway 2026-10-13 and the candidate runs on Scaleway, OVH and Outscale: the
  Jobs completed in 3 to 6 s, they ran 2m19s before; the log line `(0) reports found, no clean up needed` is noted in the lab
  journals, which are private). The raw Job log is not archived in the repository, so #48 stays open until the owner closes it with one. CI now runs
  `scripts/dev/test-kyverno-cleanup-render.py`: the render (image, `pipefail`, no download tool, numeric uid, writable HOME) and the
  rendered script under a stub `kubectl` (a failing `get` fails the Job, the 10000-report branch deletes). It does not run the
  image or touch a cluster.
- **Three `dependsOn` edges were missing (#38).** `storage`, `cnpg` and `observability` now wait for
  `external-secrets-stores`; the Istio `DestinationRule` of the OpenBao UI moved from `foundation-vault`, which is applied
  before Istio and fails its dry-run on a missing CRD, to `services-gateway`, which depends on Istio.
  Rung: static/mocked, not run on a cluster. `scripts/check-dependson-contracts.py` (in CI, with a mutation test) now fails when
  a Kustomization renders an `ExternalSecret` on the store `openbao`, an `istio.io` or Gateway API object without the provider in
  its `dependsOn` closure. It does not remove the "store not ready" phase (`external-secrets-stores` is `wait: false`) and the
  ordering is not measured on a cluster.
- The two OpenBao Jobs run `alpine/k8s:1.36.5` (was 1.31.1, five minors behind the cluster's Kubernetes).
- **The CAPI child on-ramp example could not be applied, and `edge-1.yaml` lacked the backup variables (#39).**
  `example-scaleway.yaml.example` is now `edge-1.yaml` with placeholders: no `HelmRepository` of its own (the invalid
  `source.toolkit.fluxcd.io/v2` and the duplicate of the shared `cilium` one are gone), the Cilium values of the proven
  child (KubePrism, cgroup, WireGuard: the old ones failed the infra parity check on 9 keys), a literal `${CLUSTER_NAME}` name
  removed, `CHILD_NAME` and the three `CHILD_BACKUP_*` set; `edge-1.yaml` gets the same three placeholders. The Scaleway
  template defaulted the control plane to Kubernetes v1.31.4 and Talos v1.9 while the workers defaulted to v1.35.3: both
  now default to v1.35.3 / v1.13 (the control plane moved four Kubernetes minors, an unsupported upgrade path: a live child that
  relied on the default must pin `K8S_VERSION` and `TALOS_VERSION` to its current values BEFORE pulling this, or its control plane
  rolls across them; the workers already defaulted to v1.35.3, `edge-1.yaml` sets both). The variable is not made required: the
  OpenStack template defaults it the same way and no child is enabled by default.
  New `scripts/check-child-clusters.py` (CI, job `Flux DAG`) with mutation tests: served apiVersions read from the vendored
  Flux CRDs, no duplicate object, no unsubstituted name, `CHILD_NAME` and `CHILD_BACKUP_*` required by the child's resolved
  profile, no public IP or UUID, example Cilium values equal to `edge-1.yaml`. Rung: static and mocked, not run on a cluster.
  It does NOT prove a Scaleway child comes up, nor that an undefined `CHILD_BACKUP_*` breaks the child as the
  `child-gitops` header claims; the issue stays open until the on-ramp is run on a real child.
- **The English migration is finished for the files the detector knows about, and the language check misses less (#40).** The five
  READMEs and runbooks that were still French (`apps/clusters`, `apps/flux`, `apps/flux/workload`, `apps/base/backup`, the PKI
  offline-root runbook) are English, translated from main's own text and reconciled with it, not copied from another branch: the
  backup README no longer says the restic password is printed in a log (it is never printed; read it with `bao_admin`) and
  gives the cluster-prefixed repository paths, the active WAL archive, the `BackupTarget` brick and the restore tests; the runbook names
  `openbao-vault-bootstrap`; the flux README reads the `# Pick:` header; the workload README lists the bricks the profile really picks; the
  clusters README says the floating IPs live in a Secret, not in git. Also English now: the OpenStack
  template header, `pick.py` runtime strings and docstrings, a dozen French comments in manifests, the `readme` value of the
  `capi-clusters-readme` ConfigMap, and two CiliumNetworkPolicy `description` fields. The render changes only there (and in a
  comment inside the `cnpg-dump` CronJob script, which rolls its pod template); every other touched brick renders byte-identical.
  `check-language.sh` now also reads inline comments, Python string literals and docstrings, YAML `readme:`/`description:`/
  `message:`/`summary:` values with their continuation lines, `*.example`/`*.hcl`/`*.json5`/`*.tpl`/`*.toml` files, flags any accented
  French letter and French elisions, knows more words (`sur`, measured at 15 hits, all French), and fails on a `.languageignore` entry
  whose file scans clean; `.languageignore` keeps only `CLAUDE.md` and `CHANGELOG.md`, French by decision (the 0.1.0 "Seven
  runbooks" known limit is closed, and the count was wrong). New `scripts/dev/test-check-language.sh` (CI) holds the fixtures.
  A second tier (`pas`, `utiliser`, `vendu`, `tel`, `quel` and a few more) applies only inside comment lines and inline comments,
  where they are prose; it found three French comments the first tier passed (translated here). Rung: static; no cluster behaviour
  depends on it. The detector is word-list based and it does NOT become complete: accent-free French outside its two lists, French in
  a string it does not read (a ConfigMap `note: |` block, a comment without a listed word) still passes. The sibling
  `OpenAether-infra` copy is not updated.

### Documented

- **`kubectl cnpg promote` printed "will be promoted" and changed nothing once (#36): observed, not explained.** On a real cluster
  (operator 1.23.1, PostgreSQL 18, Scaleway, 2026-10-14), three promotions in a row, one per plugin and no repeat: plugin 1.30.0
  promoted; about a minute later plugin 1.23.6 changed nothing (the operator had just logged "Cluster is not healthy"); plugin 1.23.1,
  later, promoted. A fault inside 1.23.6 and a promote ignored right after a switchover fit the same data, and they were not
  separated. `docs/traps.md` says so, no longer advises "a plugin at least as new as the operator", and pins or bans no plugin
  version. Operator 1.23.1 is unsupported and does not list PostgreSQL 18. Rung: real cloud once by hand (n = 1 per plugin); the
  issue stays open for the cause.

### Upgrade notes (0.1.1)

- **A cluster initialised by 0.1.0 cannot be moved to 0.1.1 in place: build a new cluster.** Tag 0.1.0 runs OpenBao with a Shamir seal,
  an HA unsealer, the shares and the root token in plain Secrets, and no `openbao-bootstrap` Kubernetes-auth role. This tag replaces
  all of it with a static seal and removes the unsealer. On such a cluster the StatefulSet rolls pod 2 onto a template that mounts
  `openbao-seal-key`, a Secret that does not exist, so the roll halts; the new `openbao-init` Job then refuses ("the Secret
  openbao-seal-key is missing but OpenBao has been initialised"), fails again on every re-creation, and the two old pods stay unsealed
  only until one restarts (the unsealer is gone). The refusal touches no data, but nothing here migrates the seal and **no upgrade of
  a 0.1.0 cluster was run**; the disaster-recovery text of the README does not apply to it. Only a cluster created by 0.1.1 (or by
  main since the redesign) is covered below.
- **On a cluster already running the redesign, pulling 0.1.1 re-runs BOTH OpenBao bootstrap Jobs once.** `configure.sh` and the
  admin policy changed, which changes the hash of the shared `openbao-bootstrap-scripts` ConfigMap, and Flux (`force: true`)
  re-creates `openbao-init` and `openbao-vault-bootstrap`. `init.sh` is idempotent (the harness runs it twice against a real OpenBao).
  Observed on Scaleway, Outscale and OVH moving between candidate commits: both Jobs re-ran and ended Complete in 5 to 31 s, finding an
  initialised OpenBao with its root token already revoked.
- **A live Scaleway CAPI child that relied on the template defaults must pin `K8S_VERSION` and `TALOS_VERSION` to its current values
  BEFORE pulling this** (the control plane default moves from v1.31.4 / Talos v1.9 to v1.35.3 / v1.13: four Kubernetes minors, an
  unsupported path). See #39 under Fixed.
- **Loki restarts once** (its Helm values and `loki-s3-credentials` changed); with replication factor 2, pushes are rejected while one
  pod is down, so Alloy logs a few `status=500` retries.
- **The configure Job now re-runs about every 70 minutes and reverts manual edits.** With `ttlSecondsAfterFinished: 3600` the Job is
  removed an hour after it ends and re-created at the next sync, and each run rewrites the 8 policies and the Kubernetes-auth roles.
  A hot edit of a policy or role (a break-glass drill, a temporary grant) does not survive: configuration changes go through git, by
  design. A transient OpenBao outage during a run can end in `OpenBaoBootstrapJobFailed`.
- **Maintainers (repository settings, owner action):** the new CI job `loki-render` (it pulls the pinned chart, so it needs the network)
  and the new steps (`check-bootstrap-jobs`, `check-child-clusters`, `test-check-language`, the Kyverno render test, the Cilium API
  test, ...) are required-check candidates; nothing in git can make them required.

### Not fixed

What this candidate leaves open, so that nobody reads a closed issue into it (issue numbers are this repository's):

- **#34, half:** the configure and bootstrap Jobs come back and the alert exists, but a first run interrupted inside the root window
  still fails again on every re-creation (re-init per the README), and the alert cannot show it on a first install.
- **#35:** a stuck CloudNativePG switchover has no automatic way out; nothing changed.
- **#36:** the cause of the `promote` no-op is unknown (Documented).
- **#37:** three Kyverno policies stay in Audit; on the candidate 82 + 3 results remain. Enforce is a separate decision, never run.
- **#38:** a CI guard and three `dependsOn` edges, not a cure: the "store not ready" phase remains (`external-secrets-stores` is
  `wait: false`) and the ordering was not measured on a cluster.
- **#39:** the CAPI child on-ramp is static only; no child was brought up from it.
- **#40:** the language detector is word-list based and cannot be complete.
- **#41:** CI still runs `gitleaks` bare.
- **#42:** no UI login through the gateway.
- **#44:** there is no per-object Flux Ready signal: `gotk_reconcile_condition` is gone in Flux 2.8, so nothing alerts on a
  Kustomization that reconciles and fails.
- **#47:** static patterns plus one cluster measurement; Enforce undecided.
- **#51:** fixed for new clusters only; there is no path from a 0.1.0 cluster (Upgrade notes).
- **#53, the rest:** Loki shares the backup bucket and key (OpenAether-infra #302); 2 replicas with replication factor 2 (one pod
  down rejects pushes); `insecure: true` with an `https` endpoint; the rules sidecar has no API egress; OVH's `perf` object storage
  and endpoints outside the three providers' names are not admitted.
- **Nothing is repeated, unattended or measured over time.** One hand-driven run per cloud; the lane runs dev mode only. This
  release does not make `task cluster-up` produce a working platform by itself: the Day-1 seeding, the PKI signing and the Grafana
  OIDC client stay manual steps.
- **From 0.1.0, unchanged:** the CAPI child clusters are scaffolding and none is enabled by default; the public Gateway has no
  perimeter in front of it; a pivot with `clusterctl move` needs the provider inventory installed some other way.

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
real cluster that this file records (July and August 2026, Scaleway and OVH) are on earlier commits. *Corrected in place on
2026-10-14 (#50): the tag itself was not reconciled, but one lab then reconciled the `management` profile on Scaleway
by hand on 2026-10-12 and 2026-10-13 (see Unreleased, Validated).* `OpenAether-infra` 0.2.0 validated its three clouds with
Flux off. Read Known limits before pulling this onto a cluster that matters.

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

- **No profile had been reconciled on a real cluster at this tag.** *Corrected in place on 2026-10-14 (#50):* the `management`
  profile was reconciled once by a lab, by hand, after the tag (Unreleased, Validated); the `workload` profile still has not been.
  The statements above name the gates that ran at the tag.
- **The pinned Flux source was not exercised on a live cluster at this tag.** `ref.name: ${GIT_REF:=refs/heads/main}` carries a
  tag or a branch (entry 1.0.0, Changed). *Corrected in place on 2026-10-14 (#50):* the lab run above used it.
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
