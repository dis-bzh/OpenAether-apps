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
2. **Gateway API CRDs** (vendored v1.1.0), to the release the target Istio expects.
3. **Istio** (`base`, `istiod`, `istio-cni`, `ztunnel`), minor by minor, reading each upgrade note; the control plane
   first, then the node-level `istio-cni` and `ztunnel`. `meshConfig.tlsDefaults.minProtocolVersion` is already
   `TLSV1_2` and must still apply to the Gateway API gateway afterwards.
4. **Kyverno** (re-vendor the dump, then re-run `scripts/dev/test-kyverno-policies.sh` with the matching CLI),
   **CNPG operator** (re-vendor), then the **PostgreSQL images** (`scripts/dev/check-pg-dump-version.sh` keeps the dump
   image at or above every `Cluster`), then **Longhorn**.

What is *not* done offline, and why: a hop changes CRDs and node-level DaemonSets whose behaviour cannot be shown
without a cluster, `helm rollback` does not roll CRDs back, and the upstream tags and digests were not reachable when
this was written. The per-hop commands, expected observations and rollback are in the maintainers' lab checklist
(section B28). When the last hop is merged, the `End of support` check can be made blocking by removing the waivers
from [`eol-calendar.yaml`](eol-calendar.yaml).

## Where it stands (2026-10-14)

The 30-day rule is **not met** for components already past upstream support: Istio 1.24 (EOL 2025-06-24),
cert-manager 1.15 (2025-02-03), Kyverno 1.12 (2025-07-31), the CloudNativePG operator 1.23 (not in the project's
supported table), Grafana 11.3 and Loki 3.3 (via their chart pins). They are listed in the calendar with a waiver
to 2026-12-31 and the reason each is still where it is. Longhorn 1.9 reaches its end of support on 2026-11-27.
Whether those releases have unpatched published advisories was **not checked**; the dependency graph and the
OSV feed the Renovate blocks above read are where to look.
