# End-of-support calendar

> Written to answer ANSSI hygiene measures 34 and 35 (audit ids HYG-R34, HYG-R35): know when each pinned
> component stops receiving security fixes, and have a plan. It is a snapshot with a check, not a feed:
> the dates were read on 2026-10-14 from endoflife.date and, for CloudNativePG, the project's own support page.
> Where no schedule was found the cell says `unknown`; nothing was assumed.

The source is [`eol-calendar.yaml`](eol-calendar.yaml); the table below is generated from it
(`python3 scripts/check-eol.py --markdown`). Each row names the file and pattern the pin is read from, so
`scripts/check-eol.py` (CI job `Flux DAG`) fails when a pin changes and its row does not, or when a HelmRelease
or a vendored dump has no row.

<!-- eol-table:begin -->
| Component | Pinned | Upstream line | End of support | Waiver until | Review |
|---|---|---|---|---|---|
| Istio base (CRDs) (`istio`) | 1.24.2 | Istio 1.24 | 2025-06-24 | 2026-12-31 | 2026-12-14 |
| Istio control plane (`istio`) | 1.24.2 | Istio 1.24 | 2025-06-24 | 2026-12-31 | 2026-12-14 |
| Istio CNI (`istio`) | 1.24.2 | Istio 1.24 | 2025-06-24 | 2026-12-31 | 2026-12-14 |
| Istio ztunnel (`istio`) | 1.24.2 | Istio 1.24 | 2025-06-24 | 2026-12-31 | 2026-12-14 |
| cert-manager (`cert-manager`) | v1.15.3 | cert-manager 1.15 | 2025-02-03 | 2026-12-31 | 2026-12-14 |
| longhorn (`storage`) | 1.12.1 | Longhorn 1.12 | 2027-08-14 |  | 2026-12-14 |
| grafana (`observability`) | 8.6.4 | Grafana 11.3 (chart appVersion) | 2025-07-22 | 2026-12-31 | 2026-12-14 |
| loki (`observability`) | 6.25.0 | Loki 3.3 (chart appVersion) | 2025-04-17 | 2026-12-31 | 2026-12-14 |
| alloy (`observability`) | 0.11.0 | Alloy 1.6 (chart appVersion) | unknown |  | 2026-12-14 |
| alloy-audit (second Alloy release, API audit log) (`observability`) | 0.11.0 | Alloy 1.6 (chart appVersion) | unknown |  | 2026-12-14 |
| zitadel (`identity`) | 10.0.2 | Zitadel v4.14.0 (chart appVersion) | unknown |  | 2026-12-14 |
| victoria-metrics-operator (`observability`) | 0.65.1 | VictoriaMetrics operator v0.72.0 (chart appVersion) | unknown |  | 2026-12-14 |
| kube-state-metrics (`observability`) | 8.6.0 | kube-state-metrics 2.20.0 (chart appVersion) | unknown |  | 2026-12-14 |
| prometheus-node-exporter (`observability`) | 4.60.0 | node_exporter 1.12.1 (chart appVersion) | unknown |  | 2026-12-14 |
| metrics-server (`platform`) | 3.14.0 | metrics-server 0.9.0 (chart appVersion) | unknown |  | 2026-12-14 |
| cluster-api-operator (`cluster-api-operator`) | 0.27.0 | cluster-api-operator 0.27.0 | unknown |  | 2026-12-14 |
| minio (`storage`) | 5.2.0 | MinIO RELEASE.2024-04-18 (chart appVersion) | unknown |  | 2026-12-14 |
| Kyverno (vendored dump) (`kyverno`) | 1.12.1 | Kyverno 1.12 | 2025-07-31 | 2026-12-31 | 2026-12-14 |
| CloudNativePG operator (vendored dump) (`cnpg`) | 1.23.1 | CNPG 1.23 | unsupported | 2026-12-31 | 2026-12-14 |
| PostgreSQL (zitadel cluster) (`cnpg`) | 18.6 | PostgreSQL 18 | 2030-11-14 |  | 2026-12-14 |
| PostgreSQL (grafana cluster) (`cnpg`) | 16.3 | PostgreSQL 16 | 2028-11-09 |  | 2026-12-14 |
| External Secrets Operator (vendored dump) (`external-secrets`) | 1.2.0 | ESO v1.2 | unknown |  | 2026-12-14 |
| OpenStack Resource Controller (vendored dump) (`orc`) | 2.4.0 | ORC v2.4 | unknown |  | 2026-12-14 |
| Gateway API CRDs (vendored dump) (`platform/gateway-api`) | 1.2.1 | Gateway API v1.2 | unknown |  | 2026-12-14 |
| local-path-provisioner (vendored dump) (`platform/local-path-provisioner`) | 0.0.37 | local-path v0.0.37 | unknown |  | 2026-12-14 |
| Flux controllers pushed to child clusters (vendored dump) (`cluster-api-clusters/flux-gotk`) | 2.8.8 | Flux 2.8 | not-yet |  | 2026-12-14 |
| restic (`backup`) | 0.19.1 | restic 0.19 | unknown |  | 2026-12-14 |
| OpenBao (snapshot job image; the server is pinned in infra's bootstrap) (`backup`) | 2.7.1 | OpenBao 2.7 | not-yet |  | 2026-12-14 |
| Cilium on child cluster edge-1 (`clusters`) | 1.19.2 | Cilium 1.19 | not-yet |  | 2026-12-14 |
| Talos cloud controller manager on child cluster edge-1 (`clusters`) | 0.5.5 | talos-ccm 0.5 | unknown |  | 2026-12-14 |
| Cilium on child cluster edge-2 (`clusters`) | 1.19.2 | Cilium 1.19 | not-yet |  | 2026-12-14 |
| Talos cloud controller manager on child cluster edge-2 (`clusters`) | 0.5.5 | talos-ccm 0.5 | unknown |  | 2026-12-14 |
| Cilium on child cluster edge-3 (`clusters`) | 1.19.2 | Cilium 1.19 | not-yet |  | 2026-12-14 |
| Talos cloud controller manager on child cluster edge-3 (`clusters`) | 0.5.5 | talos-ccm 0.5 | unknown |  | 2026-12-14 |
<!-- eol-table:end -->

## What the check says and does not say

- **Errors** (the job fails): a pin that differs from its row, a pattern that no longer matches, a HelmRelease or
  a vendored dump (`renovate.json5` `ignorePaths`) with no row, an unreadable date.
- **Warnings** (annotations, the job stays green): past upstream support with no live waiver, an expired waiver,
  an overdue review, an end of support within 90 days, a snapshot over 120 days old. They change with the date,
  not with a pull request, so they do not turn an unrelated PR red. `--strict` turns them into errors; that is
  for after the components below are upgraded or their waivers are the only thing left.
- **A waiver is an accepted risk, not a fix.** Six lines are past upstream support today (Istio 1.24,
  cert-manager 1.15, Kyverno 1.12, the CloudNativePG operator 1.23, Grafana 11.3, Loki 3.3). Each carries a waiver
  to 2026-12-31 with the reason it has not moved. The date is a proposal of this change, to be set by the owner.
- **The upgrades themselves are not done here.** Each hop is a cluster exercise (see the fix review of the audit:
  Istio ambient with Cilium chaining, cert-manager CRDs and rotationPolicy, Kyverno webhooks, CNPG operator with
  a restore first); one pull request per minor, validated on a cluster before merge.
- The `vendored from vX` comments in some `kustomization.yaml` files are Renovate *tracking* comments and say
  the latest release, not what is vendored (the CNPG one says v1.30.0 over a 1.23.1 dump). The calendar reads
  the dump itself.
- Talos, Kubernetes and the infrastructure pins are tracked in OpenAether-infra (`docs/upgrade.md`,
  `docs/patching.md`), not here.

## Refreshing it

1. For each row, re-read the end-of-support date: `https://endoflife.date/api/<product>.json`, or the project's
   support page where endoflife.date has no entry. Update `eol`, `cycle`, `snapshot`, `next_review`.
2. After a bump (Renovate PR), set `pin` to the new version and re-read the dates for that line.
3. `python3 scripts/check-eol.py --markdown`, paste the table between the markers, then
   `python3 scripts/check-eol.py --check-doc`.
