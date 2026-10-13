# OpenBao (foundation-vault) — architecture and runbook

`apps/base/foundation/vault/`. One OpenBao per cluster: 3 raft replicas, TLS from cert-manager, **static auto-unseal**, an
audit trail, **no root token and no unseal key kept in the cluster**. It stays out of the Istio mesh (Cilium network
policies at L3/L4 + OpenBao's own authentication).

## How it fits together

```
 StatefulSet openbao (3 replicas, raft, local-path-retain)
   seal "static"  ← Secret openbao-seal-key (32 random bytes, mounted read-only)   → restarts unseal themselves
   audit "file"   → stdout → Alloy → Loki (values are HMAC-ed by OpenBao)          → declared in the config, not via the API
 Job openbao-init       one shot; the ONLY holder of the root token, for a few seconds
 Job openbao-vault-bootstrap   idempotent; runs as the Kubernetes-auth identity `openbao-bootstrap` (15 min tokens)
 ConfigMap openbao-bootstrap-scripts   scripts/{lib,init,configure}.sh + scripts/policies/*.hcl (the single source of truth)
```

**Bootstrap, in order** (every step is idempotent, a re-run on a configured cluster changes nothing):

1. `init.sh` creates the Secret `openbao-seal-key` **once** (`kubectl create`, never `apply`: an existing key is never overwritten)
   and the pods start.
2. `sys/init` with *recovery* shares. The root token lives in a file on a memory-backed volume. With it: the bootstrap policy,
   the Kubernetes auth method, the role `openbao-bootstrap`. It then checks that this role can log in (from the Job's own
   ServiceAccount) **and revokes the root token**, also when anything in the window fails. From here on nothing in the cluster holds a root token.
3. `configure.sh` logs in as `openbao-bootstrap` and creates the engines (`pki`, KV v2 `secret/`), the 8 policies, the
   Kubernetes-auth roles and the application secrets (random, **create-only**: `cas=0`, so a re-run can not rotate one).
   Nothing secret is ever printed: pod logs are shipped to Loki and kept.

| Kubernetes-auth role | ServiceAccount (ns) | Policy | Can |
|---|---|---|---|
| `openbao-bootstrap` | `openbao-vault-bootstrap`, `openbao-init` | `openaether-bootstrap` | configure engines/auth/policies/secrets; **not** mint tokens, seal, rekey, rotate, touch raw storage, raft or the audit device |
| `external-secrets-operator` | `external-secrets` (foundation-pki-management) | `external-secrets-read` | read `secret/` |
| `cert-manager-issuer` | `cert-manager` | `cert-manager-pki` | sign with `pki/sign/*` |
| `backup-snapshot` | `openbao-snapshot` | `backup-snapshot` | read the raft snapshot |
| `secret-seeder` | `openbao-seeder` | `secret-seeder` | **create** the Day-1 secrets listed in the policy; never overwrite, never read a value |
| `openaether-admin` | `openaether-admin` | `openaether-admin` | read/write secrets, `pki/`, leases; read the configuration. **Not** write policies/roles/mounts (those change through git), mint tokens or touch the seal |
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
anywhere. A development cluster works and says it is not production; do not put anything that matters on one.

## Day 0 — what an operator must do once (a production cluster)

The cluster can not do these for you, and **losing the seal key loses every secret**:

```bash
# 1. Escrow the seal key, encrypted, off the cluster. (It is the master key of the raft data.)
kubectl -n foundation-vault get secret openbao-seal-key -o jsonpath='{.data.key}' | base64 -d \
  | gpg --encrypt --recipient <ops-key> --output openbao-seal-key.gpg
# 2. Keep a copy of the (already PGP-encrypted) recovery shares: the Secret dies with the cluster.
kubectl -n foundation-vault get secret openbao-recovery-shares -o yaml > openbao-recovery-shares.yaml
# 3. Escrow the restic password: without it the backups are unreadable the day OpenBao is lost.
bao_admin kv get -mount=secret -field=password backup/restic          # helper below
```

Operator access (`bao_admin`): a token for the ServiceAccount is exchanged for an OpenBao token **inside the pod**, so no
OpenBao token ever crosses your terminal:

```bash
bao_admin() {
  kubectl -n foundation-vault create token openaether-admin --duration=15m \
  | kubectl -n foundation-vault exec -i openbao-0 -- sh -c \
      'BAO_TOKEN="$(bao write -field=token auth/kubernetes/login role=openaether-admin jwt="$(cat)")" exec bao "$@"' sh "$@"
}
```

## Runbooks

**PKI intermediate.** The first `openbao-vault-bootstrap` run prints the intermediate's CSR (public data) and generates it once;
read it with `kubectl -n foundation-vault logs job/openbao-vault-bootstrap | sed -n '/BEGIN CERTIFICATE REQUEST/,/END CERTIFICATE REQUEST/p'`
(logs are rotated: `bao_admin write -field=csr pki/intermediate/generate/internal common_name="OpenAether Workload Intermediate CA"`
makes a new one). Sign it offline with the root ([`pki-root-offline-runbook.md`](./pki-root-offline-runbook.md)), then import it.
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

**Seal key rotation.** In the Secret `openbao-seal-key`, put the new key as `key` and keep the old one as `previous-key`
(`kubectl patch`), restart the pods one by one, check each unseals, then remove `previous-key`. The entrypoint computes the key ids.
Escrow the new key. (Tested against a real OpenBao: `scripts/dev/test-openbao-bootstrap.sh`.)

**Disaster recovery.** The raft snapshot (backup brick) is encrypted by the seal key, so a restore needs the **escrowed key**:
(1) once the namespace exists and before the pods start, create `openbao-seal-key` from the escrow; (2) let Flux deploy: `init.sh`
initialises an empty cluster; (3) restore the snapshot with a break-glass root token (`bao operator raft snapshot restore -force`);
(4) from then on the *source* cluster's recovery shares and policies apply: put your saved `openbao-recovery-shares.yaml` back.
With any other key the data does not come back (tested). If standbys do not follow the restored leader, delete `openbao-1` and
`openbao-2` (the PVCs stay).

**Interrupted first run.** A failure inside the root window (between `sys/init` and the hand-over) revokes the root token on the
way out, whatever the cause, and leaves an initialised OpenBao that nobody can configure (the root token is gone by design; the
calls in the window are retried, and the hand-over is proven from the init Job's own identity before the token goes). On a cluster
that holds nothing yet: delete the Job, the `data-openbao-*`
PVCs and the Secrets `openbao-recovery-*`, keep `openbao-seal-key` (or delete it too) and re-sync. Otherwise use the break-glass.

## Alerts and network

`prometheusrule.yaml`: `OpenBaoWorkloadSealed` (critical: the key Secret is missing or wrong), `OpenBaoRaftNoLeader` (critical),
`OpenBaoRaftLagHigh` (warning). `networkpolicy.yaml`: the OpenBao policy admits ESO, cert-manager, the snapshot job, Prometheus,
the raft peers and the two Jobs; each Job has an egress policy of its own (DNS and OpenBao; only `openbao-init` also reaches the
kube-apiserver, to create the Secrets).

## Limits, stated plainly

- **The static key sits next to the data it protects**: a Secret in the cluster (etcd, encrypted at rest on Talos) and the raft
  PVCs on the node. It removes what the former design leaked (unseal shares and root token in logs and in plain Secrets) and gives
  hands-free restarts; it does **not** separate the key from the cluster. Someone who can read Secrets in `foundation-vault` and the
  node's disk can open the data. Closing this means a key held outside the cluster (a transit seal on another OpenBao, or a KMS);
  until then RBAC on this namespace is part of the security boundary.
- Anyone who can create Pods running as `openbao-vault-bootstrap` gets the bootstrap identity, which can write policies.
- The Kubernetes-auth login itself is only exercised on a cluster; `scripts/dev/test-openbao-bootstrap.sh` replaces that one call.
- The 3-replica raft join, the restore with standbys and the runbooks above that use `kubectl` have not been drilled on a cluster
  at this version.
- **Nested PKI mounts are forbidden**: never mount `pki/` and `pki/<x>/` (the second fails with HTTP 400, sub-paths 404).

## Tests

`scripts/dev/test-openbao-bootstrap.sh` runs the real entrypoint, `init.sh` and `configure.sh` against an OpenBao container (needs
Docker, gpg, jq, openssl): both modes, root revocation (witnessed by the audit trail), no secret in any output, idempotence,
create-only, what every policy allows and denies, PGP shares and the break-glass, snapshot restore with the right and a wrong key,
seal-key rotation. It runs in CI. On a cluster: `kubectl -n foundation-vault get pods,jobs` (3× openbao Ready, `openbao-init` and
`openbao-vault-bootstrap` Complete).
