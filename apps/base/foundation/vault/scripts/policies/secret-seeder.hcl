# The operator's Day-1 seeding (OpenAether-infra scripts/ops/seed-openbao.sh): it can CREATE the destinations it
# is given and test whether they exist (metadata), and nothing else. No `update`, no `delete`, no `read` of a
# value: a seeding run can never rotate a secret that is already there, and it can not read one back.
path "secret/data/backup/s3-primary"                      { capabilities = ["create"] }
path "secret/data/backup/s3-replica"                      { capabilities = ["create"] }
path "secret/data/observability/loki-s3"                  { capabilities = ["create"] }
path "secret/data/observability/alertmanager-slack"       { capabilities = ["create"] }
path "secret/data/observability/alertmanager-deadmansswitch" { capabilities = ["create"] }
# The audit sink (opt-in, infra #318): seed-openbao.sh writes these two only when audit_sink_placement is on. Two exact paths, create only.
path "secret/data/audit/sink"                             { capabilities = ["create"] }
path "secret/data/audit/sink-secondary"                   { capabilities = ["create"] }
path "secret/metadata/backup/*"        { capabilities = ["read"] }
path "secret/metadata/audit/*"         { capabilities = ["read"] }
path "secret/metadata/observability/*" { capabilities = ["read"] }
