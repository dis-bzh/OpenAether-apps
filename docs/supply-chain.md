# Supply chain: what is inventoried, what is checked, what is only trusted

> What stands between a hostile or broken upstream release and a pod of this platform, and what does not.
> Addresses ANSSI SSDLC CICD-AIP-01a/-01b/-01c, CONT-REC05 and SBOM25-1/-2/-3 in part. Static and mocked checks, plus
> the `SBOM` workflow, which has run on GitHub (what was and was not run of its release attach is under "The inventory");
> nothing here has run on a cluster yet. OpenAether-infra has the same page for its side.

## The inventory (SBOM)

`scripts/dev/gen-sbom.py` writes a CycloneDX 1.5 file from the **rendered** DAG (`scripts/dev/render-dag.py`: what Flux
applies, not the source tree): every Helm chart a HelmRelease installs, every image the manifests name, and with `--helm`
the images the charts render with the release's own values (a `valuesFrom` target gets a placeholder, as Flux fills it
at run time). The `SBOM` workflow runs it weekly and on every pushed release tag, attaches it as
`openaether-apps-<tag>.cdx.json` to the DRAFT release (it creates the draft when there is none: an immutable release takes no
asset once published), and scans each image with Trivy: a CRITICAL finding with a fix upstream fails. The attach job needs
the inventory only, not the scans, so a red scan does not stop it. When the release is published the same workflow only
checks that the asset is there. The releases 0.3.2, 0.4.0 and 0.4.1 went out without it and cannot be given it now (a
published immutable release takes no asset).

`gen-sbom.py` does not read image layers (Trivy does), the images controllers create at run time (Cluster API machines,
CNPG pods), or what the child clusters' own profile deploys. On the 0.4.1 tag it lists 16 images from the manifests and
40 with the charts (`--helm`).

**Rebuilding an inventory by hand**, from a checkout of any tag, to keep elsewhere: the two commands of the workflow's
inventory step.

```sh
python3 scripts/dev/render-dag.py <dir>
python3 -I scripts/dev/gen-sbom.py --rendered <dir> --helm --version <tag> --output openaether-apps-<tag>.cdx.json
```

`render-dag.py` needs kubectl and PyYAML, `--rendered` is required, `--helm` needs helm and the network. Run on a copy of
the 0.4.1 tag on 2026-11-04 with fresh helm directories: rc 0, CycloneDX 1.5, 56 components, 40 images (without `--helm`:
32 components, 16 images).

**Releasing with the inventory (the procedure from 0.4.2).**

1. Push the signed tag. The workflow starts on it, builds the inventory from the tag, creates the DRAFT release when there is
   none (or uses a draft you made by hand, see the fallback below) and attaches the file.
2. Read the run before anything else: the `Attach to a draft release` job must be green. The scans and the VEX check run
   beside it and may be red (see the open items below) without stopping it. While the release is a draft, a failure costs
   nothing.
3. Write the notes and publish: `gh release edit <tag> --notes-file notes.md --draft=false`. On that event the workflow builds
   and scans nothing (the scan job has an explicit `if` for it): it runs the VEX check, as on every run, and one job that
   fails if the published release lacks the file. That run shares the concurrency group of the tag-push run
   (`cancel-in-progress: false`), so, by that setting (not observed), it waits for scans still running when you publish: the
   check comes later, it does not fail for that.

Never `gh release create` without `--draft`: the release would be published at once, immutable, and the attach job refuses a
published release with a clear message instead of trying. **Fallback**, if the tag-push run did not attach the file (it did
not start, or the job failed): create the draft by hand, then run the workflow on the tag.

```sh
gh release create <tag> --draft --verify-tag --title <tag> --notes-file notes.md
gh workflow run sbom.yml -f tag=<tag>
```

The second command runs from the default branch; that run checks out the tag and attaches to its draft. If the failed run
had already created the draft, skip the first command.

**Rung of the attach. This is the one place that states it** (`CHANGELOG.md` and the release notes point here).
**Observed in Actions at the 0.4.2 tag** (run 41842416697, 2026-11-04): the tag-push trigger and the path with no release
yet (the job created the draft, its log prints the draft URL, and uploaded the file with the `GITHUB_TOKEN`; the asset
`openaether-apps-0.4.2.cdx.json` is on the release), and, in the run the publish started (41842922385), the VEX check and
the job `The published release carries the SBOM`, both green. **Replayed by hand earlier:** the upload step with the
maintainer's own `gh` credentials, against a throwaway draft release (the asset was attached, the draft deleted afterwards)
and against the published 0.4.1 release (refused, exit 1); actionlint and yamllint on `sbom.yml`. **Not observed:** the
`workflow_dispatch` fallback above, and the refusal of a published release inside Actions.

**Open, 2026-11-04 (0.4.3).** What is still red: certifi GHSA-6q48-6c7w-9gw2 (fixed in certifi 2026.10.18) in the CNPG
PostgreSQL 16.15 and 18.6 images (apps #162 and #163). It has no waiver: the backup verifier uses certifi through botocore.
The images are to be replaced in 0.4.4 (planned, owner decision), so the weekly scan stays red on these two until then.
This release's image bumps remove the other reds of 0.4.2: Forgejo 15.0.11 (apps #179, `brotli-libs` CVE-2026-111497; the
same CVE is open for the Flux image in OpenAether-infra #444, handled in that repository), and the five findings that were
waived: `alpine/k8s` 1.36.6 and 1.37.2 (CVE-2025-68121 and certifi), `k8s-sidecar` 2.14.3 (certifi), Zitadel and
`zitadel-login` v4.20.1 (the two Next.js advisories). Read with Trivy 0.69.3 and 0.75.0 on the amd64 images, database of 2026-11-04: **a local scan**. The Actions run on the
branch named below agrees for the images it scanned (38 of 40 scan jobs green, the two red ones are the PostgreSQL images);
the first scan on main after the merge is still to come. A tag pushed while a scan is red gives a red `SBOM` run, and the attach
job is still green: observed at the 0.4.2 tag (run 41842416697: `Attach to a draft release` green, four scan jobs red,
Forgejo, the CNPG operator and the two PostgreSQL images; the last three failed on a ghcr.io rate limit, #197).

**Waivers: two VEX statements and one Trivy ignore entry, all renewed on 2026-11-04 after a re-reading and all ending on
2026-12-04.** The statements: restic (CVE-2026-60002, OpenSSH in the image's alpine layer, never called; `restic` 0.19.1 is
still the newest tag) and PyJWT in the CNPG PostgreSQL images (CVE-2026-102268, imported only by `msal`, not used). The
ignore entry: libexpat in the Longhorn v1.12.1 images (SUSE-SU-2026:4619-1; a reading of package data and file searches: no Longhorn process was run or traced, only `wget --help` and `ldd` in the backing-image-manager image).
The five statements of the list above were deleted, not renewed, because the fixed images carry no such finding. Nothing
ends on 2026-11-20 any more: `scripts/check-vex.py` passes on 2026-11-21 and on 2026-12-03, fails on 2026-12-04 for the
ignore entry (see **Expiry day** below) and from 2026-12-05 for the two statements, if the three are not re-read again or removed.

- **VEX:** a finding that does not apply is an OpenVEX statement in `.vex/openaether-apps.openvex.json`
  (`not_affected` with a justification, an `impact_statement`, an `action_statement` and ONE `Expires: YYYY-MM-DD` in
  `status_notes`, at most 90 days after the statement). Trivy reads the file once it has a statement but knows nothing of
  dates, so `scripts/check-vex.py` (a CI step, and the `VEX statements` job of the SBOM workflow) fails on a missing
  justification or expiry and on an expired statement. A statement never covers a fixable, reachable CRITICAL. Docker Hub products are written with
  Trivy's `index.docker.io` repository URL. `.github/trivy-ignore.yaml` (Trivy's format, with its own `expired_at`) holds the
  one accepted finding Trivy's VEX matching misses (libexpat in the Longhorn v1.12.1 images); `check-vex.py` holds it to the same rule.
  **Expiry day:** Trivy stops applying an ignore entry from 00:00 UTC of its `expired_at` day (its source,
  `ExpiredAt.Before(now)`, read at v0.69.3), so `check-vex.py` fails an ignore entry on its `expired_at` day, the same
  day the Longhorn scans can turn red (2026-12-04, a Friday: the scheduled scan runs on Tuesdays). A VEX statement still
  passes on its last day and fails from the next one, because Trivy does not read the dates of OpenVEX.
  Whether Trivy matches a product is only seen by scanning the image: `trivy image --config .github/trivy-image-vex.yaml <ref>`.
- **Failing scans are announced:** a failed scan opens, or comments on, one issue `[vuln] <image>` and closes it when the
  scan passes (`scripts/ops/vuln-issue.sh`); an expired waiver does the same under `[vuln] VEX statements`. Before, a red
  run was the only signal. HIGH findings are still not scanned. Only a scan that wrote a report does this. A scan that
  did not run (a pull refused or rate limited, #197; no report on disk) is **only a red job**: it fails with a message
  and opens, comments on and closes no issue, so an open `[vuln]` issue stays as it was. **Residual:** nothing announces
  an image that then stays unscanned until someone reads the run; there is no retry, and the next chance is the next
  scheduled run (weekly). **The #197 change (the `ghcr.io` login with the workflow token, and the step that tells a
  finding from a scan that did not run) has run in Actions, on the branch.** Rung: workflow run 41927056554, a
  `workflow_dispatch` on `claude/0.4.3-rc1` at 9fb9035 (2026-11-04; six commits before the lab-proven head e8ca37f, and none of
  the six touches an image, a workflow or the VEX file). The step `Log in to ghcr.io` printed `Login Succeeded` (read in three of
  the eight ghcr scan jobs: both red ones and one green one), **all eight ghcr scans ran**, and 38 of the 40 scan jobs were
  green. **The two red scans are `ghcr.io/cloudnative-pg/postgresql:16.15` and `:18.6`**, one CRITICAL each, certifi
  GHSA-6q48-6c7w-9gw2 (installed 2026.7.22, fixed in 2026.10.18): known, no waiver, apps #162 and #163, deferred to 0.4.4. On
  both, the step `Tell a finding from a scan that did not run` took its branch "report present, the scan ran". The 0.4.2
  failures read `TOOMANYREQUESTS ... allowed: 44000/minute`, a limit reported per runner address: none of the eight ghcr
  scans was refused in this run, but one run does not show that the login is what lifted the limit. **Still not run:** the
  tag-push attach with the new login (a dispatch without a tag skips the `Attach to a draft release` job), and a `[vuln]`
  issue opened or closed by the new steps (a dispatch on a branch opens none, by design). The `VEX
  statements` job follows the same rule: its report holds the output of `check-vex.py`, stderr included, so a VEX file
  the script cannot read (exit 2) opens or updates the issue with that message; a run with no output at all is only a
  red job.

## What is checked on every pull request

- `scripts/check-image-pins.py`: no image floats (no tag, or `latest`/`main`...). It prints how many are pinned by
  tag only (16 of 16 today). `--require-digest` is the next step, after Renovate has rewritten the lines.
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
| `ghcr.io/external-secrets/external-secrets:v1.2.0` (the former pin; v2.13.0 not re-checked) | yes, subject `https://github.com/external-secrets/external-secrets/.github/workflows/release.yml@refs/heads/main` |
| `ghcr.io/kyverno/kyverno:v1.12.1` (the former pin; v1.19.1 comes from `reg.kyverno.io`, not re-checked) | **no signature at all** |
| `ghcr.io/cloudnative-pg/cloudnative-pg:1.23.1` (the former pin; 1.30.1 not re-checked) | **no signature at all** |
| `quay.io/jetstack/cert-manager-controller:v1.15.3` (the former pin; not re-checked at v1.21.2) | a `.sig` tag exists but no keyless signature matches (key-signed or another scheme) |
| `docker.io/openbao/openbao:2.7.1` | a signature verifies, identity not recorded here |
| `docker.io/restic/restic:0.19.1`, `grafana/grafana:11.3.1` (the former pin; 13.2.4 not re-checked), `ghcr.io/zitadel/zitadel:v4.14.0` (the former pin; v4.20.1 not re-checked), `istio/pilot:1.24.2` (the former pin; 1.31.3 not re-checked) | no keyless signature |

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
