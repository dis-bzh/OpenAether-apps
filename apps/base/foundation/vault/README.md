# OpenBao (foundation-vault) — architecture and runbook

`apps/base/foundation/vault/`. One OpenBao per cluster: 3 raft replicas, TLS from cert-manager, **static auto-unseal**, an
audit trail, **no root token and no unseal key kept in the cluster**. It stays out of the Istio mesh (Cilium network
policies at L3/L4 + OpenBao's own authentication).

## How it fits together

```
 StatefulSet openbao (3 replicas, raft, local-path-retain)  + sidecar cert-reload (SIGHUP on a renewed certificate)
   listeners      :8200 API (TLS 1.3 only)   :8202 metrics (tokenless, vmagent only)   :8201 raft
   seal "static"  ← Secret openbao-seal-key (32 random bytes, mounted read-only)   → restarts unseal themselves
   audit "file"   → stdout → Alloy → Loki (values are HMAC-ed by OpenBao)          → declared in the config, not via the API
 Job openbao-init       one shot; the ONLY holder of the root token, for a few seconds
 Job openbao-vault-bootstrap   idempotent; runs as the Kubernetes-auth identity `openbao-bootstrap` (15 min tokens)
 ConfigMap openbao-bootstrap-scripts   scripts/{lib,init,configure}.sh + scripts/policies/*.hcl (the single source of truth)
```

**Bootstrap, in order** (every step is idempotent, a re-run on a configured cluster changes nothing):

1. `init.sh` creates the Secret `openbao-seal-key` **once** (`kubectl create`, never `apply`: an existing key is never overwritten)
   and the pods start.
2. `sys/init` with *recovery* shares. The root token lives in a file on a memory-backed volume. With it: the bootstrap and
   break-glass policies, the Kubernetes auth method (no CA pinned: it uses the OpenBao pod's own, so a snapshot restored onto a
   rebuilt cluster still logs in), the roles `openbao-bootstrap` and `openbao-breakglass`. It then checks that the bootstrap role can
   log in (from the Job's own ServiceAccount) **and revokes the root token**, also when anything in the window fails or the Job is killed
   (a token left by a killed run is revoked at the next start). Before `sys/init` it refuses what would leave a half-initialised cluster:
   a bad threshold, a key that is not base64, a leftover shares Secret. From here on nothing in the cluster holds a root token.
3. `configure.sh` logs in as `openbao-bootstrap` and creates the engines (`pki`, KV v2 `secret/`), the 8 policies, the
   Kubernetes-auth roles and the application secrets (random, **create-only**: `cas=0`, so a re-run can not rotate one).
   Nothing secret is ever printed: pod logs are shipped to Loki and kept.

| Kubernetes-auth role | ServiceAccount (ns) | Policy | Can |
|---|---|---|---|
| `openbao-bootstrap` | `openbao-vault-bootstrap`, `openbao-init` | `openaether-bootstrap` | configure engines/auth/policies/secrets. **Root-equivalent in practice** (it writes policies and roles, so it can grant itself anything); the control is who may run as those ServiceAccounts |
| `external-secrets-operator` | `external-secrets` (foundation-pki-management) | `external-secrets-read` | read `secret/` |
| `cert-manager-issuer` | `cert-manager` | `cert-manager-pki` | sign with `pki/sign/*` |
| `backup-snapshot` | `openbao-snapshot` | `backup-snapshot` | read the raft snapshot |
| `secret-seeder` | `openbao-seeder` | `secret-seeder` | **create** the Day-1 secrets listed in the policy (and `audit/sink`, `audit/sink-secondary` for the opt-in audit sink); never overwrite, never read a value |
| `openaether-admin` | `openaether-admin` | `openaether-admin` | read/write secrets (except the stored PKI CSR, read only), import the signed intermediate, issue from the `openaether` PKI role, leases; read the configuration. **Not** write policies/roles/PKI roles/mounts (those change through git), mint tokens or touch the seal |
| `openaether-reader` | `openaether-reader` | `openaether-reader` | read secrets |
| `openbao-breakglass` | `openbao-breakglass` | `openaether-breakglass` | start a root-token generation (still needs the recovery quorum) |

No Pod runs as `openbao-seeder`, `openaether-admin`, `openaether-reader` or `openbao-breakglass`: a cluster administrator mints a
short token with `kubectl -n foundation-vault create token <sa> --duration=15m` when one is needed.

## Production or development

| | Production | Development |
|---|---|---|
| Selected by | the ConfigMap `openbao-operator-keys` exists | it does not |
| Recovery shares | N, threshold T, each **encrypted to one operator's PGP key** → Secret `openbao-recovery-shares` | 1 plaintext share → Secret `openbao-recovery-dev` (labelled `openaether.io/dev-bootstrap=true`) |

The ConfigMap holds `pgp-1 … pgp-N` (each an operator's *public* key, `gpg --export <id> | base64 -w0`; N ≥ 3) and optionally
`threshold` (default ⌊N/2⌋+1, at least 2). Create it **before** the first reconcile of this brick (`OpenAether-infra` does,
from `openbao_recovery_pgp_keys`). The shares in the Secret are useless without the operators' private keys, so they can be copied
anywhere. A development cluster works and says it is not production; do not put anything that matters on one, and know that
**it can not be turned into a production one**: re-initialise it. Check every key before you paste it: `gpg --list-keys <id>` must
show an `[E]` (a signing-only or expired key makes `sys/init` fail *after* it has initialised the cluster: re-init).

## Day 0 — what an operator must do once (a production cluster)

The cluster can not do these for you, and **losing the seal key loses every secret**:

```bash
# 1. Escrow the seal key off the cluster, with at least TWO custodians (one gpg recipient is one person's laptop).
#    Choose by the goal. Several recipients (any ONE of them can decrypt: survives losing a person, not one leak):
kubectl -n foundation-vault get secret openbao-seal-key -o jsonpath='{.data.key}' | base64 -d \
  | gpg --encrypt --recipient <ops-key-a> --recipient <ops-key-b> --output openbao-seal-key.gpg
#    or a k-of-n split (no single custodian can open it: survives one leak), each share encrypted to a different person and kept
#    in a different place; `ssss-split -t 2 -n 3 -x` or `gfsplit -n 2 -m 3` (neither tried on this platform). ssss works on
#    hex text: feed it `... | base64 -d | xxd -p -c 64` (not the raw bytes) and see step 5 for the way back.
# 2. Keep a copy of the (already PGP-encrypted) recovery shares: the Secret dies with the cluster.
for i in 1 2 3; do kubectl -n foundation-vault get secret openbao-recovery-shares -o jsonpath="{.data.share-$i}" | base64 -d > openbao-share-$i.b64; done
# 3. Escrow the restic password: without it the backups are unreadable the day OpenBao is lost.
bao_admin kv get -mount=secret -field=password backup/restic          # helper below
# 4. A NEW cluster only: read Zitadel's first-admin password once, to log in and enrol (docs/zitadel-hardening.md).
bao_admin kv get -mount=secret -field=password zitadel/admin
```

**5. Prove the escrow, then attest it.** An escrow nobody has opened is a hope. Reconstruct the key from the escrow alone (a
custodian decrypts, or k of n shares are combined) into `key.bin`, and compare its hash with the live Secret's; only then say
so to the cluster. `key.bin` must hold the RAW 32 bytes, as `gpg --decrypt` writes them: from `ssss-combine -x` (hex text) first run
`xxd -r -p < hex.txt > key.bin`; `gfsplit`'s `gfcombine` already yields raw bytes. A hex `key.bin` reports MISMATCH below, which means
"decode it", not "the escrow is wrong" (the ssss path is untried, like the split itself).

```bash
LIVE="$(kubectl -n foundation-vault get secret openbao-seal-key -o jsonpath='{.data.key}' | base64 -d | sha256sum | cut -d' ' -f1)"
if [ "$(sha256sum < key.bin | cut -d' ' -f1)" = "$LIVE" ]; then
  rm -f key.bin
  kubectl -n foundation-vault create configmap openbao-escrow-ack --from-literal=escrowed-at="$(date -u +%F)" \
    --from-literal=seal-key-sha256="$LIVE" --from-literal=holders=<number of independent custodians> \
    --dry-run=client -o yaml | kubectl apply -f -
  # The same fact on the Secret itself, for OpenAether-infra's `cluster-security` check 4.7 (opt-in there, it reads this annotation).
  kubectl -n foundation-vault annotate secret openbao-seal-key openaether.io/escrowed-at="$(date -u +%FT%TZ)" --overwrite
else
  echo "MISMATCH: the escrow does not open the live key; nothing attested"
fi
```

The ConfigMap is not managed by Flux (nothing prunes it) and holds no secret (the hash of a random 32-byte key). The CronJob
`openbao-escrow-check` reads it daily on a production cluster: it fails when the ConfigMap is absent a week after initialisation,
malformed, names fewer than two holders, or is older than 400 days, and the alert `OpenBaoEscrowNotAttested` follows (warning).
It never reads the Secret that holds the key, so it can not tell that the escrow is of an OLD key: the rotation runbook deletes
the ConfigMap, and you **redo steps 1 and 5 after every seal-key rotation**. Do this in a RAM-backed directory (`mount -t tmpfs`, see the root-CA runbook, section 7): `rm` erases nothing on an SSD.

Operator access (`bao_admin`): a token for the ServiceAccount is exchanged for an OpenBao token **inside the pod**, so no
OpenBao token ever crosses your terminal:

```bash
bao_admin() {
  kubectl -n foundation-vault create token openaether-admin --duration=15m \
  | kubectl -n foundation-vault exec -i openbao-0 -- sh -c \
      'BAO_TOKEN="$(bao write -field=token auth/kubernetes/login role=openaether-admin jwt=-)" exec bao "$@"' sh "$@"
}
```

## Runbooks

**PKI intermediate.** The first `openbao-vault-bootstrap` run generates the intermediate's CSR once, prints it (public data) and stores it
in KV at `secret/pki/intermediate-csr` (create only), because the Job and its log are removed 3600 s after it ends and the offline signing
takes longer. The admin identity can read it but not replace, delete or destroy it (the rule is a path prefix, so a trailing slash does not get around it). It is the CSR of the FIRST generation only: for a renewal, or whenever its subject or public key is not what you expect, generate a new one (below) and treat the entry as stale; the admin identity cannot clear a wrong entry (the bootstrap identity, which the Jobs run as, and a break-glass root token can). Read it with `bao_admin kv get -field=csr secret/pki/intermediate-csr`. A cluster whose CSR was generated by 0.1.0 has no such
entry (the CSR can not be read back from the pending key): `bao_admin write -field=csr pki/intermediate/generate/internal common_name="OpenAether Workload Intermediate CA"`
makes a new one, and leaves the first key pending too. Sign it offline with the root ([`pki-root-offline-runbook.md`](./pki-root-offline-runbook.md)), then import it,
then re-trigger the gateway Certificate (`cmctl renew openaether-tls -n services-gateway`; it asked before the import and waits in a backoff of hours). The intermediate's
validity must exceed the gateway leaf's `duration` (90 days) or OpenBao refuses the leaf (runbook, section 3).
**PKI keys, role and revocation** (ANSSI IGC-BP-03, NT35-R8, R27-R29, R33; not a claim of compliance). `configure.sh` sets, on every run: the role
`openaether` (server certificates only, ECDSA P-256 keys only, `DigitalSignature` only, no wildcard, bare domain, IP SAN or
required common name; the names are checked against the CSR's SANs); the AIA, CRL and OCSP URLs of the in-cluster service
(`https://openbao.foundation-vault.svc.cluster.local:8200/v1/pki/{ca,crl,ocsp}`, trusted through `openbao-ca`); a CRL rebuilt
automatically (72 h validity, delta CRLs); and auto-tidy every 12 h (expired certificates, revoked ones after 72 h).
The gateway Certificate lists the four hostnames of the HTTPRoutes (zitadel, grafana, vault, longhorn) and re-keys at every
renewal: **a new route hostname needs its name added to `certificate-openaether-tls.yaml`**, the role refuses a wildcard.
Variables of the bootstrap Job: `OA_PKI_KEY_TYPE=ec` (default; a new intermediate is ECDSA P-384) or `rsa` (the legacy variant:
RSA-3072 intermediate and leaves with PSS signatures; set the Certificate's `privateKey` to `algorithm: RSA, size: 3072` in the
SAME change, the role rejects a key of another type and cert-manager retries until they agree);
`OA_PKI_DEFAULT_FOLLOWS_LATEST=false` (default; see the renewal runbook). An existing cluster keeps its RSA intermediate until it
is regenerated, and its RSA gateway key is replaced at the next sync. **Revocation is informative only**: no client checks it
(runbook, section 6). The Istio plug-in CA (the mesh root is still istiod's own self-signed RSA-2048 one) is deferred to the Istio upgrade.

Every key of the platform, its period and how to replace it: [`docs/key-inventory.md`](../../../../docs/key-inventory.md). Operating an incident:
`OpenAether-infra/docs/incident-response.md`.

Until then the `openbao` ClusterIssuer stays NotReady, by design.

**Break-glass root token** (an emergency only; the quorum of recovery-share holders is the control). Each holder decrypts their
share on their own machine: `kubectl -n foundation-vault get secret openbao-recovery-shares -o jsonpath='{.data.share-1}' |
base64 -d | base64 -d | gpg --decrypt` (try each `share-N`: only yours decrypts) prints the share as hex. Then, in the pod:

```bash
kubectl -n foundation-vault exec -it openbao-0 -- sh
export BAO_TOKEN="$(bao write -field=token auth/kubernetes/login role=openbao-breakglass jwt=-)"   # paste `kubectl … create token openbao-breakglass --duration=10m`, Ctrl-D
bao operator generate-root -cancel        # only if an earlier attempt was left half-done
OTP="$(bao operator generate-root -generate-otp)"; bao operator generate-root -init -otp="$OTP"   # prints the nonce
bao operator generate-root -nonce=<nonce>     # each holder in turn, pasting their share at the hidden prompt; the last call prints the encoded token
bao operator generate-root -decode="<encoded token>" -otp="$OTP"     # the root token. Do the work, then: bao token revoke -self
```

(Measured on a cluster: 2 of 3 PGP-decrypted shares, through the `openbao-breakglass` role, produced a root token.)

**Seal key rotation.** The new key becomes `key` and the old one `previous-key`; roll the StatefulSet (one pod at a time, each Ready and
unsealed before the next); drop `previous-key`; roll again. Then redo the Day-0 escrow of the seal key (steps 1 and 5; the commands above already removed both records, so the checks go red until you do). The entrypoint computes the key ids.

```bash
OLD="$(kubectl -n foundation-vault get secret openbao-seal-key -o jsonpath='{.data.key}')"; NEW="$(head -c 32 /dev/urandom | base64 -w0)"
kubectl -n foundation-vault patch secret openbao-seal-key --type merge -p "{\"data\":{\"key\":\"$NEW\",\"previous-key\":\"$OLD\"}}"
# The escrow is of the OLD key now: drop both records of it, so that the daily check and infra's check 4.7 ask for a new one.
kubectl -n foundation-vault annotate secret openbao-seal-key openaether.io/escrowed-at-
kubectl -n foundation-vault delete configmap openbao-escrow-ack --ignore-not-found
kubectl -n foundation-vault rollout restart statefulset/openbao && kubectl -n foundation-vault rollout status statefulset/openbao --timeout=600s
kubectl -n foundation-vault patch secret openbao-seal-key --type json -p '[{"op":"remove","path":"/data/previous-key"}]'
kubectl -n foundation-vault rollout restart statefulset/openbao && kubectl -n foundation-vault rollout status statefulset/openbao --timeout=600s
```

(Measured on a 3-replica cluster, 2026-10-13, twice: about 70 s per roll, every pod unsealed with the new key, the data stayed readable.)

**Disaster recovery.** The raft snapshot (backup brick) is encrypted by the seal key, so a restore needs the **escrowed key**, and
the recovery shares that apply afterwards are the SOURCE cluster's:

1. Once the namespace exists and before the vault pods start, create `openbao-seal-key` from the escrow
   (`gpg -d openbao-seal-key.gpg > key`, or combine k shares into `key`, then `kubectl -n foundation-vault create secret generic openbao-seal-key --from-file=key=key`, then
   delete `key`). Suspend the Flux Kustomizations that consume secrets until step 4, so that nothing picks up the throw-away values the
   fresh bootstrap generates. The init Job refuses to invent a key on a cluster that was initialised before.
2. Let `foundation-vault` deploy: `init.sh` initialises an empty cluster.
3. Restore with a break-glass root token of the fresh cluster (its shares are encrypted to the same operators):
   `bao operator raft snapshot restore -force <file>`. The Kubernetes auth config in the snapshot pins no CA, so logins keep working on a
   rebuilt cluster. If standbys do not follow the restored leader, delete `openbao-1` and `openbao-2` (the PVCs stay).
4. Put the source's shares back (delete the fresh `openbao-recovery-shares`, then
   `kubectl create secret generic openbao-recovery-shares --from-file=share-1=openbao-share-1.b64 …`), then resume Flux.

With any other key the data does not come back (tested on a real OpenBao). Not drilled on a cluster.

**Interrupted first run.** A failure inside the root window (between `sys/init` and the hand-over) revokes the root token on the
way out, whatever the cause, and leaves an initialised OpenBao that nobody can configure (the root token is gone by design; the
calls in the window are retried, and the hand-over is proven from the init Job's own identity before the token goes). On a cluster
that holds nothing yet, start over (measured on a cluster, twice):

```bash
kubectl -n flux-system patch kustomization foundation-vault --type merge -p '{"spec":{"suspend":true}}'
kubectl -n foundation-vault delete job openbao-init openbao-vault-bootstrap
kubectl -n foundation-vault scale statefulset openbao --replicas=0   # wait until the pods are gone
kubectl -n foundation-vault delete pvc data-openbao-0 data-openbao-1 data-openbao-2
kubectl -n foundation-vault delete secret openbao-recovery-shares     # or openbao-recovery-dev; keep openbao-seal-key
kubectl -n flux-system patch kustomization foundation-vault --type merge -p '{"spec":{"suspend":false}}'
```

If the cluster already holds data, use the break-glass instead (its role and policy exist from the first seconds of the root window;
a failure *before* the window ends has nothing to fall back on).

**A re-init regenerates every generated secret** (database passwords, Longhorn's passphrase, restic's password…). Anything that already
used the previous values (a database initialised with them, a volume encrypted with the passphrase) no longer matches: do it only on a
cluster that holds nothing yet, as above.

## Alerts and network

`OpenBaoBootstrapJobFailed` (critical, `observability/vm-customresources/vmrule-bootstrap.yaml`): `openbao-init` or
`openbao-vault-bootstrap` ended Failed for 15 minutes. Both Jobs retry in place (eight retries, no deadline: the waits are bounded by the scripts, 0.3.1); only after those
retries does the Job end Failed and come back about 70 minutes later by itself. The alert covers a Job that fails on a cluster whose observability is already running (a re-run, an upgrade): observed firing
and clearing on a real cluster. It can NOT show a failed first install, because the VMRule is deployed by `observability`, which waits for
OpenBao-fed secrets; after an interruption inside the root window, `openbao-init` fails again (see above) and you see it with
`kubectl -n foundation-vault get jobs` and the dependents staying NotReady. The OpenBao alerts (`OpenBaoWorkloadSealed`,
`OpenBaoNoActiveNode`, `OpenBaoRaftNoFailureTolerance`, `OpenBaoRaftApplyLag`, `OpenBaoMetricsDown`) and the scrape are in
`apps/base/observability-openbao` (see "Metrics, TLS and certificate renewal" below); they are mocked/static, not yet fired on a
cluster. `networkpolicy.yaml`: the OpenBao policy admits ESO, cert-manager, the snapshot job, vmagent (port 8202 only), the raft
peers and the two Jobs; each Job has an egress policy of its own (DNS and OpenBao; only `openbao-init` also reaches the
kube-apiserver, to create the Secrets).

## Metrics, TLS and certificate renewal

- **TLS 1.3 only** on the API (8200) and the metrics (8202) listeners (ANSSI NT-35 R10), with the key exchanges
  `X25519MLKEM768, CurveP384, CurveP256, X25519` (a client that offers none of them is refused; P-521 is out). TLS 1.3 has a fixed
  cipher set, so no `tls_cipher_suites`. The clients known here (cert-manager, ESO, the `bao` CLI and the Jobs' curl, kubelet's
  probes, the Gateway's Envoy) speak 1.3; **proven against a real OpenBao with openssl and curl, not against those clients on a cluster.**
  If one turns out not to, `OA_TLS_MIN_VERSION=tls12` on the container (a Flux `spec.patches` entry on the StatefulSet) puts the
  old floor back at the next pod restart; an unknown value stops the entrypoint.
- **Metrics.** `/v1/sys/metrics` needs a token on 8200. A second listener on 8202 serves it without one (`unauthenticated_metrics_access`):
  8202 is a port of the headless Service only, so neither `openbao-ui` nor the Gateway route reaches it, and the OpenBao network policy admits
  vmagent alone there. The scrape (`VMServiceScrape` in this namespace) verifies the server certificate against the `ca.crt` of
  `openbao-server-tls`. The listener is otherwise a full API listener (health, seal-status, login stay as on 8200), and its cluster
  port is bound to the loopback. The metric prefix is `vault_`; `prometheus_retention_time = 1m` so that a series with an old
  label set does not outlive an unseal.
- **Certificate renewal.** `openbao-server-tls` renews at one third of its life (2920 h of 8760 h) with a **new key each time**
  (`rotationPolicy: Always`, ANSSI NT-35 R25, IGC BP-08). OpenBao reads the pair at start and on SIGHUP only, so the
  `cert-reload` sidecar (same image, same uid, pod `shareProcessNamespace`) hashes the two files every 30 s and sends SIGHUP to
  `bao` when they change; the pod keeps serving, unsealed, with the new pair. On a cluster that already has the Secret, the
  new `renewBefore` may renew it at the first reconcile: that is the first test of the path.
  `openbao-ca` (10 years) keeps its key, and so does its renewal: re-keying it means issuing a new CA, then re-issuing every leaf in the
  same step (the pods trust `ca.crt` of their own Secret); not drilled, to plan for year nine.
- **Logs.** `log_format = json`, `log_level = info`. The audit trail and the server log share stdout; Alloy adds the Loki label
  `log_type="audit"` to the former (`{namespace="foundation-vault", log_type="audit"}`), by the first keys of the line. The levels
  of the other components are in `docs/detection.md`.

The configure Job runs about every 70 minutes (ttl 3600 s plus one sync), not once: each run rewrites the policies and the Kubernetes-auth
roles from git, so a manual edit of either is reverted within about 70 minutes. Change the configuration in git, never by hand (a drill
that needs a temporary grant should expect it to disappear).

## Placement and replacing a worker

The three replicas keep their raft data on `local-path-retain` volumes, each pinned to the node where its pod first ran. The StatefulSet
therefore asks for one replica per worker (hostname spread, `DoNotSchedule`, `maxSkew: 1`; the control planes are not used): 3 workers hold
1+1+1, 2 workers hold 2+1, one worker holds all three (a single-worker cluster is accepted, it has no node-loss tolerance). A worker that
is registered but not Ready yet counts, so a pod waits for it. A node that has not registered at all is invisible to the scheduler: after
the first install, and before touching a worker, check that the pods sit on at least two nodes (one worker is enough only on a one-worker cluster):

```bash
kubectl -n foundation-vault get pods -l app=openbao -o wide      # the NODE column: not all the same
```

A cluster installed before 0.3.1 whose three pods already share a worker is NOT moved by the new constraint (the volumes are pinned).
To spread it, one replica at a time, only when `bao operator raft list-peers` shows three voters and every pod is Ready: cordon nothing,
delete the standby's PVC and then its pod (`kubectl -n foundation-vault delete pvc data-openbao-N --wait=false` then `delete pod openbao-N`),
wait for the new pod to join (`retry_join`) and for the three voters again, and release the old PersistentVolume (Retain keeps it and its
data on the old node: wipe it before reuse). Never do two replicas at once, and never the active node first. This procedure was written from
the design, not run on a cluster. Replacing a worker that holds ONE replica is safe once the other two are Ready (the PodDisruptionBudget
keeps two); one that holds several is not: spread first.

## Limits, stated plainly

- **The static key sits next to the data it protects**: a Secret in the cluster (etcd) and the raft PVCs on the node, and every
  snapshot in the backup store is barrier-encrypted by this key alone (its restic password is another Kubernetes-held secret). It
  removes what the former design leaked (unseal shares and root token in logs and in plain Secrets) and gives hands-free restarts; it does
  **not** separate the key from the cluster. Whoever can read Secrets in `foundation-vault` can open the data; so can whoever reads a
  backup and that Secret. Closing this means a key held outside the cluster (a transit seal on another OpenBao, or a KMS); until then
  RBAC on this namespace is part of the security boundary, and "encrypted at rest" depends on the provider's etcd encryption, which this
  repository does not verify.
- **The bootstrap identity is root-equivalent**, and so is the init Job (it can create Secrets, including service-account-token Secrets
  for any ServiceAccount of the namespace). Both are reachable by whoever can create Pods in `foundation-vault`, exec into them, or change
  what Flux applies, i.e. Git write access. That group is the real root of trust.
- **The break-glass depends on Kubernetes auth working** (and on the recovery quorum). A first run that fails before the root window ends,
  a PGP key that `sys/init` rejects, or a bad hand-over end in a re-init, only possible on a cluster that holds nothing yet.
- A development bootstrap can not be upgraded to production. The escrow is monitored only as far as an operator's attestation
  goes (Day 0, step 5): the cluster can not check that the escrowed copy opens the CURRENT key, and the restore from it is not drilled.
- The Kubernetes-auth login is only exercised on a cluster; the harness replaces that one call. The 3-replica join, the seal-key
  rotation and the start-over were run on a cluster; the restore with standbys and the other runbooks were not.
- **Recorded deviations** (ANSSI TLS-12): the seal key is a software key, not an HSM or an external KMS, and the static seal wraps the
  barrier with AES-GCM (upstream's implementation) rather than a key-wrap mode; `vault-ca`'s `openbao-ca` is separate from the OpenBao
  PKI because OpenBao needs a certificate to start; the Istio CA is istiod's own. The way out for the first is a `transit` seal against an
  OpenBao or KMS outside the cluster: set the old seal `disabled = "true"` next to the new one and run `bao operator unseal -migrate`
  on each pod, with a fresh escrow of the new key. **Not built and not tried on this platform**; start it with a throwaway cluster
  (rolling order, egress to the external seal under the network policies, and what a cold start does when the external seal is
  down are the open questions). The root CA custody options are in the [runbook](./pki-root-offline-runbook.md).
- **Nested PKI mounts are forbidden**: never mount `pki/` and `pki/<x>/` (the second fails with HTTP 400, sub-paths 404).

## Tests

`scripts/dev/test-openbao-bootstrap.sh` runs the real entrypoint, `init.sh` and `configure.sh` against an OpenBao container (needs
Docker, gpg, jq, openssl): both modes, root revocation (witnessed by the audit trail), no secret in any output, idempotence,
create-only, what every policy allows and denies, PGP shares and the break-glass, snapshot restore with the right and a wrong key,
seal-key rotation, TLS 1.2 refused and 1.3 accepted, the metrics listener (sealed and active) and the names the alerts read, and a certificate reload by the real sidecar script. It runs in CI. On a cluster: `kubectl -n foundation-vault get pods,jobs` (3× openbao Ready, `openbao-init` and
`openbao-vault-bootstrap` Complete).
