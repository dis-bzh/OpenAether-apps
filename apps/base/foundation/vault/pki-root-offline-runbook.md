# OpenAether — PKI Root CA Offline Runbook
## Generating and managing the offline Root CA

### Goal
The Root CA **never** touches the cluster. It is generated in an ephemeral context (your machine,
a throwaway container, an isolated VM), its private key is stored in **Bitwarden EU** (or
Vaultwarden, 1Password, KeepassXC), and only the signed certificates (intermediate) are imported
into OpenBao.

### Prerequisites
- OpenSSL 3.x or cfssl
- Access to Bitwarden EU (`bw` CLI or API) or an equivalent password manager
- An isolated environment (no network, a throwaway VM is recommended)

---

### 1. Generating the Root CA

```bash
# In a throwaway container (e.g. podman run -it --rm alpine:3.20 sh)
# or on your offline machine

# Generate the private key (RSA 4096 or ECDSA P-384)
openssl genrsa -aes256 -out root-ca.key 4096
# Or ECDSA:
# openssl ecparam -genkey -name secp384r1 -out root-ca.key

# Generate the self-signed certificate (10 years = 3650 days)
openssl req -x509 -new -nodes -key root-ca.key -sha256 -days 3650 \
  -subj "/CN=OpenAether Root CA/O=OpenAether/OU=PKI" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -out root-ca.crt

# Check
openssl x509 -in root-ca.crt -text -noout
```

### 2. Storing it in Bitwarden EU

```bash
# With the bw CLI (after login: bw login)
bw create item \
  --name "OpenAether Root CA" \
  --type note \
  --notes "$(cat root-ca.key)" \
  --collection "OpenAether PKI"

# Also store the public certificate
bw create item \
  --name "OpenAether Root CA Cert" \
  --type note \
  --notes "$(cat root-ca.crt)" \
  --collection "OpenAether PKI"
```

### 3. Signing an Intermediate CSR (recurring runbook)

When the `openbao-vault-bootstrap` Job generates an intermediate CSR:

```bash
# 1. Get the CSR (public data, no secret). The Job stores it in KV and also prints it, but its log is removed 60 minutes
#    after it ends, so read it from KV:
#      bao_admin kv get -field=csr secret/pki/intermediate-csr
#    A cluster whose CSR was generated before this entry existed has none: bao_admin write -field=csr
#    pki/intermediate/generate/internal common_name="OpenAether Workload Intermediate CA" makes a new one.
#    The CSR starts with -----BEGIN CERTIFICATE REQUEST-----

# 2. Sign it with the Root CA
openssl ca -config openssl-root.cnf \
  -in intermediate.csr \
  -out intermediate-signed.crt \
  -extensions v3_intermediate_ca \
  -days 730  # 2 years

# Minimal openssl-root.cnf:
# [ca]
# default_ca = CA_default
# [CA_default]
# dir = .
# certificate = root-ca.crt
# private_key = root-ca.key
# new_certs_dir = ./newcerts
# database = ./index.txt
# serial = ./serial
# default_md = sha256
# policy = policy_loose
# [policy_loose]
# countryName = optional
# stateOrProvinceName = optional
# organizationName = optional
# organizationalUnitName = optional
# commonName = supplied
# emailAddress = optional
# [v3_intermediate_ca]
# basicConstraints = critical,CA:TRUE,pathlen:0
# keyUsage = critical,keyCertSign,cRLSign
# subjectKeyIdentifier = hash
```

### 4. Importing into OpenBao (MANUAL step)

The `openbao-vault-bootstrap` Job ONLY generates and prints the CSR (non-blocking). Importing the
signed certificate is manual (the root stays offline). There is no root token in the cluster: the import is done
with the `openaether-admin` identity (the `bao_admin` helper of the README, which exchanges the ServiceAccount token for an
OpenBao token INSIDE the pod):
```bash
kubectl -n foundation-vault exec -i openbao-0 -- sh -c 'cat > /tmp/int.crt' < intermediate-signed.crt
bao_admin write pki/intermediate/set-signed certificate=@/tmp/int.crt
```
After the import, the cert-manager `openbao` issuer (`pki/sign/openaether`) becomes Ready.

### 5. Renewal (every 1-2 years)

- Generate a new intermediate CSR
- Sign it with the same Root CA (or a new Root CA for a complete rotation)
- Import it into OpenBao
- The Root CA (10 years) rarely changes

### 6. Cleanup

```bash
# Once stored in Bitwarden, delete the local files
shred -n 3 root-ca.key root-ca.crt intermediate.csr intermediate-signed.crt
rm -f root-ca.key root-ca.crt intermediate.csr intermediate-signed.crt
```

---

### Variants by secret manager

| Manager | Private key storage | Certificate storage | CSR signing |
|-------------|---------------------|---------------------|---------------|
| **Bitwarden EU** | Encrypted note (`notes` field) | Separate note | `bw` CLI + `openssl` |
| **Vaultwarden** (self-hosted) | Same | Same | Same |
| **1Password** | Secure document | Secure document | `op` CLI + `openssl` |
| **KeepassXC** | Entry with an attachment | Entry with an attachment | Manual + `openssl` |
| **Pass** (pass.store) | `pass insert pki/root-ca` | `pass insert pki/root-ca.crt` | `pass show` + `openssl` |

The principle stays the same: **the private key never leaves the secret manager**,
only the signed certificate travels to the cluster.