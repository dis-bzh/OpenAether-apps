path "secret/data/*"     { capabilities = ["read"] }
path "secret/metadata/*" { capabilities = ["read", "list"] }
path "pki/cert/*"        { capabilities = ["read", "list"] }
path "sys/mounts"        { capabilities = ["read"] }
path "sys/policies/acl"  { capabilities = ["list"] }
path "sys/health"        { capabilities = ["read"] }
path "auth/token/lookup-self" { capabilities = ["read"] }
