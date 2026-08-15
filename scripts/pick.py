#!/usr/bin/env python3
"""pick.py — OpenAether modular brick picker.

Selects bricks from the Flux DAG (apps/flux/base) and generates a profile
(a kustomize overlay) holding the transitive closure of their dependencies.

The source of truth for dependencies is the `spec.dependsOn` of the Flux
Kustomizations in apps/flux/base/*.yaml. The apps/flux/bricks.yaml catalogue
only adds UX metadata (aliases, baseline, companions, descriptions).

The generated profile references the whole of ../base and REMOVES
($patch: delete) the unselected Kustomizations: the base stays the single
source, and the transitive closure guarantees that no retained Kustomization
depends on a removed one.

Usage:
  python3 scripts/pick.py --list                 # available bricks
  python3 scripts/pick.py --validate             # DAG + catalogue health
  python3 scripts/pick.py vault gateway          # plan (dry-run)
  python3 scripts/pick.py vault gateway -o apps/flux/edge   # generate the profile

Dependency: PyYAML (python3-yaml).
"""

import re
import argparse
import difflib
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML manquant : apt install python3-yaml (ou pip install pyyaml)")

REPO = Path(__file__).resolve().parent.parent
BASE = REPO / "apps" / "flux" / "base"
CATALOG = REPO / "apps" / "flux" / "bricks.yaml"
FLUX_GROUP = "kustomize.toolkit.fluxcd.io"


class Brick:
    def __init__(self, name, deps, path, source_file, order):
        self.name = name          # the Flux Kustomization's metadata.name
        self.deps = deps          # dependsOn names
        self.path = path          # spec.path (./apps/base/…)
        self.source_file = source_file  # file under apps/flux/base
        self.order = order        # (file, doc index) for sorted display


def load_dag():
    """Parse les Kustomizations Flux de apps/flux/base/*.yaml."""
    bricks = {}
    for i, f in enumerate(sorted(BASE.glob("*.yaml"))):
        if f.name == "kustomization.yaml":
            continue
        for j, doc in enumerate(yaml.safe_load_all(f.read_text())):
            if not isinstance(doc, dict):
                continue
            if doc.get("kind") != "Kustomization":
                continue
            if not str(doc.get("apiVersion", "")).startswith(FLUX_GROUP):
                continue
            name = doc["metadata"]["name"]
            spec = doc.get("spec", {})
            deps = [d["name"] for d in spec.get("dependsOn") or []]
            if name in bricks:
                sys.exit(f"Invalid DAG: Kustomization '{name}' defined twice "
                         f"({bricks[name].source_file.name} et {f.name})")
            bricks[name] = Brick(name, deps, spec.get("path", ""), f, (i, j))
    if not bricks:
        sys.exit(f"No Flux Kustomization found under {BASE}")
    return bricks


def load_catalog(bricks):
    cat = yaml.safe_load(CATALOG.read_text()) if CATALOG.exists() else {}
    cat = cat or {}
    errors = []
    for key in ("baseline", "companions"):
        for n in cat.get(key) or []:
            if n not in bricks:
                errors.append(f"{key}: '{n}' unknown to the DAG")
    for alias, target in (cat.get("aliases") or {}).items():
        if target not in bricks:
            errors.append(f"alias '{alias}' → '{target}' unknown to the DAG")
        if alias in bricks:
            errors.append(f"alias '{alias}' shadows a Kustomization of the same name")
    if errors:
        sys.exit("bricks.yaml catalogue inconsistent with the DAG:\n  - "
                 + "\n  - ".join(errors))
    return cat


def validate_dag(bricks):
    """Unknown dependencies, cycles, dead paths. Returns the list of errors."""
    errors = []
    for b in bricks.values():
        for d in b.deps:
            if d not in bricks:
                errors.append(f"{b.name} ({b.source_file.name}) depends on '{d}', which does not exist")
        if b.path:
            target = REPO / b.path.lstrip("./")
            if not target.is_dir():
                errors.append(f"{b.name}: spec.path {b.path} n'existe pas dans le repo")

    # Cycles (DFS trois couleurs)
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in bricks}

    def dfs(n, stack):
        color[n] = GREY
        for d in bricks[n].deps:
            if d not in bricks:
                continue
            if color[d] == GREY:
                cycle = stack[stack.index(d):] + [d] if d in stack else [n, d]
                errors.append("cycle dependsOn : " + " → ".join(cycle))
            elif color[d] == WHITE:
                dfs(d, stack + [d])
        color[n] = BLACK

    for n in bricks:
        if color[n] == WHITE:
            dfs(n, [n])

    # Every DAG file must be listed in base/kustomization.yaml
    kust = yaml.safe_load((BASE / "kustomization.yaml").read_text())
    listed = set(kust.get("resources") or [])
    for f in sorted(BASE.glob("*.yaml")):
        if f.name != "kustomization.yaml" and f.name not in listed:
            errors.append(f"{f.name} missing from base/kustomization.yaml (never deployed)")
    return errors


def resolve_names(requested, bricks, aliases):
    """Alias + validation, avec suggestion en cas de faute de frappe."""
    resolved = []
    known = list(bricks) + list(aliases)
    for r in requested:
        name = aliases.get(r, r)
        if name not in bricks:
            hint = difflib.get_close_matches(r, known, n=3)
            msg = f"unknown brick: '{r}'"
            if hint:
                msg += f" — vouliez-vous dire : {', '.join(hint)} ?"
            sys.exit(msg + "\n(full list: scripts/pick.py --list)")
        resolved.append(name)
    return resolved


def closure(selected, bricks, reasons):
    """Fermeture transitive des dependsOn."""
    work = list(selected)
    out = set(selected)
    while work:
        n = work.pop()
        for d in bricks[n].deps:
            if d not in out:
                out.add(d)
                reasons.setdefault(d, f"dependency of {n}")
                work.append(d)
    return out


def pick(requested, bricks, cat, with_baseline=True, with_companions=True):
    """Returns (selection, reasons): closure + baseline + companions."""
    reasons = {n: "requested" for n in requested}
    selected = closure(requested, bricks, reasons)

    if with_baseline:
        for n in cat.get("baseline") or []:
            if n not in selected:
                reasons[n] = "baseline"
        selected = closure(selected | set(cat.get("baseline") or []), bricks, reasons)

    if with_companions:
        companions = cat.get("companions") or []
        changed = True
        while changed:
            changed = False
            for c in companions:
                if c not in selected and all(d in selected for d in bricks[c].deps):
                    selected.add(c)
                    reasons[c] = "companion (" + ", ".join(bricks[c].deps) + ")"
                    changed = True
        # A companion never introduces a new dependency (all are already
        # selected by construction) — no need to re-close the graph.

    return selected, reasons


def print_plan(selected, reasons, bricks, cat):
    desc = cat.get("descriptions") or {}
    print(f"\nProfile: {len(selected)} Kustomizations retained, "
          f"{len(bricks) - len(selected)} excluded\n")
    print("  RETAINED")
    for n in sorted(selected, key=lambda x: bricks[x].order):
        print(f"    {n:<36} [{reasons[n]}]")
    excluded = [n for n in bricks if n not in selected]
    if excluded:
        print("\n  EXCLUDED (removed from the profile)")
        for n in sorted(excluded, key=lambda x: bricks[x].order):
            d = desc.get(n, "")
            print(f"    {n:<36} {d}")
    print()


def render_profile(outdir, selected, requested_cli, bricks):
    """A profile's kustomization.yaml text (without writing) — also used by --check."""
    rel_base = os.path.relpath(BASE, outdir)

    excluded = sorted((n for n in bricks if n not in selected),
                      key=lambda x: bricks[x].order)
    lines = [
        "apiVersion: kustomize.config.k8s.io/v1beta1",
        "kind: Kustomization",
        "# OpenAether profile GENERATED by scripts/pick.py — do not edit by hand.",
        f"# Pick: {' '.join(requested_cli)}",
        "# Regenerate: python3 scripts/pick.py <bricks from Pick: above> -o <this directory>",
        "#",
        "# The full DAG stays in ../base; this profile removes ($patch: delete) the",
        "# unselected Kustomizations. The transitive closure guarantees that no",
        "# retained Kustomization depends on a removed one.",
        "resources:",
        f"  - {rel_base}",
    ]
    if excluded:
        lines.append("patches:")
        for n in excluded:
            lines += [
                "  - patch: |-",
                "      apiVersion: kustomize.toolkit.fluxcd.io/v1",
                "      kind: Kustomization",
                "      metadata:",
                f"        name: {n}",
                "        namespace: flux-system",
                "      $patch: delete",
            ]
    return "\n".join(lines) + "\n"


def emit_profile(outdir, selected, requested_cli, bricks):
    outdir = Path(outdir)
    if not outdir.is_absolute():
        outdir = (Path.cwd() / outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "kustomization.yaml").write_text(
        render_profile(outdir, selected, requested_cli, bricks))
    return outdir


# ⚠️ This marker is the ONLY link between a generated profile and --check.
# Translating it without updating BOTH the generator above and this regex makes
# --check silently skip every profile (hit on 2026-07-28).
PICK_RE = re.compile(r"^# Pick: (.*)$", re.M)


def cmd_check(bricks, cat):
    """Checks that the generated profiles on disk are up to date with the DAG.

    A profile freezes the list of EXCLUDED Kustomizations: adding a brick to the
    DAG (or changing a dependsOn) makes every already-generated profile stale —
    the new brick is then inherited from ../base without having been picked, and
    stays stuck if its own dependencies are excluded (real case: orc inherited
    by the edge clusters while cluster-api-providers was excluded).
    This check replays the generation in memory and compares, writing nothing.
    """
    stale, checked = [], 0
    for kfile in sorted(BASE.parent.glob("*/kustomization.yaml")):
        text = kfile.read_text()
        if "GENERATED by scripts/pick.py" not in text:
            continue          # hand-written profile (e.g. local/) — out of scope
        checked += 1
        m = PICK_RE.search(text)
        if not m:
            stale.append((kfile, "missing '# Pick:' header — regenerate"))
            continue
        requested_cli = m.group(1).split()
        try:
            requested = resolve_names(requested_cli, bricks, cat.get("aliases") or {})
        except SystemExit as e:
            stale.append((kfile, f"invalid pick ({e})"))
            continue
        selected, _ = pick(requested, bricks, cat)
        if render_profile(kfile.parent, selected, requested_cli, bricks) != text:
            stale.append((kfile, "stale with respect to the DAG — regenerate: "
                          f"python3 scripts/pick.py {' '.join(requested_cli)} "
                          f"-o apps/flux/{kfile.parent.name}"))
    if stale:
        print("Profiles to regenerate:", file=sys.stderr)
        for f, why in stale:
            print(f"  - {f.parent.name} : {why}", file=sys.stderr)
        sys.exit(1)
    print(f"OK — {checked} generated profile(s) up to date with the DAG.")


def cmd_list(bricks, cat):
    desc = cat.get("descriptions") or {}
    rev_aliases = {}
    for a, t in (cat.get("aliases") or {}).items():
        rev_aliases.setdefault(t, []).append(a)
    baseline = set(cat.get("baseline") or [])
    companions = set(cat.get("companions") or [])
    print(f"\nBriques disponibles ({len(bricks)}) — socle=[S] compagnon=[C]\n")
    for n in sorted(bricks, key=lambda x: bricks[x].order):
        b = bricks[n]
        tag = "S" if n in baseline else ("C" if n in companions else " ")
        aka = f" (alias : {', '.join(sorted(rev_aliases[n]))})" if n in rev_aliases else ""
        print(f" [{tag}] {n}{aka}")
        if desc.get(n):
            print(f"       {desc[n]}")
        if b.deps:
            print(f"       depends on: {', '.join(b.deps)}")
    print("\nExemple : python3 scripts/pick.py vault gateway -o apps/flux/edge\n")


def main():
    p = argparse.ArgumentParser(description="OpenAether modular brick picker")
    p.add_argument("bricks", nargs="*", help="bricks or aliases to install")
    p.add_argument("-o", "--output", metavar="DIR",
                   help="generate the profile into DIR (e.g. apps/flux/edge)")
    p.add_argument("--list", action="store_true", help="list the bricks")
    p.add_argument("--validate", action="store_true", help="valide DAG + catalogue")
    p.add_argument("--check", action="store_true",
                   help="check that the generated profiles are up to date (CI; exits 1 on drift)")
    p.add_argument("--no-baseline", action="store_true",
                   help="do not add the security baseline (not recommended)")
    p.add_argument("--no-companions", action="store_true",
                   help="n'ajoute pas les compagnons automatiques")
    args = p.parse_args()

    bricks = load_dag()
    errors = validate_dag(bricks)
    if errors:
        print("DAG invalide :", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    cat = load_catalog(bricks)

    if args.validate:
        print(f"OK — healthy DAG: {len(bricks)} Kustomizations, consistent catalogue.")
        return
    if args.check:
        cmd_check(bricks, cat)
        return
    if args.list or not args.bricks:
        cmd_list(bricks, cat)
        if not args.bricks and not args.list:
            sys.exit("No brick requested.")
        return

    requested = resolve_names(args.bricks, bricks, cat.get("aliases") or {})
    selected, reasons = pick(requested, bricks, cat,
                             with_baseline=not args.no_baseline,
                             with_companions=not args.no_companions)
    print_plan(selected, reasons, bricks, cat)

    if args.output:
        outdir = emit_profile(args.output, selected, args.bricks, bricks)
        print(f"Profile written: {outdir}/kustomization.yaml")
        print("Check the render: kubectl kustomize " + str(outdir) + " | head")
        print("Point Flux at it: spec.path of the root Kustomization "
              "(on the OpenAether-infra side, see bootstrap-manifests).")
    else:
        print("(dry-run — add -o apps/flux/<profile> to generate the overlay)")


if __name__ == "__main__":
    main()
