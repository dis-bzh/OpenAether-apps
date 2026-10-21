# Losing a node or a pod: what the platform bricks guarantee, and how CI keeps it so

Release 0.4.0, milestone M4. Rung: **mocked/static** (rendered manifests, unit tests of the audit and of the alert rules). Nothing here has been drilled on a cloud; the drills and their pass
criteria are in OpenAether-infra `docs/resilience.md`. Not a compliance claim.

The management cluster does not heal its nodes (OpenTofu builds them, an operator rebuilds them; what Cluster API manages stays Cluster API's). So the platform has to **keep serving when one
worker is lost**, and say loudly when a person must act.

## The audit (`scripts/check-ha.py`, in CI)

It renders the Flux DAG (kustomize, then every HelmRelease through `helm template` with its values and post-renderer patches) and checks every Deployment and StatefulSet, and the pods an
operator makes from a VMCluster, VMAgent, VMAlert, VMAlertmanager or CNPG Cluster:

1. two replicas (an HPA's `minReplicas` counts), or an entry under `single_replica` in `scripts/ha-allowlist.yaml` with a reason **and what the loss means**;
2. with two or more replicas: a hostname `topologySpreadConstraint` or `podAntiAffinity` that selects the workload's own pods;
3. exactly one PodDisruptionBudget on the pods, allowing exactly one disruption (zero blocks every drain, two or more takes the quorum down at once); `pdb_exception` lists the deliberate ones (Loki);
4. nothing that can strand a pod on three workers: no required `podAffinity`, no required hostname anti-affinity or hard spread for more than two replicas (`strict_spread` lists OpenBao, with its reason),
   no `nodeSelector` but the OS one;
5. the CPU requests of what it renders stay under a bound, so that the shipped 4 x 4 vCPU examples stay roll-able (3 x 4 passes with about 0.45 vCPU of margin; infra `docs/capacity.md`).

The allow-list cannot rot: an entry for a workload that no longer exists, one that now has two replicas, or one without a reason fails the check; so does a workload that is compliant but still listed
under `known_gaps` (which must be empty at release). Not audited because the pods do not exist in the rendered manifests: the Gateway pods (HPA minimum 2, budget and spread policy, checked by
`test-gateway-ha.py`), the Longhorn CSI sidecars, an Istio waypoint (one replica unless the user scales it; the platform does not use one).

### What changed in the bricks

| Workload | Before | Now |
|---|---|---|
| cert-manager (controller, webhook, cainjector) | 2 replicas, no spread, no budget | spread and `maxUnavailable: 1` budget (chart keys) |
| istiod | 2 replicas, a budget, no spread | spread |
| ESO controller and webhook | 1 | 2 each, spread, budgets; the controller gets `--enable-leader-election=true` |
| Kyverno admission controller (apps #82, first point) | 1 | 2, budget (the other three controllers stay single, listed) |
| CNPG operator | 1 | 2 (the vendored args carry `--leader-elect`), spread, budget: it decides a failover |
| metrics-server | 1 | 2, spread, budget: the Gateway's HPA reads it |
| Zitadel login UI | 1 | 2, spread, budget (the server already had 2) |
| Alertmanager | 1 | 2, spread, budget, gossip on 9094 in the network policy: a lost worker no longer silences the alerts about it |
| Longhorn UI | 2 without spread or budget | 1, listed (not on any data or customer path) |

CPU requests added: about 345 m (Kyverno 100, CNPG operator 100, metrics-server 50, Zitadel login 50, ESO 20, Alertmanager 25). Derived from the manifests, not measured.

### Single replica on purpose

backup-verify-exporter, the Barman Cloud plugin, ORC and the CAPI operator (optional bricks), local-path-provisioner, kube-state-metrics, the VictoriaMetrics operator (leader lease), Longhorn's driver
deployer and UI, the ESO cert-controller, Kyverno's background, cleanup and reports controllers. Each with its loss in `scripts/ha-allowlist.yaml`. A pod of these on a dead node waits for the 5 minute
toleration, or for `task cluster-replace-node` in OpenAether-infra, which deletes the Node object and frees it at once.

## The alerts (`vmrule-loss.yaml`, `docs/runbooks.md`)

`ControlPlaneMemberMissing`, `FailureDomainLost` (built on the kubelet scrape and the zone label that OpenAether-infra sets on every node, not on kube-state-metrics: lab 21 found that single pod blind when it sat in the lost zone), `NodeScrapeDown` (the lost-node page that does not need kube-state-metrics either), `WorkloadReplicasBelowDesired`,
`ClusterCannotLoseWorker`, `OpenBaoReplicasMissing`, `AlertmanagerClusterDegraded`, and the two existing node alerts re-timed (`NodeUnreachable` 3 min: lab 15c reboots are 85-115 s; `NodeNotReady` 5 min). Each
description and runbook heading names the task to run (`cluster-node-state`, `cluster-node-recover`, `cluster-replace-node`, `failure-domain-check`, `dr-restore-plan` as the last resort) and what not to do.
The Longhorn "fewer than two healthy replicas" case is `LonghornVolumeDegraded` (Longhorn exports no replica count). New series no cluster has shown are listed in `docs/monitoring-0.4.md`.
