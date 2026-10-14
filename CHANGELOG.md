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

- **The OpenBao seal-key escrow is proven by a hash, needs two custodians, and a missing or stale attestation raises an alert**
  (ANSSI F-BKP-05 / GH-M37-5, DP-02 / B2 recovery rule, PA-022 R45 in part). Day 0 now escrows the key with at least two
  recipients or as a k-of-n split, and ends with a step that reconstructs the key from the escrow, compares its sha256 with the live
  Secret's and only then writes the ConfigMap `openbao-escrow-ack` (date, hash, number of holders). A new CronJob
  `openbao-escrow-check` (foundation-vault, daily, `get` on three ConfigMaps and never on the Secret) fails on a production
  cluster that has no attestation a week after initialisation, a malformed one, fewer than two holders, or one older than 400
  days; the VMRule `OpenBaoEscrowNotAttested` (warning) fires when it has not succeeded for 36 h. A development cluster passes.
  `scripts/dev/test-escrow-check.sh` (36 assertions, run on the host and under the CronJob's image, wired into CI) caught a real
  flaw in a first draft: an unreachable API read as a development cluster and passed. **Mocked/static: the CronJob, the alert and
  the ConfigMap flow were not run on a cluster, `kube_cronjob_status_last_successful_time` was not checked against the deployed
  kube-state-metrics, and the `ssss`/`gfsplit` split was not tried.** It does NOT detect an escrow of an old key after a rotation
  (the job never reads the key; compare the hash by hand), run the restore drill, or move the key out of the cluster (the
  `transit` seal stays a recorded deviation). The `get` on `openbao-seal-key` in the init Role stays: `init.sh` needs it on every
  re-run to refuse inventing a second key. The infra half (an `escrow` task, the cluster-verify check, the runbook twins) is not in
  this repository.
- **A key inventory, the replacement procedure of every application key, the root-CA custody options and the backup policy are written
  down** (ANSSI B2 impact/crypto-period/erasure rules, TLS-12, TLS-20, DP-06, DP-07, F-BKP-07, F-BKP-16). `docs/key-inventory.md`
  lists each key the applications repository creates (purpose, location, algorithm, period, impact of loss and of leak, replacement,
  archive) and the recorded deviations (software seal key and AES-GCM wrap, listener CA, Istio CA, root custody); the offline-root
  runbook is now English, offers a token/HSM or offline media (a password manager stays as a recorded deviation), signs with
  SHA-384, and covers compromise and erasure (`shred` is dropped as unreliable on SSDs); the backup README gains a per-dataset
  policy table (schedule, RPO, retention, destination, key holder, restore test, destruction), the order of a full recovery
  (OpenBao before the Zitadel database, because the masterkey lives in OpenBao) and is translated; `SECURITY.md` points to the
  incident procedure. The Longhorn comment that called a passphrase change harmless now says not to rotate while encrypted volumes
  exist. **Docs only: the OpenSSL commands of the runbook were run on a scratch directory (ECDSA P-384 root, SHA-384 signature of a
  CSR, verification); nothing else was run, and no replacement procedure was run on a cluster.** The token/HSM commands, the
  root-compromise path and the transit-seal migration are untried. It does NOT change any manifest, add a key period the code
  enforces, or claim compliance. Grafana's `secret_key` is unset (upstream's public default): listed as a gap, not changed.
  The links to `OpenAether-infra/docs/{key-management,incident-response,disaster-recovery}.md` point at documents the infra half of
  this work writes: they do not exist on the infra branch yet.
- **Zitadel no longer installs with its public admin password, and its authentication policy is written down.** A new instance used to
  get `zitadel-admin` / `Password1!` (upstream's default, reproduced: it logs in as IAM_OWNER, and Grafana's admin role hangs off that
  account) and Zitadel's open defaults: 8-character passwords, no lockout, no MFA, self-registration open, 12 h tokens. Now `configure.sh`
  creates `secret/zitadel/admin` (create only; 28 random characters and `Aa1!` so Zitadel's policy accepts it), External Secrets merges
  it into `zitadel-secret-config`, and `DefaultInstance` sets 15 characters, lockout at 5 (password and OTP), MFA forced, no
  self-registration, no external IdP, 12 h check lifetimes and 1 h tokens. Password hashing moves to argon2id (ANSSI PA-079 R29 asks
  for a memory-hard function; the audit's PBKDF2 reading was R29-, the fallback) with the `bcrypt` verifier, applied to running
  instances at the next rollout. Logs are JSON and the HTTP access log goes to stdout. **What an existing cluster needs, said
  plainly:** `FirstInstance` and `DefaultInstance` are read once, at instance creation, so on a running Zitadel they change nothing:
  `zitadel-admin` may still have `Password1!` (`docs/zitadel-hardening.md` has the check), and
  `scripts/zitadel-apply-policies.py` (dry run by default, same values as the HelmRelease) applies the policies through the Admin
  API. The OpenBao value is the initial password, not the current one. Grafana: sessions 2 h idle / 12 h (were 7 / 30 days),
  request log as JSON; the SSO-only mode (local form hidden, strict role mapping with a `grafana-viewer` role) is the overlay
  `apps/base/observability-sso-only`, **off**. Addresses ANSSI PA-079 R10, R12, R21, R23, R29, PG-078, PA-012 R27, BP-028 R51 in
  part. Rung: **a real Zitadel v4.14.0 against PostgreSQL in containers** (`scripts/dev/test-zitadel-config.sh`, 31 checks, 1 min:
  the default admin logs in before and not after, the generated password is accepted, the policies land on a new instance and not
  on an old one, the apply script and the values agree, lockout at 5 and the PAT unlock, argon2id with and without the verifier
  (without it nobody logs in), JSON and access logs, no token in them) and a real Grafana 11.3.1 on the rendered ini; the
  bootstrap test (127 checks) covers the generated password. Not run on a cluster, with OpenBao, External Secrets or Istio.
  **Not done, on purpose:** the Vault and Grafana routes stay on the shared Gateway (removing them takes the Grafana UI away
  from operators; the load-balancer ACL is infrastructure), no password expiry (Zitadel's enforcement unchecked), no breached-password
  check, no per-audience Loki tenant, no export of authentication events to Loki (the access log says a login page was hit, not by
  whom), and the chart's `iam-admin` key and PAT stay valid to 2029 (they are the break-glass). Lab check: a fresh cluster's setup
  Job completes with the generated password, and the `zitadel-secret-config` ExternalSecret on an existing cluster before the key
  is seeded.

- **An inventory of what is deployed, a scan of it, and the image pins checked.** `scripts/dev/gen-sbom.py` writes a CycloneDX 1.5 file from
  the rendered DAG: the 15 Helm charts, 14 images from the manifests and, with `--helm`, about 38 images with those the charts render
  (`valuesFrom` targets get a placeholder). The new `SBOM` workflow runs weekly and on each release, attaches it as
  `openaether-apps-<tag>.cdx.json` and scans every image with Trivy (CRITICAL with a fix upstream fails; accepted findings go in
  `.vex/openaether-apps.openvex.json`, empty today). `scripts/check-image-pins.py` fails CI on an image with no tag or a moving one. Renovate's
  `kubernetes` manager is turned on for `backup` and `foundation` with `pinDigests`, so the next bumps write `tag@sha256`
  (it tracked none of these images before). `restrict-image-registries` (still **Audit**) lists organisations instead of
  whole registries (`gcr.io` and `docker.io/registry.k8s.io` dropped), read from the inventory. **No `verifyImages` policy:** checked
  with cosign against the pinned versions, Kyverno 1.12.1 and CNPG 1.23.1 have no signature at all, cert-manager 1.15.3 no keyless one,
  and only Flux and External Secrets verify (`docs/supply-chain.md` has the table and the order that follows). A
  `repo-settings.yml` workflow runs infra's settings check (80 canned-API cases in CI; `--no-sandbox` added). Addresses ANSSI
  SBOM25-1/-2/-3, SSDLC CICD-AIP-01a/-01b/-03, ART-REC03, CONT-REC05, SCM-REC06/-07 in part. Rung: the generator, the check and the
  tests ran locally (including `helm template` of all 15 charts); not the workflows on GitHub, not Trivy on the images, not Renovate's
  new manager. **Not done:** the rulesets as JSON (they need an admin export), signature verification, `--require-digest`, any
  image upgrade. Lab check: the PolicyReports of `restrict-image-registries` and the `KyvernoPolicyViolation` alert after the org list lands.

- **A patch policy and an end-of-support calendar, with a check.** `docs/patching.md` states the rule (a security fix merged or waived in
  writing within 30 days, 7 for an exposed path) and the signals, and says plainly that it is not met today. `docs/eol-calendar.yaml`
  (rendered into `docs/eol-calendar.md`) has a row per HelmRelease, vendored dump and pinned image (33), each with the pin read from its
  file, the upstream line, its end-of-support date from endoflife.date or the project's page (`unknown` where none was found, not
  assumed), an owner and a review date. `scripts/check-eol.py` fails CI when a pin and its row differ or a chart or dump has no row;
  dates only annotate, and a weekly `End of support` workflow fails on them (`--strict`). Six lines are already past upstream support
  (Istio 1.24, cert-manager 1.15, Kyverno 1.12, the CNPG operator 1.23, Grafana 11.3, Loki 3.3) and carry a waiver to 2026-12-31, a
  date this change proposes and the owner sets; Longhorn 1.9 ends 2026-11-27. Found while reading the pins: the `vendored from vX`
  comments Renovate maintains name the latest release, not the vendored one (CNPG says v1.30.0 over a 1.23.1 dump), and
  `grafana-db-cluster.yaml` still pins PostgreSQL 16.3. Addresses ANSSI HYG-M34, HYG-R34, HYG-R35 and NT-35 R2 in part. Rung:
  static (20 fixture cases in `scripts/dev/test-check-eol.py`, the check run on the tree), dates read from the public feeds on
  2026-10-14. **Does not upgrade anything:** each hop is a cluster exercise, one minor per PR; whether the old releases carry
  unpatched advisories was not checked.

- **The apps CI gets the checks the infra CI has, and every PR gets a run.** `pull_request` no longer filters on `branches: [main]` (a PR
  stacked on another PR's branch got no run at all, measured on OpenAether-infra PR #100; `edited` is added so a retarget re-runs).
  `persist-credentials: false` on all checkouts. New: actionlint; kubeconform (`-strict`, CRD schemas from a pinned commit of the
  community catalog) and `trivy config` (HIGH, CRITICAL) on the rendered output of every DAG path, with 18 accepted findings, all in
  vendored upstream dumps or one false positive, listed with a reason in `.trivyignore.yaml`; Plumber (pipeline audit); Conventional
  Commits, no-commit-authored-by-a-tool and no-tool-authorship-trailer checks; an environment-data gitleaks pass over what a PR adds
  (the rules are tested against known-good and known-bad lines first). yamllint, PyYAML, commitizen, the Renovate validator, actionlint
  and kubeconform are pinned with versions Renovate bumps; Renovate gets `minimumReleaseAge: "7 days"`, `vulnerabilityAlerts` and OSV
  alerts (security fixes skip the weekly window), a SHA-pinned `.pre-commit-config.yaml` and a custom manager for it. Addresses
  ANSSI SSDLC CIO-REC01/-REC05/-REC07, SCM-REC08, CICD-SAC-01, CONT-REC06, CICD-IFN-03 and CICD-AIP-01c in part. Rung: the commands
  were run locally (actionlint, yamllint, kubeconform 0.8.0 on the 35 rendered paths: 326 valid, 0 invalid, 69 skipped for lack of a
  schema; trivy 0.75.0 with the ignore file: clean; the gitleaks rules test; the Renovate validator in the 44.52.1 image), not on GitHub.
  **Not seen yet:** the first run of the workflows on GitHub, Plumber's verdict on this repository (its `branchMustBeProtected`
  control reads `main`'s protection), and Renovate's behaviour with the new keys on the hosted App. **Does not** add a required check
  (a ruleset setting), hash-pin the pip installs, or make `trivy config` stricter than HIGH.

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

### Added

- **Alerts for the log pipeline, the policy engine, Flux, the node disk and clock, and the backups.** 29 rules in
  `vmrule-logging.yaml` (Alloy missing or dropping, Loki push errors, replica down, discards, ingestion burst, Grafana 5xx),
  `vmrule-security.yaml` (Kyverno violations at admission time, webhook down, Flux object not Ready or suspended), and additions to
  the cluster, platform and backup rules: `/var` (EPHEMERAL, where local-path volumes live) filling and full within 24 h, clock
  offset and sync status, Cilium policy denials in a loop (Hubble is off), restore-test CronJobs stale for 35 days, a missing backup
  CronJob, CNPG WAL archive failing, base backup failed or stale. Each family has an `*Absent` guard so a vanished scrape does not
  read as "all clear". `docs/detection.md` lists what is covered and what is not, and the `kubectl get cpol,polr -A` procedure to
  review Audit violations. `scripts/dev/test-vmrules.sh` (new CI job) runs every rule through a pinned vmalert `-dryRun`
  and checks the conventions `check-alert-metrics.py` relies on. Addresses ANSSI PA-012 R12, R21, R22, C3.1 and C3.2, HYG-M36,
  GH-BKP-2, NSA12-LOG-04 and SSDLC-CICD-MON-01/02. Rung: parsed by vmalert and built by kustomize, **never fired and never
  evaluated against real data**: metric names were read in the sources of Alloy v1.6, Loki v3.3.2, Kyverno v1.12.1, CNPG v1.23.1 and
  node-exporter v1.9 (the Kyverno counter's `_total` suffix is the OpenTelemetry exporter's convention, not read in code), thresholds are
  guesses, and `NodeClockNotSynchronised` may fire on healthy Talos nodes. It does **not** alert on OpenBao authentication or root
  use, `kubectl exec`, RBAC changes or an empty stream for one namespace (no audit log, no Loki ruler), keeps `require-security-context`
  out of `KyvernoPolicyViolation` until its 74 known violations are fixed, and does not set the time sources of the nodes (the
  OpenAether-infra half of the clock finding).

- **Scrapes for what the new alerts read, and Flux per-object state.** Alloy, Loki, Grafana, Kyverno (the four controllers, :8000),
  Zitadel and the CNPG instances (:9187) had no scrape, so no rule could see them fail; `vmscrapes-pipeline.yaml` adds them, with
  the ingress rule for vmagent in the Loki, Alloy, Kyverno and Zitadel CiliumNetworkPolicies and the four ports in vmagent's egress
  list (Grafana already accepts its whole namespace; CNPG already accepted vmagent on :9187). kube-state-metrics now also exports
  `gotk_resource_info{ready,suspended}` for Kustomizations, HelmReleases, GitRepositories and HelmRepositories (the mapping of the
  Flux docs, `customResourceState` plus list/watch on those four kinds, no secrets): the Flux controllers export no per-object
  condition (kustomize-controller v1.8.5 records only the reconcile duration, read in its source), so `gotk_reconcile_condition`
  named by the audit can never carry data. Prepares ANSSI PA-012 R12, NSA12-LOG-04 and SSDLC-CICD-MON-01. Rung: kustomize build
  and chart render, not run on a cluster. No rule reads the Zitadel metrics (names unchecked), and the kube-state-metrics
  pod restarts once (one replica: a gap of a minute in the `kube_*` series).

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
- **Loki can reach the provider's S3, enforces its retention and tolerates one pod down (ANSSI hardening B02;
  addresses PA-012 R9, R10, R25 and hygiene M36; refs #53).** The Loki network policy gains `rules.dns` on its DNS rule (without it
  Cilium never learns the S3 addresses and the FQDN rule matches nothing), and the Loki-to-Loki egress it lacked (memberlist
  7946 tcp/udp, gRPC 9095, 3100; 9095 and 7946/udp were also missing on ingress): by reading, that alone keeps the ring from forming,
  which fits the "at least 2 live replicas required" error of 2026-10-13, but it was not observed. The hard-coded `s3.insecure: true`
  is gone (the seeded endpoint's scheme decides). The compactor is on (`retention_enabled`, `delete_request_store: s3`) with
  `retention_period: 8760h`, so nothing is deleted for a year; the 30d value was never applied. Three replicas with `replication_factor: 2`
  (was two with RF 2, where a single pod down rejects every push: with three, one pod can be down and a write still reaches two). The
  PodDisruptionBudget `minAvailable: 1` is **not** a quorum guarantee: it only stops the last replica being evicted, so two
  drains in a row can proceed and leave one pod, which rejects pushes (`minAvailable: 2` would keep the quorum, but local-path pins
  each pod to its node and a drain of a node holding two of them would block forever). The canary is turned off
  at the key the chart reads (`lokiCanary`, plus `test.enabled: false`, without which the chart refuses to render). A local
  overlay (`apps/base/observability-local`) sets one replica, RF 1, plain http and 168h, but only when it is applied: locally every
  Flux Kustomization is suspended and the documented flow is `kubectl apply -k`, so the repoint in `apps/flux/local` alone does
  nothing and `apps/base/observability` applied by hand is the production Loki (the comments of both files now say so).
  Rung: mocked/static (`scripts/dev/test-loki-render.sh` renders chart 6.25.0 with the shipped values and asserts the above; CI job
  `loki-render`), not run on a cluster. **It does not:** give Loki its own bucket and key (B16, infra #302: it still holds the backup
  bucket's key and the compactor can delete in it), set a per-stream or identity-log retention, add log-flow alerts or an Alloy buffer,
  or prove one compactor runs among three replicas. Anti-affinity stays preferred (clusters have 1 to 3 workers) and there is no
  topology spread (the chart exposes none for the single binary).
- **Loki signs its S3 requests with the provider's region, not `us-east-1` (follow-up of ANSSI hardening B02; refs #53).** The
  region was hard-coded, so even with the egress rule matching, Loki's SigV4 signature carried a region the provider does not serve.
  Read in Loki 3.3.2 (`s3_storage_client.go`): the region is the configured one, `dummy` when empty, and the AWS SDK signs with it.
  Scaleway and OVH reject a mismatch (`AuthorizationHeaderMalformed`, "the region ... is wrong; expecting ..."; community reports, the
  providers' pages list one region code per endpoint); Outscale's own AWS CLI guide has the region set to the OOS region. restic
  (minio-go) discovers the region, which is why the backup path proved nothing. The `loki-s3-credentials` ExternalSecret now builds a
  `region` key (seeded `region` if any, else read from a Scaleway, OVH or Outscale endpoint host name, else `us-east-1`, the former
  value, for the local MinIO) and the HelmRelease injects it with `valuesFrom`; no seed change is needed on an existing cluster
  and `bao kv put ... region=` overrides it. The template was run against sprig, the library External Secrets uses, for the three
  providers' hosts, the MinIO and a look-alike host; `test-loki-render.sh` now fails if a literal region comes back in `spec.values`.
  Rung: mocked/static. **Not observed:** a push accepted by a provider's S3 (lab: no 403 in the Loki logs and objects appearing in
  the bucket), nor the ExternalSecret reconciling on a cluster. Another region than the three FQDNs of Loki's network policy
  also needs that policy edited.
- **The backup checks can no longer pass in CI with the `pg_restore` tests unrun (follow-up of ANSSI hardening B03).**
  `test-backup-checks.sh` skipped that part with exit 0 when the Postgres 18.4 image could not be pulled, and CI only grepped
  `N passed, 0 failed`, so a registry rate limit left the main check unrun and green. The pull is retried once; with `CI=true` or
  `REQUIRE_PG_RESTORE=1` a skip is a failure, and the CI step also fails on the word `SKIPPED`. Locally the skip stays a notice.
  Rung: mocked/static (run with an unpullable image tag: skipped locally, failed with `CI=true`).
- **`test-loki-render.sh` now asserts Loki's network policy (follow-up of ANSSI hardening B02).** The B02 entry said the test covered
  the network fix; it read only the chart render, so deleting `rules.dns`, the 9095/7946 rules or the peer egress passed every gate.
  It now builds the base and fails when the DNS rule loses `rules.dns`, when the peer rules lose 3100/9095/7946 tcp+udp (ingress
  or egress), when Alloy or Grafana lose 3100, when the S3 egress is not FQDN-scoped on 443 or loses a provider host, or the MinIO
  9000 rule goes (mutation-checked on four of them). Rung: static. It cannot show Cilium enforcing the policy (lab check).
- **The Audit policy for External Secrets objects can no longer block them when Kyverno is down (follow-up of ANSSI hardening B04).**
  `restrict-external-secrets-namespaces` set no `failurePolicy`, so it took the default `Fail`. Read in Kyverno v1.12.1
  (`pkg/controllers/webhook/controller.go`): the validating webhook is built from every policy with a validate rule whatever its
  `validationFailureAction`, so Audit did not remove it, and a Kyverno outage would have refused writes to `ExternalSecret`,
  `PushSecret`, `SecretStore` and `ClusterSecretStore`, Flux applying the store included. It now sets `failurePolicy: Ignore`
  (those kinds move to Kyverno's `Ignore` webhook) and its three rules match `CREATE` and `UPDATE` only: on DELETE `request.object` is
  empty (`policycontext.NewPolicyContext` fills the old resource), the third rule's `request.object.metadata.name` could not be
  substituted, and removing a stray object is not a violation. `check-externalsecret-namespaces.py` fails if either is lost (four new
  fixture cases). Rung: static (kustomize build, fixtures). **Not observed:** the generated webhook on a cluster. **Not done:** the six
  policies of `kyverno-policies` also use the default `Fail`; that is a separate decision (they match Pods in every namespace).
- **The databases restore-test says when its restores do not fit, and the restore tests now also run under busybox (follow-up of ANSSI
  hardening B03).** Both repositories are restored into one 2Gi `emptyDir` (the next container reads them), where the old script
  deleted each target after use; past the limit kubelet evicts the pod with no message. The init container now reads the snapshot
  size (`restic stats --json`, checked on restic 0.18.0 against a local repository) and stops with "raise the sizeLimit ... and
  limit_mib together" if the two restores exceed 1800 MiB, or if the size cannot be read. The OpenBao test already removes each
  target, so it holds one snapshot and its unpacked copy at a time. `test-backup-checks.sh` also runs the OpenBao script (intact,
  altered, truncated) inside the restic image, whose busybox `tar`, `sha256sum` and `sh` are what runs on the cluster (mutation-checked
  with a GNU-only flag). Rung: mocked/static plus a container run, not a CronJob on a cluster. **Does not:** change the volume size or
  restore less data; dumps beyond 1.8 GiB for both repositories need the limit raised.
- **The Longhorn `CRYPTO_PBKDF: argon2id` setting was checked against the sources, no change (follow-up of ANSSI hardening B03).** The
  B03 entry named the `CRYPTO_*` keys from memory. Read: longhorn.io's volume-encryption page lists `CRYPTO_KEY_CIPHER`, `_HASH`,
  `_SIZE` and `_PBKDF` as Secret keys (defaults `aes-xts-plain64`, `sha256`, 256, `argon2i`); longhorn-manager v1.9.2 (the chart pinned
  here is 1.9.2) takes them from the node-stage Secret (`csi/node_server.go`), applies its defaults only to empty values and passes
  them to `cryptsetup luksFormat --type luks2 ... --pbkdf <value>` (go-common-libs `ns/crypto.go`) without an allow-list;
  cryptsetup documents `argon2id` as a `--pbkdf` value (2.0 onward) and makes it its LUKS2 default since 2.4.0. So the value is
  forwarded and valid; it costs what the default `argon2i` costs at format time (same memory benchmark) and touches no existing
  volume. Not observed: a volume formatted with it on a node (lab check: `cryptsetup luksDump` on the backing device shows
  `PBKDF: argon2id` and a 512-bit key).
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
