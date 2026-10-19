# OpenAether Apps

**Common / platform** Kubernetes manifests for OpenAether, reconciled by **Flux**
(GitOps). This repository is read by the Flux controllers bootstrapped from
[dis-bzh/OpenAether-infra](https://github.com/dis-bzh/OpenAether-infra).

🇫🇷 [Version française](README.fr.md)

**Versions.** apps 0.3.0 (released 2026-10-19; release notes under `[0.3.0]` in CHANGELOG.md) pairs with OpenAether-infra 0.3.0: a fresh install lands on the upgraded components, and a 0.2.0 cluster hops one minor at a time as `docs/patching.md` says. apps 0.2.0 is for new clusters: a cluster initialised by 0.1.0 cannot be moved to it in place, and a 0.1.1 cluster
needs its VictoriaMetrics volumes recreated, which loses their metric history (CHANGELOG, Upgrade notes; #71). It pairs with OpenAether-infra 0.2.1, not with
0.2.0. The pairing table, what was validated where and what is not fixed are in [CHANGELOG.md](CHANGELOG.md).

## Principle: fixed foundation + modular pick

**The only fixed foundation is CNI (Cilium) + Flux.** Everything else — Istio
mesh, Zitadel, OpenBao, CNPG, Longhorn, observability… — is **optional and
composable**. A cluster picks the bricks it needs; the transitive closure of
dependencies is computed by `scripts/pick.py`.

**Business** manifests do NOT live here: they stay in each application
repository (`deploy/k8s/`). This repo only carries the shared platform and the
Flux pointer to those repos.

## Structure

```
apps/
├── base/                      # Manifests, one brick per directory
│   ├── namespaces/            # foundation-*/services-* split + PSA labels
│   ├── platform/              # StorageClasses, Gateway API, PriorityClasses,
│   │                          #   LB-IPAM, network policies, metrics-server
│   ├── foundation/vault/      # OpenBao HA raft (static auto-unseal, internal TLS)
│   ├── foundation/vault-ca/   # CA for the OpenBao listener (ns cert-manager)
│   ├── external-secrets/      # ESO + openbao ClusterSecretStore
│   ├── cert-manager/          # cert-manager + OpenBao-backed ClusterIssuers
│   ├── istio/                 # Istio ambient (istiod, CNI, ztunnel) + policies
│   ├── services-gateway/      # North-south Gateway + FIXED NodePorts 30080/30443
│   ├── cnpg/                  # CloudNativePG + zitadel-db / grafana-db clusters
│   ├── storage/               # Longhorn (replicated volumes, LUKS via OpenBao)
│   ├── backup/                # OpenBao snapshots + pg_dump → restic, 2 S3 repos
│   ├── observability/         # VictoriaMetrics, Loki, Grafana, Alloy
│   ├── identity/              # Zitadel (IAM/SSO, CNPG backend)
│   ├── kyverno/               # Policy engine + OpenAether policies
│   ├── cluster-api-operator/  # CAPI operator          ┐
│   ├── cluster-api-providers/ # Providers + inventory   │ management
│   ├── cluster-api-clusters/  # Child cluster templates │ layer
│   └── orc/                   # CAPO dependency         ┘ (optional)
├── clusters/                  # One file per CAPI child cluster (edge-1, edge-2…)
└── flux/
    ├── base/                  # FULL DAG — source of truth for dependsOn
    ├── bricks.yaml            # Catalogue: aliases, baseline, companions, descriptions
    ├── management/            # Profile: full DAG
    ├── workload/              # Generated profile (its `# Pick:` header lists the bricks)
    └── local/                 # Profile: local Docker
```

## The pick (`scripts/pick.py`)

A profile is `../base` plus `$patch: delete` patches removing the bricks that
were not selected. The **transitive closure** guarantees that no retained brick
depends on a removed one.

```bash
python3 scripts/pick.py --list                      # catalogue
python3 scripts/pick.py vault eso certs gateway cnpg storage observability identity kyverno metrics \
        -o apps/flux/workload                        # generate a profile
python3 scripts/pick.py --validate                   # DAG + catalogue consistent
python3 scripts/pick.py --check                      # profiles up to date (CI)
```

**Invariant**: `spec.dependsOn` must encode the **real** dependencies
(StorageClass, PriorityClass, LB pool…). The catalogue is decoration only.

Before touching the DAG: `task apps-validate` (from the infra repo).

## Bootstrap flow

```
OpenAether-infra: tofu apply -var talos_bootstrap=true
  └─► Talos inlineManifests:
        ├── cilium.yaml          # CNI
        ├── flux-install.yaml    # Flux controllers + CRDs
        └── flux-bootstrap.yaml  # GitRepository (→ this repo) + root Kustomization
              └─► Flux syncs apps/flux/<profile>/
                    └─► the DAG deploys apps/base/ in dependsOn order
```

## Child clusters (kubeception / gitception)

`apps/clusters/<name>.yaml` describes a complete child cluster:

1. a **Kustomization** instantiating a CAPI template (`cluster-talos-*`);
2. a **Cilium HelmRelease** applied **remotely** through `spec.kubeConfig`;
3. **Flux** installed remotely, then `child-gitops` pointing the child at ITS
   own profile — from there the child reconciles autonomously.

⚠️ **Two management clusters must never read the same `apps/clusters`**: they
would fight over the same CAPI CRs. Isolate them with the management's own
`git_ref` (infra side — point each at its own `refs/heads/<branch>`), or by
path. Not with `CHILD_REF`, which selects what a *child* follows, not what the
management reads.

No child is enabled by default, for the same reason: `apps/clusters` is read by
every management tracking this repo, so a name listed there is a name a
stranger's cluster tries to reconcile.

## Adding a brick

1. create `apps/base/<brick>/` with its `kustomization.yaml`;
2. add the `Kustomization` under `apps/flux/base/` with its **real**
   `dependsOn`;
3. reference it in `apps/flux/base/kustomization.yaml`;
4. describe the brick in `apps/flux/bricks.yaml` (alias, companion, description);
5. regenerate the profiles (`pick.py`) then run `task apps-validate`;
6. commit and push → Flux reconciles.

⚠️ **Keep an operator and its own CRs in two separate Kustomizations**: a bundle
containing both an operator and CRs of the CRDs it installs is rejected at
dry-run while those CRDs do not yet exist. The Kustomization holding the CRs must `dependsOn` the operator's, which
is `wait: true` so that the CRDs are served before the CRs are applied (`external-secrets` then `external-secrets-stores`).

## Local development

Requires [OpenAether-infra](https://github.com/dis-bzh/OpenAether-infra) cloned
alongside:

```bash
cd ../OpenAether-infra
task local-up      # 3 CP + 3 worker Talos cluster on Docker (Cilium, no Flux, no apps)
task local-test    # + Flux controllers and apps/flux/local, every Kustomization SUSPENDED
```

Locally, Flux is deliberately a no-op there: for the apps, `task local-test` proves only that the controllers start and the
Kustomization objects are admitted (it also checks the cluster itself: etcd quorum, nodes, Cilium). To deploy a brick, apply it yourself from the working
tree (`kubectl apply -k apps/base/<brick>`), which lets you test a change **before** pushing it. A CI lane
that really reconciles a subset of bricks on Docker exists (`.github/workflows/local-docker.yml`, see
[docs/local-docker.md](docs/local-docker.md)): local Docker, partial bricks, not a substitute for the cloud rung.

## License

**OpenAether** is licensed under the [Apache License 2.0](LICENSE). It was
AGPLv3 through the 0.x releases; the change is a relaxation, so anything you already had
under AGPLv3 stays yours under it.

Source: **https://github.com/dis-bzh/OpenAether-apps**
