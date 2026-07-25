#!/usr/bin/env python3
"""pick.py — pioche modulaire OpenAether.

Sélectionne des briques du DAG Flux (apps/flux/base) et génère un profil
(overlay kustomize) contenant la fermeture transitive de leurs dépendances.

La source de vérité des dépendances est `spec.dependsOn` des Kustomizations
Flux de apps/flux/base/*.yaml. Le catalogue apps/flux/bricks.yaml n'apporte
que des métadonnées UX (alias, socle, compagnons, descriptions).

Le profil généré référence ../base entier et SUPPRIME ($patch: delete) les
Kustomizations non sélectionnées : la base reste l'unique source de vérité,
et la fermeture transitive garantit qu'aucune Kustomization retenue ne
dépend d'une supprimée.

Usage :
  python3 scripts/pick.py --list                 # briques disponibles
  python3 scripts/pick.py --validate             # santé du DAG + catalogue
  python3 scripts/pick.py vault gateway          # plan (dry-run)
  python3 scripts/pick.py vault gateway -o apps/flux/edge   # génère le profil

Dépendance : PyYAML (python3-yaml).
"""

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
        self.name = name          # metadata.name de la Kustomization Flux
        self.deps = deps          # noms dependsOn
        self.path = path          # spec.path (./apps/base/…)
        self.source_file = source_file  # fichier de apps/flux/base
        self.order = order        # (fichier, index doc) pour l'affichage trié


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
                sys.exit(f"DAG invalide : Kustomization '{name}' définie deux fois "
                         f"({bricks[name].source_file.name} et {f.name})")
            bricks[name] = Brick(name, deps, spec.get("path", ""), f, (i, j))
    if not bricks:
        sys.exit(f"Aucune Kustomization Flux trouvée sous {BASE}")
    return bricks


def load_catalog(bricks):
    cat = yaml.safe_load(CATALOG.read_text()) if CATALOG.exists() else {}
    cat = cat or {}
    errors = []
    for key in ("baseline", "companions"):
        for n in cat.get(key) or []:
            if n not in bricks:
                errors.append(f"{key}: '{n}' inconnu du DAG")
    for alias, target in (cat.get("aliases") or {}).items():
        if target not in bricks:
            errors.append(f"alias '{alias}' → '{target}' inconnu du DAG")
        if alias in bricks:
            errors.append(f"alias '{alias}' masque une Kustomization du même nom")
    if errors:
        sys.exit("Catalogue bricks.yaml incohérent avec le DAG :\n  - "
                 + "\n  - ".join(errors))
    return cat


def validate_dag(bricks):
    """Dépendances inconnues, cycles, chemins morts. Retourne la liste d'erreurs."""
    errors = []
    for b in bricks.values():
        for d in b.deps:
            if d not in bricks:
                errors.append(f"{b.name} ({b.source_file.name}) dépend de '{d}' qui n'existe pas")
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

    # Chaque fichier du DAG doit être listé dans base/kustomization.yaml
    kust = yaml.safe_load((BASE / "kustomization.yaml").read_text())
    listed = set(kust.get("resources") or [])
    for f in sorted(BASE.glob("*.yaml")):
        if f.name != "kustomization.yaml" and f.name not in listed:
            errors.append(f"{f.name} absent de base/kustomization.yaml (jamais déployé)")
    return errors


def resolve_names(requested, bricks, aliases):
    """Alias + validation, avec suggestion en cas de faute de frappe."""
    resolved = []
    known = list(bricks) + list(aliases)
    for r in requested:
        name = aliases.get(r, r)
        if name not in bricks:
            hint = difflib.get_close_matches(r, known, n=3)
            msg = f"brique inconnue : '{r}'"
            if hint:
                msg += f" — vouliez-vous dire : {', '.join(hint)} ?"
            sys.exit(msg + "\n(liste complète : scripts/pick.py --list)")
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
                reasons.setdefault(d, f"dépendance de {n}")
                work.append(d)
    return out


def pick(requested, bricks, cat, with_baseline=True, with_companions=True):
    """Retourne (sélection, raisons) : fermeture + socle + compagnons."""
    reasons = {n: "demandé" for n in requested}
    selected = closure(requested, bricks, reasons)

    if with_baseline:
        for n in cat.get("baseline") or []:
            if n not in selected:
                reasons[n] = "socle"
        selected = closure(selected | set(cat.get("baseline") or []), bricks, reasons)

    if with_companions:
        companions = cat.get("companions") or []
        changed = True
        while changed:
            changed = False
            for c in companions:
                if c not in selected and all(d in selected for d in bricks[c].deps):
                    selected.add(c)
                    reasons[c] = "compagnon (" + ", ".join(bricks[c].deps) + ")"
                    changed = True
        # Un compagnon n'introduit jamais de nouvelle dépendance (toutes déjà
        # sélectionnées par construction) — pas de re-fermeture nécessaire.

    return selected, reasons


def print_plan(selected, reasons, bricks, cat):
    desc = cat.get("descriptions") or {}
    print(f"\nProfil : {len(selected)} Kustomizations retenues, "
          f"{len(bricks) - len(selected)} exclues\n")
    print("  RETENUES")
    for n in sorted(selected, key=lambda x: bricks[x].order):
        print(f"    {n:<36} [{reasons[n]}]")
    excluded = [n for n in bricks if n not in selected]
    if excluded:
        print("\n  EXCLUES (supprimées du profil)")
        for n in sorted(excluded, key=lambda x: bricks[x].order):
            d = desc.get(n, "")
            print(f"    {n:<36} {d}")
    print()


def emit_profile(outdir, selected, requested_cli, bricks):
    outdir = Path(outdir)
    if not outdir.is_absolute():
        outdir = (Path.cwd() / outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    rel_base = os.path.relpath(BASE, outdir)

    excluded = sorted((n for n in bricks if n not in selected),
                      key=lambda x: bricks[x].order)
    lines = [
        "apiVersion: kustomize.config.k8s.io/v1beta1",
        "kind: Kustomization",
        "# Profil OpenAether GÉNÉRÉ par scripts/pick.py — ne pas éditer à la main.",
        f"# Pioche : {' '.join(requested_cli)}",
        f"# Régénérer : python3 scripts/pick.py {' '.join(requested_cli)} -o <ce dossier>",
        "#",
        "# Le DAG complet reste dans ../base ; ce profil supprime ($patch: delete)",
        "# les Kustomizations non sélectionnées. La fermeture transitive garantit",
        "# qu'aucune Kustomization retenue ne dépend d'une supprimée.",
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
    (outdir / "kustomization.yaml").write_text("\n".join(lines) + "\n")
    return outdir


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
            print(f"       dépend de : {', '.join(b.deps)}")
    print("\nExemple : python3 scripts/pick.py vault gateway -o apps/flux/edge\n")


def main():
    p = argparse.ArgumentParser(description="Pioche modulaire OpenAether")
    p.add_argument("bricks", nargs="*", help="briques ou alias à installer")
    p.add_argument("-o", "--output", metavar="DIR",
                   help="génère le profil dans DIR (ex : apps/flux/edge)")
    p.add_argument("--list", action="store_true", help="liste les briques")
    p.add_argument("--validate", action="store_true", help="valide DAG + catalogue")
    p.add_argument("--no-baseline", action="store_true",
                   help="n'ajoute pas le socle sécurité (déconseillé)")
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
        print(f"OK — DAG sain : {len(bricks)} Kustomizations, catalogue cohérent.")
        return
    if args.list or not args.bricks:
        cmd_list(bricks, cat)
        if not args.bricks and not args.list:
            sys.exit("Aucune brique demandée.")
        return

    requested = resolve_names(args.bricks, bricks, cat.get("aliases") or {})
    selected, reasons = pick(requested, bricks, cat,
                             with_baseline=not args.no_baseline,
                             with_companions=not args.no_companions)
    print_plan(selected, reasons, bricks, cat)

    if args.output:
        outdir = emit_profile(args.output, selected, args.bricks, bricks)
        print(f"Profil écrit : {outdir}/kustomization.yaml")
        print("Vérifier le rendu : kubectl kustomize " + str(outdir) + " | head")
        print("Pointer Flux dessus : spec.path de la Kustomization racine "
              "(côté OpenAether-infra, cf. bootstrap-manifests).")
    else:
        print("(dry-run — ajouter -o apps/flux/<profil> pour générer l'overlay)")


if __name__ == "__main__":
    main()
