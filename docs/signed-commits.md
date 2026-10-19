# Signed commits and Flux Git source verification (apps side)

Git write access is the root of trust of the platform: Flux applies whatever the tracked ref contains, with cluster-admin.
Nothing binds a deployed commit to a maintainer today (`git log --format=%G?` shows no good signature on the history).
This page is the maintainers' standard and the opt-in switch. It addresses ANSSI SSDLC-CICD-AIP-03 and SSDLC-CONT-REC06;
it is **not** a claim of compliance. Rung of everything below: mocked/static, never run on a cluster
(see B27 in the maintainers' lab checklist).

## Stage 1 - the repository side (no cluster risk)

- **Standard: OpenPGP (GPG) signatures.** This repository standardises on OpenPGP, a choice and not a Flux limit: the
  Flux CRD vendored in OpenAether-infra documents the `spec.verify.secretRef` keys as `.asc` (PGP) and `.sshpub` (SSH);
  the copy vendored here (`gotk-components.yaml`) does not spell the suffixes out. SSH keys are NOT wired here (the
  `git-signers` Secret holds armored OpenPGP keys only). A commit signed with an SSH key would satisfy GitHub's ruleset
  but fail Flux's verification of this setup, so use GPG, whatever the transport.
  Setup: `git config --global user.signingkey <fingerprint>` and `git config --global commit.gpgsign true`, publish the
  public key on the GitHub account (Settings, SSH and GPG keys) so GitHub shows "Verified".
- **CI**: `scripts/dev/check-commit-signatures.sh` runs on every PR over the commits it adds. It is a **warning** until
  every maintainer signs; pass `--strict` in `ci.yml` (job `commits`) to make it fail. A runner without the keys cannot
  verify a signature; the cryptographic verdict is the ruleset's and Flux's.
- **Ruleset (owner action, GitHub settings, not in git)**: on `main`, require a pull request, status checks, signed
  commits, no force push, no bypass. `scripts/dev/check-required-checks.sh` already reports a missing
  `required_signatures` rule (a warning, a failure under `--strict-main`). Commit the exported ruleset under
  `.github/rulesets/` as described in `docs/supply-chain.md`.
- **CODEOWNERS** names `apps/flux/`, `apps/base/platform/rbac/` and `.github/`; it has one owner today, so it enforces
  nothing until a second reviewer exists.

### Signer set (public keys)

| Signer | Fingerprint | Role |
|---|---|---|
| (to fill by the owner: maintainers) | | commits |
| GitHub `web-flow` | (take it from GitHub's published key, `https://github.com/web-flow.gpg`, and record the fingerprint here after checking it against GitHub's documentation) | squash and web merges |
| (renovate, release tooling) | | only if they push signed commits to the tracked ref |

A rebase merge does not go through `web-flow`: the rebased commits are signed by GitHub only when the merge is done
in the web UI with a squash or merge commit. Know which strategy the repository allows before enabling stage 2.

## Stage 2 - the cluster side (default off)

1. Create the Secret in `flux-system` first: `git-signers`, one armored public key per `<name>.asc` key (maintainers
   plus `web-flow`). The infra side renders it from the tofu variable `flux_git_verify` (OpenAether-infra); by hand:
   `kubectl -n flux-system create secret generic git-signers --from-file=maintainer.asc --from-file=web-flow.asc`.
2. Enable the same `spec.verify` on the bootstrap `GitRepository` (infra, `flux_git_verify.enabled = true`) **and** here,
   in the cluster's overlay: `components: [../components/git-verify]` (`apps/flux/components/git-verify`). Both must
   carry it: this repository reapplies Flux's own source (gitception, see `gitrepository.yaml`), so a block present on
   one side only is added or removed at the first reconcile.
3. Mode is `HEAD`. `Tag` and `TagAndHEAD` wait until the release flow signs tags.
4. Watch: `kubectl -n flux-system get gitrepository openaether -o yaml` (condition `SourceVerified`). A rejected commit
   makes the object NotReady and Flux keeps the last good artifact; `FluxSourceNotVerified`
   (critical, 5 minutes, from the `SourceVerified` condition) names it, and `FluxObjectNotReady` (20 minutes) follows.

Roll out on a dev cluster for several release cycles before production.

## Break-glass

If a legitimate commit is rejected (lost key, unsigned bot commit) and the platform must move:

Suspend the root Kustomization that applies `gitrepository.yaml` first (`flux get kustomizations -n flux-system`: the one
whose path is `apps/flux/...`), otherwise it puts the block back at its next interval.

```
kubectl -n flux-system patch gitrepository openaether --type merge -p '{"spec":{"verify":null}}'
flux reconcile source git openaether -n flux-system
```

then fix the signer set, restore the block (remove the patch by re-enabling the component and the infra variable) and
reconcile again. Works through the bastion with `kubectl`; no shell on the nodes is needed. While `verify` is removed,
the cluster trusts every commit of the ref: do it once, short, and note it.

## Not done here

No ruleset change (GitHub settings), no key published, nothing run on a cluster, no tag verification, no check that the
vendored source-controller accepts the block (lab).
