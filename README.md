# OpenAether Apps

Kubernetes manifests for the OpenAether platform, managed by **Flux** (GitOps).
This repository is read by Flux controllers bootstrapped via
[dis-bzh/OpenAether-infra](https://github.com/dis-bzh/OpenAether-infra).

## Directory Structure

```
.
├── base/          # Provider-agnostic Kubernetes manifests
│   ├── namespaces/
│   ├── platform/          # Gateway API CRDs
│   ├── foundation/        # OpenBao (HA Shamir + unsealer), bootstrap Jobs
│   ├── external-secrets/  # ESO install + ClusterSecretStore
│   ├── cert-manager/      # cert-manager + ClusterIssuers (OpenBao-backed)
│   ├── istio/             # Istio ambient mesh + Gateway
│   ├── kyverno/           # Kyverno + policies
│   ├── observability/     # VictoriaMetrics + Grafana
│   ├── cnpg/              # CloudNative-PG operator + zitadel-db
│   ├── identity/          # Zitadel (IAM souverain, backend CNPG)
│   ├── storage/           # local-path-provisioner (local/Docker only)
│   └── ...
└── flux/          # Flux Kustomization DAG
    ├── base/      # Shared Kustomizations (all clusters)
    ├── management/ # Overlay: management cluster
    ├── workload/  # Overlay: workload/spoke clusters
    └── local/     # Overlay: local Docker testing (suspend heavy components)
```

## Bootstrap Flow

```
OpenAether-infra: tofu apply -var talos_bootstrap=true
  └─► Talos inlineManifests:
        ├── cilium.yaml          # CNI
        ├── flux-install.yaml    # Flux controllers + CRDs
        └── flux-bootstrap.yaml  # GitRepository (→ this repo) + root Kustomization
              └─► Flux syncs apps/flux/{management,workload}/
                    └─► Kustomization DAG deploys base/ manifests in dependency order
```

## Flux Kustomization DAG

The `apps/flux/base/` directory defines the reconciliation order via `dependsOn`:

```
namespaces
  └── platform-gateway-api    (Gateway API CRDs)
  └── platform                (storage, kyverno, ...)
        └── foundation-pki-root    (OpenBao root CA)
              └── foundation-vault (OpenBao workload PKI)
                    └── external-secrets        (ESO install)
                          └── external-secrets-stores  (ClusterSecretStore)
                                └── cert-manager
                                └── ...
```

## Multi-cluster Routing

Flux overlays select which Kustomizations are active per cluster:

| Overlay | Target | Active components |
|---------|--------|-------------------|
| `flux/management/` | Management hub | Full stack: OpenBao, Keycloak, Grafana, Istio, ... |
| `flux/workload/` | Spoke clusters | ESO, cert-manager, Kyverno, KEDA, Istio, ... |
| `flux/local/` | Local Docker | Same as management, heavy components suspended |

## Adding a New Service

1. Add manifests to `base/<service>/` with a `kustomization.yaml`
2. Add a `Kustomization` entry in `flux/base/` with appropriate `dependsOn`
3. Patch the overlay (`flux/management/` or `flux/workload/`) if cluster-specific
4. Commit and push → Flux reconciles automatically

## Local Development

Requires [dis-bzh/OpenAether-infra](https://github.com/dis-bzh/OpenAether-infra) cloned alongside:

```bash
# Both repos side-by-side:
# ~/repos/OpenAether-infra/
# ~/repos/OpenAether-apps/    ← this repo

cd ../OpenAether-infra
task local-test        # boots 3-node Docker cluster + applies apps/flux/local/
```
