# Break-glass: the ONLY thing this identity can do is start and complete a root-token generation. The generation
# itself still needs a quorum of RECOVERY shares (held, PGP-encrypted, by the operators): this policy
# only lets the request be authenticated (OpenBao 2.6+ no longer accepts it anonymously). No Pod runs as
# its ServiceAccount; a cluster administrator mints a token for it when, and only when, the procedure
# in the README is being followed (kubectl create token openbao-breakglass --duration=10m).
path "sys/generate-root-token/attempt" { capabilities = ["create", "read", "update", "delete"] }
path "sys/generate-root-token/update"  { capabilities = ["update"] }
path "sys/health"                      { capabilities = ["read"] }
