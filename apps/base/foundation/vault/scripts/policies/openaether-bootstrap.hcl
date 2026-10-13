# The identity of the OpenBao bootstrap Jobs (Kubernetes auth role `openbao-bootstrap`, 15 minute tokens).
# It configures the engines, the auth methods, the policies and the application secrets.
#
# ⚠ It is ROOT-EQUIVALENT in practice: whoever can write a policy and a role can give themselves any power the ACL system has,
# this policy's own denies included (measured: a token with only this policy rewrote it with `path "*"` and minted tokens). The denies
# at the bottom stop a mistake in a script, not a hostile holder. The real control is who can obtain the identity: only a Pod running as
# the ServiceAccounts foundation-vault/openbao-vault-bootstrap or openbao-init can log in with it, i.e. whoever may create Pods in this
# namespace or change what Flux applies. Keep that group small.
path "sys/mounts"        { capabilities = ["read"] }
path "sys/mounts/*"      { capabilities = ["create", "read", "update", "delete", "sudo"] }
path "sys/auth"          { capabilities = ["read"] }
path "sys/auth/*"        { capabilities = ["create", "read", "update", "delete", "sudo"] }
path "sys/policies/acl"  { capabilities = ["list"] }
path "sys/policies/acl/*" { capabilities = ["create", "read", "update", "delete", "list"] }
path "auth/kubernetes/*" { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
path "pki/*"             { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
path "secret/*"          { capabilities = ["create", "read", "update", "delete", "list"] }
path "sys/health"        { capabilities = ["read"] }
path "sys/leader"        { capabilities = ["read"] }
path "sys/capabilities-self" { capabilities = ["update"] }
path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self"  { capabilities = ["update"] }
path "auth/token/revoke-self" { capabilities = ["update"] }

# Never, whatever a future edit above widens.
path "sys/seal"                 { capabilities = ["deny"] }
path "sys/step-down"            { capabilities = ["deny"] }
path "sys/rekey/*"              { capabilities = ["deny"] }
path "sys/rekey-recovery-key/*" { capabilities = ["deny"] }
path "sys/rotate"               { capabilities = ["deny"] }
path "sys/generate-root*"       { capabilities = ["deny"] }
path "sys/raw/*"                { capabilities = ["deny"] }
path "sys/storage/raft/*"       { capabilities = ["deny"] }
path "sys/audit*"               { capabilities = ["deny"] }
path "auth/token/create*"       { capabilities = ["deny"] }
