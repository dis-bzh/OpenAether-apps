# Forgejo in 0.4.0: the git forge, the first brick of OpenAether-self

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

## Default install: on

The brick is in the default management profile and converges unattended **in principle**: the secrets it needs are seeded by the same
Job that seeds Zitadel's and Grafana's, an upgraded cluster gets them because the changed script recreates the Job, and a failure of the
forge cannot hold the rest back (the `forgejo` Kustomization has no `wait`, `forgejo-db` is its own Kustomization). The `workload`
profile does not carry it (`python3 scripts/pick.py --check` pins that). To run a management cluster **without** it, generate a profile
that does not pick it (`python3 scripts/pick.py` with the other bricks, `-o apps/flux/<name>`); there is no switch in the shipped one.
"In principle" is the honest word: the next lab is the first run (below).

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

`scripts/check-ha.py` counts the CNPG pods apart from the Deployments. `forgejo-db` adds 3 instances x (50 m + 50 m) = **300 m**, so the
CNPG budget moves from 600 m to **900 m**; Forgejo adds 100 m to the Deployments (3140 m of the 3300 m budget). Total **+400 m** of CPU
requests, 1.4 GiB of memory requests, 15 GiB of `local-path` and 10 Gi x 2 of Longhorn. Against the roll gate of OpenAether-infra
(`docs/capacity.md` there): lab 21 measured a worker at about 3990 m, so +400 m is **+10 points of a worker**, from the 223-228% of lab
21/22 to about **233-238% against the 300%** a 4-worker cluster allows (75-77% of the gate's limit before, about 78-79% now): it still
passes. **Derived, not measured.** Three workers were refused already; the two-worker zone is no better. Infra's `docs/capacity.md` still
quotes the figures without the forge.

## Data: what protects what

| What | Where | Protection | Not covered |
|---|---|---|---|
| users, issues, pull requests, settings | `forgejo-db` | no acknowledged commit lost (3 instances, synchronous), WAL and daily base backup to the primary store, copy to the replica store | the same limit as the other databases: both disks that hold an acknowledged commit lost at once |
| the git repositories themselves | the 10 Gi Longhorn volume `longhorn-x2-rwo` | two Longhorn replicas: the volume survives the loss of a node | **no backup by default**: Longhorn's recurring backup is opt-in (`apps/base/storage/backup-target/recurringjob.yaml`: label the volume with the group, or set `LONGHORN_BACKUP_GROUP=default`); the database and the volume are not backed up at the same instant, so a restore of both can disagree about the newest pushes |

## What is NOT delivered (open, each one is for an issue)

- SSH (git over SSH needs a Gateway `TCPRoute`; git is HTTPS only), OIDC login with Zitadel (rung 3: the client registration, `identity`
  in `dependsOn`, in-cluster resolution of `*.openaether.local`, enabling the commented `gitea.oauth` block), a mail relay (#174) and a
  status page (#175, out of scope here).
- A second Forgejo replica (needs ReadWriteMany, Valkey for sessions and cache, a shared object store), so the forge is a single replica
  listed in `scripts/ha-allowlist.yaml`: while its pod is rescheduled after a node loss there is **no push, pull or web access** (the 5
  minute toleration plus the re-attach of the volume: unmeasured); the cluster and Flux keep running.
- Image **digest pinning** (the image is the chart's `appVersion` tag, `15.0.10-rootless`, bumped with the chart).
- **Backups**: the repositories' volume (above), and `forgejo-db` in the rest of the backup brick: the restic `pg_dump` (`cronjob.yaml`),
  the canary, the restore tests (`restore_cnpg.py`) and the verifier (`barman_check.py`) still know two databases. The WAL archive and the
  base backups exist and the replica copy runs, but no restore of `forgejo-db` has been exercised and nothing alerts if its archive goes
  stale beyond the generic CNPG rules (`CnpgSyncReplicaMissing` and the WAL archiver ones already read every instance pod).
- `readOnlyRootFilesystem` on the Forgejo containers: the one Kyverno finding left (Audit). The chart's init containers write under `/`;
  to prove read-only works needs a run.
- A rule on Forgejo's metrics (the names are not checked against a running Forgejo), and the HTTP 500 the Gateway is expected to answer
  to `/metrics` (a rule with no backend: standard Gateway API behaviour, unverified with Istio 1.31).

## What the next lab must prove

1. A **fresh install converges** with the brick: `forgejo-db` then `forgejo` Ready, the three OpenBao paths exist and `forgejo-admin` has
   both keys, `cluster-verify` green, the flux-errors Alert quiet. The Flux source controller pulls the chart from `code.forgejo.org`
   under the restricted egress policy (the registry's token request and blob download on 443, no redirect to another host).
2. **`forgejo-db` synchronous replication and archiving**: `cluster-verify` shows the same lines as for `grafana-db` (sync replicas,
   `ContinuousArchiving`, a first base backup at 03:40 or by hand), `cnpg-store-replicate` copies its archive to the replica store.
3. A **repository pushed through the forge** over HTTPS at `forge.openaether.local`: admin login, forced password change, create a
   repository, `git push`, `git clone`; anonymous access refused, registration refused, `/metrics` refused on the Gateway and scraped by vmagent.
4. A **Longhorn backup of the repositories' volume** (opt-in, labelled), restored into a scratch volume.
5. **Node and zone loss drills with a Forgejo push writer**: how long the forge is away (the allow-list entry says unmeasured), no acknowledged
   push lost; `forgejo-db` with a writer like `zitadel-db` in lab 23.
6. The **dogfooding V1 acceptance of #170**: OpenAether's own source hosted on the forge and used from the forge.
7. Whether Forgejo starts with `readOnlyRootFilesystem: true`, and what the cluster says about the 3 + 1 pods on the 4-worker examples
   (`cluster-roll`'s survey: the derived 233-238% above).
