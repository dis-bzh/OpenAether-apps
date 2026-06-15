# OpenAether — PKI Root CA Offline Runbook
## Génération et gestion de la Root CA hors-ligne

### Objectif
La Root CA ne touche **jamais** le cluster. Elle est générée dans un contexte éphémère (ta machine,
un conteneur jetable, un VM isolé), sa clé privée est stockée dans **Bitwarden EU** (ou
Vaultwarden, 1Password, KeepassXC), et seuls les certificats signés (intermediate) sont importés
dans OpenBao.

### Prérequis
- OpenSSL 3.x ou cfssl
- Accès Bitwarden EU (CLI `bw` ou API) ou gestionnaire de mots de passe équivalent
- Environnement isolé (pas de réseau, VM jetable recommandée)

---

### 1. Génération de la Root CA

```bash
# Dans un conteneur jetable (ex: podman run -it --rm alpine:3.20 sh)
# ou sur ta machine hors-ligne

# Générer la clé privée (RSA 4096 ou ECDSA P-384)
openssl genrsa -aes256 -out root-ca.key 4096
# Ou ECDSA:
# openssl ecparam -genkey -name secp384r1 -out root-ca.key

# Générer le certificat auto-signé (validité 10 ans = 3650 jours)
openssl req -x509 -new -nodes -key root-ca.key -sha256 -days 3650 \
  -subj "/CN=OpenAether Root CA/O=OpenAether/OU=PKI" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -out root-ca.crt

# Vérifier
openssl x509 -in root-ca.crt -text -noout
```

### 2. Stockage dans Bitwarden EU

```bash
# Via CLI bw (après login: bw login)
bw create item \
  --name "OpenAether Root CA" \
  --type note \
  --notes "$(cat root-ca.key)" \
  --collection "OpenAether PKI"

# Stocker aussi le certificat public
bw create item \
  --name "OpenAether Root CA Cert" \
  --type note \
  --notes "$(cat root-ca.crt)" \
  --collection "OpenAether PKI"
```

### 3. Signature d'un CSR Intermediate (runbook récurrent)

Quand le bootstrap-roles-job génère un CSR intermediate :

```bash
# 1. Récupérer le CSR (affiché dans les logs du job, ou via API)
#    Le CSR commence par -----BEGIN CERTIFICATE REQUEST-----

# 2. Le signer avec la Root CA
openssl ca -config openssl-root.cnf \
  -in intermediate.csr \
  -out intermediate-signed.crt \
  -extensions v3_intermediate_ca \
  -days 730  # 2 ans

# Contenu minimal openssl-root.cnf:
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

### 4. Import dans OpenBao (via bootstrap-roles-job)

Le certificat signé `intermediate-signed.crt` est fourni au job qui fait :
```bash
bao write -format=json pki/intermediate/set-signed \
  certificate=@intermediate-signed.crt
```

### 5. Renouvellement (tous les 1-2 ans)

- Générer un nouveau CSR intermediate
- Le signer avec la même Root CA (ou une nouvelle Root CA si rotation complète)
- Importer dans OpenBao
- La Root CA (10 ans) ne change que rarement

### 6. Nettoyage

```bash
# Après stockage dans Bitwarden, supprimer les fichiers locaux
shred -n 3 root-ca.key root-ca.crt intermediate.csr intermediate-signed.crt
rm -f root-ca.key root-ca.crt intermediate.csr intermediate-signed.crt
```

---

### Variantes selon le gestionnaire de secrets

| Gestionnaire | Stockage clé privée | Stockage certificat | Signature CSR |
|-------------|---------------------|---------------------|---------------|
| **Bitwarden EU** | Note chiffrée (champ `notes`) | Note séparée | CLI `bw` + `openssl` |
| **Vaultwarden** (self-hosted) | Même chose | Même chose | Même chose |
| **1Password** | Document sécurisé | Document sécurisé | CLI `op` + `openssl` |
| **KeepassXC** | Entrée avec pièce jointe | Entrée avec pièce jointe | Manuel + `openssl` |
| **Pass** (pass.store) | `pass insert pki/root-ca` | `pass insert pki/root-ca.crt` | `pass show` + `openssl` |

Le principe reste identique : **la clé privée ne quitte jamais le gestionnaire de secrets**,
seul le certificat signé circule vers le cluster.