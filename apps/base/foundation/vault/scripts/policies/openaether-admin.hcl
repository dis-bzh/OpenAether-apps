# Daily operations by a named human (Kubernetes auth role `openaether-admin`, ServiceAccount
# foundation-vault/openaether-admin, tokens up to 8 hours): read and rotate application secrets, import the signed
# intermediate CA, revoke leases, read the configuration.
#
# What it can NOT do, on purpose: write policies, auth roles or mounts (whoever can write a policy can grant themselves
# anything, so the configuration changes only through git, the Flux-run configure Job); mint tokens; seal, rekey,
# rotate, generate a root token, touch raw storage, the raft cluster or the audit trail (recovery-share holders).
path "secret/*"               { capabilities = ["create", "read", "update", "delete", "list"] }
path "pki/*"                  { capabilities = ["create", "read", "update", "delete", "list"] }
path "sys/leases/*"           { capabilities = ["create", "read", "update", "delete", "list"] }
path "sys/policies/acl"       { capabilities = ["list"] }
path "sys/policies/acl/*"     { capabilities = ["read", "list"] }
path "sys/mounts"             { capabilities = ["read"] }
path "sys/auth"               { capabilities = ["read"] }
path "auth/kubernetes/role"   { capabilities = ["list"] }
path "auth/kubernetes/role/*" { capabilities = ["read"] }
path "sys/health"             { capabilities = ["read"] }
path "sys/capabilities-self"  { capabilities = ["update"] }
path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self"  { capabilities = ["update"] }

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
