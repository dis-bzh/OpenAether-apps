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
| kube-state-metrics (apps #156) | 1 | 2, hostname and zone spread, the existing budget; the scrape drops `pod` and `instance`, VMCluster deduplicates (+10 m CPU) |

CPU requests added: about 345 m (Kyverno 100, CNPG operator 100, metrics-server 50, Zitadel login 50, ESO 20, Alertmanager 25). Derived from the manifests, not measured.

### Single replica on purpose

backup-verify-exporter, the Barman Cloud plugin, ORC and the CAPI operator (optional bricks), local-path-provisioner, the VictoriaMetrics operator (leader lease), Longhorn's driver
deployer and UI, the ESO cert-controller, Kyverno's background, cleanup and reports controllers. Each with its loss in `scripts/ha-allowlist.yaml`. A pod of these on a dead node waits for the 5 minute
toleration, or for `task cluster-replace-node` in OpenAether-infra, which deletes the Node object and frees it at once.

## The alerts (`vmrule-loss.yaml`, `docs/runbooks.md`)

`ControlPlaneMemberMissing`, `FailureDomainLost` (built on the kubelet scrape and the zone label that OpenAether-infra sets on every node, not on kube-state-metrics: lab 21 found that single pod blind when it sat in the lost zone), `NodeScrapeDown` (the lost-node page that does not need kube-state-metrics either), `WorkloadReplicasBelowDesired`,
`ClusterCannotLoseWorker`, `OpenBaoReplicasMissing`, `AlertmanagerClusterDegraded`, and the two existing node alerts re-timed (`NodeUnreachable` 3 min: lab 15c reboots are 85-115 s; `NodeNotReady` 5 min). Each
description and runbook heading names the task to run (`cluster-node-state`, `cluster-node-recover`, `cluster-replace-node`, `failure-domain-check`, `dr-restore-plan` as the last resort) and what not to do.
The Longhorn "fewer than two healthy replicas" case is `LonghornVolumeDegraded` (Longhorn exports no replica count). New series no cluster has shown are listed in `docs/monitoring-0.4.md`.

## What a node loss costs, and the levers (0.4.0, lab 21 findings; offline changes, not yet drilled)

Rung: **mocked/static**. Every number below is from lab 21 (before the changes) or a derivation marked UNVERIFIED. Not a compliance claim.

### Database failover (apps #154)

Lab 21: a primary on a lost node was promoted only when the TaintManager evicted its pod, after the default 300 s `tolerationSeconds` of `node.kubernetes.io/unreachable` (write gaps 328 s and 333 s; 59 s for a node back in 82 s; target 30 s).

- **Chosen:** both Clusters (`zitadel-db`, `grafana-db`) carry `spec.affinity.tolerations` for `unreachable` and `not-ready`, `NoExecute`, `tolerationSeconds: 30` (`test-cnpg-eviction.py` in CI fails on the default). Expected gap for a powered-off node: the two measured gaps minus the 300 s toleration leave 28 s and 33 s (node detection, eviction and promotion), so **about 60 s with 30 s, not 30 s**. The 30 s target (met by a primary kill, 24 s, and by a planned switchover, 12-14 s) is not reachable for a power loss by tolerations alone: even a toleration of 0 leaves the 28-33 s of detection and promotion, and the detection part is a Talos `kube-controller-manager` setting (`node-monitor-grace-period`) that changes the eviction of every pod and that this change does not touch. The owner decides whether the 30 s objective applies to a node power loss.
- **Why 30 s and not less:** the primary's liveness probe (instance manager, period 10 s, 3 failures) includes the isolation check, enabled by default: a primary that is alive but cut off from the API and its replicas stops itself in about 30 s. Evicting later than that keeps the evicted pod and the promoted replica from both taking writes (split brain). Below it a node blip costs a failover; at 30 s the 82 s undrained reboot of lab 21 also fails over, which is the intended behaviour for a primary that cannot be reached.
- **Why not 60 s:** it adds 30 s to every loss for a protection the probe already gives. 30 is the lower bound of the range the test accepts (30-60).
- **`failoverDelay` stays 0** (the default, the test refuses a non-zero value: it only adds to the gap). `switchoverDelay` (3600), `stopDelay` (1800) and `startDelay` (3600) bound a graceful stop or a start, not the detection of a lost node: left alone.
- **Consequences:** a short blip (a kubelet restart of 30-80 s) now evicts the instances of that node. A primary fails over (a replica is promoted, the old primary comes back as a replica, about 12 s without a write); a replica instance waits on its local-path volume (pinned to the node) and rejoins when the node is back, or is re-created by `task cluster-replace-node`. PodDisruptionBudgets do not apply to taint evictions. A short toleration does not change what Longhorn does (the CNPG volumes are local-path, no attach/detach on a new node).
- **UNVERIFIED:** that CNPG 1.30.1 promotes on the pod deletion that the eviction causes and not earlier or later (it did so in lab 21 at the eviction, from observation, not from the operator code); the 60 s figure (derived from two measurements); that no split brain appears on a partitioned-but-alive node (needs a partition drill, not a power-off).

### Other stateful workloads: nothing to change, and why

An eviction only matters where something reacts to the pod being deleted. OpenBao (raft, 3 members), Loki and VictoriaMetrics (replicated) have a local-path volume pinned to a node: an evicted pod stays Pending until the node returns, so a shorter toleration frees nothing. Zitadel, the Gateway pods, Kyverno, ESO, cert-manager, metrics-server and kube-state-metrics are removed from their Service endpoints when the node turns NotReady (about 40 s), and have a second replica: eviction time does not change what a client sees. The CNPG operator takes over through its 15 s leader lease, not through eviction.

A cluster-wide Kyverno mutation of Deployments and StatefulSets (short tolerations) was **not** chosen: Kubernetes adds its 300 s default tolerations itself (DefaultTolerationSeconds), a mutation would have to replace them, the platform would depend on Kyverno being up to create any pod, and one blip would evict every pod of a node. Changing the apiserver defaults (`default-unreachable-toleration-seconds`) has the same blast radius. Per-workload tolerations are explicit, testable and limited to the two Clusters.

### Zone spread (apps #155)

Every two-or-more-replica platform workload names a zone: a `ScheduleAnyway` spread or a preferred anti-affinity term on `topology.kubernetes.io/zone` next to the hostname one (cert-manager, ESO, istiod, metrics-server, Kyverno admission, Zitadel, Grafana, OpenBao, Loki, the VictoriaMetrics components, the Gateway through its Kyverno mutation, kube-state-metrics). It costs no request and never blocks a pod (a lost zone leaves fewer zones than replicas). OpenBao keeps its required hostname spread. `scripts/check-ha.py` prints a `warn:` line for every workload with two or more replicas and no zone term, and a coverage line; it is a warning list, not an error. A spread only decides where a NEW pod goes: pods already placed (or pinned by a local-path volume) move only when deleted.

### kube-state-metrics, two replicas (apps #156)

Two pods, not autosharding (a shard holds only part of the objects, so losing one blinds that part). Both export the same series: the scrape drops the target label `pod` and, on scraped series only, `instance`, and vmstorage and vmselect run `-dedup.minScrapeInterval=30s`, so `sum()` and `count()` in the rules do not double. `KubeStateMetricsDuplicated` fires if that breaks (a node with two `kube_node_info` series). CPU requests: +10 m, the audited Deployments and StatefulSets 3005 m -> 3015 m (budget 3300 m). The loss group is evaluated every 30 s instead of 1 m; the `for` windows stay (a control plane reboots in 92-115 s). Expected first page for a node loss, UNVERIFIED until the drill: `FailureDomainLost` about 1 min 40 s - 2 min, `NodeScrapeDown` about 4 min 30 s - 4 min 40 s, `NodeUnreachable` 6 min or more, now also when kube-state-metrics sat on the lost node.

### Lab pass criteria (worker power-off with the primary on it)

Power the worker off through the provider API and leave it off for 5 minutes: the longest write gap of the 1 Hz database writer (fresh connection per sample) is at most 75 s (expected about 60 s; the 30 s target is unmet by design, see above); a replica is promoted within 45 s of the node turning Unknown (the eviction is 30 s after the taint); the data facts are identical; the old primary rejoins as a replica without a manual step after power-on (or after `cluster-replace-node`, see OpenAether-infra). With kube-state-metrics on the lost node: `NodeUnreachable` still fires. `count(kube_node_info)` equals the number of nodes with both kube-state-metrics pods up and with one killed. `zonespread` shows no workload with all replicas in one zone on a fresh install (a preference: record the exceptions).
