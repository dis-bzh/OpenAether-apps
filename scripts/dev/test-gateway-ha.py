#!/usr/bin/env python3
"""The Gateway is the only way in for every request: three replicas, spread over nodes and zones, a disruption budget, a drain-safe
termination, and the Service on externalTrafficPolicy Cluster (owner decision 2026-10-28).

Istio creates `<gateway>-<class>` with one replica and no PDB (lab 15c: one drained node cut all ingress for 74 s). So:
  - an HPA with minReplicas >= 3 and a PDB that allows ONE disruption and leaves at least two pods;
  - the Kyverno mutation spread-gateway-pods puts a HARD (DoNotSchedule, maxSkew 1, nodeTaintsPolicy Honor) spread on the hostname and
    on the zone on every Gateway pod; Honor is what keeps a hard rule from stranding a pod on a cordoned or lost node;
  - the same mutation makes a planned drain drop no connection behind the cloud load balancer: TERMINATION_DRAIN_DURATION_SECONDS
    above what the load balancer needs to drop a member (check every 5 s, 3 checks, twice = 30 s), and a
    terminationGracePeriodSeconds that covers preStop + drain;
  - the public NodePort Service stays `Cluster` (`Local` was rejected: it turns a drain into failed connections on the load balancer).
Static: it proves the manifests, not the scheduler's spreading or the drain on a cloud (infra docs/resilience.md lists the drill).
Usage: python3 -I scripts/dev/test-gateway-ha.py
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "apps/base/services-gateway"
POLICY = REPO / "apps/base/kyverno-policies/spread-gateway-pods.yaml"
LB_CHECK_S = 5 * 3 * 2  # interval x checks x 2 (owner, 2026-10-28)
MIN_REPLICAS = 3
bad = 0


def check(cond, msg):
    global bad
    if not cond:
        print("FAIL " + msg)
        bad += 1


def main():
    docs = [d for d in yaml.safe_load_all((BASE / "gateway-ha.yaml").read_text()) if d]
    gw = [d for d in yaml.safe_load_all((BASE / "gateway-openaether.yaml").read_text()) if d and d.get("kind") == "Gateway"][0]
    target = f"{gw['metadata']['name']}-{gw['spec']['gatewayClassName']}"
    check("gateway-ha.yaml" in (BASE / "kustomization.yaml").read_text(), "kustomization.yaml does not list gateway-ha.yaml")
    hpa = next((d for d in docs if d["kind"] == "HorizontalPodAutoscaler"), None)
    pdb = next((d for d in docs if d["kind"] == "PodDisruptionBudget"), None)
    check(hpa and hpa["spec"]["scaleTargetRef"]["name"] == target and hpa["spec"]["minReplicas"] >= MIN_REPLICAS,
          f"no HPA with minReplicas >= {MIN_REPLICAS} on {target}")
    check(hpa and hpa["spec"]["maxReplicas"] >= hpa["spec"]["minReplicas"] + 1, "the HPA cannot scale above its floor")
    if not pdb:
        check(False, "no PDB on the Gateway")
    else:
        check(pdb["spec"].get("maxUnavailable") == 1 or pdb["spec"].get("minAvailable") == MIN_REPLICAS - 1,
              "the PDB must allow exactly one disruption at the floor (maxUnavailable 1, or minAvailable floor-1)")
        check(pdb["spec"]["selector"]["matchLabels"].get("gateway.networking.k8s.io/gateway-name") == gw["metadata"]["name"],
              "the PDB does not select the Gateway's pods")

    svc = [d for d in yaml.safe_load_all((BASE / "service-nodeport.yaml").read_text()) if d and d.get("kind") == "Service"][0]
    check(svc["spec"].get("externalTrafficPolicy") == "Cluster", "the public NodePort Service must stay externalTrafficPolicy: Cluster")

    pol = yaml.safe_load(POLICY.read_text())
    rule = pol["spec"]["rules"][0]
    pod = rule["mutate"]["patchStrategicMerge"]["spec"]
    sel = {"gateway.networking.k8s.io/gateway-name": gw["metadata"]["name"]}
    check(rule["match"]["any"][0]["resources"]["selector"]["matchLabels"] == sel, "the mutation does not match the Gateway's pods")
    tsc = {c["topologyKey"]: c for c in pod.get("+(topologySpreadConstraints)", [])}
    for key in ("kubernetes.io/hostname", "topology.kubernetes.io/zone"):
        c = tsc.get(key)
        check(c is not None, f"no topologySpreadConstraint on {key}")
        if c:
            check(c["whenUnsatisfiable"] == "DoNotSchedule" and c["maxSkew"] == 1, f"the {key} spread must be hard with maxSkew 1")
            check(c.get("nodeTaintsPolicy") == "Honor", f"the {key} spread needs nodeTaintsPolicy Honor (a cordoned node is not an empty domain)")
            check(c["labelSelector"]["matchLabels"] == sel, f"the {key} spread does not select the Gateway's own pods")
    anti = pod.get("+(affinity)", {}).get("podAntiAffinity", {}).get("preferredDuringSchedulingIgnoredDuringExecution", [])
    check(any(t["podAffinityTerm"]["topologyKey"] == "kubernetes.io/hostname" for t in anti), "no hostname anti-affinity preference")

    proxy = next((c for c in pod.get("containers", []) if c["name"] == "istio-proxy"), {})
    env = {e["name"]: e["value"] for e in proxy.get("env", [])}
    drain = int(env.get("TERMINATION_DRAIN_DURATION_SECONDS", 0))
    sleep = proxy.get("lifecycle", {}).get("preStop", {}).get("sleep", {}).get("seconds", 0)
    tgp = pod.get("terminationGracePeriodSeconds", 0)
    check(drain > LB_CHECK_S, f"TERMINATION_DRAIN_DURATION_SECONDS {drain} must exceed the load balancer detection time {LB_CHECK_S} s")
    check(sleep > 0, "no preStop sleep on istio-proxy")
    check(tgp > sleep + drain, f"terminationGracePeriodSeconds {tgp} must exceed preStop {sleep} + drain {drain}")
    print("ok" if not bad else f"{bad} failure(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
