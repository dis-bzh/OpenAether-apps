# Contributing

## Issues, and what they are not

**Issues are for what you hit** — a brick that will not reconcile, a dependency
that resolves wrong. Reports from outside are welcome and outrank our own list:
the order in the backlog is a guess about what matters, something that broke on
somebody is a fact.

**Our own work is not in issues.** It lives in
[`OpenAether-infra/docs/backlog.md`](https://github.com/dis-bzh/OpenAether-infra/blob/main/docs/backlog.md),
one list for both repositories, where every entry names the command that would
close it. A working list wants to be read offline at the start of a session; an
intake wants to be where a stranger can reach it. Duplicating one into the other
would give us two records that disagree.

Never put a real IP, a cloud account id or a bucket name in an issue. The
repository is public and has had to purge its history once for that.

## Setup

Requires `kubectl` (with the built-in Kustomize) and Python 3 with PyYAML. See
the sibling `OpenAether-infra` repo's `task setup` for the fuller toolchain —
this repo doesn't provision infrastructure itself.

## Before opening a PR

    python3 scripts/pick.py --validate           # DAG health: unknown deps, cycles, dead paths
    python3 scripts/pick.py --check               # generated profiles still match the DAG
    python3 scripts/check-object-collisions.py     # builds every brick, checks cross-brick object collisions
    yamllint -c .yamllint apps/ scripts/ .github/workflows/

All four run in CI too — a PR can't merge until they're green (see the required
checks on `main`).

## Conventions

- English for manifests, comments, commit messages and docs (`README.md` is
  canonical, `README.fr.md` is a translation, never the source).
- **Fixed baseline: CNI (Cilium) + Flux only.** Everything else is optional and
  composable — pick bricks via `scripts/pick.py`, see `apps/flux/README.md`.
  `dependsOn` in a Flux Kustomization must encode *real* dependencies (a
  StorageClass, a PriorityClass, an LB pool) — see `apps/flux/bricks.yaml`.
- Business/app-specific manifests belong in each application's own repo
  (`deploy/k8s/`), not here — this repo provides the common platform plus the
  Flux pointers to those repos.
- Open work items live in `OpenAether-infra/docs/backlog.md` — read it before
  starting non-trivial work.
- Dependency bumps (CAPI providers, Helm charts, vendored manifests, CI actions)
  are proposed by Renovate and never auto-merged. Anything labeled
  `vendored-manifest` only bumps a tracking comment — re-download and replace
  the file by hand per the kustomization.yaml's own upgrade notes before
  merging.

## Pull requests

`main` requires a PR with all CI checks green — including for maintainers, no
direct pushes. No mandatory reviewer count yet (small team), but PRs stay open
for review during that window regardless.
