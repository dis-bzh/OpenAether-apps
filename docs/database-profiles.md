# Database profiles: which PostgreSQL for your application

The platform tunes its own databases (`zitadel-db`, `grafana-db`, and `forgejo-db` when the forge is installed: three instances, synchronous quorum replication, `docs/resilience.md`). For **your** application it offers CNPG (CloudNativePG, PostgreSQL) as three named profiles. You pick one per application; each states what it guarantees, what it does not, and which numbers were measured. A profile is a promise limited to what was measured: a row that is not measured below (it says so) is **experimental** (apps #157). Nothing here is a compliance claim.

Rung of this page: manifests, render and policy tests pass offline (`scripts/dev/test-cnpg-profiles.py`, `test-db-bench.sh`), and **the three profiles ran once on a real cluster** (lab 23, 2026-10-22: Scaleway, 3 control planes + 5 workers POP2-4C-16G, volumes on `local-path-retain`, PostgreSQL 18.6): the numbers and the observed behaviour below come from that one lab. What it found wrong in the profiles was fixed on the branch (list in "What the lab found"). Everything marked `UNVERIFIED` or `UNMEASURED` was not observed; nothing here is a compliance claim, and one lab on one provider is not a support matrix.

## Choose

| | `light` | `standard` | `ha` |
|---|---|---|---|
| For | development, tests, data you can lose | data that matters, a short bounded loss is acceptable | data that must not lose an acknowledged commit |
| Instances | 1 | 2 | 3 |
| Replication | none | quorum `ANY 1`, durability **preferred** | quorum `ANY 1`, durability **required**, failover quorum |
| Pooler | no | no | PgBouncer, 2 pods, transaction mode |
| Backups | none | WAL + daily base backup, one store | WAL + daily base backup, primary store and a read-side replica store |
| Survives | a pod restart (the data stays on its volume) | loss of one node, replica present | loss of any one node; with a replica lost, writes continue |
| Does **not** survive | a node loss or drain = downtime; node-local storage: data unreachable until the node returns, lost with its disk; no backup; **a node replace destroys the data** (see "`light` and a node replace") | a primary lost while the replica is absent or lagging out of the quorum loses the commits acknowledged in that window; both disks lost | both disks holding an acknowledged commit lost at once (two nodes, or one zone holding both); both replicas lost = writes BLOCK, nothing is lost; the whole cluster lost = restore from the stores (RPO of minutes) |
| Write gap on a primary loss | outage until the node returns | like `ha` expected (UNMEASURED: only `ha` was failed over) | `ha`, measured (lab 23): 12.5-14 s planned switchover (direct and through the pooler), 28-36 s pod kill, 40-60 s node power-off, 62 s forced reboot, 112-120 s zone power-off direct and **314 s through the pooler** (see "The Pooler"); a primary and a replica lost together = no promotion until one returns (about 6 minutes in the lab) |

Not sure? `ha` for production data, `light` for everything else. `standard` exists for the middle ground and is the least certain: its loss window is real (see its row), say so to your users.

## `light` and a node replace

`light` keeps its only copy on the disk of one node (the node-local class `local-path-retain`) and has no backup, so **replacing that node destroys the data**: `task cluster-roll -- --node=<worker>` and `task cluster-replace-node` create a new machine with a new disk. Observed in lab 28B (Scaleway, 2026-10-30): the roll of the worker holding `lab27-light` finished (rc 0, 790 s), the old machine and its disk were gone, 42,583 acknowledged commits of the test writer with them, and the cleanup that follows a replace refused to touch that volume ("primary of record: its data was on the replaced disk"); lab 27B met the same refusal. This is outside the zero-acknowledged-loss rule, by the owner's decision of 2026-10-30: `light` is for data you can lose, and the rule covers `ha`, `standard`, the platform's databases and the forge.

Before a node of yours is rolled or replaced: list the CNPG pods on it (`kubectl get pods -A -l cnpg.io/cluster --field-selector spec.nodeName=<node> -o wide`) and the Clusters with `kubectl get clusters.postgresql.cnpg.io -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,INSTANCES:.spec.instances`; a pod of a Cluster with 1 instance is a `light` database whose data goes with the node. Move that application to `standard` or `ha` first (another copy then exists), or accept the loss. Today the roll and the replace do not warn about it: a survey that names such Clusters and requires an explicit yes is planned for 0.4.1 (OpenAether-infra), with no date.

## Use one

1. **Prerequisites** (the platform operator): the application's namespace is in the `conditions` of the `openbao` ClusterSecretStore (`scripts/check-externalsecret-namespaces.py` lists the namespaces; an ExternalSecret elsewhere stays NotReady); OpenBao holds `databases/<name>/credentials` (`password`) and, for `standard` and `ha`, `databases/<name>/backup-s3-primary` (and `-replica` for `ha`) with `access_key` and `secret_key`, keys scoped to the prefix `cnpg/<name>/`; the Flux Kustomization of the database has `postBuild.substituteFrom` on `cluster-identity` (the `${BACKUP_S3_*}` of the stores).
2. **Copy** `apps/components/cnpg-profile-examples/<profile>/` per application, edit the values, and point a Flux Kustomization at the copy. It references the component by a relative path: keep it inside this repository layout, or reference the component by URL.
3. **Values**: one generated ConfigMap `db-profile-values` feeds Kustomize replacements; the templates carry the upper-case placeholder `NAME`, which Kubernetes refuses as a name, so an unfilled template cannot be applied. A component ordering rule: `cnpg-profile-values` is listed last.

| value | meaning |
|---|---|
| `name` | the database; Services `<name>-rw`, `-ro`, `-r`, and `<name>-pooler-rw` (ha). Unique platform-wide (the archive path is `cnpg/<name>`), never `rt-*` |
| `namespace` | where it lives (restricted PSA, outside the ambient mesh like `foundation-databases`) |
| `database`, `owner` | the application's database and its owner role; the owner's password is the ExternalSecret |
| `size`, `storageClass` | volume per instance. `local-path-retain` is node-local; Longhorn replicates blocks under CNPG's own replication (the platform avoids stacking them: `grafana-db-cluster.yaml`) |
| `cpuRequest`, `cpuLimit`, `memory` | per instance; memory request = limit |
| `clientNamespace`, `clientApp` | who may connect: pods labelled `app.kubernetes.io/name=<clientApp>` in `<clientNamespace>` |
| `archiveServerName` | the WAL archive generation: `<name>`, then `<name>-g2` after a rebuild from a store (`apps/base/backup/README.md`) |
| `profile` | the label of the benchmark rows |

Not parameterised on purpose: `instances`, replication and pooler settings (changing them is choosing another profile). Anything else: a Kustomize patch in your copy.

## How a profile is built

`cnpg-profile-common` (credentials, per-database CiliumNetworkPolicy, and through it the shared `cnpg-bounded-shutdown` component: `smartShutdownTimeout: 5`, lab 27 FI) + the profile's Cluster + `cnpg-profile-backup` (standard, ha: ObjectStore, daily ScheduledBackup with `immediate: true` (one base backup when the object is created, so a new Cluster does not wait for 03:40; on a cluster where the object already exists the operator does not repeat it, `docs/forgejo.md`), S3 egress) + `cnpg-profile-backup-replica` (ha) + `cnpg-profile-pooler` (ha) + `cnpg-profile-values` last. Off by default: no brick, no generated profile references them (`test-cnpg-profiles.py`). Reused from the platform: restricted PSA, memory limits on every container, 30 s tolerations on a lost node (apps #154), hostname and zone preference (lab 21), `primaryUpdateMethod: switchover`, Barman plugin archiving with the checksum fix, the S3 egress FQDNs. The platform's own policies apply to the pods (`test-cnpg-profiles.py` runs them with Kyverno CLI 1.19.1 on pods built from the rendered Clusters, Pooler and Jobs: the instance pods are built the way the operator is documented to; lab 23 read the real policy reports of the three profiles: 0 failures after the pooler init container got its limits).

## The Pooler (ha)

Applications connect to `<name>-pooler-rw` (port 5432). PgBouncer 1.25.1, `pgbouncer` auth is the operator's: its own role, an auth query on the Cluster (SCRAM hashes), a client certificate to PostgreSQL over TLS.

| setting | value | why |
|---|---|---|
| `poolMode` | `transaction` | server connections are shared between transactions: many clients, few PostgreSQL backends |
| `default_pool_size` / pods | 20 x 2 | 40 server connections per (database, user), under PostgreSQL's default `max_connections` 100 |
| `max_client_conn` | 1000 | clients per pooler pod |
| `query_wait_timeout` | 150 s | a client waits through a failover (measured gaps 12-122 s) and sees a slow statement, not an error; matches the retry horizon of `docs/database-failover-clients.md` |
| `server_login_retry` | 1 s | the default 15 s would add up to 15 s to every failover |
| `server_connect_timeout`, `tcp_keepalive*` | 5 s; idle 10, interval 5, count 3 | notice a dead server connection (powered-off node, no reset) in about 25 s instead of the kernel's minutes |
| `max_prepared_statements` | 100 | protocol-level prepared statements (JDBC, pgx, psycopg3) work in transaction mode |
| `server_reset_query_always` | 0 | no reset between clients in transaction mode: see the limits |
| `client_tls_sslmode` | require | clients must use TLS (the default accepts plaintext; lab 23: `sslmode=disable` was accepted by the pooler while refused by the database) |

HA of the pooler itself: 2 pods, `RollingUpdate` with `maxUnavailable 0`, a PodDisruptionBudget of one, a hostname and a zone spread (both soft), 30 s tolerations, restricted pod and container security contexts (read-only root filesystem: verified in lab 23, the pods run with it under restricted PSA), a policy that lets in only your application and lets out only to the Cluster, DNS and the API server.

**Zone loss through the pooler (lab 23, observed twice, unexplained).** Powering off the zone that held the primary: the first time the pooler path was down for 314.5 s while the direct path was back at 120 s (pgbouncer logged `DNS lookup failed` for `<name>-rw` 3331 times; two of the three CoreDNS replicas were in the lost zone); the repeat, with one CoreDNS replica in the zone, took 60.1 s through the pooler and 79.8 s direct; losing one CoreDNS node alone cost 9.5 s. Open as apps #158. Until this is understood, count a zone loss as a possible several-minute outage on the pooler path and retry as in `docs/database-failover-clients.md`.

**What a client sees during a failover** (lab 23, `ha` through the pooler: 5 idle clients saw no error across a planned switchover and their next statement ran; the 8 pgbench clients with a transaction in flight all aborted at the switchover (`terminating connection due to administrator command`); deleting ONE of the two pooler pods aborted the clients that sat on it (2 of 5 idle sessions: `SSL SYSCALL error: EOF`, 2 of 8 pgbench clients) and cost a fresh-connection writer 0.3 s; the intended behaviour follows): a connection that is idle keeps its pooler connection and its next statement waits for the new primary, up to `query_wait_timeout`; a transaction in flight when the primary dies fails with a connection error and the client connection is closed, so retry the whole transaction on a fresh connection (`docs/database-failover-clients.md`, unchanged). The pooler hides the reconnect storm and the DNS/IP change, not the in-flight transaction. A pooler pod rolled or lost drops its client connections: same retry.

**Limits of transaction pooling** (PgBouncer's, documented): no session state across transactions (`SET` without `LOCAL`, temporary tables, `LISTEN/NOTIFY`, session advisory locks, `WITH HOLD` cursors); SQL-level `PREPARE`/`EXECUTE` is not tracked (protocol-level is, see above); a migration tool that takes an advisory lock must use `<name>-rw` directly, which the database policy allows to the same application. The pooler requires TLS from its clients (`client_tls_sslmode: require`; its default accepted `sslmode=disable`, found in lab 23). TLS to the pooler: the clients' TLS on port 5432 and its certificate names are the operator's defaults; **`verify-full` against `<name>-pooler-rw` FAILED in lab 23** (PgBouncer serves the Cluster's server certificate, which named only `-rw`, `-ro`, `-r`): the profile now adds the pooler Service names to `spec.certificates.serverAltDNSNames`, after which `verify-full` works (the pooler pods need one rollout to serve the reissued certificate on an existing database); `verify-ca` always worked, and `<name>-rw` enforces TLS in the database whatever the pooler does. No pooler metrics are scraped yet (known gap).

## Capacity

Requests of the shipped example values, per application (`python3 scripts/check-ha.py --profiles` prints and bounds the CPU; the platform's own figure is unchanged because profiles are off by default). Add your limits' headroom: a CPU limit is a burst, not a reservation.

| profile | pods | CPU requests | memory requests | storage |
|---|---|---|---|---|
| light | 1 | 100 m | 512 Mi | 5 Gi |
| standard | 2 (+ Barman sidecar each) | 2 x (250 + 50) = 600 m | 2 x (1 Gi + 128 Mi) = 2.25 Gi | 2 x 10 Gi |
| ha | 3 + 2 poolers | 3 x (250 + 50) + 2 x 100 = 1100 m | 3 x (2 Gi + 128 Mi) + 2 x 64 Mi = 6.5 Gi | 3 x 20 Gi |

The infra capacity gate counts requests: one `ha` database is about 1.1 vCPU of requests, a quarter of a 4 vCPU worker. PostgreSQL parameters are PostgreSQL's defaults except TLS and SCRAM (`shared_buffers`, `max_connections` sized from measured shapes is a lab output, not a guess).

## Numbers

Measured in lab 23 (2026-10-22) with `db-bench.sh`: Scaleway fr-par, workers POP2-4C-16G (4 vCPU, 16 GB, block-storage root disk), Cluster volumes on `local-path-retain` (the node's disk; Longhorn was **not** measured), PostgreSQL 18.6 with the profiles' parameters (shared_buffers 128 MB, max_connections 100), pgbench scale 10 (about 150 MB: over shared_buffers), 16 clients, 60 s per row, **3 runs, the median of each column**, `failed` 0 in every run. The client pod ran on a worker of the primary's zone (fr-par-1 for `ha` and `standard`; the `light` primary was in fr-par-2). The CPU **limits** of the shipped examples shape the rows: `light` is capped at 500 m and `standard` at 1 CPU, `ha` at 2.

| profile | path | mode | scale | clients | secs | TPS | avg ms | p50 ms | p95 ms | p99 ms | failed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| light | direct | rw | 10 | 16 | 60 | 643 | 24.90 | 20.88 | 44.43 | 73.34 | 0 |
| light | direct | ro | 10 | 16 | 60 | 4546 | 3.52 | 2.48 | 5.31 | 32.08 | 0 |
| standard | direct | rw | 10 | 16 | 60 | 952 | 16.81 | 13.51 | 34.24 | 61.03 | 0 |
| standard | direct | ro | 10 | 16 | 60 | 8857 | 1.81 | 0.94 | 2.37 | 39.07 | 0 |
| ha | direct | rw | 10 | 16 | 60 | 1048 | 15.27 | 12.38 | 29.71 | 49.30 | 0 |
| ha | direct | ro | 10 | 16 | 60 | 13967 | 1.15 | 0.94 | 2.04 | 3.40 | 0 |
| ha | pooler | rw | 10 | 16 | 60 | 748 | 21.39 | 17.83 | 41.69 | 67.57 | 0 |
| ha | pooler | ro | 10 | 16 | 60 | 8580 | 1.86 | 1.15 | 4.03 | 5.71 | 0 |

Same shape and storage, also measured:

- **Cost of synchronous replication** (`ha` against the same Cluster with `spec.postgresql.synchronous` removed, `rw`, 3 runs each, `pg_stat_replication.sync_state` read as `quorum` before the synchronous runs): synchronous **1046 TPS, p50 12.58 ms, p99 50.16 ms**; asynchronous **1395 TPS, p50 9.63 ms, p99 32.90 ms**. Synchronous replication costs about **25% of the write throughput** (ratio 0.75) and adds about 17 ms to p99 at 16 clients; reads do not change. One lab note: after the synchronous setting was restored in the spec, the running `synchronous_standby_names` stayed empty for about 5 minutes until `pg_reload_conf()` was called by hand (the instance manager had logged "reloading the instance" at once; cause not found, seen once, under a 16-client load): after any change of that setting, read `pg_stat_replication.sync_state` (`quorum`) before trusting it.
- **Commit latency across zones** (1 client, `rw`, 40 s, 3 to 5 runs): instances in three zones **8.07 / 8.14 / 8.15 ms (123 TPS)**; the same profile pinned to one zone (a second Cluster, `affinity.nodeSelector` zone, since deleted) **5.93 / 5.89 / 5.87 ms (169 TPS)**: waiting for a replica in another zone adds **about 2.2 ms (+37%)** to every commit. A read costs 0.55-0.62 ms either way. The `ha` Cluster of the lab had one instance in each of the three zones (the zone term is a preference: read `kubectl get pods -o wide` on yours).
- **Cost of the pooler** (`ha`, 16 clients): `rw` 748 TPS against 1048 direct (-29%), `ro` 8580 against 13967 (-39%), p99 67.6 against 49.3 ms for `rw`. The pooler pays for connection reuse at 16 clients that already reuse their connections: it is for many short-lived clients, not for speed.
- **Resources observed** (`kubectl top`): PostgreSQL 4-25 m CPU and 72-120 Mi idle; the `ha` primary 647 m and the replicas 102-119 m, 202-271 Mi, under 16 clients; the Barman sidecar 0-1 m CPU and 17-103 Mi at all times (request 50 m / 128 Mi); each PgBouncer pod 112-210 m CPU and 22-35 Mi serving that load (request 100 m / 64 Mi).
- **Storage class**: only `local-path-retain` was measured. Longhorn: UNMEASURED.

## Measure it

`scripts/ops/db-bench.sh` (also the suspended Jobs of `apps/components/cnpg-profile-bench`, example `cnpg-profile-examples/ha-bench`):

```
PGUSER=app PGPASSWORD=... PGDATABASE=scratch PGSSLMODE=verify-full PGSSLROOTCERT=ca.crt \
  scripts/ops/db-bench.sh -P ha -H -I -s 10 -c 16 -t 60 -m "rw ro" \
  -T direct=<name>-rw.<ns>.svc -T pooler=<name>-pooler-rw.<ns>.svc -o results.md
```

Rows append to `results.md` in the table format above (`-H` prints the header). Latency percentiles are exact, from pgbench's per-transaction log. `-I` creates the `pgbench_*` tables: use a scratch instance, never a database with data. `scripts/ops/db-zeroloss.sh` is the acknowledged-id writer and checker of `docs/resilience.md` ("Zero-loss proof"): `setup`, `write <run> <secs> <dir>`, `check <run> <dir>` (PASS only when no acknowledged id is missing). Tests: `test-db-bench.sh` shows it catches a lost id and survives a crash restart of a single PostgreSQL.

## Lab pass criteria

A profile moves from experimental to supported on a row only when its criterion is met on a cluster, with the provider, node shape and storage class recorded.

1. **Numbers**: the table above filled for the profile, 3 runs, median reported, scale chosen so the data exceeds `shared_buffers`; `failed` is 0.
2. **Zero loss** (`ha`): writer of `docs/resilience.md` through `<name>-rw` and again through `<name>-pooler-rw`, faults (a) pod kill, (b) worker power-off 5 min, (c) forced reboot, (d) zone power-off, (e) both replicas deleted: **0 missing acknowledged ids** in every run, no acknowledgement while both replicas are down (e), `CnpgSyncReplicaMissing` fires. `standard`: the same with the replica present (expect 0) and with the replica stopped first (record the missing count: that is its limit). `light`: record the outage length and that data returns with the node.
3. **Failover through the pooler** (`ha`): write gap through the pooler no longer than 75 s for a node loss and 20 s for a planned switchover (the platform's measured 44-64 s and 12 s, direct); clients idle during the failover see no error, in-flight transactions fail and are counted; deleting one pooler pod and draining its node under the writer: 0 missing, the budget allows exactly one.
4. **Sync cost**: `ha` against the same Cluster asynchronous: recorded (no bar), with the quorum behaviour confirmed (`pg_stat_replication.sync_state` `quorum`).
5. **Cross-zone latency**: three zones against one zone, one client `rw`: recorded; if the instances share a zone the run says so (the zone term is a preference).
6. **Security** (below): each line of the checklist observed once per profile.
7. **Policies**: the pods of each profile in a namespace with the platform's Kyverno policies: no new report; pods Ready under restricted PSA, the pooler with its read-only root filesystem.

### Status after lab 23 (2026-10-22, one cluster, Scaleway; this is not a support matrix)

| criterion | `light` | `standard` | `ha` |
|---|---|---|---|
| 1 numbers (3 runs, median) | measured | measured | measured, direct and pooler |
| 2 zero loss | not run | not run | **0 missing acknowledged ids in 7 fault runs** through `-rw` AND the pooler (pod kill, worker power-off, forced reboot, zone power-off, both replicas down x3, plus a primary and a replica lost together); see `docs/resilience.md` |
| 3 failover through the pooler | - | - | planned switchover 12.7-14.1 s (bar 20 s) met; node loss 40-62 s (bar 75 s) met; idle clients saw no error across a switchover; in-flight transactions DO fail (8 of 8 pgbench clients aborted at a switchover, 2 of 8 when one pooler pod was deleted); a deleted pooler pod cost the fresh-connection writer 0.3 s; **zone power-off through the pooler: 314.5 s once, 60.1 s on the repeat** (cause not found, see "The Pooler") |
| 4 sync cost | - | - | measured: about -25% write TPS, +17 ms p99; `sync_state` `quorum` confirmed |
| 5 cross-zone latency | - | - | measured: +2.2 ms per commit (8.1 against 5.9 ms) |
| 6 security | observed | observed | observed (and the pooler accepted plaintext until fixed) |
| 7 policies | restricted PSA, no report | restricted PSA, no report | restricted PSA, pooler read-only root filesystem, no report after the fixes |

`standard` was not failed over or loss-tested; `light` was not stopped. Both are experimental on those rows.

### What the lab found wrong in the profiles (all fixed on the branch)

The operator could not reach instances outside `foundation-databases` (its egress policy named two namespaces), so the `ha` Cluster stayed at 1 of 3 instances; `verify-full` to the pooler failed (its certificate did not name it); the bench Jobs had no egress to their database; nothing scraped a profile, so `CnpgSyncReplicaMissing` and the other database alerts could never fire for it; the pooler's init container had no limits (Kyverno `require-limits`); the pooler accepted `sslmode=disable`; and `CnpgSyncReplicaMissing` fired for good on `light`. **Not fixed, and yours to do:** an application's own network policy. A pod selected by any policy of the platform is denied the rest, so the application needs egress to its database (and pooler) on 5432; the profile only admits it on the database side.

## Security checklist

| | light | standard | ha | how it is checked |
|---|---|---|---|---|
| Network policy per database: default deny both ways, only your application (+ operator, metrics agent, the instances; the pooler in ha) | yes | yes | yes | `test-cnpg-profiles.py` (shape); lab 23: a pod of another namespace and a pod of the same namespace without the client label both time out, on all three |
| TLS and SCRAM only: `hostnossl ... reject`, then `hostssl ... scram-sha-256`; `password_encryption scram-sha-256`; no `md5`, no `trust` | yes | yes | yes | `test-cnpg-profiles.py`; lab 23: `sslmode=disable` refused on all three (`pg_hba.conf rejects connection`), `verify-full` connects with `ssl = t`; the pooler refuses plaintext since `client_tls_sslmode: require` (it accepted it before); the operator kept the rule order (the `hostnossl` reject is effective) |
| No superuser over the network: `enableSuperuserAccess: false` | yes | yes | yes | `test-cnpg-profiles.py`; lab 23: `postgres` over the network answers `password authentication failed` on all three |
| Owner's password from OpenBao (ExternalSecret), none in git | yes | yes | yes | `test-cnpg-profiles.py`; `check-externalsecret-namespaces.py` |
| Restricted PSA namespace, memory limits, pinned images, no Kyverno report | yes | yes | yes | `test-cnpg-profiles.py` (Kyverno CLI) |
| Backup keys per application, scoped to `cnpg/<name>/` | n/a | yes | yes | OpenBao policy (platform operator); not testable here |
| Backup encryption | n/a (no backup) | server-side `AES256` requested; **no client-side encryption** (apps #149) | same, on both stores | `apps/base/backup/README.md`: whoever reads the bucket reads the database; client-side encrypted copies are the restic dumps of the platform's own datasets only |
| Encryption at rest | the node disks' business, not the profile's (LUKS2 node volumes are an open item of apps #157: UNVERIFIED) | same | same | not a profile property |
| Audit logging (`pgaudit`) | not enabled | not enabled | not enabled | decision below |

**Client connection, verify-full** (the CNPG CA is the Secret `<name>-ca`, key `ca.crt`; the server certificate names `<name>-rw` and its namespace forms):

```
kubectl -n <ns> get secret <name>-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > ca.crt
psql "host=<name>-rw.<ns>.svc port=5432 dbname=<db> user=<owner> sslmode=verify-full sslrootcert=ca.crt"
```

Through the pooler use `host=<name>-pooler-rw.<ns>.svc`; `sslmode=verify-full` works there since the profile names the pooler in the server certificate (lab 23; an existing database needs one rollout of the pooler pods to serve the reissued certificate).

**`pgaudit` decision**: not enabled by default in any profile. It logs statements, including their text, to the instance logs the platform ships to its log store: that is data about your data, volume proportional to traffic, and a setting (`pgaudit.log`) that is a per-application choice of classes. Enable it in your copy with `postgresql.shared_preload_libraries: [pgaudit]` and `postgresql.parameters.pgaudit.log`, after deciding who may read those logs and for how long; it is not measured here.

## Other database types

The platform **validates PostgreSQL through CNPG**, in the profiles above: manifests, policies, failover and loss behaviour are tested and measured as stated. Anything else you run (an in-memory store, a document database, PostgreSQL outside CNPG, a managed service) is **best effort**: the platform gives you the namespace, the policies, the network default-deny, the secrets path and the PodDisruptionBudget conventions, but does not test or measure the database, does not back it up with the Barman plugin, and does not promise its failover or durability. Choose its replication and persistence yourself, and run `db-bench.sh`-style measurements of your own before relying on it.

## Not done

Lab 23 ran each profile once on one provider. Not observed (open: apps #158 for the pooler gap, apps #159 for the reload): `standard` and `light` under a node loss or a power-off; Longhorn volumes under a profile; any provider other than Scaleway; the cause of the 314 s zone-loss gap through the pooler (once; 60 s on the repeat); why `synchronous_standby_names` stayed empty for 5 minutes after the setting was restored (once, under load); the operator upgrade rolling a profile; `cnpg-store-replicate` copying a customer database (add its name to the `CLUSTERS` of that CronJob: not done, a customer database is not on its list); backup verification and restore tests of a customer database (the daily restore tests cover the platform's two Clusters only); PostgreSQL major upgrades; LUKS2 at rest (apps #157).
