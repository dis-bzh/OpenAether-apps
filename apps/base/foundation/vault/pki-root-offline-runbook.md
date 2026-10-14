# OpenAether — offline root CA runbook

How the root CA of the OpenBao PKI is generated, kept and used. Its inventory row, crypto-period and compromise
procedure are in [`docs/key-inventory.md`](../../../../docs/key-inventory.md).

## Objective and custody

The root CA never touches the cluster. It signs the OpenBao intermediate once (and again at each renewal); only the
signed intermediate certificate is imported. The root **private key** has three possible custodies, in order of preference:

| Custody | What it is | Status |
|---|---|---|
| **Hardware token / HSM** | Key generated ON the device (PIV smartcard, PKCS#11 HSM), non-exportable; signing happens on the device | Recommended for production |
| **Offline encrypted media** | Passphrase-protected key on encrypted USB media (or an encrypted blob printed on paper), at least two copies in two places, never plugged into a networked host | Acceptable for production |
| **Password manager** | Passphrase-protected key as a note in a networked service (Bitwarden, Vaultwarden, 1Password, KeePassXC) | **Recorded deviation**: the key is reachable from a networked service, which ANSSI's CA guidance (IGC-BP-05) asks to avoid. Lab or small deployments only |

A password manager is always fine for the PUBLIC certificate (`root-ca.crt`). Whichever custody you pick, write it in
the key inventory so that a deviation is a decision on record and not an accident.

Why the root is offline and the intermediate online: the intermediate lives in OpenBao and is unwrapped by the static seal
key held in the cluster (README, "Limits"), so it is only as safe as that key. The root is what limits the damage: a
compromised intermediate is replaced, a compromised root means re-trusting everything.

## Prerequisites

- OpenSSL 3.x. For a token: `ykman` or `pkcs11-tool` plus the PKCS#11 module of the device.
- An air-gapped host or a disposable VM with no network, a RAM-backed working directory (`mount -t tmpfs`), and
  encrypted media for the output.

## 1. Generate the root

```bash
# Working directory in RAM, so that nothing of the key lands on a disk.
sudo mount -t tmpfs -o size=16m tmpfs /mnt/ca && cd /mnt/ca

# ECDSA P-384 (preferred; or `openssl genrsa -aes256 -out root-ca.key 4096`). The passphrase prompt: use a long random one.
openssl ecparam -genkey -name secp384r1 | openssl pkcs8 -topk8 -v2 aes-256-cbc -out root-ca.key

# Self-signed certificate, 10 years (3650 days), SHA-384.
openssl req -x509 -new -key root-ca.key -sha384 -days 3650 \
  -subj "/CN=OpenAether Root CA/O=OpenAether/OU=PKI" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -out root-ca.crt
openssl x509 -in root-ca.crt -text -noout
```

On a token, generate the key on the device instead (for a YubiKey PIV: `ykman piv keys generate -a ECCP384 9c pub.pem`, then
`ykman piv certificates generate -s "/CN=OpenAether Root CA/" -d 3650 9c pub.pem`) and sign through OpenSSL's PKCS#11
provider. The token commands were NOT run for this repository: dry-run the whole procedure (generation, one signature, the
import on a scratch OpenBao) with a throwaway root before the first real one.

## 2. Store

- Token: nothing to store but the PIN/PUK custody, and a second token enrolled the same way if the policy asks for a spare.
- Offline media: copy `root-ca.key` (already passphrase-encrypted) to the media, twice, to two places; keep the passphrase
  apart from the media. Check that a copy decrypts: `openssl pkey -in root-ca.key -noout` on the copy.
- Password manager (the recorded deviation): the encrypted key as a note (`bw create item`). Keep the passphrase elsewhere.
- Always: keep `root-ca.crt` anywhere, it is public.

## 3. Sign an intermediate CSR (the recurring step)

The `openbao-vault-bootstrap` Job prints the CSR (public data) on its first run (README, "PKI intermediate").

```bash
# openssl-root.cnf, minimal:
#   [ca]  default_ca = CA_default
#   [CA_default]  dir = .  certificate = root-ca.crt  private_key = root-ca.key  new_certs_dir = ./newcerts
#                 database = ./index.txt  serial = ./serial  default_md = sha384  policy = policy_loose
#   [policy_loose]  commonName = supplied  organizationName = optional  organizationalUnitName = optional
#   [v3_intermediate_ca]  basicConstraints = critical,CA:TRUE,pathlen:0
#                         keyUsage = critical,keyCertSign,cRLSign  subjectKeyIdentifier = hash
# (write each section on its own lines in the real file)
mkdir -p newcerts && : > index.txt && echo 1000 > serial
openssl ca -config openssl-root.cnf -md sha384 -in intermediate.csr -out intermediate-signed.crt \
  -extensions v3_intermediate_ca -days 730 -notext -batch
openssl verify -CAfile root-ca.crt intermediate-signed.crt
```

730 days matches the 17520 h that `configure.sh` asks for; the intermediate can not outlive the root (10 years).

## 4. Import into OpenBao (a manual step)

There is no root token in the cluster: the import uses the `openaether-admin` identity (the `bao_admin` helper of the README),
which may import the signed certificate but not write PKI roles or mounts.

```bash
kubectl -n foundation-vault exec -i openbao-0 -- sh -c 'cat > /tmp/int.crt' < intermediate-signed.crt
bao_admin write pki/intermediate/set-signed certificate=@/tmp/int.crt
```

The `openbao` ClusterIssuer (`pki/sign/openaether`) becomes Ready: `kubectl get clusterissuer openbao`.

## 5. Renewal (every 1 to 2 years; put it in a calendar)

- Generate a new intermediate CSR (`bao_admin write -field=csr pki/intermediate/generate/internal common_name="OpenAether Workload Intermediate CA"`),
  sign it with the same root, import it. Certificates already issued keep working until they expire; new ones come from the
  new intermediate.
- The root (10 years) changes rarely. A new root means a new trust anchor everywhere the old one was distributed (the clients
  that trust `*.openaether.local`): plan it as a migration, not a renewal.

## 6. Compromise

No CRL or OCSP is published for this PKI, so **revocation is replacement**.

- **Intermediate compromised or suspected** (the seal key leaked together with a snapshot, a readable backup, an admin
  token): with a break-glass root token (the admin identity can not touch mounts) disable and re-enable the `pki/` mount, which
  destroys its keys and issuers, generate a new intermediate, sign it with the root, import it, then have cert-manager
  re-issue every leaf (`cmctl renew --all`). Rotate the seal key as well (README) and follow
  [`docs/key-inventory.md`](../../../../docs/key-inventory.md). Not run on a cluster.
- **Root compromised**: generate a new root, a new intermediate under it, replace the trust anchor in every place that holds
  the old one, and treat everything issued since the compromise as untrusted.

## 7. Erasure

`shred` is not a reliable erasure on an SSD (wear levelling), a journaled or copy-on-write filesystem, or a snapshotted
volume. The working directory above is RAM (`umount /mnt/ca` ends it). For media: encrypt first, so that destroying the
passphrase is the erasure, and physically destroy media at end of life.
