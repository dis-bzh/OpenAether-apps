# Surviving a database failover from your own application

A PostgreSQL primary of this platform (CloudNativePG, one writer, two synchronous replicas) can change under a running application: a node loss, a pod kill, a planned switchover or an upgrade. The platform makes the change **safe** (no acknowledged commit is lost, see `resilience.md`, "Data classes") and **short** (measured write gaps in lab 22: 12 s planned, 44-64 s for a powered-off worker, 122 s in one zone-loss case). It cannot make it invisible: for that time, writes fail or wait. Your application hides the gap, or shows it as a failure.

Rung: the settings are static-checked in CI for Zitadel and Grafana; the behaviour of your own client is yours to test (the lab criteria of `resilience.md` give a writer you can copy).

## Connect to the service, never to a pod

Use `<cluster>-rw.foundation-databases.svc` (the read-write Service follows the primary). A pod name or IP stops being the primary after a failover.

## What your client must do

1. **Retry, with a bound.** A failed connection or a statement that fails with a connection error (`08xxx`, `57P01` admin shutdown, `57P03` cannot connect now, a reset or timed-out socket, a read-only error `25006` during a promotion) is retried with backoff and jitter (for example 0.2 s doubling to 5 s) for a total a little longer than the expected gap (90 s for a node loss, 150 s to be safe). After that, fail loudly.
2. **Retry only what is safe to repeat.** A reconnect-and-retry of a read is always safe. A write is safe when it is idempotent: give each logical write an idempotency key (a unique constraint, `INSERT ... ON CONFLICT DO NOTHING`, an upsert, or a client-generated id) so that repeating it after an unknown outcome cannot apply it twice. The unknown outcome is real: a commit acknowledged just before the crash is on the new primary (that is the guarantee), but your client may never have seen the acknowledgement.
3. **Keep transactions short and retry them whole.** Never retry half a transaction on a new connection: on a connection error, roll back the whole unit of work and run it again from the start. No transaction held open across a user interaction or a remote call; no long-running batch in a single transaction (it dies with the connection and restarts from zero).
4. **Recycle pooled connections.** Set a maximum connection lifetime in minutes (not hours) and an idle timeout, validate a connection on checkout (or rely on the pool dropping one that errors), and bound the pool size (`max_connections` is 200 per Cluster, shared by every client). After a failover the pool must throw dead connections away, not keep handing them out. Zitadel is set to recycle in 5 minutes and Grafana in 300 s; copy the order of magnitude.
5. **Set timeouts everywhere.** A connect timeout (5 s), a statement timeout (`statement_timeout`) and a client-side socket/read timeout. A powered-off node sends no reset: without a socket timeout a connection hangs for the kernel's TCP retransmission timeout (minutes). TCP keepalives (`keepalives_idle`, `keepalives_interval`, `keepalives_count` in libpq-style drivers) bound it too.
6. **Expect blocked writes, not only errors.** With synchronous replication and no replica available, a commit waits instead of failing (alert `CnpgSyncReplicaMissing`). A statement timeout turns that wait into an error you can retry; without one the call hangs.
7. **Fresh connection per probe.** Health checks must open a new connection, or they report a dead pooled one as healthy.

## What not to do

- Do not catch the error and carry on as if the write had happened.
- Do not retry non-idempotent writes blindly (double charge, double e-mail).
- Do not disable synchronous replication to make writes "faster" or "unblock" them: that is the one way to lose acknowledged commits (`resilience.md`).
- Do not point a pooler's server connections at a pod; a connection pooler in front of the Service helps only if it also reconnects and retries. The `ha` database profile ships one (PgBouncer, transaction mode, `<name>-pooler-rw`): it hides the reconnect, not the transaction in flight, and has limits of its own (`database-profiles.md`).

## Checking your own client

Run the writer of `resilience.md` ("Zero-loss proof"; the script is `scripts/ops/db-zeroloss.sh`, checker included) against your Cluster, or your application's own workload, while you delete the primary pod (`kubectl cnpg promote` for a planned switchover, `kubectl delete pod <primary> --grace-period=0 --force` for a crash). Pass: no error reaches the user beyond the gap, no duplicate row, no lost row.
