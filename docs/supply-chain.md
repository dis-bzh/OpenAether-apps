# Supply chain: what is inventoried, what is checked, what is only trusted

> What stands between a hostile or broken upstream release and a pod of this platform, and what does not.
> Addresses ANSSI SSDLC CICD-AIP-01a/-01b/-01c, CONT-REC05 and SBOM25-1/-2/-3 in part. Static and mocked checks only:
> nothing here has run on GitHub or a cluster yet. OpenAether-infra has the same page for its side.

## The inventory (SBOM)

`scripts/dev/gen-sbom.py` writes a CycloneDX 1.5 file from the **rendered** DAG (`scripts/dev/render-dag.py`: what Flux
applies, not the source tree): every Helm chart a HelmRelease installs, every image the manifests name, and with `--helm`
the images the charts render with the release's own values (a `valuesFrom` target gets a placeholder, as Flux fills it
at run time). The `SBOM` workflow runs it weekly and on every published release, attaches it as
`openaether-apps-<tag>.cdx.json`, and scans each image with Trivy: a CRITICAL finding with a fix upstream fails.
It does not read image layers (Trivy does), the images controllers create at run time (Cluster API machines, CNPG
pods), or what the child clusters' own profile deploys. 14 images come from the manifests and about 38 with the
charts on 2026-10-14.

- **VEX:** a finding that does not apply is an OpenVEX statement in `.vex/openaether-apps.openvex.json`
  (`not_affected` with a justification, one product purl each); the scan reads the file once it has a statement.
  The file is empty today: nothing has been scanned yet.

## What is checked on every pull request

- `scripts/check-image-pins.py`: no image floats (no tag, or `latest`/`main`...). It prints how many are pinned by
  tag only (14 of 14 today). `--require-digest` is the next step, after Renovate has rewritten the lines.
- `kubeconform` (strict) and `trivy config` on the rendered DAG, with the accepted findings in `.trivyignore.yaml`.
- Renovate: `pinDigests` for the images of the hand-written bricks (`backup`, `foundation`), so a bump writes
  `tag@sha256:...`. The Postgres images stay on the custom managers. Vendored dumps stay a human re-vendor.

## The registry allow-list

`restrict-image-registries` (Kyverno, **Audit**) lists organisations, not registries: ghcr.io, quay.io and Docker Hub
accept anything any account publishes. The list is the image inventory above. `gcr.io/*` and the nonsensical
`docker.io/registry.k8s.io/*` were dropped (nothing in the inventory uses them); `registry.k8s.io/*` stays whole (a
registry the Kubernetes project controls). It reports, it blocks nothing: read `kubectl get polr -A` on a cluster
before switching it to Enforce, then Enforce for the application namespaces first.

## Signatures: what upstream gives us (measured 2026-10-14, cosign 3.1.3, keyless)

The audit proposed a Kyverno `verifyImages` policy for the signed upstreams (fluxcd, cilium, kyverno, cert-manager,
external-secrets). Checked against the versions **pinned here**:

| Image | Keyless signature verifies |
|---|---|
| `ghcr.io/fluxcd/*` v1.5.5, v1.8.5 | yes, subject `https://github.com/fluxcd/gha-workflows/.github/workflows/controller-release.yaml@refs/tags/v0.9.0` |
| `ghcr.io/external-secrets/external-secrets:v1.2.0` | yes, subject `https://github.com/external-secrets/external-secrets/.github/workflows/release.yml@refs/heads/main` |
| `ghcr.io/kyverno/kyverno:v1.12.1` | **no signature at all** |
| `ghcr.io/cloudnative-pg/cloudnative-pg:1.23.1` | **no signature at all** |
| `quay.io/jetstack/cert-manager-controller:v1.15.3` | a `.sig` tag exists but no keyless signature matches (key-signed or another scheme) |
| `docker.io/openbao/openbao:2.7.1` | a signature verifies, identity not recorded here |
| `docker.io/restic/restic:0.18.0`, `grafana/grafana:11.3.1`, `ghcr.io/zitadel/zitadel:v4.14.0`, `istio/pilot:1.24.2` | no keyless signature |

So a `verifyImages` policy in Enforce would admit only a minority of what runs, and the largest parts of the audit's
list cannot be verified at these versions. **No policy is shipped.** The practical order is: pin by digest (Renovate,
above), upgrade the unsigned lines (the end-of-support calendar), and only then verify the images that are signed,
Audit first, with Kyverno's egress to the Sigstore endpoints proven (the Kyverno namespace's network policy would
have to allow it), `failurePolicy: Ignore`, `mutateDigest: false`. Flux's own `spec.verify` on an OCIRepository is the
other route, and isolates a failure to one Kustomization.

## Not covered

- Layers inside the images (Trivy reads them weekly, and only the CRITICAL-with-a-fix ones fail).
- The Talos boot image and the bootstrap manifests (OpenAether-infra).
- Charts are pinned by version, not by digest, and fetched over HTTPS from the chart repositories.
- Nothing verifies a signature anywhere yet. Git commits: an opt-in Flux verification and a warning-only CI check exist (default off, never run on a cluster), see [signed-commits.md](signed-commits.md).

## Repository settings

`repo-settings.yml` (daily and on every push to `main`) runs `scripts/dev/check-required-checks.sh`, the script
OpenAether-infra uses (`--no-sandbox`: no cloud workflow here). It compares what GitHub enforces with what happens: the
required checks of `main` against the check runs of the last merged PR, a ruleset on every tag with no bypass (infra
pins this repository by tag), pull request and no force-push on `main`, private vulnerability reporting on. What a single
maintainer cannot meet yet (0 approving reviews, signed commits, admin bypass) is a warning; `--strict-main` makes it a
failure. Its 80 cases run against a canned API on every pull request.

- **Not done here:** `.github/rulesets/*.json`, the rulesets as code. They are the live ones exported by an admin, and
  this change could not read them. Once exported the same script compares them field for field:
  `gh api repos/dis-bzh/OpenAether-apps/rulesets` lists the ids, `gh api repos/dis-bzh/OpenAether-apps/rulesets/<id> >
  .github/rulesets/<name>.json` writes one (volatile fields are ignored). To change a ruleset: change it on GitHub,
  re-export in the same PR.
- **The first run may be red.** It has never run on this repository: if there is no ruleset on tags, or private
  vulnerability reporting is off, it says so. That is the finding, not a broken job.
