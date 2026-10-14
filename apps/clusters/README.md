# apps/clusters — CAPI client clusters (kubeception / gitception)

This layer: edge-1 (Scaleway) and edge-2 (OVH) were provisioned end to end
from a real management cluster (2026-07-25/26), Cilium and Flux injected
remotely, gitception included, but before the `CHILD_BACKUP_*` variables were
added (2026-07-30). Since then the chain is validated **statically only**
(`scripts/check-child-clusters.py`, CI), not replayed on a cluster. The template
apiVersions are checked against the pinned providers (CAPS v0.2.1 →
`v1alpha2`, CACPPT v0.5.13 / CABPT v0.6.12 → `v1alpha3`).

A management cluster (with the `cluster-api-operator` + `cluster-api-providers`
bricks picked) drives its client clusters from here: **one file per cluster**,
listed in `kustomization.yaml`, reconciled by the `capi-clusters` Flux
Kustomization.

## Principle (100 % native Flux, no new tooling)

1. **CAPI** provisions the Talos machines (template
   `apps/base/cluster-api-clusters/templates/…`, parameterised by
   `postBuild.substitute`) and publishes the `<name>-kubeconfig` Secret.
2. The management applies **remotely** (`spec.kubeConfig` of the Flux
   HelmRelease/Kustomization) inside the child:
   Cilium (baseline CNI) → `flux-gotk` (Flux pinned in git) → `child-gitops`
   (GitRepository `openaether` + root Kustomization `openaether-platform`,
   same names as the parent bootstrap).
3. **Gitception**: the child then reconciles its own profile
   `apps/flux/${CHILD_PROFILE}` — same bricks, same picker
   (`scripts/pick.py … -o apps/flux/workload-<name>`), autonomous even if the
   management goes down.

No strict order is required: each step retries until its condition
(kubeconfig present, API reachable) holds — Flux-style convergence.

See `example-scaleway.yaml.example` for the full commented lifecycle.

`postBuild.substitute` variables of the `<name>-gitops` Kustomization:
`CHILD_NAME` always; `CHILD_BACKUP_S3_BUCKET` + `CHILD_BACKUP_S3_ENDPOINT` if
the profile picks `cnpg`; `CHILD_BACKUP_TARGET_URL` if it picks `storage`
(placeholders in git, see `edge-1.yaml`). `scripts/check-child-clusters.py`
checks all of this, the example included.

## Prerequisites outside git (once per management cluster)

Scaleway:
```bash
kubectl create secret generic scaleway-capi-credentials -n capi-clusters \
  --from-literal=SCW_ACCESS_KEY=… --from-literal=SCW_SECRET_KEY=…   # CAPS docs: only these 2 keys are read
kubectl create secret generic <child>-substitutes -n flux-system \
  --from-literal=SCW_PROJECT_ID=…                                    # consumed by postBuild.substituteFrom
```

OpenStack / OVH — a single Secret, the region lives INSIDE `clouds.yaml`
(definitely not in `identityRef`, see the template header). ⚠️ The key under
`clouds:` MUST be named `ovh` (= `${OS_CLOUD_NAME:=ovh}` in the template,
`apps/base/cluster-api-clusters/templates/cluster-talos-openstack/cluster.yaml`)
— any other name (e.g. `openstack`) makes CAPO fail silently on the status side
(an `OpenStackCluster` with no status at all), with this in the
`capo-controller-manager` logs: `auth option failed for cloud : Missing input
for argument [auth_url]` (observed for real, 2026-07-30):
```bash
kubectl -n capi-clusters create secret generic ovh-capi-credentials \
  --from-file=clouds.yaml=./clouds.yaml
# clouds.yaml:
#   clouds:
#     ovh:                    # <- not "openstack", the template reads this exact name
#       auth: { auth_url: …, username: …, password: …, project_id: …, project_name: …, user_domain_name: …, project_domain_name: … }
#       region_name: …
#       interface: …
#       identity_api_version: …
```

Outscale: Secret `outscale-capi-credentials` (**lowercase** keys
`access_key`/`secret_key`/`region`) + keypair `openaether-capi` on the account.

### Floating IPs of an OpenStack child (to recreate after every teardown)

The OpenStack IMDS does not expose the FIP: it must be known **before** boot to
enter the Talos `certSANs`. It is the only child resource created outside both
OpenTofu **and** CAPI — it does not survive a `task fleet-down`. The real
addresses never go in git (this repository is public): they live in the
`flux-system/edge-2-env` Secret that `edge-2.yaml` reads through
`postBuild.substituteFrom` (see its header). Idempotent re-allocation (infra
repository):

```bash
source .env.sh
python3 scripts/ops/ensure-capo-fip.py edge-2 --count 2   # one FIP per CP replica, plus one for the upgrade surge
# → put them in the edge-2-env Secret (OS_CP_FLOATING_IPS, OS_CP_FLOATING_IPS_SURGE)
```

The child's kubeconfig: Secret `<child>-kubeconfig` (ns `capi-clusters`, key
`value`). Full day-1 path: `OpenAether-infra/docs/admin-access.md`.

## Decommissioning — `task edge-down` FIRST

⚠️ **Do NOT just remove the file from the kustomization.** Flux prunes all the
objects at once, in no order: if the `<Infra>Cluster` goes before the machines,
the provider loses the cloud client it needs to destroy them and loops on
"… is not available yet" — machines stuck on finalizers and **orphaned VMs
still billed** (seen twice on Scaleway).

Correct, idempotent order:

```bash
export KUBECONFIG=<management>
task edge-down CLUSTER=edge-1 -- --yes     # clean CAPI cascade, waits for the end
# only then: remove edge-1.yaml from apps/clusters/kustomization.yaml
git commit && git push                     # Flux prune has nothing left to do
```

`task edge-down` deletes the `Cluster` (CAPI cascades through ownerReferences),
waits until it is completely gone, and if the provider blocks it lifts the
finalizers **while explicitly flagging the VMs to check in the cloud console**.

The child's backups (backup brick, if picked) stay in the S3 buckets.
