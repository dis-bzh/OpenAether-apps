# Forgejo in 0.4.0: the git forge, the first brick of OpenAether-self (OPT-IN, off by default)

Rung: **mocked, offline**. Rendered manifests, unit tests and the Kyverno CLI; the vault configure Job's seeding ran against a real
OpenBao in a container (`test-openbao-bootstrap.sh`). **Nothing of the forge has run on a cluster**, not in 0.3.x either: the brick
arrived in PR #173 (dogfooding V1, issue #170) after rungs 1-2 only. Not a compliance claim.

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
   `./apps/flux/management`. It is the full DAG of `../base` with nothing removed; the only difference with `management` is those two
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
| the git repositories themselves | the 10 Gi Longhorn volume `longhorn-x2-rwo` | two Longhorn replicas: the volume survives the loss of a node; a **daily Longhorn backup** (below) to the platform's backup target | a restore of the volume (never run); consistency with the database (below); the unencrypted volume (the class is not LUKS) and a backup of it that nothing here encrypts |

### The recurring backup of the repositories' volume

When the brick is on, the repositories' volume is backed up by default: the chart's `persistence.labels` puts
`recurring-job-group.longhorn.io/oa-forgejo: enabled` on the PVC, and `RecurringJob forgejo-repos-backup-daily`
(`apps/base/forgejo/recurringjob.yaml`, namespace `longhorn-system`, task `backup`, group `oa-forgejo`, **daily at 03:55**, **14 kept**,
concurrency 1) selects that group. It sends the backup to the platform's Longhorn backup target, which is configured by the existing
`backup_target_url` identity value (`BACKUP_TARGET_URL` of `cluster-identity`, the `storage-backup-target` brick). 03:55 is after the
other jobs that touch the same link or the same data: the restic dumps (02:17 and 02:47), `oa-backup-daily` (03:37), the `forgejo-db`
base backup (03:40); before the etcd snapshot (04:10). The group is its own, so the job changes no other volume and
`oa-backup-daily` (opt-in, group `oa-backup`) is not involved. `test-forgejo-brick.py` fails without the label, with a job on another
group, a non-backup job, a non-daily or colliding schedule, or no retention.

What this covers: the content of the volume (the bare repositories) as of the night's snapshot, 14 of them, if the platform has a backup
target. What it does not cover, precisely:

- **No target, no backup.** Without `backup_target_url` Longhorn has nowhere to send the backup; the job is rendered anyway (the
  chart and the job do not know). What Longhorn does then is not verified here.
- **The database and the volume are not backed up at the same instant.** `forgejo-db` has its base backup at 03:40 and a continuous WAL
  archive, the volume a snapshot at 03:55; nothing quiesces the forge in between. Restored as a pair (the database to a point in time,
  the volume from the night's backup), they can disagree: a repository the database knows with pushes the volume lacks, or pushes on
  disk that no issue, pull request or webhook row in the database accounts for; an application-consistent copy of both needs
  `forgejo dump` (or stopping the forge) at one moment, which nothing schedules. A restored volume is crash-consistent at best.
- **A restore of the repositories' volume has not been run on a cluster**, nor has a restore of `forgejo-db`. The documented test of the
  volume is the opt-in `longhorn-restore-test` component (a canary volume backed up and restored weekly): it proves that a Longhorn
  backup comes back on the platform, not that the Forgejo one does. Label propagation from the PVC to the Longhorn volume is
  Longhorn's documented behaviour and is unverified here.

## What is NOT delivered (open, each one is for an issue)

- SSH (git over SSH needs a Gateway `TCPRoute`; git is HTTPS only), OIDC login with Zitadel (rung 3: the client registration, `identity`
  in `dependsOn`, in-cluster resolution of `*.openaether.local`, enabling the commented `gitea.oauth` block), a mail relay (#174) and a
  status page (#175, out of scope here).
- A second Forgejo replica (needs ReadWriteMany, Valkey for sessions and cache, a shared object store), so the forge is a single replica
  listed in `scripts/ha-allowlist.yaml`: while its pod is rescheduled after a node loss there is **no push, pull or web access** (the 5
  minute toleration plus the re-attach of the volume: unmeasured); the cluster and Flux keep running.
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
4. A **Longhorn backup of the repositories' volume** by the default job (the PVC carries `oa-forgejo`, the Longhorn volume carries it too,
   `forgejo-repos-backup-daily` runs at 03:55 and a backup appears at the target), restored into a scratch volume and the repository cloned
   from it; then the **pair**: the database and the volume restored from their different instants, and what disagrees.
5. **Node and zone loss drills with a Forgejo push writer**: how long the forge is away (the allow-list entry says unmeasured), no acknowledged
   push lost; `forgejo-db` with a writer like `zitadel-db` in lab 23.
6. The **dogfooding V1 acceptance of #170**: OpenAether's own source hosted on the forge and used from the forge.
7. Whether Forgejo starts with `readOnlyRootFilesystem: true`, and what the cluster says about the 3 + 1 pods on the 4-worker examples
   (`cluster-roll`'s survey: the derived 233-238% above, with the brick on).
