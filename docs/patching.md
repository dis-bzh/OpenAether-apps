# Patching policy: what is pinned, how a fix is noticed, how long it may wait

> Written to answer ANSSI hygiene measure 34 (audit id HYG-M34): a defined update policy, security fixes applied
> within a month. It states a target and the signals that feed it. It is not a measurement: nothing here has been
> exercised on a cluster, and **the target is not met today** (see the last section).
> The infrastructure side (Talos, Kubernetes, providers, bootstrap manifests) is in OpenAether-infra's
> `docs/patching.md`; this page covers what Flux deploys from this repository.

## The rule

A security fix released for a component this repository pins is **merged, or waived in writing, within 30 days
of its release.** Faster (target: 7 days) when the advisory is exploited in the wild or sits on a path reachable
from the internet (the Gateway and the mesh, cert-manager and the OpenBao PKI, Zitadel).

A waiver is a row in [`eol-calendar.yaml`](eol-calendar.yaml) (or a GitHub issue for a fix on a supported line)
naming the component, the reason the fix cannot land yet and an **expiry date**; past it, the waiver is a
missed deadline and the weekly `End of support` workflow fails.

## What is pinned and who bumps it

| Pin | Where | Bumped by | Applied to a cluster by |
|---|---|---|---|
| Helm charts (Istio, cert-manager, Longhorn, Zitadel, Grafana, Loki, Alloy, VictoriaMetrics, CAPI operator...) | `HelmRelease` `spec.chart.spec.version` | Renovate (`flux` manager), a PR | Flux, after the merge |
| Vendored upstream dumps (Kyverno, CNPG operator, External Secrets, ORC, Gateway API CRDs, local-path, Flux for child clusters) | `ignorePaths` of `renovate.json5` | Renovate bumps a *tracking comment* only; **a human re-vendors the file** | Flux, after the merge |
| Container images in our own manifests (backup jobs, Postgres, restic, OpenBao) | `image:` lines | Renovate where a manager matches the line | Flux, after the merge |
| CI actions, pre-commit hooks, tool versions | workflows, `.pre-commit-config.yaml` | Renovate | merged PR |

Every bump is a reviewed PR, never auto-merged: Flux only reconciles what a person merged.

## How a fix is noticed

1. **Renovate `vulnerabilityAlerts` and `osvVulnerabilityAlerts`** open a PR for a pin with a published advisory,
   outside the weekly window and the 7-day release age (Renovate's default for that block), labelled `security`.
   They need the repository's Dependabot alerts on and the Renovate App allowed to read them. For a vendored dump
   the PR bumps only the comment: it is the reminder, the re-vendoring is by hand.
2. **The end-of-support calendar** ([`eol-calendar.md`](eol-calendar.md)): a component whose line upstream no
   longer patches receives no security fix at all, whatever the advisory feed says. CI annotates it on every
   pull request and `End of support` fails weekly.
3. **The weekly `SBOM` workflow** scans every image the DAG deploys, charts included, with Trivy and fails on a
   CRITICAL with a fix upstream ([`supply-chain.md`](supply-chain.md)); it reads the layers, which Renovate cannot.
4. **Renovate's weekly PRs** themselves (Monday before 6am). Read them.

## What this does not do

- It applies nothing: Istio, cert-manager, Kyverno and CNPG hops each need a cluster exercise (one minor per PR).
- One maintainer reviews and merges: the deadline is a commitment, not a control with a second person behind it.
- Renovate and the calendar see only what they were told to read. A component pulled in another way is invisible
  to both; `scripts/check-eol.py` fails on a HelmRelease or a vendored dump it has no row for, which closes that
  for charts and dumps, not for images inside charts.
- Images are tracked by tag until Renovate's `pinDigests` has rewritten them (see [`supply-chain.md`](supply-chain.md)):
  a re-pushed tag is not noticed meanwhile.

## Upgrade campaign: order and gates

The components past upstream support are upgraded **one component per PR, one supported minor per hop**, each hop
validated on a staging cluster before the next (never in one Renovate batch). The order, and why:

1. **cert-manager** to the newest supported minor. From 1.18 the default `rotationPolicy` of a `Certificate` flips from
   `Never` to `Always`: every `Certificate` of this repository must name its policy first (the Gateway and OpenBao
   leaves already do; `foundation/vault-ca` keeps `Never` on purpose), then the baseline of issued certificates is
   recorded, then the hops. CRDs ride `crds: CreateReplace`; webhook and cainjector must be Ready before
   `13b-cert-manager-issuers`.
2. **Gateway API CRDs** (vendored v1.2.1), to the release the target Istio expects. v1.2.1 is what Istio 1.24 is built against and
   the highest this repository can justify with it: with the v1.5 CRDs installed, Istio up to 1.29 never becomes Ready (istio/istio#59443,
   the 1.24 CRD watcher has no maximum-version filter). The CRDs move to 1.5.x only with the Istio hop that supports them (1.30).
3. **Istio** (`base`, `istiod`, `istio-cni`, `ztunnel`), minor by minor, reading each upgrade note; the control plane
   first, then the node-level `istio-cni` and `ztunnel`. No mesh TLS floor is set in
   `meshConfig` (`tlsDefaults` does not apply to ISTIO_MUTUAL); after the hop, re-check with `openssl s_client` that the
   Gateway listener still refuses TLS 1.1.
4. **Kyverno** (re-vendor the dump, then re-run `scripts/dev/test-kyverno-policies.sh` with the matching CLI),
   **CNPG operator** (re-vendor), then the **PostgreSQL images** (`scripts/dev/check-pg-dump-version.sh` keeps the dump
   image at or above every `Cluster`), then **Longhorn**.

What is *not* done offline, and why: a hop changes CRDs and node-level DaemonSets whose behaviour cannot be shown
without a cluster, `helm rollback` does not roll CRDs back, and the upstream tags and digests were not reachable when
this was written. The per-hop commands, expected observations and rollback are in the maintainers' lab checklist
(section B28). When the last hop is merged, the `End of support` check can be made blocking by removing the waivers
from [`eol-calendar.yaml`](eol-calendar.yaml).

## Phase 1 (0.3.0): independent bumps

Done outside the order above because they touch nothing the order depends on, one commit per component: local-path-provisioner v0.0.37
(re-vendored), prometheus-node-exporter 4.60.0, kube-state-metrics 8.6.0, metrics-server 3.14.0, restic 0.19.1, Gateway API CRDs v1.2.1 (not
1.5.x, see item 2). Offline only: the rendered Helm output at the old and new pins was diffed, the scripts under `scripts/dev/` that touch each
component pass; the per-bump observations and rollbacks are in the maintainers' lab checklist (section UPGRADE-PHASE1). `helm rollback` does not
roll CRDs back, and the Gateway API CRDs are not a Helm release at all.

## Phase 2: the pins that were past support

Same rules, one commit per component, offline only. Moved: Longhorn 1.12.1, cert-manager v1.21.2, Kyverno 1.19.1 and CNPG operator 1.30.1 (both
re-vendored), grafana-db PostgreSQL 16.15, External Secrets 2.13.0 (re-vendored from the chart), Flux for the children 2.9.6, cluster-api-operator
0.29.0, ORC v2.6.0, victoria-metrics-operator 0.68.1, Alloy 1.13.1, Zitadel chart 10.4.0 (v4.19.2), Loki 18.16.2 and Grafana 14.0.1 (both from the
`grafana-community` chart repository, the old one no longer carries them), and the children's Cilium 1.19.9. **These pins describe a fresh install.**
A cluster that already runs the old versions follows each project's own hop rule (one minor at a time for Longhorn, cert-manager and CNPG; a CNPG
Backup before Zitadel or Grafana; the order above for the rest): the observations, rollbacks and the data-safety gates are the lab checklist's, not
this page's. Not moved, with the reason: Istio (waived, needs the Gateway API CRDs and a chart repository decision), MinIO (dev profile, no maintained
upstream), OpenBao (stays 2.7.1), talos-cloud-controller-manager on the children (0.5.8 follows Talos 1.14, the children run 1.13).

Two follow-ups these bumps create and that nothing in the repository enforces yet: CNPG 1.30.x is the last line with the in-tree `barmanObjectStore`
(both Clusters use it; 1.31 removes it), and Kyverno 1.19.x is the last with `kyverno.io/v1` policies (1.20 removes them).

## Longhorn in-place hops (a running cluster pinned at 1.9.x)

A **fresh install** takes the pin (1.12.1) directly. A cluster already running 1.9.x cannot: Longhorn only upgrades one
minor at a time (the chart's pre-upgrade job refuses a skip) and does not support downgrades. The path is
**1.9.2 -> 1.10.2 -> 1.11.3 -> 1.12.1**, one `version:` change per PR, each hop finished (below) before the next.
Avoid 1.10.0 and 1.11.0 (manager and instance-manager regressions fixed by later patches). 1.13.0 needs Kubernetes
1.34 or later and is not taken. The `HelmRelease` has `retries: 0` for install and upgrade and `timeout: 15m`: a failed
upgrade is **not** rolled back automatically (the refused downgrade would strand the release); a person reads the
failed pre-upgrade job and fixes forward. The only real rollback is restoring volumes from backup.

Preconditions, to check before every hop (none was run from this repository; they need a cluster):

1. Every CRD has only `v1beta2` in `status.storedVersions` (required before 1.10; the 1.10.0 release notes carry the migration).
2. The backup target is Available, every volume has a backup under 24 hours old, and one restore to a throwaway PVC
   was done on the current version (keep `longhorn-encryption-secret` for the encrypted volumes).
3. All volumes `healthy` with 2 running replicas, nothing rebuilding, nodes Ready; `/var/mnt/longhorn` has the 15 % free
   `storageMinimalAvailablePercentage` plus room for the temporary doubled replicas of a live engine upgrade.
4. No Talos reboot, CNPG operator roll or Kubernetes upgrade in the same window (with 2 replicas a drained node leaves one).
5. `talosctl etcd snapshot` and a Longhorn `SystemBackup` just before.
6. After the manager hop, upgrade the engine image of every volume (live when attached and healthy, offline when
   detached) and wait for the old instance-manager pods to go; whether the next hop refuses volumes left on an old
   engine image is not documented upstream, so treat it as a precondition.

Behaviour changes met between 1.9 and 1.12 that touch this repository: the chart renders `NetworkPolicy` objects
by default (`networkPolicies.restrictInternalTraffic`; set to `false` here so the brick's `CiliumNetworkPolicy` stays the
single authority); the conversion-webhook `Service` is gone with the v1beta1 API; V2 backing images are removed
(the repository uses the V1 engine only). `defaultSettings.orphanAutoDeletion` is not a key of the chart (it reads
`orphanResourceAutoDeletion`) and was already ignored at 1.9.2, so orphan auto-deletion is not enabled; left as is.

## Where it stands (2026-10-16)

After phase 2 the pins in [`eol-calendar.yaml`](eol-calendar.yaml) are on supported lines except Istio 1.24 (EOL 2025-06-24, waived to 2026-12-31),
MinIO (dev profile only) and the rows whose upstream publishes no schedule. The 30-day rule is therefore **not met for Istio**, and the new pins have
**never run on a cluster**: the checks behind them are renders, schema checks and real-software runs on a workstation (see the CHANGELOG). Whether the
releases still pinned (Istio 1.24) have unpatched advisories was **not checked**; the dependency graph and the OSV feed the Renovate blocks above read
are where to look.
