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

## [0.3.0] — 2026-10-19

0.3.0 is the pair of OpenAether-infra 0.3.0: a **version upgrade campaign** (every component that was past support, plus the independent low-risk bumps) and a
**security hardening wave**, the latter opt-in or in Audit unless stated. The project follows ANSSI recommendations as far as possible.
**Nothing here is a claim of compliance with any ANSSI document**: each entry names the requirements it addresses and the rung it reached.

**A fresh install lands on the new versions (run on Scaleway, OVH and Outscale, labs 12 to 15). A running cluster must hop one minor at a time where the
table says so** (`docs/patching.md`; the in-place hops were rehearsed on Scaleway only, in lab 15c, and not all of them).

### Version upgrades

| Component | 0.2.0 | 0.3.0 | Running cluster |
|---|---|---|---|
| Longhorn | 1.9.2 | 1.12.1 | hop 1.10.2 -> 1.11.3 -> 1.12.1; **pre-step on the five CRDs with a conversion webhook before 1.10** (lab 15c: the 1.10 hop failed without it, 14 min without attach); no downgrade exists; the HelmRelease no longer remediates |
| cert-manager | 1.15.3 | 1.21.2 | one minor at a time, six Flux hops, 221-255 s each, no probe failure |
| Kyverno | 1.12.1 | 1.19.1 | 1.13 -> 1.15 -> 1.17 -> 1.19 ran in 57-123 s each; **1.19 is the last line serving `kyverno.io/v1`; migrate before 1.20** (#113) |
| CloudNativePG operator | 1.23.1 | 1.30.1 | every hop rolls the Clusters; with the new `primaryUpdateMethod: switchover` the write gap is about 10 s instead of 195 s; take the 0.3.0 `cnpg` Kustomization no later than the 1.27 hop (CRD substitute opt-out) |
| PostgreSQL (Grafana DB image) | 16.3 | 16.15 | a minor roll of that Cluster |
| External Secrets Operator | 1.2.0 | 2.13.0 | 1.3.2 then 2.13.0 ran without failure |
| Loki chart (Loki) | 6.25.0 (3.3.2) | 18.16.2 (3.7.8), chart now from `grafana-community` | procedure below; the 6.25 -> 6.55 hop timed out in lab 15c |
| Grafana chart (Grafana) | 8.6.4 | 14.0.1 (13.2.4), from `grafana-community` | **not rehearsed in place**; take a CNPG backup first |
| Alloy chart (Alloy) | 0.11.0 (1.6.1) | 1.13.1 (1.20.1) | **not rehearsed in place** |
| Zitadel chart (Zitadel) | 10.0.2 | 10.4.0 (4.19.2) | 4.14 -> 4.15 -> 4.17 -> 4.19.2 ran in 219-250 s each; migrations are forward-only: CNPG backup first |
| victoria-metrics-operator | 0.65.1 | 0.68.1 | one rolling restart of the VM pods |
| Flux on the children | 2.8.8 | 2.9.6 | not rehearsed |
| cluster-api-operator / ORC | 0.27.0 / 2.4.0 | 0.29.0 / 2.6.0 | the ORC version the pinned CAPO needs |
| Cilium on the children | 1.19.2 | 1.19.9 | a patch; one minor behind the management cluster on purpose |
| Gateway API CRDs | 1.1.0 | 1.2.1 | **not 1.5.x** (Istio 1.24 does not become Ready with them); CRDs are not rolled back; check `storedVersions` first |
| restic | 0.18.0 | 0.19.1 | exit code 3 now fails the Job; `prune` repacks more |
| local-path-provisioner | 0.0.31 | 0.0.37 | restarts once; PVs untouched |
| node-exporter chart / kube-state-metrics chart / metrics-server chart | 4.39.0 / 5.25.1 / 3.12.2 | 4.60.0 / 8.6.0 / 3.14.0 | each restarts once |
| Kubernetes / Talos (infra) | 1.37.1 / 1.14.2 | unchanged | |
| Istio, OpenBao, MinIO | 1.24.2, 2.7.1, RELEASE.2024-04-18 | unchanged | Istio and MinIO waived to 2026-12-31 (`docs/eol-calendar.md`) |

### Upgrade notes by component (from 0.2.0)

- **Longhorn.** Follow `docs/patching.md` ("Longhorn in-place hops") in order: the CRD conversion pre-step, then 1.10.2, 1.11.3, 1.12.1, engines upgrade automatically afterwards.
  The chart NetworkPolicies are off and the brick's CiliumNetworkPolicy opens the new ports (9503/9600, 10000-30000, NFS 2049). The RWX StorageClasses
  now carry `softerr,timeo=100,retrans=3` (a graceful reboot of a node with a share-manager and a client no longer hangs for more than 12 minutes, apps #124);
  a volume created earlier keeps its old mount options until it is recreated.
- **Loki chart 6.x -> 18.** A fresh install takes 18 directly. In place: the StatefulSet selector and volume claim template are unchanged. The HelmRelease has a 15 minute
  timeout and **no automatic upgrade rollback**: on a stalled hop follow "Loki chart hop" in `docs/patching.md` (inspect the pod that is not Ready, delete all Loki pods at
  once if only the memberlist ring waits, `flux reconcile helmrelease loki --reset --force`). Logs are not lost: Alloy retries and the chunks are in the object store.
- **Kyverno.** The five Pod policies get a per-namespace Enforce through the **rule-level `failureAction` / `failureActionOverrides` spelling**; the old spec-level
  `validationFailureActionOverrides` is ignored on 1.19.1 and denies nothing (lab 15a, apps 669494b). Enforce stays off by default.
- **CloudNativePG.** Both Clusters use `primaryUpdateMethod: switchover`. `barmanObjectStore` stays until the Barman Cloud plugin replaces it (removed in CNPG 1.31, #114).
- **Disk.** The infra default root volume is 50 GB; a 20 GB node exhausts EPHEMERAL after the larger images of 0.3.0 (DiskPressure, evictions). Grow the disks before the hops (infra CHANGELOG).
- **OpenBao** metrics move to a second listener on 8202 (headless Service only); the listeners accept TLS 1.3 only.
- **Grafana and Zitadel** database migrations are forward-only. Take a CNPG Backup first.

### BREAKING and behaviour changes

- `Longhorn` HelmRelease: `retries: 0`, no remediation (a person fixes forward). `Loki` likewise.
- Kyverno is on 1.19.1: the cleanup CronJobs no longer exist; `PolicyException` stays off; images come from `reg.kyverno.io`.
- node-exporter runs in its own privileged namespace `services-observability-node`; four namespaces can drop to the baseline Pod Security level (opt-in).
- Zitadel no longer installs with its public admin password: a generated 32-character password is used.
- The kubelet and apiserver scrapes verify TLS (the kubelet side is an opt-in component that needs infra's `kubelet_serving_cert_rotation`).
- Kyverno reports sensitive capabilities, init containers and read-only rootfs in Audit (new violations to review, none blocking).
- The Gateway API CRDs v1alpha2 `GRPCRoute`/`ReferenceGrant` versions disappear.

### Known limitations

- **Unsigned history:** no commit on the history verifies yet, so the Flux Git verification component cannot be turned on (#86; infra #332, #333). The maintainer creates a dedicated signing key later.
- **CNPG `barmanObjectStore` is removed in CNPG 1.31** (#114) and **Kyverno `kyverno.io/v1` ClusterPolicy is removed in Kyverno 1.20** (#113): stay on 1.30.x and 1.19.x until migrated.
- **In-place hops not rehearsed:** Loki chart 6.25 -> 18 (the lab hop stopped at 6.55 and timed out), Grafana 8.6 -> 14, Alloy, Gateway API CRDs, Flux on the children,
  Cilium and Talos patches, Istio (#121). A fresh install of these versions was run on three clouds.
- **Scaleway first install with the app load balancer allow-list needs two applies** (infra #361).
- **OVH S3 has no bucket policies through the S3 API** (infra #379); **Outscale did not pass the stability criteria with the full stack in lab 16** (3 CP + 3 W of 3 vCPU / 6 GB: an IO and memory
  cascade after one backup and restore and one drained reboot, not cured by a live `io1 3000`; the 20 vCPU quota allows no larger shape, infra #376) and OOS refused dedicated EIM keys (infra #377).
- **Losing a worker.** The shipped 2-worker examples could not be rolled (172% of CPU requested, infra #378; the infra examples now ship 3, not re-run); only the CPU gate is checked. Replicas with a required
  spread stay short while a worker is out. `CnpgWalArchiveFailing` can stay pending about 25 minutes after a drained reboot (#138), and Flux has no notification Alert or Provider (#139): both 0.4.0.
- **MinIO (dev profile) is archived upstream;** the waiver runs to 2026-12-31 (#104). Istio 1.24 is waived to the same date.
- **Kubernetes 1.37 is outside the range cert-manager 1.21, CNPG 1.30, Flux 2.9 and others test upstream** (#105, #120); the labs ran on it.
- **Loki retention deletion of expired chunks was never exercised** (#127); Alloy keeps log batches in memory only (#80, #119); **Zitadel -> Grafana login with MFA** was not run (#126).
- OpenBao replicas and volumes can land on one worker (#129); the Zitadel setup Job killed half way leaves a migration lock (#133); the Longhorn install has no retries (#134).
- The alert thresholds are estimates; most rules have run against synthetic series only.

### Validated on

The same labs as the infra CHANGELOG (11 to 15c, 2026-10-16 to 2026-10-18; Scaleway full DAG in 12, 13, 14 and 15c, OVH complete in 15a, Outscale full in 15b),
each torn down and checked clean. At the end of a fresh install on the defaults: 38 Kustomizations and 16 HelmReleases Ready, `cluster-verify` 16/16, `cluster-security` 33/36 to 34/36
(the rest skipped, not failed), strict plan "No changes". Lab 15c ran 40 upgrade hops and drills from apps 0.2.0 on Scaleway: 20896 HTTP samples through the load balancer,
135 failed, one 73 s episode (a drain evicting the single Gateway pod); CNPG primary kill 24 s of write gap, planned switchover 12 s; Loki outage of 240 s lost no line.
Offline: Kyverno CLI 1.19.1 (90 + 8 fixtures), Alloy 1.20.1, Zitadel 4.19.2 with PostgreSQL, Loki `-verify-config`, Grafana 13.2.4, CRD schema validation, vmalert dry runs.

**Lab 16 (2026-10-18, final validation on the head of this branch with a signed throwaway Flux branch, each cloud torn down and checked clean):** Scaleway and OVH, a fresh install from the shipped
examples: 38 Kustomizations and 16 HelmReleases Ready 23.5 min (Scaleway) and 29 min (OVH) after the start, idle alerts Watchdog only (61 rules healthy), Longhorn RWO, LUKS2 and RWX backups restored with matching
checksums, CNPG backup and restore into a new cluster (150 tables = origin), the five Kyverno Enforce policies and the ephemeral-container denials, OpenBao TLS 1.3 only with re-key, ESO 15/15, Zitadel setup, Loki
write and read-back, a drained worker reboot and a control-plane reboot. Outscale (3 CP + 3 W of 3 vCPU / 6 GB): everything passed for the first hour, then the cascade above; its control-plane reboot, alert drill and
final acceptance were not run. A provoked alert reached Alertmanager (the webhook-receiver leg was skipped on purpose). Found and filed: #137 (fixed here), #138, #139.

**NOT run:** the etcd snapshot restore; OpenBao raft snapshot restore; Istio hops; Alloy, Grafana and Gateway API CRD in-place hops; Flux (children) and Cilium/Talos patch hops;
Zitadel -> Grafana interactive login; the alert drills (no alert was provoked); Loki retention expiry; the Flux Git verification (no signing key); an unattended run to completion.

---

### Detail of the 0.3.0 entries

ANSSI hardening work, released with 0.3.0. **Nothing here is a claim of compliance with any ANSSI document**: each entry names the requirements it addresses and the rung it reached; the ones that only a cluster can show are listed in the lab checklist of the maintainers.

### Security

- **Review fixes (mirrors the infra review). Static / mocked, not run on a cluster.**
  - `check-commit-signatures.sh` skips merge commits: on a PR run HEAD is GitHub's unsigned synthetic merge commit, so the documented `--strict`
    switch-on would have failed every PR. Its test now has a merge-commit case and expired-key/signature cases (status X/Y, throwaway key signed in the past).
  - `docs/signed-commits.md` and the `git-verify` component comment no longer say Flux checks OpenPGP only: the Flux CRD vendored in infra documents
    SSH keys (`.sshpub`) too. OpenPGP is this repository's choice, and SSH keys are NOT wired.

- **Critic follow-ups: apps areas the audit had not read** (no ANSSI requirement id was assigned to them by the audit; they sit near DP-02, DP-03/F06, LOG-13 and SSDLC-CICD-AIP-01; mocked/static, not run on a cluster):
  - CAPI provider egress: `capi-system` no longer allows the Outscale endpoints and `capi-outscale-system` no longer allows Scaleway and OVH
    (each namespace runs the providers of its own cloud). Rollback: restore the removed `toFQDNs` lines.
  - `cert-manager-vault-token` Role: `serviceaccounts/token` `create` is limited to the `cert-manager` ServiceAccount (`resourceNames`), not every
    ServiceAccount of the namespace.
  - `openbao-snapshot` CronJob: the Job revokes its own token right after the snapshot (it read a copy of every secret); a failed revoke only warns.
  - Alloy (logs): `discovery.kubernetes` is limited to the pods of the node (`NODE_NAME` from the downward API). The DaemonSet had every instance
    tail every pod through the API server. The config parses with Alloy 1.6.1 (`alloy fmt`); that logs still arrive from every node is a lab check.
  - `apps/vendored-dumps.sha256` + `scripts/dev/check-vendored-lock.sh` (CI): the six vendored upstream dumps are hash-locked, so a change to one
    moves the lock in the same commit.
  - Does NOT: prove a dump equals its upstream release, narrow the ClusterRoles that the vendored External Secrets dump grants, split the
    `openbao-snapshot` pod so the restic container has no ServiceAccount token, or restrict who may use the `openbao-ca` ClusterIssuer.

- **Flux Git source verification, opt-in** (ANSSI SSDLC-CICD-AIP-03, SSDLC-CONT-REC06; apps half of F04, F10; mocked/static, not run on a cluster):
  - New component `apps/flux/components/git-verify`: adds `spec.verify {mode: HEAD, secretRef: git-signers}` to the `GitRepository`, only when a cluster's overlay lists it. Default unchanged (`scripts/dev/test-git-verify.py`, in CI).
  - `scripts/dev/check-commit-signatures.sh` in the `commits` job: a WARNING for unsigned commits of a PR (the history has none signed), `--strict` to fail; own test with a throwaway key. `CODEOWNERS` names the Flux, RBAC and workflow paths. `docs/signed-commits.md`: GPG as the standard, signer table (to fill), enable order, break-glass.
  - Does NOT: change any GitHub ruleset, publish a key, create the `git-signers` Secret or the bootstrap `verify` (OpenAether-infra, `flux_git_verify`), verify tags, or prove source-controller accepts it on a cluster.

- **Named operator identities** (ANSSI PA022 R38, R40, NSA12 AUTH-02; apps half of F04, F13, F20):
  - New brick `platform/rbac` (Flux `platform-rbac`, in the baseline of every generated profile): `ClusterRoleBinding`
    `oa-admins` (group `oa:admins` to `cluster-admin`) and `oa-readers` (group `oa:readers` to `view`). Inert until a credential
    carries one of these groups; nothing existing changes. Rollback: remove the Kustomization.
  - **Not done here**: the CSR-based kubeconfig tool, the RBAC review script, the break-glass rules and the apiserver OIDC
    configuration belong to `OpenAether-infra`; OIDC groups, the OpenBao OIDC/JWT auth method and the Zitadel MFA login policy
    need an issuer reachable outside the cluster and are **not** attempted. A client certificate stays non-revocable: the control is
    its short lifetime, a group binding cannot single out one holder.
  - **Rung: static** (`scripts/dev/test-rbac-brick.py` in CI), mocked/static, not run on a cluster.
- **In-cluster transport and metadata egress** (ANSSI HYG-M21, PG078-R11, NT-35, NSA12 NET-07, NET-09, HYG-M22; apps half of
  F14, F12, DP-11, F20, F05):
  - Zitadel (User and Admin) and Grafana connect to CNPG with `sslmode=require`: the credentials stop crossing the pod network
    in clear. It encrypts but does **not authenticate the server**; `verify-full` needs the CNPG CA copied into the consuming
    namespaces with a rotation-safe mechanism and is **not done**. Rollback: `disable`.
  - No TLS version floor is set in istiod's `meshConfig`: `tlsDefaults.minProtocolVersion` (added then removed in this release
    cycle) is ignored by Istio, which reads only `ecdhCurves` and `cipherSuites` there and leaves ISTIO_MUTUAL to `meshMTLS`.
    Envoy's own TLS 1.2 floor applies, unchanged. A TLS 1.3 floor and cipher lists are **not** set (client versions not
    measured); the Gateway floor would be a per-listener `tls.options`, not done.
  - New CCNP `deny-cloud-metadata-egress`: every pod is denied 169.254.169.254 and 169.254.42.42 (deny-only, no default-deny
    side effect, Talos hostDNS untouched).
  - The cert-manager egress rule no longer names the Let's Encrypt hosts: no issuer in the repo is ACME. Anyone with an ACME
    ClusterIssuer must add its directory FQDN back.
  - **Not done / already done**: Loki `insecure` was fixed in 0.2.0; the kubelet and metrics-server CA path is the opt-in component of
    B23; Zitadel's own TLS (gateway to Zitadel stays h2c behind WireGuard and the CNPs), the gateway key algorithm, node-level egress
    (infra) are not touched.
  - **Rung: static** (`scripts/dev/test-in-cluster-transport.py` in CI), mocked/static, not run on a cluster.
- **Container hardening and resource bounds** (ANSSI FT082 R10, R11, R12, NSA12 POD-06, BP028 R64, PA012 R10, R14; apps half of
  K8S/F09, K8S/F10, LOG-13, LOG-14):
  - Alloy and its config-reloader, Grafana and the CAPI operator run with a **read-only root filesystem**; what each writes
    goes to an emptyDir (Alloy's storage path, Grafana's and the operator's `/tmp`). The CAPI operator also gets a pod and a
    container `securityContext` (uid 65532, drop ALL, seccomp RuntimeDefault). Alloy's `loki.write` retries 30 times with
    at most 1 minute between tries instead of giving up after about 5 minutes (Alloy v1.6 has no write-ahead log).
  - **local-path-provisioner**: the provisioner has a restricted `securityContext`, its helper image is `busybox:1.37.0`
    instead of the bare `busybox`. It does NOT change `mkdir -m 0777` to 0770: the volumes are used by non-root pods with
    other uids and the hostPath volumes ignore `fsGroup`, so 0770 would stop them from writing; that needs a different
    storage design (Longhorn, or a Talos user volume), not a mode.
  - **ServiceAccount token dropped** from the Zitadel server and login pods and the MinIO pod (Helm post-renderer: neither
    chart has a value for it). Grafana keeps it (its dashboards sidecar reads ConfigMaps through the API); the Zitadel setup
    Job keeps it (it runs kubectl).
  - **Bounds**: `sizeLimit` on the OpenBao `tmp` and openbao-init emptyDirs (64Mi); memory and CPU limits on the three ESO
    Deployments, CPU limits on the four Kyverno controllers (their memory limits were already in the vendored file).
    **The ESO and Kyverno values are generous guesses, not measured** (no cluster here); the lab step tightens them.
  - **Rung: static** (`scripts/dev/test-container-bounds.py` in CI, `helm template` of the Alloy, Grafana, CAPI operator,
    Zitadel and MinIO charts at the pinned versions, `kubectl kustomize`, and the new `loki.write` block loaded by a real
    Alloy v1.6.1); **not run on a cluster**. Whether each workload really starts and works with a read-only root filesystem is
    unproven until then. It does NOT add LimitRange or ResourceQuota (deferred), `ephemeral-storage` requests, or the Talos
    `podPidsLimit` (infra), and does not bump any chart (Renovate PRs); Alloy to Loki stays cleartext HTTP, covered by
    WireGuard and the CiliumNetworkPolicy, not by TLS.

- **Four namespaces can drop from the privileged to the baseline Pod Security level, opt-in** (ANSSI FT082 R4, R6, NSA12 POD-05;
  apps half of K8S/F08, OSN/F16, SCC/F09): `services-observability` (node-exporter has left it), `services-gateway` (the Envoy proxy
  istiod generates), `foundation-service-mesh` (HelmReleases and the waypoint Gateway; ztunnel and istiod run in `istio-system`) and
  `foundation-storage` (MinIO). **Off by default**: `enforce` stays `privileged` as released, `warn`/`audit` are restricted (they only
  log); the new component `apps/flux/components/psa-baseline` (tested by `scripts/dev/test-psa-restricted.py`) lowers them. It is the
  one change of the series that can stop a pod at admission, and the required server-side dry run (`LAB-CHECKLIST`) was NOT run:
  do that dry run first. Baseline is only checked on pods created or rescheduled after it. To go back, remove the component.
  **Upgrade note**: a cluster owner who wants it adds `components: [../components/psa-baseline]` to the cluster overlay; it patches
  `spec.patches` of three Flux Kustomizations (`namespaces`, `istio`, `storage`), which replaces a list, so merge by hand if the
  overlay already patches them or also uses `psa-restricted`. It does NOT lower `istio-system`, `longhorn-system`,
  `kube-system` or the new node-exporter namespace, nor raise `foundation-networking` (Cilium hostPort), `zitadel`, `kyverno`,
  `cnpg-system`, `orc-system` or `flux-system` (their warn logs have to be read first).

- **node-exporter moves to its own privileged namespace, `services-observability-node`** (ANSSI FT082 R4, R6, NSA12 POD-05;
  apps half of K8S/F08, OSN/F16, SCC/F09). It is the only observability workload that needs hostPath, hostNetwork and hostPID,
  and Pod Security has no per-pod level, so the whole `services-observability` namespace was privileged for it. The HelmRelease,
  the `node-exporter` VMServiceScrape and the Kyverno exemptions (`restrict-privileged` by pod name, `disallow-root-user` by
  namespace as before) follow; `services-observability` itself is NOT lowered here (previous entry, opt-in). The CiliumNetworkPolicies and Istio
  policies need no change: node-exporter is hostNetwork, out of the mesh, and reached through vmagent's host egress on :9100.
  `restrict-privileged` also stops sparing `foundation-service-mesh` wholesale (ztunnel and the Istio CNI agent by name).
  **Rung: static (`scripts/dev/test-node-exporter-namespace.py` in CI, render, Kyverno CLI fixtures); not run on a cluster.**
  **Upgrade note**: Flux uninstalls the old release and installs the new one, so node_* series stop for one or two scrape
  intervals and the `node` DaemonSet briefly has both pods wanting :9100; series gain `namespace="services-observability-node"`
  (no shipped rule or dashboard selects on it). It does NOT touch the Talos or Cilium host policies.

- **Kyverno: sensitive capabilities, init containers and read-only rootfs are reported, all in Audit** (ANSSI FT082 R1, R8, R15
  in part, NSA12 POD-01, BP028 R64; apps half of K8S/F04). New `restrict-capabilities` (names `capabilities.add` values such as
  SYS_ADMIN, NET_ADMIN, NET_RAW, SYS_PTRACE, SYS_MODULE on containers, initContainers and, through `Pod/ephemeralcontainers`,
  ephemeral containers; NET_BIND_SERVICE is left alone; ztunnel and the Istio CNI agent are exempt by pod name, the
  single-purpose system namespaces by namespace), plus the informational `require-ro-rootfs` and `require-limits` (memory only).
  `restrict-privileged` now also reads initContainers; `require-security-context` is split into three rules so that
  runAsNonRoot and seccomp are satisfied at the pod OR the container level (the two fields exist at both), while
  allowPrivilegeEscalation and `drop: [ALL]` stay container-level, initContainers included. The alert `KyvernoPolicyViolation`
  skips the two informational policies. The header of `apps/base/kyverno/kustomization.yaml` named v1.18.2; the vendored file is
  v1.12.1 (same sha256), so the `validationFailureAction` syntax is the right one. **Rung: Kyverno CLI 1.12.1 on fixture pods
  (`scripts/dev/test-kyverno-policies.sh`, 80 expectations, in CI), static render; not run on a cluster**: the report counts on a
  fresh install and after a node reboot, the ephemeral-container match and `names` against real pod names are for the lab. It does
  NOT flip any policy to Enforce, narrow the exclusions of `disallow-root-user` (the one Enforce policy that spares whole
  namespaces), cover the Gateway-generated pods' needs, or add `verifyImages` (the registry allow-list was already per organisation).

- **OpenBao PKI: ECDSA keys, a server-only role, revocation information and auto-tidy** (ANSSI IGC-BP-03, NT-35 R8, R27-R29, R33,
  IGC-BP-06; TLS-07, TLS-08, TLS-09, DP-08, apps half). `configure.sh` generates an ECDSA P-384 intermediate CSR and writes the
  `openaether` role as ECDSA P-256, serverAuth only, `DigitalSignature` only, no wildcard, bare domain, IP SAN or required CN; it
  also sets `pki/config/urls` (AIA, CRL, OCSP on the in-cluster service), `pki/config/crl` (automatic rebuild, delta, 72 h) and
  `pki/config/auto-tidy`. The gateway Certificate becomes ECDSA P-256 with `rotationPolicy: Always`, the four explicit hostnames
  (no wildcard, no commonName) and `renewBefore` of one third. `OA_PKI_KEY_TYPE=rsa` is the documented RSA-3072 + PSS variant;
  `OA_PKI_DEFAULT_FOLLOWS_LATEST` (off) lets a renewed intermediate become the default issuer, which a plain import does not.
  Rung: mocked/static plus the real-OpenBao harness (`scripts/dev/test-openbao-bootstrap.sh`: EC leaf, RSA/wildcard/bare/IP/outside
  names refused, URLs in the leaf, revoked serial on the CRL, idempotent re-run, both key types, a throwaway EC root signing the
  intermediate); not run on a cluster. It does NOT: convert an existing RSA-2048 intermediate (regenerate it, runbook section 5), make
  any client check revocation (none does; Envoy does not hard-fail), replace the Istio mesh root (deferred to the Istio upgrade),
  replace the `openbao-ca-bundle` leaf Certificate (needs trust-manager), or alert on CRL age (OpenBao exposes no such metric, and
  its 1 m metric retention would drop the tidy ones). **Upgrade note**: a new HTTPRoute hostname must be added to
  `certificate-openaether-tls.yaml`; the gateway key is re-issued as ECDSA at the first sync.
- **OpenBao's listeners accept TLS 1.3 only, and its server certificate re-keys at every renewal and is reloaded without a restart**
  (ANSSI NT-35 R10, R11 and R25, IGC BP-08; TLS-02, TLS-10). `tls_min_version = tls13` and `tls_key_exchange_preferences`
  (`X25519MLKEM768, CurveP384, CurveP256, X25519`, a key that OpenBao 2.7.1 does honour: P-521 is refused) on the API listener;
  `OA_TLS_MIN_VERSION=tls12` on the container is the documented way back and an unknown value stops the entrypoint.
  `openbao-server-tls` gets `rotationPolicy: Always` and `renewBefore: 2920h` (a third of its life, was 720h). OpenBao reads the pair at
  start and on SIGHUP only, so a `cert-reload` sidecar (same image, pod `shareProcessNamespace`) hashes the two files every 30 s and
  sends SIGHUP. **Real OpenBao in Docker (`scripts/dev/test-openbao-bootstrap.sh`): TLS 1.2 refused, 1.3 accepted, a new pair served on
  both listeners with the server staying unsealed and the audit device still writing.** Not run on a cluster: that cert-manager,
  ESO, the `bao` CLI in the Jobs, kubelet's probes and the Gateway's Envoy all speak 1.3, and a real renewal reaching a running pod
  through the Secret volume. The StatefulSet pod template changes, so the three pods roll once. It does NOT re-key `openbao-ca`
  (10 years; re-keying it means re-issuing every leaf) nor the Gateway certificate (a later batch).

- **Every route on the shared Gateway is registered with what authenticates its callers, and an opt-in component takes the OpenBao
  and Grafana routes off it** (ANSSI BP-028 R51 and R79 in part; the Zitadel first-admin half is the
  Zitadel entry of this section). The app load balancer has no source ACL, so each HTTPRoute on `openaether-gateway` answers anyone who sets its Host
  header. `docs/public-exposure.yaml` lists the five routes and their guard; `scripts/check-gateway-routes.py` (fixture-tested,
  in CI) fails on an unregistered route, a missing authentication, a route served by default that states none, or flags that
  disagree with the manifests. `apps/flux/components/private-admin-routes` (default off, a cluster adds it to its own overlay)
  deletes the `openbao` and `grafana` routes through Flux `spec.patches`; `scripts/dev/test-private-admin-routes.py` renders it
  and applies those patches to the real bricks (exactly two routes go). **Mocked/static: Flux's merge of `spec.patches` into a
  path, and the admin paths after the routes are gone (OpenBao through `bao_admin`, Grafana through a port-forward without its
  Zitadel login), were not run on a cluster.** It does NOT filter the app load balancer (an `app_lb_allowed_cidrs` ACL is the
  infra half), create a second Gateway for public applications (reviewed as high risk: every policy names `openaether-gateway`),
  or change what a deployed cluster serves unless its overlay opts in.
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
  kube-state-metrics, and the `ssss`/`gfsplit` split was not tried.** It does NOT detect an escrow of an old key by itself
  (the job never reads the key: the rotation runbook now deletes the ConfigMap and the `escrowed-at` annotation that
  OpenAether-infra's check 4.7 reads, and the Day 0 step sets that annotation), run the restore drill, or move the key out of the cluster (the
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

- **The component upgrade campaign is written down, not run** (ANSSI NT-35 R2, HYG-M34; apps half of TLS-01, F02, DP-18): `docs/patching.md`
  gains the order of the hops (cert-manager, Gateway API CRDs, Istio, Kyverno, CNPG operator, PostgreSQL, Longhorn), the `rotationPolicy`
  prerequisite for cert-manager 1.18 and the reason each hop needs a cluster. Rung: documentation only. **No version is bumped**: no
  upgrade path is provable offline (CRDs, ambient node DaemonSets, webhooks), and upstream tags and digests were not reachable. The
  waivers in `docs/eol-calendar.yaml` stay until the hops are done. The rest of these findings (calendar, check, Renovate alerts) is in the entry above.

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

- **The Kubernetes API audit log reaches Loki** (ANSSI PA-012 R2, R9 and Annex A; HYG-42 M36; batch B22 step 1, apps half
  of LOG-01, K8S/F12, OSN/F08, ADM/F08). A second Alloy release, `alloy-audit`, runs on the **control planes only**
  (nodeSelector, control-plane toleration) in its own privileged namespace `services-observability-audit`, reads
  `/var/log/audit/kube/kube-apiserver.log` through a read-only hostPath (DirectoryOrCreate) and pushes it to Loki as
  `{log_type="k8s-audit", node, cluster}`, the timestamp taken from the event's `stageTimestamp`. **Off by default**: the
  HelmRelease is `suspend: true` and the opt-in component `apps/flux/components/audit-log-collector` starts it (the alerts stay
  silent while the DaemonSet does not exist; a cluster owner adds `components: [../components/audit-log-collector]` to the overlay
  after the lab gate; removing it suspends the release but does not uninstall it, delete the HelmRelease too). It runs as root with
  a read-only root filesystem and ALL capabilities dropped except `DAC_READ_SEARCH` (the apiserver writes the file 0600 in a
  directory it owns, so root alone is refused; the file's owner and mode are still to be confirmed on a node), starts from the end of the file, and
  has no RBAC and no kube-apiserver egress: its CiliumNetworkPolicy allows DNS and Loki :3100 only, and Loki's ingress
  accepts it. Kyverno exempts it **by pod name** (`alloy-audit-*`) from `deny-hostpath`, `disallow-root-user` and the capabilities rule, not its
  namespace. Three alerts: `K8sAuditCollectorNotRunning`, `K8sAuditLogSilent` (no line read for 15 min), `K8sAuditLogNeverRead`.
  The local Docker overlay removes it. Retention needed no change here: Loki already keeps 8760h.
  **Rung: mocked/static, not run on a cluster**: `scripts/dev/test-alloy-audit.sh` runs the shipped config in a real Alloy
  on a fixture file (labels, backlog skipped), `scripts/dev/test-audit-collector.py` pins the narrow shape, the Kyverno CLI
  fixtures and the vmalert unit test cover the exemption and the silence alert. **Does NOT**: change the audit policy
  (Talos's default Metadata level stays: no request bodies), ship Talos service or kernel logs or the bastion's (infra),
  survive a pod re-creation without a gap (positions are in an emptyDir), or prove the file is readable on a real node.
- **Kyverno Enforce, per policy and per namespace, off by default** (ANSSI FT082 R1, R4, R8, R15 in part, NSA12 POD-01, POD-05,
  SSDLC-CONT-REC04; apps half of K8S/F04, SCC/F09, SCC/F03, K8S/F10; mocked/static, not run on a cluster):
  - `restrict-privileged`, `restrict-capabilities`, `require-security-context`, `restrict-image-registries` and `require-limits`
    carry `validationFailureActionOverrides`: Enforce in a namespace labelled `kyverno.openaether.io/enforce-<policy>: "true"`, Audit
    everywhere else. No namespace carries a label, so **behaviour is unchanged**; the only measurement on file (a fresh OVH cluster, 2
    Istio-generated proxies still failing `require-security-context`) is not zero, and the rules added since were never measured.
    Rollback: remove the label. Order, gate, exceptions and the Kyverno-outage note are in `docs/kyverno-enforcement.md`.
  - The flippable rules now match Pods at **CREATE only** (an update of a running pod is never denied). In Audit that means a pod
    changed after creation is no longer re-reported at admission; the background scan still sees it. `restrict-capabilities` is split in
    two rules so the `kubectl debug` ephemeral containers (an UPDATE) are still judged; the Pod rule is `deny-sensitive-capabilities`.
  - New opt-in component `apps/flux/components/psa-restricted`: Pod Security enforce baseline to restricted on foundation-networking,
    foundation-policy-engine, services-identity and the `capi-*` namespaces, for a cluster whose warn/audit lines are clean.
  - Does NOT: enforce anything by default, move kyverno off baseline, add Kyverno HA,
    `verifyImages`, digest pins, `LimitRange`/`ResourceQuota`, nor use Kyverno 1.13's `validate.failureActionOverrides` (the vendored
    1.12.1 rejects it). `KyvernoPolicyViolation` still ignores `require-security-context`.
  - **Rung: static** (`scripts/dev/test-kyverno-enforcement.py`, `test-psa-restricted.py`, the Kyverno CLI fixtures; all in CI).


### Changed

Upgrade campaign, phase 1 (independent low-risk bumps, one commit each; static / rendered offline, **not run on a cluster**).
Phase 2 follows below.

- **local-path-provisioner v0.0.31 -> v0.0.37** (re-vendored; the dump joins `apps/vendored-dumps.sha256`). Local changes kept: provisioner
  securityContext, no `--debug`, the `local-path-retain` StorageClass, the `busybox:1.37.0` helper pin. v0.0.36 validates the helper-pod template
  (security fix GHSA-7fxv-8wr2-mfc4); this repository's template passes. Upgrade note: the provisioner Deployment restarts once (new probes on :8080);
  existing PVs are untouched.
- **prometheus-node-exporter chart 4.39.0 -> 4.60.0** (node_exporter 1.8.2 -> 1.12.1). Upgrade note: the DaemonSet rolls node by node; the chart now
  excludes kubelet/containerd/docker mount points and erofs from `node_filesystem_*`; the `/var` alerts keep their series. No metric the repository reads was renamed.
- **kube-state-metrics chart 5.25.1 -> 8.6.0** (2.13.0 -> 2.20.0). Upgrade note: the pod restarts once; `kube_pod_status_reason` now has a row only for the
  reason that is set; `kube_endpoint_address_*` is gone (no rule used it). The explicit collectors list is kept, so endpointslices stay off.
  The `customResourceState` for the Flux kinds was not run against 2.20.0: check `gotk_resource_info` after the roll.
- **metrics-server chart 3.12.2 -> 3.14.0** (0.7.2 -> 0.9.0). Upgrade note: the pod restarts once and is Ready only when both node and pod metrics exist.
- **restic 0.18.0 -> 0.19.1** (the four backup CronJobs). Upgrade note: the push scripts handle the new exit codes explicitly (3 = incomplete snapshot: the Job
  fails at once; a failed retention fails the Job after the replica got its snapshot); `prune` repacks small packs more, so expect more S3 traffic.
  The repository format is unchanged in the release notes read.
- **Gateway API CRDs v1.1.0 -> v1.2.1** (standard). Upgrade note: the CRDs are not rolled back by Helm or Flux pruning; GRPCRoute/ReferenceGrant `v1alpha2`
  disappear (check `storedVersions` first, see the lab checklist); `Gateway.spec.infrastructure` is now accepted. **Deliberately not 1.5.x**: Istio 1.24
  has no maximum-version filter and istiod does not become Ready with the 1.5 CRDs (istio/istio#59443); 1.5.x waits for the Istio hops.

Upgrade campaign, phase 2 (the operators, data services and charts that were past support, one commit each; rendered, schema-checked and, where a
container image exists, run offline; **nothing run on a cluster**). Not in this phase: Istio (still 1.24.2, waived), MinIO (dev profile only, no
maintained upstream, waiver kept), OpenBao (stays 2.7.1), talos-cloud-controller-manager on the children (0.5.8 moves the CCM to the Talos 1.14 line, the children run Talos 1.13).
**A fresh install lands on the new versions; a running cluster must hop one minor at a time where the project says so** (docs/patching.md).

- **Longhorn 1.9.2 -> 1.12.1.** A cluster on 1.9 hops 1.10.2 -> 1.11.3 -> 1.12.1 (Longhorn refuses a skipped minor and any downgrade); the preconditions are in
  `docs/patching.md`. The HelmRelease no longer remediates (install and upgrade `retries: 0`): a failed upgrade is not rolled back by Flux, a person fixes forward.
  The chart's own NetworkPolicies are switched off (`networkPolicies.restrictInternalTraffic: false`) so the brick's CiliumNetworkPolicy stays the only authority.
  `defaultSettings.orphanAutoDeletion` is not a chart key and never was in effect: orphan auto-deletion is NOT on.
- **cert-manager v1.15.3 -> v1.21.2.** `installCRDs` replaced by `crds.enabled` + `crds.keep` (same CRDs). The controller metrics port is renamed
  `http-metrics`: the VMServiceScrape follows. All Certificates already named their `rotationPolicy`. Step one minor at a time on a running cluster.
- **Kyverno 1.12.1 -> 1.19.1** (re-vendored). The two cleanup CronJobs no longer exist upstream (their patch and test are gone); PolicyException stays off and
  `--exceptionNamespace=kyverno` is set; images come from `reg.kyverno.io` (allow-list updated); the CLI test fixtures and CI pin CLI 1.19.1.
  **1.19 is the last line serving `kyverno.io/v1` ClusterPolicy: the policies must move to `policies.kyverno.io` before 1.20.**
- **CloudNativePG operator 1.23.1 -> 1.30.1** (re-vendored) and the grafana-db image 16.3 -> 16.15. Every operator upgrade rolls each Cluster with a switchover.
  **`barmanObjectStore` (used by both Clusters) is removed in CNPG 1.31: stay on 1.30.x until the Barman Cloud plugin replaces it.**
- **External Secrets Operator 1.2.0 -> 2.13.0** (re-vendored from the chart, local patches re-applied). Released the day it was vendored; upstream supports only the latest minor.
- **Flux on the children 2.8.8 -> 2.9.6; cluster-api-operator 0.27.0 -> 0.29.0; OpenStack Resource Controller 2.4.0 -> 2.6.0** (what the pinned CAPO v0.15.0 requires);
  **victoria-metrics-operator 0.65.1 -> 0.68.1** (one rolling restart of the VMAgent, VMAlert, VMSingle and VMAlertmanager pods; the VM component versions are the operator's
  defaults and move with it, they are not pinned here).
- **Alloy 0.11.0 -> 1.13.1** (Alloy 1.6.1 -> 1.20.1; the config-reloader image changes, allow-list updated), **Zitadel chart 10.0.2 -> 10.4.0** (v4.19.2, not 4.18.0),
  **Loki 6.25.0 -> 18.16.2 (3.7.8)** and **Grafana 8.6.4 -> 14.0.1 (13.2.4)**, both now from the `grafana-community` chart repository (new HelmRepository, new Flux egress FQDN).
  Grafana: the duplicate `tmp` mount is removed and default plugin preinstall is disabled. Zitadel and Grafana database migrations are forward-only: take a CNPG Backup first.
- **Cilium on the children 1.19.2 -> 1.19.9** (one minor behind the parent foundation, on purpose).
- Offline checks that now run the real software: Kyverno CLI 1.19.1 (90 fixtures), Alloy 1.20.1 (`validate`, scrub and audit tests), Zitadel v4.19.2 + PostgreSQL (32 checks),
  Loki 3.7.8 `-verify-config`, Grafana 13.2.4 with the shipped ini and dashboards, CRD-schema validation of the Cluster, ExternalSecret, provider and VM objects.

### Added

- **Kubelet and apiserver scrapes verify TLS; metrics-server and the kubelet scrapes can too, opt-in** (ANSSI TLS-11, K8S-F14; apps half of
  B23). The `kube-apiserver` VMServiceScrape no longer sets `insecureSkipVerify`: it verifies against the ServiceAccount CA with
  `serverName: kubernetes.default.svc`. The new component `apps/flux/components/kubelet-verified-tls` (default off, a cluster adds it to its
  own overlay, rollback = remove the line) drops `--kubelet-insecure-tls` from metrics-server (it gets `--kubelet-certificate-authority`)
  and `insecureSkipVerify` from the `kubelet` and `cadvisor` VMNodeScrapes (they get `caFile`). **Enable it only once infra's
  `kubelet_serving_cert_rotation` is on**: on Talos' default self-signed kubelet certificates `kubectl top` and the node targets would fail.
  **Rung: mocked/static, not run on a cluster**: `scripts/dev/test-kubelet-verified-tls.py` (in CI) renders both states and applies the Flux
  patches to the real bricks. **Does NOT**: approve kubelet CSRs or change the kubelets (infra), cover the Grafana-to-Zitadel trust or the
  Postgres `sslmode` hops (B25), or prove that the apiserver certificate carries `kubernetes.default.svc` on every provider.

- **OpenBao's metrics are scraped and its alerts are deployed** (ANSSI PA-012 R12 and C3.1 in part; LOG-06). The earlier
  `prometheusrule.yaml` and `servicemonitor.yaml` were never in a kustomization, used Prometheus Operator kinds this platform does
  not install, and read `openbao_*` metrics: OpenBao 2.7.1 serves `vault_*`. They are replaced by a VMServiceScrape and a VMRule
  in a new companion brick `observability-openbao` (Flux Kustomization of the same name; picked automatically with
  observability + OpenBao): `OpenBaoWorkloadSealed`, `OpenBaoNoActiveNode`, `OpenBaoRaftNoFailureTolerance`, `OpenBaoRaftApplyLag`,
  `OpenBaoMetricsDown`. Metrics need a token on the API port, so a second listener on 8202 serves them without one, on the
  headless Service only (not on `openbao-ui`, so not through the Gateway route), admitted by the OpenBao network policy for vmagent
  alone (the old rule named a pod that never existed); vmagent's egress port moves from 8200 to 8202. The scrape verifies the
  certificate against the CA of `openbao-server-tls`. **Mocked/static: the real OpenBao serves every metric the rules read, sealed
  and active (harness); vmalert parses the rules and `vmalert-tool` runs the sealed and no-active-node logic on synthetic series.
  Not run on a cluster:** the operator reading the CA Secret across the scrape's namespace, a sealed pod being scraped, the
  alerts firing. It does NOT cover the audit log (no Loki ruler) and leaves `scripts/ops/check-alert-metrics.py` (OpenAether-infra)
  to be pointed at the new rules.
- **OpenBao logs as JSON, and the audit trail carries `log_type="audit"` in Loki** (ANSSI PA-012 R1, R6 and R23 in part; LOG-15).
  `log_format = json`, `log_level = info` in OpenBao; an Alloy `stage.match` on `foundation-vault`/`openbao` lines that start
  `{"time":...,"type":"request"|"response"` adds the label (a real Alloy, `scripts/dev/test-alloy-scrub.sh`: only the audit lines get it).
  `docs/detection.md` lists the format and level of each component. The `cluster` label stays the constant `management`: only that
  cluster runs Alloy, and templating it needs a `substituteFrom` the local Docker lane cannot satisfy. Not run on a cluster.

- **Alerts for the log pipeline, the policy engine, Flux, the node disk and clock, and the backups.** 29 rules in
  `vmrule-logging.yaml` (Alloy missing or dropping, Loki push errors, replica down, discards, ingestion burst, Grafana 5xx),
  `vmrule-security.yaml` (Kyverno violations at admission time, webhook down, Flux object not Ready or suspended), and additions to
  the cluster, platform and backup rules: `/var` (EPHEMERAL, where local-path volumes live) filling and full within 24 h, clock
  offset and sync status, Cilium policy denials in a loop (Hubble is off), restore-test CronJobs stale for 35 days, a missing backup
  CronJob, CNPG WAL archive failing, base backup failed or stale. Most families have an `*Absent` guard so a vanished scrape does not
  read as "all clear" (`docs/detection.md` lists the ones without: the node disk and clock rules, `CiliumPolicyDeniedBurst`,
  `GrafanaServerErrors`, `RestoreTestStale`). `docs/detection.md` lists what is covered and what is not, and the `kubectl get cpol,polr -A` procedure to
  review Audit violations. `scripts/dev/test-vmrules.sh` (new CI job) runs every rule through a pinned vmalert `-dryRun`
  and checks the conventions `check-alert-metrics.py` relies on. Addresses ANSSI PA-012 R12, R21, R22, C3.1 and C3.2, HYG-M36,
  GH-BKP-2, NSA12-LOG-04 and SSDLC-CICD-MON-01/02. Rung: parsed by vmalert and built by kustomize, **never fired and never
  evaluated against real data**: metric names were read in the sources of Alloy v1.6, Loki v3.3.2, Kyverno v1.12.1, CNPG v1.23.1 and
  node-exporter (read at v1.9, the chart pins 1.8.2: the metric names are the same in both) (the Kyverno counter's `_total` suffix is the OpenTelemetry exporter's convention, not read in code), thresholds are
  guesses, and `NodeClockNotSynchronised` may fire on healthy Talos nodes. It does **not** alert on OpenBao authentication or root
  use, `kubectl exec`, RBAC changes or an empty stream for one namespace (no audit log, no Loki ruler), keeps `require-security-context`
  out of `KyvernoPolicyViolation` until its 74 known violations are fixed, and does not set the time sources of the nodes (the
  OpenAether-infra half of the clock finding).

- **Scrapes for what the new alerts read, and Flux per-object state.** Alloy, Loki, Grafana, Kyverno (the four controllers, :8000),
  Zitadel and the CNPG instances (:9187) had no scrape, so no rule could see them fail; `vmscrapes-pipeline.yaml` adds them, with
  the ingress rule for vmagent in the Loki, Alloy, Kyverno and Zitadel CiliumNetworkPolicies and the four ports in vmagent's egress
  list (Grafana already accepts its whole namespace; CNPG already accepted vmagent on :9187). kube-state-metrics now also exports
  `gotk_resource_info{ready,suspended}` for Kustomizations, HelmReleases, GitRepositories and HelmRepositories (the mapping of the
  Flux docs, `customResourceState` plus list/watch on those four kinds, no secrets in those extra rules; the chart's own role is trimmed in the Fixed section below): the Flux controllers export no per-object
  condition (kustomize-controller v1.8.5 records only the reconcile duration, read in its source), so `gotk_reconcile_condition`
  named by the audit can never carry data. Prepares ANSSI PA-012 R12, NSA12-LOG-04 and SSDLC-CICD-MON-01; closes the gap #44 left open (Refs #44). Rung: kustomize build
  and chart render, not run on a cluster. No rule reads the Zitadel metrics (names unchecked), and the kube-state-metrics
  pod restarts once (one replica: a gap of a minute in the `kube_*` series).

### Fixed

- **Longhorn's csi-snapshotter no longer errors every 16 seconds (#137).** The `snapshot.storage.k8s.io` CRDs were not installed, so the sidecar failed to list `VolumeSnapshotContent` and
  `VolumeSnapshotClass` for ever (about 290 error lines per 40 minutes on an idle cluster, three clouds, lab 16) and its pods restarted during the first hour. The chart cannot disable the sidecar
  (only its replica count and image are values), so the `storage` brick now vendors the three external-snapshotter v8.6.0 CRDs (sha256 lock, Renovate and end-of-life calendar entries).
  The snapshot controller is NOT shipped: CSI VolumeSnapshots stay unsupported, volume backups use Longhorn's own objects. `test-longhorn-values.py` fails without the CRDs.

- **The Loki PodDisruptionBudget is rendered by Helm 3, which Flux uses (found by CI on the 0.3.0 pull request).** `podDisruptionBudget` was set at the top level of the Loki values, the key chart 18
  deprecates; Helm 3 merges the empty `singleBinary.podDisruptionBudget` over it and renders no PDB, Helm 4 renders it. The value now sits under `singleBinary`. `test-loki-render.sh`
  catches it with Helm 3 (it passed on the Helm 4 of the maintainer's host). A cluster that took the 0.3.0 candidates from the labs had no Loki PDB; whether the old chart honoured the top-level key was not checked.

- **A rejected commit signature raises its own alert (infra #337).** kube-state-metrics now exposes the GitRepository condition `SourceVerified` as the label
  `source_verified` of `gotk_resource_info`, and `FluxSourceNotVerified` (critical, 5 minutes) says "Flux rejected the commit: unsigned or unknown signer" instead of
  leaving it to the generic `FluxObjectNotReady` after 20 minutes. Silent while `git-verify` is off. Synthetic-series unit test in `vmrules-unittest.yaml` (run by
  `test-vmrules.sh`); that the condition really reaches the metric needs a cluster with git-verify on.

- **The Kubernetes 1.37 decision is recorded (apps #105, #120).** `docs/patching.md` states the owner decision (keep 1.37.1, labs prove it), what the labs do and
  do not show, and the per-hop re-read of each add-on's matrix. No automated matrix check: the matrices are prose, not data.

- **VictoriaMetrics and Alertmanager images are pinned, and the operator probe values are real chart keys (apps #117).** VMCluster (`v1.136.0-cluster`),
  VMAgent and VMAlert (`v1.136.0`) and VMAlertmanager (`v0.31.0`) now set `image.tag` to what operator 0.68.1 chose by default, so nothing moves today
  and an operator bump no longer moves the storage engine with it. The operator's `probePath`, `livenessProbe` and `readinessProbe` were ignored by
  the chart; they are now `probe.readiness` / `probe.liveness` `failureThreshold: 10`. New test `scripts/dev/test-vm-pins.py` (CI). VictoriaTraces is not
  deployed here. Bumping the pins is now a deliberate edit with the operator hop.

- **Zitadel: the recovery of a setup Job that died half way is written down (apps #133).** `docs/traps.md` gives the measured procedure (delete the Job, remove
  the one `started` event and its unique-constraint row on a first install, reset the HelmRelease) and says to restore from backup instead on a database
  that holds data. An automatic clean-up is not done (it edits the event store from a hook; needs a cluster, 0.4.0).

- **Longhorn first-install remediation is decided and documented (apps #134).** `install.remediation.retries` stays 0: Helm's remediation is an
  uninstall, and Longhorn's pre-delete Job refuses it while `deleting-confirmation-flag` is false (read in longhorn-manager v1.12.1) and could remove
  data when it is true, so a retry would strand the release instead. The one-command reset after a transient first-install error is in
  `docs/patching.md` ("A first install that stalls"); `test-longhorn-values.py` pins the choice. An automatic retry is left to 0.4.0.

- **Longhorn: the dead `defaultSettings` keys are gone and orphan auto-deletion stays off on purpose (apps #115).** `orphanAutoDeletion` and
  `defaultSettings.backupTarget` are not keys of the chart (1.12.1 reads `orphanResourceAutoDeletion` and `defaultBackupStore.backupTarget`);
  they were silently dropped, and the comment claimed orphan deletion was on. It is not, and is left off: an orphan is replica data
  with no replica object, which a restored etcd or a rebuilt node also looks like. New test `scripts/dev/test-longhorn-values.py` (CI) fails on a
  `defaultSettings` key the pinned chart does not read. No cluster effect: the keys were already ignored.

- **The two Gateway pods are spread over the nodes (lab 15c W2 follow-up).** The HPA floor of 2 and the PDB (dc6e12f) let a drain evict one
  pod, but both replicas had landed on the same node, so a node loss still cut the ingress. Istio 1.24 offers no scheduling setting for the
  Deployment it generates (`infrastructure.parametersRef` is in the CRDs, istiod 1.24 does not read it), so a Kyverno mutation
  (`kyverno-policies/spread-gateway-pods.yaml`, pods of `openaether-gateway` at CREATE) adds a `topologySpreadConstraint` and a preferred
  anti-affinity on the hostname. Soft on purpose: a one-worker cluster still runs both. Running pods keep their node until recreated
  (`kubectl -n services-gateway rollout restart deployment openaether-gateway-istio`). Test: Kyverno CLI mutation test in
  `scripts/dev/test-kyverno-policies.sh`; whether the scheduler then separates the pods needs a cluster.

- **Loki chart hops are not rolled back by Helm, and get a 15 minute budget (apps #121, lab 15c).** The in-place hop 6.25 -> 6.55 timed out at
  the default 5 minutes (loki-1 never Ready: the memberlist join dialled the address of a pod that no longer existed) and the automatic Helm
  rollback re-rolled the StatefulSet to the old chart, which stalled the same way and left it at 1 of 2 Ready. `upgrade.timeout: 15m`,
  `upgrade.remediation.retries: 0` and `remediateLastFailure: false` now; a fresh install keeps its three retries. Chart 18 starts Loki with
  `-memberlist.advertise-addr=$(POD_IP)` (checked in the render). The exact recovery is in `docs/patching.md`, "Loki chart hop". Test:
  `scripts/dev/test-loki-render.sh` (fails with the old values). Static; the stale-address behaviour itself needs a cluster.

- **Certificates: rotation policy written down on every one, and a guard that routes are covered** (review of B18/B19; static,
  not run on a cluster). `openbao-ca` and `openbao-ca-bundle` now say `privateKey.rotationPolicy: Never` (the value cert-manager
  applies today, so no behaviour change on 1.15; without it the 1.18 hop would re-key the 10-year listener CA, as
  `docs/patching.md` already warned). `scripts/dev/test-certificates.py` (CI) fails if a Certificate omits the policy or an
  HTTPRoute hostname is missing from the Gateway Certificate's `dnsNames` (the role refuses wildcards). It does not stop a
  route added from a cluster's own overlay outside this repository. `docs/key-inventory.md` renewal windows corrected.
- **metrics-server args guard** (review of B23, static): `scripts/dev/test-kubelet-verified-tls.py` now checks that the
  `kubelet-verified-tls` component leaves the other metrics-server args in place (the Flux patch replaces the list), so an arg
  added to the base later cannot vanish silently when the component is on. Does not remove the list-replace itself.
- **Alloy's control-plane toleration was ignored** (found while adding the audit collector; LOG-01). `tolerations` sat at the
  top of the `alloy` HelmRelease values, where the chart does not read it (`controller.tolerations` is the key), so the
  pod-log collector never ran on a tainted control plane. It now does. The same file's top-level `resources` is also not a
  chart value (`alloy.resources` is): left as is, the limits would be a behaviour change on a running cluster, to be decided
  with measurements. Rung: `helm template`, not run on a cluster.

- **local-path-provisioner no longer runs with `--debug`** (ANSSI PA-012 R6; LOG-15). It is the default StorageClass of the base
  profile and logged every volume path and helper-pod step. Static only.

- **Zitadel's access log no longer carries tokens from a query string into Loki (review of the ANSSI branch, L9).** Zitadel's
  access log keeps the request URL, and an OIDC logout puts the ID token in it (`end_session?id_token_hint=...`): checked on a real
  Zitadel v4.14, the token reached stdout, and Loki keeps lines for a year. Alloy gets a `loki.process "scrub"` stage for the
  `services-identity` namespace that blanks the value of `id_token_hint`, `*_token`, `*_secret` and `assertion` parameters
  (the parameter and the rest of the URL stay). `scripts/dev/test-alloy-scrub.sh` runs the shipped block in a real Alloy v1.6.1
  against the log shape Zitadel produces (new CI step); `test-zitadel-config.sh` records the behaviour. Also corrects a stale
  script name in the Zitadel HelmRelease comment. Rung: a real Alloy and a real Zitadel in containers; **not** run on a cluster,
  with Loki or with the Grafana logout. It does NOT scrub other namespaces, other secrets that might appear in a URL, or lines
  already in Loki, and every log line of the cluster now passes through one more Alloy component (a line filter keeps the cost
  to the matching lines).

- **The escrow check's "never reads the Secret" is now tested, and its pod has a writable `/tmp` (review of the ANSSI branch, L1, L5).**
  `scripts/dev/test-escrow-check.sh` asserts on the manifest that the Role grants `get` on the three named ConfigMaps and
  nothing else (adding `secrets` to it used to leave the test green), and that the root filesystem is read-only with an
  in-memory `emptyDir` as `/tmp` and `HOME`, where kubectl keeps its discovery cache (the image has `HOME=/`). Whether kubectl
  actually failed without it was not observed; the change is the cheap hardening. The Day 0 text of the vault README now says how
  a k-of-n reconstruction becomes the raw key that step 5 hashes (an ssss share set is hex text and needs `xxd` on both
  sides; neither split tool was run). Rung: static and a stubbed kubectl, also under the CronJob's image; the CronJob was not run on
  a cluster.

- **Two alerts of the observability work could not do their job (review of the ANSSI branch, M2, M3).**
  `OpenBaoEscrowNotAttested` read only `kube_cronjob_status_last_successful_time`, which kube-state-metrics emits once a run has
  succeeded: on an existing cluster where the check never succeeds the alert could not fire. A second branch now fires when that
  series is absent and the CronJob is older than 36 h (a new cluster is given its first daily run). `KyvernoMetricsAbsent` is silent
  when no Kyverno Deployment exists, as Kyverno is optional in `pick.py` (it warned every 12 h for ever on such profiles).
  `scripts/dev/vmrules-unittest.yaml` runs both on synthetic series with `vmalert-tool`, from `scripts/dev/test-vmrules.sh` (CI job
  `vmrules`); the old expressions fail it. Addresses ANSSI PA-012 R12. Rung: vmalert-tool on synthetic series; the alerts have
  **not** fired on a cluster.

- **kube-state-metrics no longer lists Secrets and ConfigMaps cluster-wide (review of the ANSSI branch, M4; same class as the
  vmagent, Alloy, Loki and Grafana entries above).** The chart's default ClusterRole lists Secrets, which returns their data; no rule,
  scrape or dashboard reads `kube_secret_*` or `kube_configmap_*`, so the `collectors` list drops both. `test-observability-rbac.sh`
  now renders this chart as well (it examined only Alloy and Grafana, so this role escaped it). Rung: chart render, role not read back
  on a cluster. A collector added upstream later must be added to the list by hand.

- **Two 0.2.0 lines the merge had dropped are back (review of the ANSSI branch, L10, L11).** `.gitleaks-envdata.toml` has its
  `\.terraform.*/` path exclusion again (this repository has no such directory, so nothing changed today), and Loki's
  `sidecar.securityContext` of 0.2.0 sits next to `sidecar.rules.enabled: false`: the rules sidecar does not exist now, but re-enabling
  it no longer brings a container without the context. `scripts/dev/test-loki-render.sh` asserts the context in the values.
  Rung: render and static checks, not run on a cluster. It does NOT re-enable the sidecar.

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

## [0.2.0] — 2026-10-15

**The pod-security work the first real clusters asked for, a Loki that no longer shares the backups bucket, and a scan for environment
data in pull requests. For new clusters.** Read *Upgrade notes* and *Not fixed* before pulling it, and the rung written under each entry:
this file distinguishes static checks, CI, local Docker, and real clouds.

**Pairing with OpenAether-infra.** Infra tags pin an apps tag and cannot be moved, so this is a table of what was proven:

| OpenAether-infra | apps | Status |
|---|---|---|
| 0.2.1 (not yet cut) | 0.2.0 | The intended pair. Validated with an OpenAether-infra branch (`39df1b8` on OVH and Outscale, `4ae2357` on Scaleway), not a tag. |
| 0.2.1 (not yet cut) | 0.1.1 | **Not run.** Infra 0.2.1's code (the Loki seed, the logs bucket) was only run with apps 0.2.0 candidates; the only validated 0.1.1 pair is infra `f127a26` (a commit on main) with apps 0.1.1. Infra 0.2.1's pin moves to 0.2.0. |
| 0.2.0 | 0.2.0 or 0.1.1 | **Not supported**, for the reasons given under 0.1.1 (no operator PGP keys, a seed script that waits for a root-token Secret). |
| 0.2.0 | 0.1.0 | The pair infra 0.2.0 validated on its three clouds with Flux off. |

### Upgrade notes

- **VictoriaMetrics data written as root cannot be opened by the new non-root workloads (#47, #71).** vmstorage and vmselect now run as
  uid 65534; on a cluster that ran the earlier code their local-path volumes hold files owned by root and the pods crash with
  `cannot create lock file … permission denied` (seen on Scaleway, 2026-10-15). `fsGroup` does not help: the local-path
  provisioner's volumes are not ownership-managed. What worked: delete the PVC and the pod of each vmstorage, then of each
  vmselect (`vmselect-cachedir-*` is a cache), one at a time. **That loses the metric history of that node; with
  `replicationFactor: 2` the second node keeps its own until it is treated too, so treating both loses all of it.** Take a
  `vmbackup` first if the history matters. New clusters are not affected. Rung: real cloud, once (Scaleway, a cluster that booted on a candidate commit of this release and was moved by Flux commit by commit; not
  a cluster built from the 0.1.1 tag).
- A cluster initialised by 0.1.0 still cannot be moved to this release in place (see 0.1.1): build a new cluster, on new buckets.
- Loki, Grafana, Alloy, kube-state-metrics, node-exporter, the VictoriaMetrics pods, cert-manager, the CNPG operator and istiod restart once
  (new container `securityContext`); the ones using the Longhorn or local-path volumes come back on them.

### Changed

- **Kyverno `require-security-context`: the container-level `securityContext` it reads is now set on the observability stack, cert-manager,
  the CNPG operator and istiod (#47, #37).** Grafana and its sidecar, kube-state-metrics, node-exporter, the VictoriaMetrics operator,
  Alloy and its config reloader, Loki and its rules sidecar, and every VMAgent / VMAlert / VMAlertmanager / VMCluster pod (their
  `spec.securityContext` takes the pod and container fields flat; `containerSecurityContext` is not a field of these CRs and was
  silently ignored) run with `runAsNonRoot`, `allowPrivilegeEscalation: false`, `drop: [ALL]` and `seccompProfile: RuntimeDefault`.
  The VictoriaMetrics images run as root, so they also get `runAsUser: 65534`: with `runAsNonRoot` alone the containers fail with
  `CreateContainerConfigError`. **Measured on Scaleway management clusters: 82 `require-security-context` results (and 3 of `restrict-image-registries`)
  on the 0.1.1 candidate, 2026-10-14; 86 results across the policies at the start of the 2026-10-15 lab, and 8 on the policy report at its end,
  2 of them genuine:** the Istio-generated `istio-proxy` of the north-south Gateway and of the waypoint carry seccomp on the pod
  but not on the container, and `Gateway.spec.infrastructure` (the only knob) is not in the Gateway API v1.1.0 CRDs this repository
  installs (it arrives in v1.2), so they stay until those are bumped. The other 6 are `ztunnel` (5) and the Outscale provider
  controller, which the policy now exempts. **Hypothesis, not observed:** Kyverno had not re-evaluated them when the lab ended (the policy
  changed during the lab; a fresh cluster does not list them, and whether a later background scan drops them was not watched, #37). `ztunnel` (the ambient node proxy, root with NET_ADMIN by design) and the Outscale CAPI provider controller (an upstream manifest)
  are exempt by name/namespace; the registry allow-list gains the single image `wait4x/wait4x` (the Zitadel chart's init container,
  which accounted for the `restrict-image-registries` results: the Deployment, ReplicaSet and Pod entries of one init container). **The three policies stay in Audit** (#37): Enforce needs a refreshed report at
  zero and a lab that survives a node roll, and neither exists yet. Rung: real cloud, once, by hand.

- **Comments and docs follow OpenAether-infra's Loki bucket (#53 rest, infra #302).** The seed now gives Loki a bucket of its own
  and a dedicated key pair when the operator supplies one; with the shared keys it can still reach the backups. No manifest
  changes: the ExternalSecret reads whatever `observability/loki-s3` holds. Not changed: 2 replicas with replication factor 2.

### Added

- **Pull requests are scanned for environment data, not only credentials (#41).** A `leaks` job in `ci.yml` runs gitleaks with
  `.gitleaks-envdata.toml` over what a PR adds: routable IPv4 addresses, 12-digit account ids and account/project UUIDs, concrete
  `s3-…-backups`/`-logs`/artifact bucket names, on top of the default credential rules. The rules were measured against their own probe
  (`scripts/dev/check-gitleaks-rules.sh`: 16 values it must catch, 13 it must leave alone, including mixed lines and prefixed key names, which an
  independent review found slipping through the first version; the probe runs before the scan because a rule that
  matches nothing passes silently). A /16-or-wider network block (a provider's published range) is let through, a /32 is not. **Known limit:** a
  line that carries a version word (`version`, `chart`, `sha256`…) is excused whole, so a real address on that same line passes.
  The file is not named `.gitleaks.toml` on purpose: `security.yml` keeps scanning the full history with the default rules.
  Rung: CI and a local run over this tree (no finding after the allowlists). Not made a required check: that is a repository
  setting.

- **A static guard for the two mistakes of this block** (`scripts/dev/test-container-security-context.py`, in CI): the VictoriaMetrics CRs must
  carry a complete `spec.securityContext` with a non-zero uid and no `containerSecurityContext` (a field the operator ignores), and no
  Gateway may use `spec.infrastructure` while the vendored Gateway API CRDs are older than v1.2. Its self-test fails on each mistake.
  Rung: CI (static; it proves the manifests, not the operator).

### Validated

- **The 0.2.0 candidate on three clouds, 2026-10-15, with OpenAether-infra `claude/infra-0.2.1` (not a tag).** Rung: real cloud, **once per
  cloud**, driven by hand; nothing is repeated or unattended, and `cluster-up` still ends 201 on the documented `flux-system exists` line
  until the Day-1 seeding has run. Management role, 3 control planes + 2 workers, `deploy_flux = true`, OpenBao in production mode with 3
  throwaway PGP recovery keys, threshold 2, the apps followed by branch.
  - **Scaleway, full DAG** (infra `4ae2357`, replica on OVH; Flux followed `claude/apps-0.2.0` through the commits of this block, so the
    final tree was not the one that booted): 35 of 35 Kustomizations Ready about 3 minutes after the seeding; Loki chunks and index in its own
    logs bucket while the backups bucket held only `cnpg/` (no Loki object); CNPG `grafana-db` and `zitadel-db` archiving True; Longhorn
    BackupTarget available; `cluster-security` 30 of 31; strict plan "No changes". Found on this lab: the VictoriaMetrics CR field
    name and the root user of its images (both fixed, and guarded by a test), root-owned volumes (an upgrade note, not fixed: #71), and a `Gateway.spec.infrastructure` attempt that broke the `istio`
    Kustomization for about six minutes until reverted (that field needs Gateway API v1.2).
  - **OVH, full DAG** (infra `39df1b8`, apps `82f5919`, replica on Scaleway): 35 of 35 about 2.5 minutes after the seeding, every pod Running 4
    minutes later; Loki objects in its logs bucket, backups bucket `cnpg/` only; both CNPG clusters Ready and archiving; Longhorn available;
    **the Kyverno report of this fresh cluster held exactly 2 results (the Istio-generated proxies), against the 82 + 3 above and 86 at the start of the Scaleway lab**; no Cilium drop in a
    40 s window; `cluster-security` 30 of 31; strict plan "No changes". One worker did not come up after boot (OVH, infra #49): hard-rebooted, `up` re-run.
  - **Outscale, REDUCED profile only** (infra `39df1b8`, apps `82f5919` + a throwaway profile commit, 18 Kustomizations, no observability, CNPG,
    Longhorn or Istio, so none of the entries above is observed there): 18 of 18 Ready; an OpenBao snapshot job pushed to both destinations
    and the restic check found no error; `cluster-security` 25 of 31 (the four failures are the mesh checks, there is no Istio in that profile).
  - **What ran is `82f5919` plus the throwaway profile on OVH and Outscale; nothing after it was run on a cluster.** The commits since touch CI,
    scripts, documentation and the gitleaks rules only (`apps/` is byte-identical).
  - **Not run:** a rolling upgrade or any upgrade from a 0.1.1 cluster beyond the Scaleway one that was moved by Flux commit by commit, Kyverno
    Enforce, a DR restore, a CAPI child, the local Docker lane on prod mode. CI on the pull request is the other rung (see the PR).

### Not fixed

The two results left by the Istio-generated proxies (#70) and the loss of the VictoriaMetrics history on a 0.1.1 → 0.2.0 move (#71) are
new and open. Unchanged since 0.1.1: #34 (half: a first run interrupted inside the root window still fails again), #35, #36 (cause of
the `promote` no-op unknown), #37 (the three Kyverno policies stay in Audit), #47 (partly: the optional-key anchors and the image names are done, 2 results remain, #70), #38 (guard, not a cure), #39 (static only), #42, #44 (no
per-object Flux Ready signal), #51 (new clusters only), the rest of #53 (2 Loki replicas with replication factor 2; the dedicated Loki key
pair was never run on a cloud, infra #302). **Not observed anywhere:** a disaster-recovery restore, a rolling upgrade of the OpenBao
replicas, Kyverno Enforce, a CAPI child, prod-mode local Docker, a Longhorn backup or restore. This release does not make `task cluster-up`
produce a working platform by itself: the Day-1 seeding, the PKI signing and the Grafana OIDC client stay manual.

### Maintainers

Merge by **squash**. The ruleset on `main` requires `Lint`, `Flux DAG`, `Kustomize Build + Collision Check` and `Secret Detection`; the new
`Environment data in new commits` job is not required yet (a repository setting). The tags ruleset (id 25117854) forbids moving a tag.

## [0.1.1] — 2026-10-14

**The fixes the first real Flux runs asked for, for new clusters.** Read *Upgrade notes* (a cluster
initialised by 0.1.0 cannot be moved in place) and *Not fixed* before pulling it, and the rung written under each entry: this file
distinguishes static checks, CI, local Docker, and real clouds.

**Pairing with OpenAether-infra.** Infra tags pin an apps tag and cannot be moved, so this is a table of what was proven:

| OpenAether-infra | apps | Status |
|---|---|---|
| 0.2.0 | 0.1.0 | The pair infra 0.2.0 validated on its three clouds with Flux off (2026-10-12); one Scaleway run reconciled apps 0.1.0 by hand with Flux on (2026-10-12, UTC). |
| 0.2.0 | 0.1.1 | **Not supported.** Infra 0.2.0 has no operator PGP keys (OpenBao would start in development mode) and its `seed-openbao.sh` waits for a root-token Secret that 0.1.1 never creates. |
| 0.2.1 (not yet cut) | 0.1.1 | The intended pair. Validated with OpenAether-infra `f127a26` (a commit on main, not a tag), on the clouds and commits listed under Validated. |
| main or 0.2.1 | 0.1.0 | Not supported: the seed script and `cluster-security` refuse it by design. |

### Added

- **A local Docker validation lane** (`.github/workflows/local-docker.yml`, `scripts/dev/local-docker-lane.sh`,
  `docs/local-docker.md`): on a GitHub-hosted runner, infra's own `task local-up` (1 control plane + 1 worker), Flux, then a
  conservative set of bricks reconciled from the commit under test (namespaces, platform prerequisites, External Secrets,
  cert-manager, OpenBao in dev or prod mode, Kyverno), bounded waits, read-only assertions, a pod-log leak scan, artifacts and
  an `if: always()` teardown. Rung reached: local Docker, **dev mode only**, on a GitHub-hosted runner: every run on the pull request of this
  candidate so far was green (the first three on the merge ref, later ones on the head commit), 19 assertions after a bounded wait. **Not run:** prod mode (`openbao_mode=prod`) and the
  `workflow_dispatch` route with its inputs; they are written and statically checked. Static checks in CI: `plan` refusals,
  the workflow's shape (pinned actions, minimal permissions, step order, an always-run teardown, no `secrets.*`), and the leak scan
  against planted secrets. It is local Docker with partial bricks: it does not prove Longhorn, routable LoadBalancers, provider
  S3, Zitadel, Istio, observability or CloudNativePG, nor the `openbao` ClusterSecretStore or ClusterIssuer (no assertion reads them),
  nor the default-deny network baseline; it does not replace the cloud rung.
- Corrected the `apps/flux/local` header, which said `task local-test` "drives the whole order", and the README, which gave an
  equally wrong picture of it: for the apps it only installs Flux and applies the suspended overlay (it also checks the cluster
  itself: etcd quorum, nodes, Cilium).

### Validated

- **The 0.1.1 candidate on three clouds, 2026-10-14: different commits and different profiles on each (OpenAether-infra `f127a26`, a
  commit on main, not a tag; management role, 3 control planes + 2 workers, Kubernetes v1.37.1, `deploy_flux = true`, OpenBao in
  production mode with 3 throwaway PGP recovery keys, threshold 2).** Rung: real cloud, each cloud driven by hand (Scaleway and Outscale in two labs on different commits, OVH in one cluster that
  followed four commits); no commit was observed twice on a cluster, and nothing is repeated or unattended, and `cluster-up` still ends 201 on the documented `flux-system exists` line until the Day-1 seeding
  has run. Which commit each cloud saw matters, because the S3 egress rules, Longhorn's rule and the CSR prefix rule are later
  commits than the first labs:
  - **Scaleway, full DAG, two labs.** *Lab 4* (apps `883d888`, moved by Flux to `4405794`; torn down by its deadline before the later
    fixes existed): 35 of 35 Kustomizations Ready (34 apps Kustomizations plus infra's root) after the seeding. The PKI CSR stored
    in KV, `ttlSecondsAfterFinished` 3600 and `activeDeadlineSeconds` 3600 on the configure Job, both Jobs removed by their ttl about
    an hour after they ended and re-created by Flux roughly 1.5 minutes later (the removal time is approximate), as no-ops (5 to 6 s);
    `OpenBaoBootstrapJobFailed` **firing** for a deliberately failed `openbao-init` (with `openaether-platform` and `foundation-vault`
    suspended: suspending the child alone is reverted by its parent) and clearing after the resume (#34); the pool at `cilium.io/v2`
    with 0 deprecation warnings and 13 addresses available (#49); the Kyverno cleanup Jobs in 4 s (#48); `loki-s3-credentials` synced
    with region `fr-par`, the `namespace` label, a LogQL marker line, the S3 host in Cilium's FQDN cache, Loki objects in the bucket,
    no `i/o timeout` (#53; two Alloy `status=500` lines at start-up and none in the minute after); no secret in the `foundation-vault`
    logs; the CNPG `promote` observation (#36, see Documented). The stored CSR entry on this cluster held a probe value left by a
    test, not a CSR, so it shows the refusals but not "reads a CSR". *Lab 6* (apps `065d440`, the final behaviour: only tests, docs,
    one regex and one policy `description` come after it): 35 of 35 Ready, both Jobs Complete in production mode (34 s and 40 s on a
    first install), the pool at `v2` with 0 warnings, the cleanup Jobs in 4 s, Loki's labels and a marker line, no `i/o timeout`,
    region `fr-par`, Longhorn's BackupTarget `available: true` (it was not checked on lab 4), and Kyverno at 82 `require-security-context`
    (45 + 37 autogen) and 3 `restrict-image-registries` (1 + 2 autogen, images not yet attributed), none `restrict-privileged`
    (#37, #47). Flux then moved lab 6 to `4639c98` (the last code commit; the only later one changes this file only and was not run): 35 of 35 Ready again, no pod outside Running or Completed, no
    `i/o timeout`, no "Policy denied" drop in two later windows (three in the first 45 seconds after the re-apply, while pods restarted;
    not identified). CloudNativePG on lab 6: archiving first failed because the lab reused its bucket and the previous cluster's WAL archive was still
    there (the pods did reach Scaleway's S3 through the new rule; the trap is in `docs/traps.md`); after that stale `cnpg/` prefix was
    removed both clusters reported `Continuous archiving is working`.
  - **OVH, full DAG, Paris region with the Gravelines S3 endpoint (`s3.gra.io.cloud.ovh.net`); one cluster that saw `4405794`, then
    `953f459`, `07d9347` and `065d440` through Flux:** 35 of 35 Ready on `4405794`, `953f459` and `065d440` (`07d9347` was observed for the CSR requests and Longhorn only), the same Job and CSR observations, `cilium.io/v2` with
    0 warnings, cleanup Jobs in 3 s. The first lab of the day never got that far: every node lost its egress (no DNS, no NTP) behind a
    router whose centralized SNAT port stayed DOWN, so no apps object was deployed there; a lab with a fresh router came up in about
    8 minutes. This run **found a real defect that no earlier gate could**: the provider-S3 egress rules named one OVH host, so
    Cilium dropped Loki, the CNPG backups and Longhorn's backup target (see Fixed, #53). After the fix, on OVH: no further "Policy
    denied" drop in a 60-second window, no Loki timeout in ten minutes, Loki's objects in the bucket, `zitadel-db` and `grafana-db`
    reporting `Continuous archiving is working`, and Longhorn's BackupTarget `available: true` (it had read `false` when first
    looked at).
  - **Outscale, REDUCED profile, two labs on twins of the candidate** (throwaway branches, each the candidate plus one regenerated management profile file: lab 4
    was installed on a twin of `883d888` and moved to a twin of `4405794`; lab 6 on a twin of `4639c98`; picks `vault eso certs kyverno platform-cilium-lb-ipam`, 18
    Kustomizations): the shipped Outscale quota (20 vCPU, 40 GB) cannot hold the full DAG, so the full DAG was **not** run on
    Outscale. Lab 6: 18 of 18 Ready, OpenBao in production mode with the CSR stored, the pool at `v2` with 0 warnings, the cleanup
    Jobs in 3 and 4 s, the Day-1 seeding writing five paths (lab 4 recorded the same 18 of 18, production mode, the CSR and the five
    paths in the lab journal, and 6 s cleanup Jobs). That profile has no Loki, database or Longhorn, so the
    `oos.*.outscale.com` pattern has been checked by `scripts/dev/test-s3-egress-fqdns.py` only, never by a live flow; it mirrors the
    two that were.
  - **The stored CSR.** Through the `bao` CLI, `openaether-admin` was refused (exit code 2) an overwrite, a delete, a destroy and a
    metadata wipe on Scaleway (labs 4 and 6), OVH and Outscale (labs 4 and 6): Scaleway lab 4, Outscale lab 4 and the first OVH cluster ran the exact-path
    rule of `4405794`; Scaleway lab 6 and Outscale lab 6 ran the prefix rule through the CLI as well, and OVH after `a3cb3d2` only through raw
    requests. The CLI strips a trailing slash, so those checks could not see that a raw HTTP
    `DELETE .../intermediate-csr/` got through the first rule (found by the independent review on the pinned image). The raw requests
    (trailing-slash `DELETE` of the data and metadata paths, the delete and undelete endpoints: all 403, the entry still readable at
    version 1) were made against the prefix rule on OVH, Scaleway lab 6 and Outscale lab 6.
  - **CI on the pull request, per head:** CI, Security and the Local Docker lane were green on `883d888`, `4405794` and `953f459`;
    Security was red on `07d9347` (a fake key header in the lane's own test, fixed by `2776555` and a documented fingerprint); all seven
    checks were green on `065d440` and on `4639c98`, including `openbao-bootstrap` against a real OpenBao (136 checks: 131 up to `4405794`, 136 from
    `a3cb3d2`), `loki-render`, the S3 egress test and the local Docker lane (dev mode, 19 assertions). The result for the head that carries this file is read on the pull request.
  - **Not observed, on any cloud:** the disaster-recovery restore on a cluster, a rolling upgrade of the OpenBao replicas, Kyverno in
    Enforce mode, the #39 CAPI child on-ramp on a real child, #38's "store not found" window (the events had expired), prod-mode
    local Docker, the `workflow_dispatch` route of the lane, an in-place upgrade of a cluster initialised by 0.1.0 (it cannot be done,
    see Upgrade notes), a Longhorn backup or restore (its target was seen available on OVH and Scaleway lab 6 only), and anything
    repeated or unattended.
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
  rotation procedure (every pod unsealing with the new key id; the roll timings were not retained); the Kyverno cleanup
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
  three replicas, Kyverno in Enforce mode, on that day, before the Loki and egress fixes of this block, any log in Loki (it stored nothing on a cloud
  cluster, #53), the Loki side of the log search, and the 3-replica restore. **A lab artefact of the first run, not a defect:** re-initialising
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
  Job log). `openaether-admin` holds `secret/*`, so the first version of that entry could be overwritten, deleted or destroyed by it (the protection is against that identity; the bootstrap identity, which the Jobs run as, holds `secret/*` by design),
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
  cloud on Scaleway, OVH and Outscale: `cilium.io/v2`, 0 deprecation warnings in the kustomize-controller log, on Scaleway the pool kept its 13 addresses.
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
- **The provider-S3 egress rules named one host per provider, so a real OVH configuration (`s3.gra.io.cloud.ovh.net`) had Loki and the
  CNPG backups dropped by Cilium, and Longhorn's policy had no S3 rule at all (#53, found on a real OVH cluster, not by any earlier gate).** The four
  `toFQDNs` rules (`observability`, `backup/openbao`, `backup/databases`, the `cnpg` restricted policy) allowed only
  `s3.fr-par.scw.cloud`, `s3.eu-west-par.io.cloud.ovh.net` and `oos.eu-west-2.outscale.com`, while the owner's OVH configuration
  uses the Gravelines endpoint: Cilium answered "Policy denied" to `loki-0`, `zitadel-db-1` and `longhorn-manager` (935 Loki
  `i/o timeout` lines in an hour). They now use one `matchPattern` per provider (`s3.*.scw.cloud`, `s3.*.io.cloud.ovh.net`,
  `oos.*.outscale.com`; `*` never crosses a dot), and the `longhorn` policy, which had **no** S3 rule at all on any provider (its BackupTarget sat at
  `available: false` on the OVH cluster; that was not checked on the others before the fix), gets the same rule plus `rules.dns` on its DNS rule. New `scripts/dev/test-s3-egress-fqdns.py` (in CI) checks
  the five files against the regions the tfvars can name and against look-alike hosts that must stay refused, and shows that the
  former rule missed `s3.gra.io.cloud.ovh.net`. Rung: static in CI, then real cloud on OVH and Scaleway lab 6: the drops from Loki and the
  database pod stopped when Flux applied the rule, Loki wrote its chunks to the bucket within seconds, both CNPG clusters reported
  `Continuous archiving is working`. The egress now admits every region of three provider domains on 443: a pod holding that provider's credentials could already
  reach its own bucket through the one host, so this adds regions, not a new channel, and an exact per-cluster host would be narrower.
  It does NOT: cover OVH's high-performance class (`*.perf.cloud.ovh.net`) or an endpoint outside
  the three providers' names; Longhorn's BackupTarget turned `available: true` on OVH once the rule landed (it read `false` when first looked at) and
  read `true` on Scaleway lab 6; the Outscale profile has no Longhorn, and no backup was taken or restored through it. The rule applies
  to every pod of `longhorn-system` (the policy selects them all).
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

- **A cluster initialised by 0.1.0 cannot be moved to 0.1.1 in place: build a new cluster, on NEW buckets.** A rebuild that reuses the
  backup bucket finds the old cluster's WAL archive under `cnpg/`, CloudNativePG refuses to archive into it ("Expected empty archive", seen
  on Scaleway lab 6) and no WAL leaves the cluster: use a new `bucket_suffix`, or remove the old `cnpg/<cluster>` prefix first, or
  bootstrap from it (`docs/traps.md`). Tag 0.1.0 runs OpenBao with a Shamir seal,
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
  Observed on Scaleway, Outscale and OVH moving between candidate commits: both Jobs re-ran and ended Complete (Outscale 6 s each; OVH 3 s
  and 4 s on the last hop; not recorded for the Scaleway hop), finding an initialised OpenBao with its root token already revoked.
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
  test, ...) are required-check candidates; nothing in git can make them required (today the ruleset on the default branch requires only `Lint`,
  `Flux DAG`, `Kustomize Build + Collision Check` and `Secret Detection`). **Merge the pull request by squash or merge commit, never by
  rebase:** `.gitleaksignore` holds the fingerprint of one commit of the branch, and a rebase merge rewrites its sha. There is no tag
  ruleset on this repository: a tag can be moved or deleted.

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
- **Nothing is repeated, unattended or measured over time.** Each cloud was driven by hand; the lane runs dev mode only. This
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
by hand on 2026-10-12 (UTC; see 0.1.1, Validated).* `OpenAether-infra` 0.2.0 validated its three clouds with
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
  profile was reconciled once by a lab, by hand, after the tag (0.1.1, Validated); the `workload` profile still has not been.
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
  `.languageignore` and the list must not grow. *Corrected in place on 2026-10-14 (#40): all translated in 0.1.1 (see Fixed there);
  `.languageignore` keeps only `CLAUDE.md` and `CHANGELOG.md`.*

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
