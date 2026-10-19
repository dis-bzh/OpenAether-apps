#!/usr/bin/env python3
"""The Gateway Deployment must keep two replicas and a disruption budget (lab 15c: one drained node cut all ingress for 74 s).

Istio creates `<gateway>-<class>` with one replica and no PDB. An HPA with minReplicas >= 2 and a PDB selecting the Gateway's
pods fix that. Static: it proves the manifests, not the scheduler's spreading.
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "apps/base/services-gateway"


def main():
    docs = [d for d in yaml.safe_load_all((BASE / "gateway-ha.yaml").read_text()) if d]
    gw = [d for d in yaml.safe_load_all((BASE / "gateway-openaether.yaml").read_text()) if d and d.get("kind") == "Gateway"][0]
    target = f"{gw['metadata']['name']}-{gw['spec']['gatewayClassName']}"
    kust = (BASE / "kustomization.yaml").read_text()
    bad = 0
    if "gateway-ha.yaml" not in kust:
        print("FAIL kustomization.yaml does not list gateway-ha.yaml"); bad += 1
    hpa = next((d for d in docs if d["kind"] == "HorizontalPodAutoscaler"), None)
    pdb = next((d for d in docs if d["kind"] == "PodDisruptionBudget"), None)
    if not hpa or hpa["spec"]["scaleTargetRef"]["name"] != target or hpa["spec"]["minReplicas"] < 2:
        print(f"FAIL no HPA with minReplicas >= 2 on {target}"); bad += 1
    if not pdb or pdb["spec"].get("minAvailable", 0) == 0 and pdb["spec"].get("maxUnavailable") != 1:
        print("FAIL no PDB allowing one disruption"); bad += 1
    elif pdb["spec"]["selector"]["matchLabels"].get("gateway.networking.k8s.io/gateway-name") != gw["metadata"]["name"]:
        print("FAIL the PDB does not select the Gateway's pods"); bad += 1
    print("ok" if not bad else f"{bad} failure(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
