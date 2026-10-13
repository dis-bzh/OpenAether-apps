# The identity of the OpenBao bootstrap Jobs (Kubernetes auth role `openbao-bootstrap`, 15 minute tokens).
# It configures the engines, the auth methods, the policies and the application secrets, and nothing else:
# it can not seal, rekey, rotate, generate a root token, touch raw storage or the raft cluster.
# Whoever can write policies can grant themselves anything the ACL system allows, so the guard is where the
# token comes from: only a Pod running as ServiceAccount foundation-vault/openbao-vault-bootstrap can log in.
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
