# Forgejo in 0.4.0: the git forge, the first brick of OpenAether-self (OPT-IN, off by default)

Rung: **mocked, offline**, plus **real runs**. **Lab 27 sitting 1 (Scaleway, 2026-10-25)** enabled the brick on a running cluster
and found three defects; labs 27B, 27C, 28B and 28C (Scaleway) and 27D and 28 (OVH) enabled it again, and what they showed is in "What is NOT delivered"
and in the status line of each section on the durability of a push. What lab 27 showed: the brick converged in 2 min 30 s
from the path patch (chart pulled from `code.forgejo.org` under the restricted egress policy, `forgejo-db` 3/3 with synchronous
replication, archiving and the replica copy), HTTPS through the load balancer and the Gateway worked (`/api/healthz` 200, registration
refused, anonymous API 403, anonymous clone refused, `/metrics` answered 500 at the Gateway in that lab (the route's own rule with no backend; a 403 since
the Gateway policy of 0.4.1, and `//metrics` is covered since 0.4.3, see "The metrics endpoint and its token") and is scraped by vmagent), the admin
logged in and was forced to change the password, two repositories were pushed (3 s each) and cloned. The defects: the daily
volume backup never selected the volume (FG), acknowledged pushes were lost on a zone power-off (FK, DATA LOSS), and enabling the
brick is a hand patch (FF). FG and FK have a fix below; FF is a documented gap ("Opt-in"). Not a compliance claim. The sections below on
`core.fsync`, `dirsync`, the lock cleaners and the first base backup keep the lab 27 figures; where a sentence says "UNVERIFIED", "the next drill"
or "the only loss seen", it names lab 27 and then says what labs 27B to 28C showed, and the two sections on the durability of a push open with a
status line. What is still open (apps #176) is in "What is NOT delivered".

## What is delivered

- **Forgejo 15.0.11** (LTS, end of support 2027-07-15, `docs/eol-calendar.yaml`), from the chart `forgejo-helm` 17.3.2, a single replica
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
  account once and asks for a change at the first login), `secret/forgejo/oidc` (a **public placeholder**: the Zitadel auth source
  ships disabled; an operator overwrites it when rung 3 starts) and, since 0.4.3, `secret/forgejo/metrics` (40 characters, the bearer token of `/metrics`).
- Metrics scraped by vmagent (`VMPodScrape forgejo`, with the bearer token since 0.4.3); no rule reads them yet. `/metrics` is not routed on
  the Gateway (403), `//metrics` neither since 0.4.3, and the endpoint asks for the token anyway (next section but one).
- A row in `docs/public-exposure.yaml`: Forgejo's own login, **self-registration off, sign-in required to view anything** (the app load
  balancer has no source ACL, so the route answers anyone; Forgejo's defaults would have let that visitor register).

## Opt-in: OFF by default, how to turn it on

The 0.4.0 release gate forbids anything that gets worse. The brick costs +400 m of CPU requests and 1.4 GiB of memory requests (below)
and has run on a cluster only when a lab turned it on by hand (labs 27 to 28), never as part of a default install, so a default install does
**not** carry it: `apps/flux/management` and `apps/flux/workload` remove
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
`secret/forgejo/admin`, `secret/forgejo/oidc` and (0.4.3) `secret/forgejo/metrics` on every install, whichever profile runs: it has no knowledge of the profile, and the
repository already seeds the secrets of bricks a profile may not carry in the same way (`minio/root`, `longhorn/encryption`,
`grafana/*`). Created only if absent, never printed, never rotated. With the brick off nothing reads them: no ExternalSecret, no database
role, no account exists, so they open nothing; they are four unread entries of the KV store (and of its snapshots). The one exception is
`forgejo/metrics`, which has a second ExternalSecret in `services-observability` (the scrape's Secret, one Secret nobody reads without the forge). The placeholder of
`forgejo/oidc` is a fixed public string and protects nothing. In exchange, enabling the brick later needs no seeding step. An upgraded
cluster re-runs the Job (the script ConfigMap changed) and gains the entries it lacks (`forgejo/metrics` on a cluster moved from 0.4.2) whether or not it enables the forge.
"The brick converges unattended" is **not** claimed as a property of a default install, which does not carry it. After the hand patch of the
path the labs saw it converge by itself in 1 min 17 s to 2 min 33 s (NOMINAL, no fault; Scaleway 3 CP + 5 W and + 6 W and OVH 3 CP + 4 W and + 6 W: "What
is NOT delivered", "Seen on a cluster since"); a cluster rebuilt from the tfvars starts without it again.

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
   sub-chart (17.3.2, the pin of this change, has the same `values.yaml` and `values.schema.json`). Every values key the brick sets was checked against that chart's `values.yaml` and schema by `helm template`.
3. **The admin Secret had no `username`.** The chart reads `username` and `password` of `gitea.admin.existingSecret`; the ExternalSecret
   provided only the password, so the `configure-gitea` init container would have stayed in `CreateContainerConfigError`.
4. **Nothing seeded the three OpenBao paths**, so `forgejo-db-credentials` would never exist, the database would never bootstrap and (it
   was inside `cnpg`) the Kustomization every other database waits for would have stayed NotReady.
5. **The database was a 0.3.x one**: two instances, an asynchronous `barmanObjectStore` (CNPG 1.31 removes it), no tolerations, no zone
   term. It is the `grafana-db` form now. There is **no upgrade path to describe**: `forgejo-db` is new in 0.4.0, nothing of it exists on a
   0.3.x cluster, so the presync of synchronous replication before the apps ref moves (`docs/resilience.md`) does not apply to it.
6. **Registration was open on a public route** and `/metrics` was served to the Internet (the rule written for it covered the plain path only: `//metrics`
   was served at 0.4.1 and 0.4.2, fixed in 0.4.3). And the Gateway's egress policy
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
passes, **which is why the brick is opt-in and why the lab must enable it explicitly**. **The +400 m is derived, not measured.** Three workers were refused already; the two-worker zone is no better.

Readings with the forge on, none against a twin cluster without it, so none isolates its share (they count requests, not use):

| Run | Cluster | With the forge, the three profile examples of `docs/database-profiles.md` (and the audit sink in lab 28) | Without them |
|---|---|---|---|
| Lab 27 (Scaleway) | 3 CP + 5 W POP2-4C-16G | 313% of a worker requested; the 3 workers left by a two-worker zone hold 300%, so `failure-domain-check` refuses and the pods would stay Pending (infra `docs/capacity.md`) | not recorded |
| Lab 27D (OVH) | 3 CP + 4 W c3-8 | 10.47 vCPU requested, 262% of a worker | about 8.6 vCPU, 215% (the default platform) |
| Lab 28 (OVH) | 3 CP + 6 W c3-8 | 11.92 vCPU, 302% of a worker, 24.8 GiB of memory; after the loss of any one zone (2 workers) what is left to place takes 73% of the CPU and 84% of the memory the remaining workers can allocate | not recorded |

The lab 27D difference (10.47 minus about 8.6, about 1.9 vCPU) is of the size of the three profile examples alone (1.8 vCPU of requests, `docs/database-profiles.md`), against 2.2 vCPU derived for the examples plus the forge: it neither confirms nor refutes +400 m (the 8.6 is rounded), so the forge's own share stays derived. Reading the 313% and the 302% as "the forge costs this much" would be wrong. Infra's `docs/capacity.md` quotes the 313% of lab 27.

## Data: what protects what

| What | Where | Protection | Not covered |
|---|---|---|---|
| users, issues, pull requests, settings | `forgejo-db` | no acknowledged commit lost (3 instances, synchronous), WAL and daily base backup to the primary store, copy to the replica store | the same limit as the other databases: both disks that hold an acknowledged commit lost at once |
| the git repositories themselves | the 10 Gi Longhorn volume of the class `longhorn-x2-rwo-forgejo` (two replicas) | two Longhorn replicas: the volume survives the loss of a node, **for what git had synced** (below); `core.fsync = all` so an acknowledged push is on disk; a **daily Longhorn backup** (below) to the platform's backup target | the fsync setting and the new class held in the later drills (status lines below); consistency with the database (below); the unencrypted volume (the class is not LUKS) and a backup of it that nothing here encrypts |

### The first base backup of `forgejo-db` is taken at enablement (F-FORGEDB-BASE, lab 27 sitting 2)

Lab 27 enabled the forge on a running cluster and, five hours later, `forgejo-db` had WAL in both stores and **no base backup in either**: the only ScheduledBackup runs at 03:40, and a WAL archive cannot be replayed without a base backup, so a point-in-time recovery or a restore was impossible for up to a day, the whole database exposed. **Change:** the `forgejo-db-daily` ScheduledBackup sets `immediate: true` (the field `spec.immediate` of the vendored CRD, "If the first backup has to be immediately start after creation or not"). It acts once, when the object is created, and the daily schedule is unchanged; there is still one ScheduledBackup for forgejo-db, and `test-forgejo-brick.py` fails if there are two or if the field is missing. **Seen on a cluster since:** the immediate Backup is created when the forge is enabled, together with a Cluster that is still bootstrapping, and it Completed (NOMINAL; lab 27C, Scaleway, 3 CP + 5 W: 43 s after the patch; lab 27D, OVH, 3 CP + 4 W: created 2 s after the patch and Completed), so the first base backup no longer waits for 03:40. Had it failed, the Backup object would be Failed and the first base backup the 03:40 one, no worse than before (take one by hand with a `Backup` of method `plugin`). **Also set, since the owner decision of 2026-10-26, on `zitadel-db`, `grafana-db` and the customer profile (`apps/components/cnpg-profile-backup`):** the first version left them alone for fear of simultaneous backups, but the only simultaneous start is ONE TIME, when several Clusters are created together (a fresh install: up to three base backups start at once, a one-time burst of base-backup writes to the one S3 endpoint, plainly; nothing recurs), while the 03:10, 03:25, 03:37 and 03:40 slots are recurring and untouched. **What `immediate` does to an object that already exists** (an upgraded 0.3.x cluster updates `zitadel-db-daily` and `grafana-db-daily`, which keep their names): in CloudNativePG v1.30.1 (`internal/controller/scheduledbackup_controller.go`, read through the GitHub API) the immediate Backup is created only while the ScheduledBackup's `status.lastCheckTime` is empty, and the controller sets that field the first time it sees the object, with or without `immediate`; so an object that already exists and was already seen by the operator **does not trigger a backup on upgrade**. It would if the object were deleted and created again, or if the operator had never reconciled it. For method `plugin` the code gates the immediate backup on a ready Cluster only for `volumeSnapshot`, so a Backup is created at once even while the Cluster bootstraps (seen above). A test (`test-cnpg-barman-plugin.py`, `test-cnpg-profiles.py`) fails if a Cluster has no ScheduledBackup with `immediate: true` or has two. **Seen on fresh clusters (NOMINAL, no fault):** the immediate Backups of `zitadel-db` and `grafana-db` Completed within 5 minutes of the first Flux apply (lab 27D, OVH, 3 CP + 4 W), and on the fresh cluster of lab 27C (Scaleway, 3 CP + 5 W) those of `zitadel-db`, `grafana-db` and `forgejo-db` Completed within the first minutes (in those labs the forge was enabled by the patch after the install). UNVERIFIED: the load of such a burst on the S3 endpoint (not measured), and the three databases created in the same second by one fresh install that has the forge on.

### The durability of a push: `core.fsync = all` (FK, lab 27, DATA LOSS)

**Status since lab 27** (an UNVERIFIED mark below says what lab 27 could not show; what the later labs showed follows it, or the mark stays). With `core.fsync = all` and
`dirsync` in force, no acknowledged push was missing afterwards in the forge drills that followed, each an INCIDENT by design: lab 27D (OVH,
3 CP + 4 W) three hard power losses of the forge's node (500, 42 and 554 pushes); lab 28 (OVH, 3 CP + 6 W) six hard reboots with the forge in
them (five of its node, one of its whole zone) and three partitions of its node, `git fsck` clean; lab 28B (Scaleway, 3 CP + 6 W) two graceful
zone power-offs with the forge in the zone, the case that lost 1 of 59 in lab 27 (487 and 511 pushes); lab 28C (Scaleway, same shape) ten more, 4,500+
pushes in all (the lab's total). Acknowledged against present was read from a fresh clone. The cost was measured once (lab 27B, Scaleway, 3 CP + 5 W,
NOMINAL): `core.fsync = all` against `none`, 60 pushes each, alternating, from the lab host over HTTPS: +30 to +60 ms on a push of about 1 s (3 to 6 %),
inside the run-to-run noise of about 30 ms; the cost of `dirsync` alone was not separated. The cause below stays a hypothesis (no experiment
isolated it), and a series of drills where the fix held does not prove that no push can ever be lost.

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
sheet says so). And the fix is a hypothesis on a cause: **at lab 27 it was UNVERIFIED on a cluster until a drill re-ran it** (the push writer in the
zone power-off, with fsync on, then `git fsck` and a clone). The drills of the status line above re-ran it and lost no acknowledged push;
that agrees with the fix and does not isolate the cause. **Cost (not measured at lab 27, measured once since):** every push now pays the
fsyncs of its loose objects, packs, index and references, so the push latency goes up; lab 27 measured pushes of 3 s each without it,
through the load balancer, which is dominated by the network path, and lab 27B measured +30 to +60 ms (3 to 6 %) on a push of about 1 s
(NOMINAL, Scaleway, 3 CP + 5 W, inside the noise of about 30 ms), with the cost of `dirsync` alone still not separated.

**Why Longhorn does not already cover it.** The volume has two replicas (`longhorn-x2-rwo-forgejo`), which protects against the loss of
a node holding one replica of data the engine already has. The filesystem on the volume is mounted on the node where the pod runs, and
what git wrote but never synced sits in that node's page cache: it has not reached the Longhorn engine, so NEITHER replica has it. A
node or a zone that powers off while it holds the mount (and, in the drill, the writable replica too) loses exactly that tail, whatever
the replica count. fsync is what makes the data leave the cache before the push is acknowledged; replication is what keeps it once it
has. (This is the reading that fits the lab. The drills since, with fsync on, lost no acknowledged push: that agrees with it and does not isolate
it; the lab 27B zone drill with fsync on lost nothing (830 of 830) and the lab 27C one, same setting, lost 1 of 59, so one drill proves little.)

### One acknowledged push lost at a graceful zone power-off, and stale locks (FK2, lab 27 sitting 3; `dirsync` and the lock cleaner)

**Status since lab 27** (as above, an UNVERIFIED mark below says what lab 27 could not show; what the later labs showed follows it, or the mark stays). The 1 of 59 lost at a
graceful zone power-off was not seen again: the drills listed in the section above had no push missing, among them twelve graceful zone power-offs on
Scaleway. `dirsync` was on the mount of the node (`ext4 rw,dirsync` in `/proc/mounts`, globalmount and pod mount, lab 27D). The cleaners: with the
init container that removes a lock of any age, a hand-made stale lock younger than a minute was removed at pod start and the branch took a push 1 s after
the forge answered (lab 28, OVH, 3 CP + 6 W, INCIDENT: a hard reboot of the forge's node; the first version, 5 minutes, had left the branch blocked 9 minutes
in lab 27D, OVH, 3 CP + 4 W, INCIDENT: a hard power loss during a push); in two other cuts the hand-made lock
was gone after the cut, so those runs say nothing about the cleaner; the 60 s sweeper released a hand-made lock after 666 s (lab 28, OVH, 3 CP + 6 W, NOMINAL:
no fault, no power loss; the `kill -9` of the forge process was not run). The lock of a REAL interrupted
push (a window of a few milliseconds) was met once (lab 27D, the case that led to the change) and in none of the hard cuts of lab 28: the cleaner is
proven on a hand-made lock, not on that case. The cost of `dirsync` alone is still not measured.

**What was measured at lab 27 (Scaleway, ONE lab, 3 CP + 5 W, each drill an INCIDENT by design, `core.fsync = all` effective).** Zone power-off of the zone holding the forge pod: the push writer counted 59 acknowledged pushes and **the 59th, acknowledged at 01:14:05, was not on the branch**: the server's `refs/heads/<branch>` held push 58, `refs/heads/<branch>.lock` held the id of push 59, the commit object of push 59 was in a pack, `git fsck` was clean (one dangling commit) and a clone worked. Git's last step of a ref update is the rename of that lock file onto the ref; it had been acknowledged to the client and was not durable. Every later push to that branch was then refused ("remote rejected (reference already exists)") because the stale lock stayed. In the other drills: 0 of 700 and 0 of 37 lost in the two partition drills, 1525 and 881 in the shrink and replace windows. In the partition drill with the Longhorn policy the push in flight at the cut left a lock and **the branch refused every later push for the next 10 minutes** (the writer never recovered): neither Forgejo nor git cleans it.

**Two changes, and what each does and does not fix.**

1. **Stale lock cleaners (a fix for the stale lock; the second round was UNVERIFIED at lab 27, and has been seen since on a hand-made lock, not on the lock of a real interrupted push: status line above).** Two containers run ONE script (post-renderer patches in `helmrelease.yaml`) on the app's own image, non-root, with the chart's restricted securityContext and the init containers' requests and limits. It removes the regular files named `refs/**/*.lock`, `packed-refs.lock`, `HEAD.lock`, `config.lock` and `shallow.lock` of the bare repositories under `/data/git/gitea-repositories` (the `[repository] ROOT` of the rendered app.ini). Not `index.lock`, not objects, not symlinks or directories. It logs the relative path of each file removed, never its contents, and always exits 0 (a cleanup that cannot run must not keep the forge from starting); an age that is not a number removes nothing. `test-forgejo-brick.py` runs the script on a temporary tree (old, fresh and non-lock files) and pins that both containers carry the same text.
   - **The init container `clean-stale-git-locks` (any age).** It runs FIRST in the pod. At that point no Forgejo git process runs on the volume: the Deployment strategy is Recreate (the chart's default; the brick test pins it on the rendered Deployment), so the old pod's containers are stopped before the new pod exists, and the volume is ReadWriteOnce with Longhorn refusing a second attach while the old one is held, so a pod on a partitioned node cannot write to it any more. Any lock found there is stale. **Why it changed (lab 27 sitting 4, OVH, F-LOCKAGE):** the first version removed only locks older than 5 minutes; after a HARD power loss during a push the new pod started 2 minutes later, kept the 2 minute old lock, and the branch refused every push for 9 minutes until a deliberate pod restart.
   - **The sidecar `sweep-stale-git-locks` (every 60 s, locks older than 10 minutes).** An init container does not run again when only the main container restarts (an OOM kill, a crash): its git children die with it and leave their locks. A RUNNING forge can hold a live lock, so the sweeper never touches a young one: a ref lock lives for the milliseconds of one ref update, Forgejo ends its git commands after `[git.timeout] DEFAULT` = 360 s, and the age is looked at again right before each removal. If a git process were nevertheless stalled for more than 10 minutes on a failing disk, deleting its lock makes its final rename fail, which is an error to the client of that push, not a corrupt ref (reasoning, not measured). The container stops at once on TERM. `kubectl logs deploy/forgejo` still shows the forge (the `default-container` annotation); the cleaners' lines are `-c clean-stale-git-locks` (init) and `-c sweep-stale-git-locks`.
   **It does NOT recover the lost acknowledged push**: the lock of push 59 held the lost update and the cleaners delete it. Promoting a complete lock onto its ref would apply an update whose acknowledgement the client may never have seen and that skipped Forgejo's post-receive bookkeeping, so it was not done. By hand, the same cleanup: `find /data/git/gitea-repositories -name '*.lock' -mmin +5` to list them first. **Asked of the next lab at lab 27, and where it stands:** after a hard power loss during a push, the next push to the branch goes through as soon as the new pod's init container has run (target: no push refused after the forge answers): seen on a hand-made lock younger than a minute, 1 s after the forge answered (lab 28, OVH, 3 CP + 6 W, INCIDENT), not on the lock of a real interrupted push; after `kill -9` of the forge process (main container restart only), the branch is released within about 11 minutes and the sweeper logs the path: the `kill -9` was not run, the sweeper released a hand-made lock in 666 s (11 min 6 s, lab 28, OVH, 3 CP + 6 W, NOMINAL) and logged its path; no push is refused or corrupted while the sweeper runs under a push load (it must remove nothing under 10 minutes): in that test, with one writer pushing every 3 s to the locked branch, the lock stayed until 666 s (nothing removed under 10 minutes) and the 429 pushes acknowledged after the release met no refusal; a sweeper running under a heavier push load on several branches was not a test of its own.
2. **`dirsync` mount (an experiment on the lost ref update; UNVERIFIED at lab 27, seen on the mount since, and its effect on the window is not isolated).** The StorageClass `longhorn-x2-rwo-forgejo` now has `mountOptions: [dirsync]`: ext4 then applies directory updates synchronously (mount(8): https://man7.org/linux/man-pages/man8/mount.8.html), which is the only knob here that reaches the DIRECTORY entry the lock-to-ref rename changes (`core.fsync = all` syncs files, not that entry). Longhorn formats ext4 by default and supports ext4 and xfs (https://longhorn.io/docs/1.12.0/references/storage-class-parameters/); the Kubernetes StorageClass passes `mountOptions` to the CSI driver (https://kubernetes.io/docs/concepts/storage/storage-classes/#mount-options). Seen since: Longhorn's node plugin applies it (`ext4 rw,dirsync` in `/proc/mounts` of the node, globalmount and pod mount, lab 27D, OVH, 3 CP + 4 W, NOMINAL; read it with `talosctl read /proc/mounts`: the volume's mount must list `dirsync`). Not verified: that it closes the window (no acknowledged push was lost in the drills since, which agrees with it and does not isolate it), and its cost, **a performance cost on every push, branch and repository creation that is UNVERIFIED (not measured: the lab 27B figure is for `core.fsync` alone)**. It applies to volumes created after the class changed; a forge volume that already exists keeps its mount (the class field can be updated, a provisioned PV is not): to move one, restore it into a new volume of this class (the daily Longhorn backup), not in place.

**What is still true.** A ref update that git acknowledged and the volume then lost at an abrupt power loss is addressed ONLY by the `dirsync` experiment. At lab 27 that was **UNVERIFIED until the drill**; the drills since (status lines above: three hard power losses in lab 27D, six hard reboots and three partitions in lab 28, twelve graceful zone power-offs in labs 28B and 28C) lost no acknowledged push, which agrees with the experiment and does not prove it. The 1 of 59 at a graceful zone power-off was the only loss seen in the more than 3000 acknowledged pushes of lab 27 sitting 3, and it was not seen again. **Owner decision 2026-10-27: if the next drill still loses one acknowledged push, the forge stays optional and is documented as NOT lossless on abrupt power loss** (it is opt-in, off by default, and that sentence goes on this page and in the release notes). The drills since lost none, so that condition has not occurred; the forge stays opt-in and off by default, and a series of drills without a loss does not prove that no push can ever be lost.

**What lab 27 asked the next lab to PROVE, and where each point stands:** (a) the zone-loss drill again, the forge pod and the writable replica in the lost zone, **with a push writer that reads the branch back afterwards: report acknowledged against present** (target 0 missing, over several repetitions, since one loss in 59 is the rate to beat), `git fsck` clean and a clone: run twelve times with the forge pod in the zone (labs 28B and 28C, Scaleway, 3 CP + 6 W, INCIDENT drills on a NOMINAL cluster), 0 missing each time, read from a fresh clone; `git fsck` is not recorded in those two journals (it is in labs 27B, 27D and 28), and where the volume's replicas sat is not recorded either, so the "writable replica in the lost zone" part of the arrangement is not shown; (b) `dirsync` in `/proc/mounts` of the node: seen (lab 27D); (c) after an interrupted push, the next push to the branch goes through once the pod restarts (the cleaner log line names the lock), also when the lock is younger than 5 minutes (sitting 4: 9 minutes blocked with the first version, INCIDENT, OVH 3 CP + 4 W): seen on a hand-made lock of under a minute, the push 1 s after the forge answered (lab 28, INCIDENT, OVH 3 CP + 6 W), not on the lock of a real interrupted push; (d) the push latency with and without `dirsync` and `core.fsync = all`: `core.fsync` alone measured once (lab 27B), `dirsync` alone not.

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
colliding schedule, or no retention. **Seen on a cluster since:** a volume created from the new class carried the group at creation with no manual
step and the job's backup completed at the target (lab 27B, Scaleway, 3 CP + 5 W: 289 MB; lab 27D, OVH,
3 CP + 4 W: a backup of the 396 MB volume). Lab 27 had proved only the job and the restore after the manual label.

What this covers: the content of the volume (the bare repositories) as of the night's snapshot, 14 of them, if the platform has a backup
target. What it does not cover, precisely:

- **No target, no backup.** Without `backup_target_url` Longhorn has nowhere to send the backup; the job is rendered anyway (the
  chart and the job do not know). What Longhorn does then is not verified here.
- **The database and the volume are not backed up at the same instant.** `forgejo-db` has its base backup at 03:40 and a continuous WAL
  archive, the volume a snapshot at 03:55; nothing quiesces the forge in between. Restored as a pair (the database to a point in time,
  the volume from the night's backup), they can disagree: a repository the database knows with pushes the volume lacks, or pushes on
  disk that no issue, pull request or webhook row in the database accounts for; an application-consistent copy of both needs
  `forgejo dump` (or stopping the forge) at one moment, which nothing schedules. A restored volume is crash-consistent at best.
  **The window, and how to narrow it (reasoning, UNVERIFIED: no restore of `forgejo-db` has been run).** Between the database's base backup (03:40)
  and the volume's backup (03:55) there are 15 minutes: that is the disagreement if `forgejo-db` is restored from its base backup alone. It does not
  have to be: the WAL archive lets CloudNativePG recover to any instant after the base backup (point-in-time recovery, `apps/base/backup/README.md`,
  "Order of a full recovery"). To restore the pair with the smallest disagreement, restore the volume from a Longhorn backup (Longhorn lists the time
  of each) and recover `forgejo-db` to that time (`targetTime`). What can still disagree is what Forgejo had in flight at that instant (a push whose
  objects reached the volume and whose row was not committed yet, or the reverse); only a `forgejo dump` closes that, and nothing runs it. The
  window is documented, not closed.
- **A restore of the repositories' volume was run TWICE**: in lab 27, by hand after the manual label, and in lab 27B (Scaleway) from the backup of the
  default job. Each time the backup (289 MB) was restored into a scratch volume through a StorageClass with `fromBackup`, a PVC and a pod (about 35 s
  in lab 27B, Scaleway, 3 CP + 5 W, NOMINAL); it held both repositories, `refs/heads/main` was the pushed commit, `git clone` and `git fsck` were clean, and the scratch namespace
  and class were deleted. No restore of `forgejo-db` has been exercised. The documented recurring test of a volume is the opt-in `longhorn-restore-test` component (a canary volume backed up
  and restored weekly): it proves that a Longhorn backup comes back on the platform, not that the Forgejo one does.

## How long the forge is away after a node or zone loss (F-FORGE-TOL, lab 27 sitting 2)

**Measured at Kubernetes' default toleration (lab 27, sitting 2, Scaleway, 3 CP + 5 W, zone power-off as an INCIDENT by design, forge pod and one Longhorn replica of its volume inside the zone):** the pushes stopped at +31 s and came back about **8 minutes** after the power-off (push gap 471.8 s). The pod went NotReady at +79 s and was **not** evicted for 300 s, the default `tolerationSeconds` that Kubernetes adds to a pod with no toleration of `node.kubernetes.io/not-ready` and `unreachable`; the new pod was scheduled on another worker, Longhorn showed "Waiting for detach" (the old attachment was on the lost node) and attached the volume 20 s after the pod was scheduled, and the container started 10 s later. The databases do not wait: they tolerate 30 s. No data was lost (830 of 830 acknowledged pushes, `git fsck` clean).

**Change:** the HelmRelease sets the chart's `tolerations` value to the same two tolerations as the databases, `node.kubernetes.io/unreachable` and `node.kubernetes.io/not-ready`, `NoExecute`, `tolerationSeconds: 30` (the chart renders the value into the Deployment's pod spec; `test-forgejo-brick.py` checks the value and the rendered Deployment). **Projected, NOT measured:** NotReady at +80 s, eviction 30 s later, then the lab's 20 s attach and 10 s start: the forge back in about 2.5 to 3 minutes instead of 8. That figure assumes the attach takes as long as it did once; the drill with the 30 s toleration has not been run.

**Measured with the 30 s toleration (lab 27 sitting 3, Scaleway, 3 CP + 5 W, ONE lab, each drill an INCIDENT by design, forge pod on worker-0, volume replicas on two other workers; the projection above was 2.5 to 3 minutes):**

| Drill | Forge away | Why |
|---|---|---|
| Zone power-off, default Longhorn policy `do-nothing` | **372 s** | the replacement pod was scheduled after the 30 s toleration (01:15:05) but stayed in `FailedAttachVolume: Waiting for detach ... Volume is already used by pod(s) <old>` until the zone came back |
| Partition of the forge's node (netem 100 %, 300 s), default policy | **341 s** (the partition plus 41 s) | the same wait; Longhorn did NOT re-attach elsewhere during the partition (the "unknown" of sitting 2: answered, it waits for the node). Push 700 acknowledged, 0 missing |
| The same partition with `node-down-pod-deletion-policy = delete-both-statefulset-and-deployment-pod` set by hand (LAB-ONLY, reverted) | **128 s** | NodeNotReady +27 s, eviction +57 s, Longhorn detached the volume 3 s later and attached it on another worker 27 s after; the projected 2.5 to 3 minutes is met. Push 37 acknowledged, 0 missing, but the push in flight at the cut left a stale lock (above) |

**Change (0.4.0, owner decision 2026-10-27): the platform sets `node-down-pod-deletion-policy` to `delete-both-statefulset-and-deployment-pod`**, with the mechanism, the evidence on what reaches an upgraded cluster, the side effects and the one-line check in [`resilience.md`](resilience.md), "Longhorn: node-down-pod-deletion-policy". Until lab 28, **the figures were 341-372 s measured with the default and 128 s measured with the setting applied by hand, and the shipped path itself was UNVERIFIED.** Lab 28 (OVH, a NOMINAL 3 CP + 6 W cluster, each drill an INCIDENT by design) ran it with the shipped setting: the forge was away 154, 155 and 169 s for three partitions of its node, 85, 115, 116, 126 and 169 s for five hard reboots of its node and 125 s for a simultaneous hard reboot of its whole zone (three nodes), with every acknowledged push present. (The lab's summary line counts four hard cuts, 116, 125, 126 and 169 s; two later stale-lock runs also cut the forge's node and are counted here.) Lab 28B repeated it on Scaleway with the shipped setting (INCIDENT drills on a NOMINAL cluster): away 178 and 174 s for two graceful zone power-offs and 145 s for a partition (3 CP + 6 W), 134 s for a graceful node power-off (3 CP + 7 W), every acknowledged push present. The push gap and the volume stay a single-replica design: the forge is away for this long on a node or zone loss.

## Updating the forge: 15.0.10 to 15.0.11, chart 17.3.1 to 17.3.2 (apps #179)

**Rung: read offline and rendered on 2026-11-04; then run on two proof clusters (lab 34, Scaleway, 3 CP + 6 W, n=1, the moving branch ref, not a tag): the move from 15.0.10 on cluster A (six of six containers on the new image) and a fresh install on cluster B.** What was read, on 2026-11-04:

- **The chart diff is two files.** `Chart.yaml` (`appVersion` 15.0.11) and the `helm test` Pod, which this brick switches off (`test.enabled: false`).
  `values.yaml`, `values.schema.json`, the templates of the Deployment, the Secrets, the Service and the PVC and the vendored `common` sub-chart
  are identical. Rendered with the brick's values (`helm template` plus the post-renderer), the two versions differ only by the image tag of the
  main container and of the three chart init containers, the `version` labels, and the `checksum/config` annotation (a hash that reads those labels;
  the config Secrets carry the same keys and values). No change in probes, security context, resources, tolerations, strategy (Recreate) or PVC.
- **Two images are written by hand** (the lock cleaner and the sweeper, `helmrelease.yaml`). A chart bump alone leaves them on the old Forgejo:
  `test-forgejo-brick.py` went red on exactly that and is green with both moved. The pod therefore runs the Forgejo image in **six containers**:
  `forgejo` (main), the chart's `init-directories`, `init-app-ini` and `configure-gitea`, the lock cleaner `clean-stale-git-locks` (init) and
  the sweeper `sweep-stale-git-locks` (sidecar). After the roll all six must read `15.0.11-rootless`; none may read `15.0.10-rootless`.
- **Upstream notes of 15.0.11** (released 2026-10-26 on endoflife.date, which still gives the 15.0 LTS cycle end of support 2027-07-15). Three security fixes: Actions `pull_request_review` could run a workflow from an arbitrary
  `commit_id`; the visibility of users was not considered when listing watched and starred repositories; a NuGet `X-NuGet-ApiKey` token did not respect
  its scopes (package APIs only). Also one dashboard fix, two Actions-log fixes, and updates of the Go toolchain (1.26.10, which clears the `stdlib` HIGH CVE-2026-111943 that the
  15.0.10 binaries report) and of a build-time dependency. Forgejo 15.0.11 has Actions and Packages on by default and the brick sets no `[actions]`
  or `[packages]` key, so both are on; what narrows the reach is that no runner is registered (a workflow cannot run), registration is closed and
  sign-in is required to view. Which of the three fixes applies to this platform was not established.
- **No schema migration is visible.** 15 commits between `v15.0.10` and `v15.0.11` touch 21 files, none of them a migration. That is a reading of the file
  list, not a run: a roll back to 15.0.10 was not tried.
- **The brotli CVE is not in the release notes.** It leaves with the rebuilt Alpine base (3.23.6 to 3.23.7). Trivy 0.75.0, database of 2026-11-04, `linux/amd64`
  (arm64 not scanned), by index digest: 15.0.10-rootless reports 1 CRITICAL (`brotli-libs` 1.2.0-r0, CVE-2026-111497, fixed in 1.2.1-r0) and 8 HIGH;
  15.0.11-rootless reports 0 CRITICAL and 3 HIGH, all `libexpat` 2.9.0-r0 (fixed in 2.10.0-r0, not yet in the image),
  with `brotli-libs` 1.2.1-r0. A second scan with Trivy 0.69.3, the version the SBOM workflow caches, on the same database gave the same figures
  (15.0.10: 1 CRITICAL, 8 HIGH, 6 of them Alpine and 2 in Go binaries; 15.0.11: 0 CRITICAL, 3 HIGH), so the figure of the CI scanner exists for this
  image without a cluster; the `SBOM` run on the merged head still confirms it. The scan gate of the SBOM workflow is CRITICAL with a fix, so these 3 HIGH do not fail it.
- **Pins, to compare with what runs.** The image is pinned by tag, not by digest (see "Still open" below). For the comparison of declared with
  running, the registry gave on 2026-11-04: `15.0.11-rootless` index `sha256:3cd6bc2b74c87cbde0a0fd527c9665cb416f5a0540dee54fe5afb3fa2df9f40c`, amd64
  manifest `sha256:ab0b8fd8bfc55b6e0721347c0173c128c6431b3543ff2ab7a863929f1c5d2629`; `15.0.10-rootless` index
  `sha256:6ffebd9ab9ee9f6677e3523fa60debd9de630ff2a226fa681d6cc0c7ba541d77`. A pod's `imageID` shows either the index or the platform manifest,
  depending on the runtime: both are correct.
- **The roll is a downtime.** The Deployment is Recreate on a ReadWriteOnce volume: the old pod stops, then the new pod runs its init containers and
  starts. A push or a clone in flight at that moment fails and has to be retried. There was **no baseline** for how long. The first figure is lab 34's move
  on cluster A: the forge was away **50.3 s** (NOMINAL, 3 CP + 6 W), in ONE roll that carried this update and the token environment of 0.4.3
  ("The metrics endpoint and its token"); it is a first figure for the pair, not a baseline for either change alone, and not better or the same.

## The metrics endpoint and its token (0.4.3)

**Why.** Forgejo serves `/metrics` (`gitea.metrics.enabled`, for vmagent) with no credential unless `[metrics] TOKEN` is set. The Gateway
refused `/metrics` with a 403 since 0.4.1, but not `//metrics`, which Forgejo cleans to `/metrics`: at 0.4.1 and 0.4.2 a forge behind the app load
balancer answered `https://forge.<domain>//metrics` with its metrics (66 families, 11 KB, no credential). The Gateway is fixed in two layers
(`docs/public-exposure.md`: slash merging in istiod, two more paths in the policy); this is the third layer, at the source: the endpoint
answers 401 without the right bearer, whatever the path.

**Rung: rendered and first measured on a LOCAL RIG (k3s in a container, Istio 1.31.3, Forgejo 15.0.11 from chart 17.3.2 with SQLite and `emptyDir`,
VM operator 0.68.1, vmagent v1.136.0, the Secrets made by hand); the ExternalSecrets, the OpenBao path, the scrape and the upgrade were not run on
it. Then observed on two proof clusters (lab 34: Scaleway, 3 CP + 6 W, n=1 per figure, the moving branch ref at the heads apps e8ca37f and infra
d1d268c, NOT a tag): cluster A, a move from 0.4.2 with the forge on, and cluster B, a fresh install with the forge on.** Each item below says which.

- **The token.** `secret/forgejo/metrics` `{token}`, 40 alphanumeric characters, created by the vault configure Job (create only, never rotated by
  a re-run, never printed). The chart has no value for it: `gitea.metrics.enabled` only renders `[metrics] ENABLED = true`, and
  `gitea.config.metrics.TOKEN` would put the secret in the HelmRelease values and in the chart's config Secret (the rendered Secret has no
  `TOKEN` line; `test-forgejo-brick.py` fails if a `TOKEN` is added to the values). So the Deployment gets `GITEA__metrics__TOKEN` from the Secret
  `forgejo-metrics` through a `secretKeyRef`, as the database password does. **Where the token then is:** in the pod's environment, and in
  `/data/gitea/conf/app.ini` on the repositories' volume, because the chart's `init-app-ini` runs `environment-to-ini` (read in the rendered
  chart, as for `GITEA__database__PASSWD`; **observed on both proof clusters: `app.ini` holds exactly 1 `TOKEN` line, equal to the Secret**). The
  daily Longhorn backup of that volume therefore carries it. **The operator's generated vmagent configuration Secret in `services-observability`
  contains the token (observed on cluster A)**, and the scrape target's label is `job="services-observability/forgejo"`.
- **The reference is NOT optional, on purpose.** An optional `secretKeyRef` starts the forge with no token when the Secret is missing, silently
  open. This one makes the pod wait in `CreateContainerConfigError` until the ExternalSecret has written the Secret, then start by itself.
- **The scrape.** With the token set, the unchanged `VMPodScrape forgejo` goes DOWN (vmagent: `unexpected status code 401`). It now carries
  `authorization.credentials` `{name: forgejo-metrics, key: token}`, read from a Secret of its own namespace: the operator resolves a scrape's
  Secret next to the scrape, and the forge's Kustomization (`services-git`) does not depend on the VM CRDs, so a twin ExternalSecret
  `forgejo-metrics` in `services-observability` (`apps/base/observability/externalsecret-forgejo-metrics.yaml`) reads the same OpenBao key. On a
  profile without the forge it is one Secret nobody reads. A scrape whose Secret does not exist is skipped by the operator with an error log
  ("skip config generation for resource"); vmagent and the other scrapes keep running, and the forge target returns when the Secret appears.
- **Measured on the rig** (Forgejo 15.0.11, token from a Secret): straight to the pod, no bearer 401, a wrong bearer 401, the right bearer 200
  (11,110 bytes); `//metrics` without a bearer 401; `/api/healthz` stays 200, so the kubelet probes are unaffected. Through the Gateway with the
  0.4.2 policy and no merging (the unfixed Gateway), the 25 spellings that leaked all answered 401, 0 of 35 leaking, plain `/metrics` 403. With the
  right token, `//metrics` answers 200, as expected: the token is the secret. The scrape was UP with the credentials and DOWN without.
- **An existing forge keeps being scraped after the move, but not without a gap.** On the move from 0.4.2 three things change together: the vault
  script ConfigMap (Flux re-creates the configure Job, which creates `forgejo/metrics`), the two ExternalSecrets, and the Deployment's
  environment. The Deployment is **Recreate**, so the pod rolls (31 s on the rig with no volume, NOMINAL, one pod; the forge was Ready 72 s and 79 s after its
  patch in lab 34 at the 0.4.2 arm, runs 1 and 2, 3 CP + 6 W, NOMINAL, a different measure) and, if the Secret is not there yet, it sits in `CreateContainerConfigError`, with the forge
  away, until the ExternalSecret has been synced (ESO retries a failed read with a back-off, not on its `refreshInterval`). Nothing orders the forge behind the vault Job (the
  `forgejo` Kustomization has no `dependsOn` on `foundation-vault`, which is `wait: false`), so on an
  upgraded cluster the order is luck. The HelmRelease therefore carries `timeout: 15m` (Flux's default is 5 min): at 5 min Helm would roll the Deployment
  back to the spec without the token (the forge without layer 3, layers 1 and 2 still holding), and after `retries: 3` the release would stay stalled (reasoned from
  Helm's behaviour, not run: the unfavourable case below was run with `timeout: 15m`). **The window, measured in lab 34 in both orders** (3 CP + 6 W, n=1 per order,
  the moving branch ref, not a tag):
  - *Favourable order (cluster A, the move from 0.4.2 with the forge on, NOMINAL).* Flux re-created the vault Jobs inside their TTL, and they wrote
    `secret/forgejo/metrics` at 12:15:27; both ExternalSecrets synced at 12:16:42 to 12:16:45 (75 to 78 s later, computed from the timestamps; why it took that
    long is not shown); the forge pod rolled with **0 s in `CreateContainerConfigError`**. The forge was away **50.3 s** (last answer 12:16:45.6, first
    answer 12:17:35.9, 49 probe failures) and the scrape gap was **65 s**, back with no manual step; 1,284 acknowledged pushes, 0 missing, `git fsck`
    clean. That order was favourable by 76 s, and nothing enforces it.
  - *Unfavourable order (cluster B, the forge Secret unavailable during a Helm upgrade; INCIDENT, induced in the lab, 3 CP + 6 W: Flux suspended, the forge
    ExternalSecret pointed at a missing key, the upgrade triggered by an added env var; not the sequence of a late vault Job).* The pod waited **6 min 30 s** in `CreateContainerConfigError`;
    the HelmRelease reached `UpgradeSucceeded` with **no rollback** (`timeout: 15m`); the Deployment kept `GITEA__metrics__TOKEN` in 37 of 37 polls; the
    pod started by itself 19 s after the Secret came back. **A wait beyond 15 minutes is not shown.** During it `up{forgejo}` was 0 for about 7.5 minutes
    (90 samples), and no rule names the forge scrape (alerts were sampled at two moments only).
  - *Fresh install (cluster B).* The forge was enabled by the documented live patch 14 s BEFORE the vault Jobs were first created, so this is the favourable
    order by construction (the case of a Job still running is not shown): the pod started at the first try with 0 `CreateContainerConfigError` and was Ready
    101 s after the pod was created, which is not comparable with the patch-to-Ready figures above.

  The scrape comes back by itself when both Secrets exist and the new pod is up. Either order is safe: a scrape that sends the bearer to a forge that has
  no token yet is answered 200 (no token configured, the header is ignored, read in Forgejo's documented behaviour and not run), and a forge that has the token before the
  scrape has it answers 401, so the target is DOWN until the twin Secret exists. No alert reads the forge's `up` today (`vmrule-*.yaml` do not name it), so the gap pages nobody and shows only as missing `gitea_*` points. A push or a clone in flight across the
  roll fails and is retried, as for every roll of this Deployment.
- **Rotation.** Overwrite `secret/forgejo/metrics` in OpenBao; both ExternalSecrets follow within their `refreshInterval` (1 h); then restart the forge
  pod (the environment is read at start; `environment-to-ini` rewrites the key in `app.ini`). Between the Secret change and the restart the
  scrape gets 401. Not exercised.
- **The token travels in cleartext inside the cluster.** The scrape goes to the pod on `:3000` over plain HTTP, and `services-git` is outside the mesh with no pod-network
  encryption claimed anywhere in these docs: a pod or node that can see that traffic reads the bearer. The sensitivity is low (counters and build information), so this is a line here and
  an issue, not a design change.
- **Not covered by the token.** The kubelet probes (`/api/healthz`) and every other path are unchanged. Anyone who holds the token reads the metrics
  on any path spelling; the token is no stronger than the OpenBao policy that guards it (`secret/data/*` is readable by the ESO role).

## What is NOT delivered (open, each one is for an issue: apps #176)

Checked item by item against the runs that enabled the brick after this page was first written: labs 27B and 27C (Scaleway, 3 control planes +
5 workers), labs 28B (Scaleway, 3 control planes + 5 workers, then + 6 and + 7) and 28C (Scaleway, 3 control planes + 6 workers) and labs 27D and 28
(OVH, 3 control planes + 4 and + 6 workers).

**Seen on a cluster since.** Enabled by the documented path patch, no fault (NOMINAL): the brick converged in 1 min 44 s (27B), 2 min 23 s on an
upgraded and 1 min 36 s on a fresh cluster (27C), 1 min 17 s (27D); in lab 28 `forgejo` was ready at 72 s and `forgejo-db` at 2 min 27 s; 1 min 48 s on an upgraded cluster (28B, 3 CP + 5 W) and 2 min 33 s on a fresh one (28C, 3 CP + 6 W). The first
base backup of `forgejo-db` was taken at once (Completed 43 s after the patch in 27C). `backup-verify` covers `forgejo-db` and passed on the primary
and the replica store (lab 28). Under faults (each drill an INCIDENT by design, on the NOMINAL 3 CP + 6 W cluster of lab 28): five hard reboots of the forge's node,
one hard reboot of its whole zone and three partitions of its node, every acknowledged push present, `git fsck` clean, the forge away 85 to 169 s;
on Scaleway (labs 28B and 28C, 3 CP + 6 W) twelve graceful zone power-offs with the forge in the zone, no push missing, the forge away 174 and 178 s
in the two of lab 28B (the status lines of the two sections on the durability of a push, and "How long the forge is away", give the detail).
The volume's backup and its restore are in "The recurring backup of the repositories' volume".

**Still open:**

- SSH (git over SSH needs a Gateway `TCPRoute`; git is HTTPS only), OIDC login with Zitadel (rung 3: the client registration, `identity`
  in `dependsOn`, in-cluster resolution of `*.openaether.local`, enabling the commented `gitea.oauth` block), a mail relay (#174) and a
  status page (#175, out of scope here).
- A second Forgejo replica (needs ReadWriteMany, Valkey for sessions and cache, a shared object store), so the forge is a single replica
  listed in `scripts/ha-allowlist.yaml`: while its pod is rescheduled after a node loss there is **no push, pull or web access** (about 8 minutes
  measured with Kubernetes' 300 s default toleration; 341-372 s with the 30 s toleration and Longhorn's default policy; 128 s with the policy set,
  measured once with the setting applied by hand; 85 to 178 s in labs 28 and 28B with the shipped policy (INCIDENT drills, 3 CP + 6 W, one on 3 CP + 7 W):
  see "How long the forge is away"); the cluster and Flux keep running.
- Image **digest pinning** (the image is the chart's `appVersion` tag, `15.0.11-rootless`, bumped with the chart; the two lock-cleaner images are written by hand and bumped with it).
- **Backups.** (a) The database and the volume are not backed up at the same instant: the window, and how to narrow it by recovering the database to
  the time of the volume's backup, are written above (UNVERIFIED). (b) `forgejo-db` is not in the restic `pg_dump` (`cronjob.yaml`), the canary, or the
  restore tests (`restore_cnpg.py`, `cnpg-recovery-restore-test`), which know `zitadel-db` and `grafana-db`; no restore of `forgejo-db` has been
  exercised, and the disaster-recovery chain of OpenAether-infra (`task dr-restore-plan`) does not name it either (read in infra at commit 59e7bfa).
  (c) Nothing alerts if its archive goes stale beyond the generic CNPG rules (`CnpgSyncReplicaMissing` and the WAL archiver ones already read every
  instance pod). The verifier (`barman_check.py`) is no longer on this list: it covers `forgejo-db` (above).
- `readOnlyRootFilesystem` on the Forgejo containers: the one Kyverno finding left (Audit). The chart's init containers write under `/`;
  to prove read-only works needs a run.
- A rule on Forgejo's metrics. The `VMPodScrape` exists and vmagent scraped Forgejo in lab 27 (`up` = 1, 26 `gitea_*` series), but no rule reads
  the series and their names are not checked against a running Forgejo. Through the Gateway, `/metrics` is refused with a 403 for every route
  (`docs/public-exposure.md`). That 403 was read on the other hosts of a Scaleway proof lab, and on the forge only in a later one, which found
  that `//metrics` was NOT refused (fixed in 0.4.3: next section). Before the policy the route answered HTTP 500 (the path has no backend, so
  nothing was exposed for the plain path): labs 27, 27B and 27C.

## What the next lab must prove

Status after labs 27B to 28C (details in "What is NOT delivered" and in the status lines above), then lab 34 (0.4.3 heads, Scaleway, 3 CP + 6 W, n=1, branch
ref): on a fresh install with the brick on the forge pod started at the first try, `/metrics` and `//metrics` were refused on the Gateway and the forge was
scraped with the token (`up` = 1), and a forge moved from 0.4.2 kept its scrape (forge away 50.3 s, scrape gap 65 s). Items 1 to 3 and the first half of 4 were seen,
and item 5 was run: hard reboots and partitions in lab 28, graceful zone power-offs in labs 28B and 28C, the `core.fsync` cost once in lab 27B, `dirsync`
on the mount in lab 27D. The list below stays the acceptance list for the rest: the pair restore of 4, 6 and 7, the cost of `dirsync` alone and the lock
of a REAL interrupted push in 5.

0. **The lab enables the brick explicitly** (`apps/flux/management-forge`, or the instance profile): a default install does not carry it,
   so a lab that does not turn it on proves nothing about it. Also there: the default install still renders and converges without it.
1. A **fresh install converges** with the brick: `forgejo-db` then `forgejo` Ready, the four OpenBao paths exist (`forgejo/metrics` since 0.4.3) and `forgejo-admin` has
   both keys, `cluster-verify` green, the flux-errors Alert quiet. The Flux source controller pulls the chart from `code.forgejo.org`
   under the restricted egress policy (the registry's token request and blob download on 443, no redirect to another host).
2. **`forgejo-db` synchronous replication and archiving**: `cluster-verify` shows the same lines as for `grafana-db` (sync replicas,
   `ContinuousArchiving`, a first base backup right after the database is healthy: `immediate: true`, not 03:40), `cnpg-store-replicate` copies its archive to the replica store.
3. A **repository pushed through the forge** over HTTPS at `forge.openaether.local`: admin login, forced password change, create a
   repository, `git push`, `git clone`; anonymous access refused, registration refused, `/metrics` and `//metrics` refused on the Gateway, and scraped by vmagent
   with the token (`up` = 1; a forge upgraded from 0.4.2 keeps its scrape: "The metrics endpoint and its token").
4. A **Longhorn backup of the repositories' volume** by the default job (a volume created from `longhorn-x2-rwo-forgejo` carries
   `oa-forgejo` at once, with no manual label; `forgejo-repos-backup-daily` runs at 03:55 and a backup appears at the target), restored into a scratch volume and the repository cloned
   from it; then the **pair**: the database and the volume restored from their different instants, and what disagrees.
5. **Node and zone loss drills with a Forgejo push writer, with `core.fsync = all`, `dirsync` and the lock cleaner**: the SAME drill as lab 27 (the forge pod and the writable
   replica of its volume in a zone that is powered off): **acknowledged against present** (no acknowledged push lost; the one lost in 59 is the case to beat, so repeat it), `git fsck` clean, a fresh clone works, the next push to a branch interrupted by the cut goes through (cleaner log line); `dirsync` listed in the node's `/proc/mounts`; the push latency
   with and without fsync and `dirsync`; **how long the forge is away with the SHIPPED Longhorn policy** (128 s measured once with the setting applied by hand; 341-372 s without it; 85 to 178 s with the shipped one in labs 28 and 28B), on a power-off AND on a partition; `forgejo-db` with a writer like `zitadel-db`
   in lab 23. If an acknowledged push is still lost, the forge stays optional and is declared NOT lossless on abrupt power loss (owner, 2026-10-27).
6. The **dogfooding V1 acceptance of #170**: OpenAether's own source hosted on the forge and used from the forge.
7. Whether Forgejo starts with `readOnlyRootFilesystem: true`, and what the cluster says about the 3 + 1 pods on the 4-worker examples
   (`cluster-roll`'s survey: the derived 233-238% above, with the brick on).
