# Forgejo in 0.4.0: the git forge, the first brick of OpenAether-self (OPT-IN, off by default)

Rung: **mocked, offline**, plus **one real run: lab 27 sitting 1 (Scaleway, 2026-10-25)**, which enabled the brick on a running cluster
and found three defects, fixed below and UNVERIFIED on a cluster until the next lab. What lab 27 showed: the brick converged in 2 min 30 s
from the path patch (chart pulled from `code.forgejo.org` under the restricted egress policy, `forgejo-db` 3/3 with synchronous
replication, archiving and the replica copy), HTTPS through the load balancer and the Gateway worked (`/api/healthz` 200, registration
refused, anonymous API 403, anonymous clone refused, `/metrics` answers 500 at the Gateway and is scraped by vmagent), the admin
logged in and was forced to change the password, two repositories were pushed (3 s each) and cloned. The defects: the daily
volume backup never selected the volume (FG), acknowledged pushes were lost on a zone power-off (FK, DATA LOSS), and enabling the
brick is a hand patch (FF). Not a compliance claim.

## What is delivered

- **Forgejo 15.0.10** (LTS, end of support 2027-07-15, `docs/eol-calendar.yaml`), from the chart `forgejo-helm` 17.3.1, a single replica
  with its repositories on a 10 Gi `longhorn-x2-rwo` volume, in the namespace `services-git` (outside the mesh, a `CiliumNetworkPolicy`
  of its own), reached at `https://forge.openaether.local` through the shared Gateway (HTTPS only; no SSH).
- **`forgejo-db`**, its PostgreSQL: a CNPG Cluster of 3 instances in `foundation-databases`, built from what `grafana-db` is in this
  branch: quorum synchronous replication (`method: any`, `number: 1`, `dataDurability: required`, `failoverQuorum`), WAL and base backups
  through the Barman Cloud plugin (a primary and a replica `ObjectStore`, the `${BACKUP_*}` defaults of `cluster-identity` including the
  `.invalid` placeholders of the replica store, the Outscale checksum variables on the sidecar), a daily `ScheduledBackup` at 03:40, zone
  and host anti-affinity, 30 s tolerations, the operator's `PodDisruptionBudget`, the `switchover` update method, and the same image
  line as `grafana-db` (`postgresql:16.15`; #162 and #163, the open scans of the 16.15 and 18.6 images, apply to it as to the others,
  and no image was added to the inventory).
- **Its own Flux Kustomization**, `forgejo-db` (group 36, `wait: true`), which `forgejo` (group 37) depends on. Not in `cnpg`: a profile
  without the forge (the `workload` profile) must not install three database pods it does not use, and a slow or failed `forgejo-db`
  must not hold `cnpg` (also `wait: true`), identity or observability back.
- **Secrets from OpenBao**, seeded by the vault configure Job (create only, never printed, never rotated by a re-run):
  `secret/forgejo/db` (32 characters), `secret/forgejo/admin` (the initial admin password, 28 characters and `Aa1!`; Forgejo creates the
  account once and asks for a change at the first login) and `secret/forgejo/oidc` (a **public placeholder**: the Zitadel auth source
  ships disabled; an operator overwrites it when rung 3 starts).
- Metrics scraped by vmagent (`VMPodScrape forgejo`); no rule reads them yet. `/metrics` is not routed on the Gateway.
- A row in `docs/public-exposure.yaml`: Forgejo's own login, **self-registration off, sign-in required to view anything** (the app load
  balancer has no source ACL, so the route answers anyone; Forgejo's defaults would have let that visitor register).

## Opt-in: OFF by default, how to turn it on

The 0.4.0 release gate forbids anything that gets worse. The brick costs +400 m of CPU requests and 1.4 GiB of memory requests (below)
and has never run on a cluster, so a default install does **not** carry it: `apps/flux/management` and `apps/flux/workload` remove
the Kustomizations `forgejo-db` (group 36) and `forgejo` (group 37) with a `$patch: delete` each. Both stay in the DAG
(`apps/flux/base`) and in the catalogue (`forge` is an alias of `forgejo` in `apps/flux/bricks.yaml`), so the picker can include them.
`python3 scripts/pick.py --check` pins that the generated `workload` profile does not carry them, and `test-forgejo-brick.py` that the
management one does not either. To turn it on, one of:

1. **The overlay `apps/flux/management-forge`**: point the root Flux Kustomization at `./apps/flux/management-forge` instead of
   `./apps/flux/management`. **On a running cluster this is one live patch, the supported way (FF, lab 27):**
   `kubectl -n flux-system patch kustomizations.kustomize.toolkit.fluxcd.io openaether-platform --type merge -p '{"spec":{"path":"./apps/flux/management-forge"}}'`
   (back: the same with `./apps/flux/management`). The path comes from the Talos inline manifest of the infra repository
   (`path: ./apps/flux/${cluster_role}`), an object `cluster-up` never rewrites, and there is no tfvar for it: **`cluster-up` neither sets
   nor reverts the patch and `cluster-verify` does not check it** ("not converged, not checked": a gap, the issue stays open; a cluster
   rebuilt from the tfvars starts on the default path again). Lab 27: `forgejo-db` and `forgejo` Ready 2 min 30 s after the patch.
   The overlay is the full DAG of `../base` with nothing removed; the only difference with `management` is those two
   Kustomizations (a test fails if it grows another).
2. **A generated profile**: `python3 scripts/pick.py forge <the other bricks> -o apps/flux/<name>` (the closure of `forgejo` pulls in
   `cnpg`, `external-secrets`, `services-gateway`, `storage`, OpenBao and the rest of its dependencies).
3. **The instance repository's own profile** (OpenAether-self does this): a `kustomization.yaml` that lists
   `../../../apps/flux/management-forge` (or `../base`) as its resource, from the apps ref the instance pins.

Rollback is the reverse: point back at `management`; Flux prunes both Kustomizations and what they applied (`prune: true`). What survives
the prune (the repository volume, the database volumes) has not been exercised: export what matters first.

**The OpenBao seeding is not conditional, and is harmless with the brick off.** The vault configure Job creates `secret/forgejo/db`,
`secret/forgejo/admin` and `secret/forgejo/oidc` on every install, whichever profile runs: it has no knowledge of the profile, and the
repository already seeds the secrets of bricks a profile may not carry in the same way (`minio/root`, `longhorn/encryption`,
`grafana/*`). Created only if absent, never printed, never rotated. With the brick off nothing reads them: no ExternalSecret, no database
role, no account exists, so they open nothing; they are three unread entries of the KV store (and of its snapshots). The placeholder of
`forgejo/oidc` is a fixed public string and protects nothing. In exchange, enabling the brick later needs no seeding step. An upgraded
cluster re-runs the Job (the script ConfigMap changed) and gains the three entries whether or not it enables the forge.
"The brick converges unattended" is **not** claimed: the next lab is the first run (below).

### What a default install renders, against the one before the merge

Render of `apps/flux/management` (kustomize, then every HelmRelease through `helm template`, `scripts/ha_render.py`) at this commit,
against the same render at 0d7328a (the commit before the Forgejo merge): the Deployment, StatefulSet and DaemonSet CPU and memory requests
(init containers included), the replicas, the requests of every CNPG Cluster, and the list of Kustomizations with their `path` and
`dependsOn`. **Result: identical, no difference** (39 Kustomizations with the same paths and `dependsOn`; every workload and CNPG Cluster request
unchanged). The default install differs from the pre-merge one only by dead code: the Forgejo and `forgejo-db` manifests under
`apps/base` (never applied without their Kustomizations), the `forge` alias, the `management-forge` overlay, the Forgejo rows of the
scrape (`VMPodScrape forgejo`, selecting nothing), the Gateway egress rule for `services-git` (to a namespace that does not exist), the
`code.forgejo.org` entries of the Flux egress and Kyverno registry lists, and the three OpenBao seeds above (a changed `configure.sh`,
hence a different ConfigMap and a re-run of the Job). The render with the brick on (`management-forge`) adds exactly: 2 Kustomizations,
`Deployment/services-git/forgejo` (100 m, 256 Mi, plus 3 init containers of 100 m, 128 Mi) and `Cluster/foundation-databases/forgejo-db`
(3 instances x 50 m, 256 Mi).

## From PR #173 to this: what the offline read found

Each line is a defect of the first version, fixed here and held by `scripts/dev/test-forgejo-brick.py` unless said otherwise.

1. **The chart could never be fetched.** The `HelmRepository` pointed at `https://forgejo-contrib.codeberg.page/forgejo-helm/`, which
   answers 404; the chart is published only as an OCI artifact (`oci://code.forgejo.org/forgejo-helm/forgejo`). `type: oci` now, and
   Flux's egress policy admits `code.forgejo.org` (rule 9b). `check-ha.py` was red on this alone.
2. **Chart 6.1.0 was two years old and rendered two databases and a cache.** It shipped Forgejo 7.0.3 (long past its end of support) and,
   with the values of the PR, a `postgresql-ha` and a `redis-cluster` next to the forge. Chart 17.3.1 renders one Deployment and no
   sub-chart. Every values key the brick sets was checked against that chart's `values.yaml` and schema by `helm template`.
3. **The admin Secret had no `username`.** The chart reads `username` and `password` of `gitea.admin.existingSecret`; the ExternalSecret
   provided only the password, so the `configure-gitea` init container would have stayed in `CreateContainerConfigError`.
4. **Nothing seeded the three OpenBao paths**, so `forgejo-db-credentials` would never exist, the database would never bootstrap and (it
   was inside `cnpg`) the Kustomization every other database waits for would have stayed NotReady.
5. **The database was a 0.3.x one**: two instances, an asynchronous `barmanObjectStore` (CNPG 1.31 removes it), no tolerations, no zone
   term. It is the `grafana-db` form now. There is **no upgrade path to describe**: `forgejo-db` is new in 0.4.0, nothing of it exists on a
   0.3.x cluster, so the presync of synchronous replication before the apps ref moves (`docs/resilience.md`) does not apply to it.
6. **Registration was open on a public route** and `/metrics` was served to the Internet. And the Gateway's egress policy
   (`apps/base/istio/networkpolicy.yaml`, a whitelist of backends) did not list the forge: the route would have timed out.
7. The init containers had no limits (Kyverno `require-limits`), the pod ran with no `runAsNonRoot`, `seccompProfile` or
   ServiceAccount of its own, the chart's helm-test Pod (`busybox:latest`) was part of the release, and the image registry
   `code.forgejo.org` was not on the Kyverno allow-list. The pod the chart renders now passes every platform policy except one (below).
8. The replica store of `forgejo-db` is declared but nothing copied into it: `cnpg-store-replicate` lists it now (an empty prefix on
   both stores is a note, not a failure, so a profile without the forge is unaffected).

## Capacity

**Only with the brick on.** A default install has none of it. `scripts/check-ha.py` counts the CNPG pods apart from the Deployments and
audits two renders: the default one against the unchanged budgets (CNPG 600 m, Deployments 3300 m), and the `management-forge` one against
a CNPG budget of **900 m** (`CNPG_CPU_BUDGET_FORGE_M`), the default budget being left alone. `forgejo-db` adds 3 instances x (50 m +
50 m) = **300 m**; Forgejo adds 100 m to the Deployments (3140 m of the 3300 m budget). Total **+400 m** of CPU requests, 1.4 GiB of
memory requests, 15 GiB of `local-path` and 10 Gi x 2 of Longhorn. Against the roll gate of OpenAether-infra
(`docs/capacity.md` there): lab 21 measured a worker at about 3990 m, so +400 m is **+10 points of a worker**, from the 223-228% of lab
21/22 to about **233-238% against the 300%** a 4-worker cluster allows (75-77% of the gate's limit before, about 78-79% now): it still
passes, **which is why the brick is opt-in and why the lab must enable it explicitly**. **Derived, not measured.** Three workers were refused already; the two-worker zone is no better. Infra's `docs/capacity.md` still
quotes the figures without the forge.

## Data: what protects what

| What | Where | Protection | Not covered |
|---|---|---|---|
| users, issues, pull requests, settings | `forgejo-db` | no acknowledged commit lost (3 instances, synchronous), WAL and daily base backup to the primary store, copy to the replica store | the same limit as the other databases: both disks that hold an acknowledged commit lost at once |
| the git repositories themselves | the 10 Gi Longhorn volume of the class `longhorn-x2-rwo-forgejo` (two replicas) | two Longhorn replicas: the volume survives the loss of a node, **for what git had synced** (below); `core.fsync = all` so an acknowledged push is on disk; a **daily Longhorn backup** (below) to the platform's backup target | the fsync setting and the new class are UNVERIFIED on a cluster; consistency with the database (below); the unencrypted volume (the class is not LUKS) and a backup of it that nothing here encrypts |

### The durability of a push: `core.fsync = all` (FK, lab 27, DATA LOSS)

Lab 27 power-cycled the zone that held the Forgejo pod (graceful provider power-off of three nodes, 300 s). A push writer that counts a
push acknowledged only when `git push` returned 0 had 68 acknowledged pushes. Afterwards the repository on the Longhorn volume held **75
zero-length loose object files** (of 211), the branch pointed at an empty object, every later push was refused ("object file ... is
empty", "missing necessary objects") and a fresh clone failed; of the 68, only 64 commits were readable. The two older repositories
(written 30 minutes before) were intact and `forgejo-db` lost nothing (10641 acknowledged, 0 missing).

Cause (a hypothesis that fits every fact, not isolated by an experiment): git does not fsync loose objects by default. Its documented
default is `core.fsync=committed,-loose-object`
(https://github.com/git/git/blob/v2.45.0/Documentation/config/core.txt, `core.fsync`), so what a push wrote was in the page cache of
the node when it powered off. **Change:** `gitea.config.git.config.core.fsync: all` in the HelmRelease. The chart renders it into the
inline app.ini sources as the section `[git.config]` with `core.fsync=all` (rendered and tested), and Forgejo applies the section as
`git config` (https://forgejo.org/docs/latest/admin/config-cheat-sheet/, "Git - Config options": "The key/value pairs in this section will
be used as git config", "this section only performs set config"). `all` is git's aggregate option that syncs every component,
loose objects included. `core.fsyncMethod` is not set: git's default on Linux is `fsync`, whereas `writeout-only` and `batch` give part
of the guarantee back. **Two caveats.** Forgejo only SETS keys: removing the line later does not unset it in git's config (the cheat
sheet says so). And the fix is a hypothesis on a cause: **UNVERIFIED on a cluster until a drill re-runs it** (the push writer in the
zone power-off, with fsync on, then `git fsck` and a clone). **Cost, not measured:** every push now pays the fsyncs of its loose
objects, packs, index and references, so the push latency goes up; by how much is UNVERIFIED (lab 27 measured pushes of 3 s each
without it, through the load balancer, which is dominated by the network path).

**Why Longhorn does not already cover it.** The volume has two replicas (`longhorn-x2-rwo-forgejo`), which protects against the loss of
a node holding one replica of data the engine already has. The filesystem on the volume is mounted on the node where the pod runs, and
what git wrote but never synced sits in that node's page cache: it has not reached the Longhorn engine, so NEITHER replica has it. A
node or a zone that powers off while it holds the mount (and, in the drill, the writable replica too) loses exactly that tail, whatever
the replica count. fsync is what makes the data leave the cache before the push is acknowledged; replication is what keeps it once it
has. (This is the reading that fits the lab; the drill with fsync on is what shows it.)

### The recurring backup of the repositories' volume

When the brick is on, the repositories' volume is backed up by default: the volume is created **already in the group `oa-forgejo`**, and
`RecurringJob forgejo-repos-backup-daily` (`apps/base/forgejo/recurringjob.yaml`, namespace `longhorn-system`, task `backup`, group
`oa-forgejo`, **daily at 03:55**, **14 kept**, concurrency 1) selects that group. It sends the backup to the platform's Longhorn backup
target, which is configured by the existing `backup_target_url` identity value (`BACKUP_TARGET_URL` of `cluster-identity`, the
`storage-backup-target` brick). 03:55 is after the other jobs that touch the same link or the same data: the restic dumps (02:17 and
02:47), `oa-backup-daily` (03:37), the `forgejo-db` base backup (03:40); before the etcd snapshot (04:10). The group is its own, so the job
changes no other volume and `oa-backup-daily` (opt-in, group `oa-backup`) is not involved.

**How the volume joins the group (FG, lab 27).** The first version put the label `recurring-job-group.longhorn.io/oa-forgejo: enabled`
on the PVC (`persistence.labels`). Longhorn does not copy a PVC label to its Volume: lab 27 found the Volume with only
`recurring-job-group.longhorn.io/default`, the job logged "Found 0 volumes with recurring job forgejo-repos-backup-daily", and the
daily backup NEVER ran until the Volume was labelled by hand. Now `apps/base/forgejo/storageclass.yaml` defines the StorageClass
`longhorn-x2-rwo-forgejo`: the same parameters as `longhorn-x2-rwo` (2 replicas, RWO, Retain, expansion, not encrypted; the test compares
them) plus `recurringJobSelector: '[{"name":"oa-forgejo","isGroup":true}]'`, the parameter Longhorn documents for this ("A JSON array of
recurring job or group specifications to attach to the volume", applied at volume creation only,
https://longhorn.io/docs/latest/references/storage-class-parameters/). The chart's `persistence.storageClass` names it, so the volume is
created labelled. **A forge volume created before this change keeps its class** (a PVC cannot change class, and a class cannot be edited):
label its Longhorn Volume by hand, as lab 27 did (`kubectl -n longhorn-system label volumes.longhorn.io <volume>
recurring-job-group.longhorn.io/oa-forgejo=enabled`), or restore it into a new PVC of the new class. `test-forgejo-brick.py` fails without
the class, with a class that differs from `longhorn-x2-rwo`, with a selector on another group or on a job rather than a group, with a PVC on
another class (rendered from the chart), with the PVC label used again, a job on another group, a non-backup job, a non-daily or
colliding schedule, or no retention. **UNVERIFIED on a cluster:** that a volume created from the new class carries the label and that the
job then selects it (lab 27 only proved the job and the restore after the manual label).

What this covers: the content of the volume (the bare repositories) as of the night's snapshot, 14 of them, if the platform has a backup
target. What it does not cover, precisely:

- **No target, no backup.** Without `backup_target_url` Longhorn has nowhere to send the backup; the job is rendered anyway (the
  chart and the job do not know). What Longhorn does then is not verified here.
- **The database and the volume are not backed up at the same instant.** `forgejo-db` has its base backup at 03:40 and a continuous WAL
  archive, the volume a snapshot at 03:55; nothing quiesces the forge in between. Restored as a pair (the database to a point in time,
  the volume from the night's backup), they can disagree: a repository the database knows with pushes the volume lacks, or pushes on
  disk that no issue, pull request or webhook row in the database accounts for; an application-consistent copy of both needs
  `forgejo dump` (or stopping the forge) at one moment, which nothing schedules. A restored volume is crash-consistent at best.
- **A restore of the repositories' volume was run ONCE, in lab 27, by hand** (after the manual label): the 289 MB backup was restored
  into a scratch volume through a StorageClass with `fromBackup`, a PVC and a pod; it held both repositories, `refs/heads/main` was
  the pushed commit, `git clone` and `git fsck` were clean, and the scratch namespace and class were deleted. No restore of `forgejo-db`
  has been exercised. The documented recurring test of a volume is the opt-in `longhorn-restore-test` component (a canary volume backed up
  and restored weekly): it proves that a Longhorn backup comes back on the platform, not that the Forgejo one does.

## How long the forge is away after a node or zone loss (F-FORGE-TOL, lab 27 sitting 2)

**Measured at Kubernetes' default toleration (lab 27, sitting 2, zone power-off, forge pod and one Longhorn replica of its volume inside the zone):** the pushes stopped at +31 s and came back about **8 minutes** after the power-off (push gap 471.8 s). The pod went NotReady at +79 s and was **not** evicted for 300 s, the default `tolerationSeconds` that Kubernetes adds to a pod with no toleration of `node.kubernetes.io/not-ready` and `unreachable`; the new pod was scheduled on another worker, Longhorn showed "Waiting for detach" (the old attachment was on the lost node) and attached the volume 20 s after the pod was scheduled, and the container started 10 s later. The databases do not wait: they tolerate 30 s. No data was lost (830 of 830 acknowledged pushes, `git fsck` clean).

**Change:** the HelmRelease sets the chart's `tolerations` value to the same two tolerations as the databases, `node.kubernetes.io/unreachable` and `node.kubernetes.io/not-ready`, `NoExecute`, `tolerationSeconds: 30` (the chart renders the value into the Deployment's pod spec; `test-forgejo-brick.py` checks the value and the rendered Deployment). **Projected, NOT measured:** NotReady at +80 s, eviction 30 s later, then the lab's 20 s attach and 10 s start: the forge back in about 2.5 to 3 minutes instead of 8. That figure assumes the attach takes as long as it did once; the drill with the 30 s toleration has not been run.

**The volume.** It is ReadWriteOnce with two Longhorn replicas, so the new pod can only start once Longhorn has released the attachment of the lost node. In the one drill it did so by itself, 20 s after the replacement was scheduled. What is **not** known: whether that holds when the node is partitioned rather than powered off (the old pod keeps running on the other side, and the old and the new attachment would both claim the volume; Longhorn's own guard against a double attach is what stands between them, and its behaviour on a partition was not drilled), and what Longhorn's setting `node-down-pod-deletion-policy` is on the cluster (the repository does not set it; its default is to delete nothing, which concerns pods stuck Terminating, not the ReplicaSet's replacement of a Deployment pod: read it with `kubectl -n longhorn-system get settings.longhorn.io node-down-pod-deletion-policy`). A shorter toleration makes the window in which a partitioned node's pod and its replacement could overlap start 4.5 minutes earlier; it is the same class of risk the databases accept with 30 s, and a Deployment with `strategy: Recreate` does not change what happens on an eviction.

## What is NOT delivered (open, each one is for an issue)

- SSH (git over SSH needs a Gateway `TCPRoute`; git is HTTPS only), OIDC login with Zitadel (rung 3: the client registration, `identity`
  in `dependsOn`, in-cluster resolution of `*.openaether.local`, enabling the commented `gitea.oauth` block), a mail relay (#174) and a
  status page (#175, out of scope here).
- A second Forgejo replica (needs ReadWriteMany, Valkey for sessions and cache, a shared object store), so the forge is a single replica
  listed in `scripts/ha-allowlist.yaml`: while its pod is rescheduled after a node loss there is **no push, pull or web access** (about 8 minutes
  measured with Kubernetes' 300 s default toleration; with the 30 s toleration the pod now carries, about 2.5 to 3 minutes projected, UNVERIFIED: see "How long the forge is away"); the cluster and Flux keep running.
- Image **digest pinning** (the image is the chart's `appVersion` tag, `15.0.10-rootless`, bumped with the chart).
- **Backups**: the proof that the repositories' volume restores and an application-consistent copy of it with the database (above), and `forgejo-db` in the rest of the backup brick: the restic `pg_dump` (`cronjob.yaml`),
  the canary, the restore tests (`restore_cnpg.py`) and the verifier (`barman_check.py`) still know two databases. The WAL archive and the
  base backups exist and the replica copy runs, but no restore of `forgejo-db` has been exercised and nothing alerts if its archive goes
  stale beyond the generic CNPG rules (`CnpgSyncReplicaMissing` and the WAL archiver ones already read every instance pod).
- `readOnlyRootFilesystem` on the Forgejo containers: the one Kyverno finding left (Audit). The chart's init containers write under `/`;
  to prove read-only works needs a run.
- A rule on Forgejo's metrics (the names are not checked against a running Forgejo), and the HTTP 500 the Gateway is expected to answer
  to `/metrics` (a rule with no backend: standard Gateway API behaviour, unverified with Istio 1.31).

## What the next lab must prove

0. **The lab enables the brick explicitly** (`apps/flux/management-forge`, or the instance profile): a default install does not carry it,
   so a lab that does not turn it on proves nothing about it. Also there: the default install still renders and converges without it.
1. A **fresh install converges** with the brick: `forgejo-db` then `forgejo` Ready, the three OpenBao paths exist and `forgejo-admin` has
   both keys, `cluster-verify` green, the flux-errors Alert quiet. The Flux source controller pulls the chart from `code.forgejo.org`
   under the restricted egress policy (the registry's token request and blob download on 443, no redirect to another host).
2. **`forgejo-db` synchronous replication and archiving**: `cluster-verify` shows the same lines as for `grafana-db` (sync replicas,
   `ContinuousArchiving`, a first base backup at 03:40 or by hand), `cnpg-store-replicate` copies its archive to the replica store.
3. A **repository pushed through the forge** over HTTPS at `forge.openaether.local`: admin login, forced password change, create a
   repository, `git push`, `git clone`; anonymous access refused, registration refused, `/metrics` refused on the Gateway and scraped by vmagent.
4. A **Longhorn backup of the repositories' volume** by the default job (a volume created from `longhorn-x2-rwo-forgejo` carries
   `oa-forgejo` at once, with no manual label; `forgejo-repos-backup-daily` runs at 03:55 and a backup appears at the target), restored into a scratch volume and the repository cloned
   from it; then the **pair**: the database and the volume restored from their different instants, and what disagrees.
5. **Node and zone loss drills with a Forgejo push writer, with `core.fsync = all`**: the SAME drill as lab 27 (the forge pod and the writable
   replica of its volume in a zone that is powered off): no acknowledged push lost, `git fsck` clean, a fresh clone works; the push latency
   with and without fsync; how long the forge is away with the 30 s toleration (projected 2.5 to 3 minutes against the 8 measured at 300 s), and once with a partition instead of a power-off; `forgejo-db` with a writer like `zitadel-db`
   in lab 23.
6. The **dogfooding V1 acceptance of #170**: OpenAether's own source hosted on the forge and used from the forge.
7. Whether Forgejo starts with `readOnlyRootFilesystem: true`, and what the cluster says about the 3 + 1 pods on the 4-worker examples
   (`cluster-roll`'s survey: the derived 233-238% above, with the brick on).
