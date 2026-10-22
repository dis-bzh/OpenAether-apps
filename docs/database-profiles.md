# Database profiles: which PostgreSQL for your application

The platform tunes its own databases (`zitadel-db`, `grafana-db`: three instances, synchronous quorum replication, `docs/resilience.md`). For **your** application it offers CNPG (CloudNativePG, PostgreSQL) as three named profiles. You pick one per application; each states what it guarantees, what it does not, and which numbers were measured. A profile is a promise limited to what was measured: a row marked `UNMEASURED` below is **experimental** until the lab fills it (apps #157). Nothing here is a compliance claim.

Rung of this page: manifests, render and policy tests pass offline (`scripts/dev/test-cnpg-profiles.py`, `test-db-bench.sh`); **no profile has run on a cluster yet**. Every behaviour described as "intended" or `UNVERIFIED` is the lab's to confirm.

## Choose

| | `light` | `standard` | `ha` |
|---|---|---|---|
| For | development, tests, data you can lose | data that matters, a short bounded loss is acceptable | data that must not lose an acknowledged commit |
| Instances | 1 | 2 | 3 |
| Replication | none | quorum `ANY 1`, durability **preferred** | quorum `ANY 1`, durability **required**, failover quorum |
| Pooler | no | no | PgBouncer, 2 pods, transaction mode |
| Backups | none | WAL + daily base backup, one store | WAL + daily base backup, primary store and a read-side replica store |
| Survives | a pod restart (the data stays on its volume) | loss of one node, replica present | loss of any one node; with a replica lost, writes continue |
| Does **not** survive | a node loss or drain = downtime; node-local storage: data unreachable until the node returns, lost with its disk; no backup | a primary lost while the replica is absent or lagging out of the quorum loses the commits acknowledged in that window; both disks lost | both disks holding an acknowledged commit lost at once (two nodes, or one zone holding both); both replicas lost = writes BLOCK, nothing is lost; the whole cluster lost = restore from the stores (RPO of minutes) |
| Write gap on a primary loss | outage until the node returns | expected like `ha` (UNMEASURED) | 12 s planned, 44-64 s node power-off, 122 s in one zone-loss case: measured on the platform databases (lab 22), not yet on a profile |

Not sure? `ha` for production data, `light` for everything else. `standard` exists for the middle ground and is the least certain: its loss window is real (see its row), say so to your users.

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

`cnpg-profile-common` (credentials, per-database CiliumNetworkPolicy) + the profile's Cluster + `cnpg-profile-backup` (standard, ha: ObjectStore, daily ScheduledBackup, S3 egress) + `cnpg-profile-backup-replica` (ha) + `cnpg-profile-pooler` (ha) + `cnpg-profile-values` last. Off by default: no brick, no generated profile references them (`test-cnpg-profiles.py`). Reused from the platform: restricted PSA, memory limits on every container, 30 s tolerations on a lost node (apps #154), hostname and zone preference (lab 21), `primaryUpdateMethod: switchover`, Barman plugin archiving with the checksum fix, the S3 egress FQDNs. The platform's own policies apply to the pods (`test-cnpg-profiles.py` runs them with Kyverno CLI 1.19.1 on pods built from the rendered Clusters, Pooler and Jobs: the instance pods are built the way the operator is documented to, UNVERIFIED until a cluster's policy reports are read).

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

HA of the pooler itself: 2 pods, `RollingUpdate` with `maxUnavailable 0`, a PodDisruptionBudget of one, a hostname and a zone spread (both soft), 30 s tolerations, restricted pod and container security contexts (read-only root filesystem: UNVERIFIED that the pooler image starts with it), a policy that lets in only your application and lets out only to the Cluster, DNS and the API server.

**What a client sees during a failover** (intended, UNVERIFIED): a connection that is idle keeps its pooler connection and its next statement waits for the new primary, up to `query_wait_timeout`; a transaction in flight when the primary dies fails with a connection error and the client connection is closed, so retry the whole transaction on a fresh connection (`docs/database-failover-clients.md`, unchanged). The pooler hides the reconnect storm and the DNS/IP change, not the in-flight transaction. A pooler pod rolled or lost drops its client connections: same retry.

**Limits of transaction pooling** (PgBouncer's, documented): no session state across transactions (`SET` without `LOCAL`, temporary tables, `LISTEN/NOTIFY`, session advisory locks, `WITH HOLD` cursors); SQL-level `PREPARE`/`EXECUTE` is not tracked (protocol-level is, see above); a migration tool that takes an advisory lock must use `<name>-rw` directly, which the database policy allows to the same application. TLS to the pooler: the clients' TLS on port 5432 and its certificate names are the operator's defaults; **whether `verify-full` works against `<name>-pooler-rw` is UNVERIFIED** (lab step below), `verify-ca` is the fallback, and `<name>-rw` enforces TLS in the database whatever the pooler does. No pooler metrics are scraped yet (known gap).

## Capacity

Requests of the shipped example values, per application (`python3 scripts/check-ha.py --profiles` prints and bounds the CPU; the platform's own figure is unchanged because profiles are off by default). Add your limits' headroom: a CPU limit is a burst, not a reservation.

| profile | pods | CPU requests | memory requests | storage |
|---|---|---|---|---|
| light | 1 | 100 m | 512 Mi | 5 Gi |
| standard | 2 (+ Barman sidecar each) | 2 x (250 + 50) = 600 m | 2 x (1 Gi + 128 Mi) = 2.25 Gi | 2 x 10 Gi |
| ha | 3 + 2 poolers | 3 x (250 + 50) + 2 x 100 = 1100 m | 3 x (2 Gi + 128 Mi) + 2 x 64 Mi = 6.5 Gi | 3 x 20 Gi |

The infra capacity gate counts requests: one `ha` database is about 1.1 vCPU of requests, a quarter of a 4 vCPU worker. PostgreSQL parameters are PostgreSQL's defaults except TLS and SCRAM (`shared_buffers`, `max_connections` sized from measured shapes is a lab output, not a guess).

## Numbers

Filled by the lab, one row per run, produced by `db-bench.sh` (below). Until a row exists the profile is experimental for that metric. Empty on purpose:

| profile | path | mode | scale | clients | secs | TPS | avg ms | p50 ms | p95 ms | p99 ms | failed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| light | direct | rw | 10 | 16 | 60 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| light | direct | ro | 10 | 16 | 60 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| standard | direct | rw | 10 | 16 | 60 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| standard | direct | ro | 10 | 16 | 60 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| ha | direct | rw | 10 | 16 | 60 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| ha | direct | ro | 10 | 16 | 60 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| ha | pooler | rw | 10 | 16 | 60 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |
| ha | pooler | ro | 10 | 16 | 60 | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED | UNMEASURED |

Also to record, same shape and storage: the cost of synchronous replication (`ha` against the same Cluster with `spec.postgresql.synchronous` removed: TPS ratio and p99 of `rw`), the commit latency of three instances in three zones against one zone (one client, `rw`), and the storage class (`local-path-retain` against Longhorn). Together with the provider and node shape.

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

## Security checklist

| | light | standard | ha | how it is checked |
|---|---|---|---|---|
| Network policy per database: default deny both ways, only your application (+ operator, metrics agent, the instances; the pooler in ha) | yes | yes | yes | `test-cnpg-profiles.py` (shape); lab: a pod from another namespace times out |
| TLS and SCRAM only: `hostnossl ... reject`, then `hostssl ... scram-sha-256`; `password_encryption scram-sha-256`; no `md5`, no `trust` | yes | yes | yes | `test-cnpg-profiles.py`; lab: `sslmode=disable` refused; operator-managed rules come first (UNVERIFIED that the operator keeps the rule order) |
| No superuser over the network: `enableSuperuserAccess: false` | yes | yes | yes | `test-cnpg-profiles.py` |
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

Through the pooler use `host=<name>-pooler-rw.<ns>.svc` and `sslmode=verify-ca` until the lab confirms `verify-full` works there.

**`pgaudit` decision**: not enabled by default in any profile. It logs statements, including their text, to the instance logs the platform ships to its log store: that is data about your data, volume proportional to traffic, and a setting (`pgaudit.log`) that is a per-application choice of classes. Enable it in your copy with `postgresql.shared_preload_libraries: [pgaudit]` and `postgresql.parameters.pgaudit.log`, after deciding who may read those logs and for how long; it is not measured here.

## Other database types

The platform **validates PostgreSQL through CNPG**, in the profiles above: manifests, policies, failover and loss behaviour are tested and measured as stated. Anything else you run (an in-memory store, a document database, PostgreSQL outside CNPG, a managed service) is **best effort**: the platform gives you the namespace, the policies, the network default-deny, the secrets path and the PodDisruptionBudget conventions, but does not test or measure the database, does not back it up with the Barman plugin, and does not promise its failover or durability. Choose its replication and persistence yourself, and run `db-bench.sh`-style measurements of your own before relying on it.

## Not done

Nothing here ran on a cluster. Unverified: pods Ready under every restriction (pooler read-only root filesystem); the operator's pg_hba rule order; TLS verify-full to the pooler; failover through the pooler; the numbers; PostgreSQL major 18 with the shipped parameters (the Cluster image is the one `zitadel-db` runs); the S3 egress of a customer namespace; `cnpg-store-replicate` copying a customer database (add its name to the `CLUSTERS` of that CronJob: not done, a customer database is not on its list); backup verification and restore tests of a customer database (the daily restore tests cover the platform's two Clusters only).
