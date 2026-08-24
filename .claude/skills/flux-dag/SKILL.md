---
name: flux-dag
description: Changing the Flux DAG, the profiles or the common manifests in OpenAether-apps. Use when adding a component, editing dependsOn edges, touching apps/flux/**, or before opening a PR here.
---

# The Flux DAG

Run `task apps-validate` **from the OpenAether-infra checkout** before opening a
PR. It checks the DAG's integrity, that the `pick.py` profiles still match it,
that the children's Cilium values agree with the render script's, and that the
`.claude/skills` shared with infra have not drifted.

## What this repository is

Common manifests and the Flux wiring. Business manifests live in each
application's own repository. The fixed floor for a cluster is CNI (Cilium) and
Flux; everything here is picked per cluster — mesh or not, Zitadel without
OpenBao, observability without mesh. A component that cannot be left out is a
design mistake, not a feature.

## Edges

`dependsOn` is the only thing that orders reconciliation. Three missing edges
have already been found, and they only bite an ad-hoc pick — the default profile
happened to order them correctly by accident. When you add a component, ask what
it reads at startup, not what it reads eventually.

**Four ways the DAG has bitten, each once.**

- **Flux substitution applies to the whole Kustomization render** — it blanks bare
  shell variables. Isolate `substituteFrom` in a brick that contains no script.
- **An operator and its own CRs need two Kustomizations.** A bundle carrying both
  is rejected at dry-run, because the CRDs do not exist yet.
- **A failed Job is permanent under Flux.** It re-applies the same spec and never
  restarts a finished Job, so a bootstrap Job must WAIT for its dependency rather
  than fail and rely on a retry.
- **A Kustomize `namespace:` overrides every resource**, including those of a
  referenced base. Validate the directory (`kubectl apply -k`), never a file.

## Two formats, one value

Cilium's settings exist twice: as `--set` flags in the infra render script, and
as `values:` in a HelmRelease for CAPI children. They drift silently — it cost
two child clusters when `ipam.mode` was lost. `check-cilium-parity.py` compares
them; it is not optional.

## Single-replica components

A component with one replica under a PodDisruptionBudget requiring one available
replica pins its node forever: it can never be evicted, so the node can never be
drained, so a rolling upgrade reboots it with that pod aboard. istiod was exactly
this. Either run two, or declare a budget that permits a disruption.

Not every zero-disruption budget is a mistake: CNPG guards each cluster's primary
until a switchover, and OpenBao's `minAvailable: 2` of 3 raft replicas is correct.
Know which kind you are looking at before you loosen anything.

## This repository is public

No real data, ever — see the `public-repo-safety` skill, which is duplicated here
from OpenAether-infra and must stay identical.
