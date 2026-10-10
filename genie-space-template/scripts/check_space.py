"""Checks for the Genie space in this bundle, from the fastest to the slowest.

    python scripts/check_space.py              # config + space JSON, no workspace needed (used in CI)
    python scripts/check_space.py --fix        # also add missing ids and sort id lists, then re-check
    python scripts/check_space.py --live dev -p dev   # also check the tables' Unity Catalog metadata

Offline it fails on: CHANGE_ME left in databricks.yml, a JSON not in `bundle generate` format, bad or
unsorted ids, version 1 column fields, a data source outside the dev catalog, a benchmark without SQL.
It then prints an AI-readiness scorecard (the practices that made the FreshCart space accurate).

--live <target> reads every table and metric view of the space from Unity Catalog (in that target's
catalog) and reports missing table comments, column comments and primary keys: Genie uses all three to
pick tables, columns and joins. Auth comes from your Databricks CLI profile / DATABRICKS_* variables.
"""
from __future__ import annotations

import argparse
import sys

import space_lib as L


def offline(do_fix: bool) -> int:
    errors = [f"{line}  <- fill this in" for line in L.placeholders_left()]
    space = L.load_space()
    if do_fix:
        space, changes = L.fix(space)
        if L.dump_space(space) != L.SPACE_FILE.read_text(encoding="utf-8"):
            L.SPACE_FILE.write_text(L.dump_space(space), encoding="utf-8")
            changes.append("rewrote the JSON in `bundle generate` format")
        for c in changes:
            print(f"fixed: {c}")
    if L.SPACE_FILE.read_text(encoding="utf-8") != L.dump_space(space):
        errors.append(f"{L.SPACE_FILE.name} is not in `bundle generate` format (run with --fix)")
    errs, warns = L.validate(space, None if L.placeholders_left() else L.catalog_for("dev"))
    errors += errs

    print(f"\nAI-readiness of '{L.SPACE_FILE.name}'")
    icons = {"ok": "✅", "warn": "⚠️ ", "info": "💡"}
    for status, check, advice in L.readiness(space):
        print(f"  {icons[status]} {check}" + ("" if status == "ok" else f"  -> {advice}"))
    for w in warns:
        print(f"WARNING: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    print("\nSpace checks passed." if not errors else f"\n{len(errors)} error(s).")
    return 1 if errors else 0


def live(target: str, profile: str | None) -> int:
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.errors import NotFound

    space = L.load_space()
    dev, catalog = L.catalog_for("dev"), L.catalog_for(target)
    w = WorkspaceClient(profile=profile) if profile else WorkspaceClient()
    missing, problems = [], 0
    print(f"\nUnity Catalog metadata in target {target} (catalog {catalog})")
    for ident, kind in sorted(L.source_identifiers(space).items()):
        name = L.rewrite_catalog(ident, dev, catalog)[0].replace("`", "")
        try:
            if kind == "function":
                fn = w.functions.get(name)
                note = "" if fn.comment else "  ⚠️  no comment: say what it returns and when to use it"
                problems += bool(note)
                print(f"  {name} (function){note}")
                continue
            t = w.tables.get(name)
        except NotFound:
            missing.append(name)
            print(f"  ❌ {name}: not found (or you lack access)")
            continue
        cols = t.columns or []
        commented = sum(1 for c in cols if c.comment)
        has_pk = any(c.primary_key_constraint for c in (t.table_constraints or []))
        notes = []
        if not t.comment:
            notes.append("no table comment")
        if cols and commented < len(cols):
            notes.append(f"{len(cols) - commented}/{len(cols)} columns without a comment")
        if kind == "table" and not has_pk:
            notes.append("no primary key (declare PK/FK with RELY so Genie knows the joins)")
        problems += len(notes)
        ttype = getattr(t.table_type, "value", t.table_type)
        print(f"  {'✅' if not notes else '⚠️ '} {name} ({ttype}, {commented}/{len(cols)} columns commented)"
              + (": " + "; ".join(notes) if notes else ""))
    print(f"\n{problems} metadata gap(s); sql/uc_metadata_template.sql shows how to close them.")
    if missing:
        print(f"ERROR: {len(missing)} data source(s) not found in {catalog}: {missing}")
    return 1 if missing else 0


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fix", action="store_true", help="add missing ids, sort id lists, rewrite the format")
    ap.add_argument("--live", metavar="TARGET", help="also check Unity Catalog metadata in this target")
    ap.add_argument("-p", "--profile", help="Databricks CLI profile for --live")
    args = ap.parse_args(argv)
    rc = offline(args.fix)
    if args.live:
        rc = live(args.live, args.profile) or rc
    sys.exit(rc)


if __name__ == "__main__":
    main(sys.argv[1:])
