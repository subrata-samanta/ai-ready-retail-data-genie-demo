"""Bring a Genie space from the workspace into git (space/genie_space.yml).

    python scripts/sync_from_workspace.py -t dev
        edits made in the dev Genie UI -> git (genie-dev-sync runs this hourly, and first in every release)
    python scripts/sync_from_workspace.py -t prod
        adopt an edit someone made in the prod UI (the drift gate stopped the release), for a pull request
    python scripts/sync_from_workspace.py -t dev --space-id <id> --from-catalog <c> [--from-schema <s>]
        import any existing space (for example the one you prototyped by hand): replaces the file

How a sync works (not an import):
  1. the base: the version the bundle last deployed to the target, the git tag genie-deployed-<target> that the
     release and rollback workflows move after every deploy (--base to choose another ref; HEAD if there is none)
  2. the live space: `databricks bundle summary` (its id) + `databricks bundle generate genie-space` (its content),
     real catalog and schema names turned back into ${var.catalog} and ${var.schema}
  3. the UI edits = live space versus base. None: nothing to sync, the file is left as it is.
  4. otherwise the edits are applied, item by item, onto the file as it is now (main), so changes merged to main
     since that deploy are kept, never reverted. An item changed on both sides keeps the UI's version (reported).

If the bundle has not deployed the space to the target yet (a new project), there is nothing to sync: it says so
and exits 0.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genie_tools as T                                        # noqa: E402

CLI = os.environ.get("DATABRICKS_CLI", "databricks")


def cli_json(*args) -> dict:
    p = subprocess.run([CLI, *args, "-o", "json"], cwd=T.PROJECT_DIR, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"`databricks {' '.join(args)}` failed:\n{p.stderr.strip()}")
    return json.loads(p.stdout[p.stdout.find("{"):])


def deployed_space_id(target: str) -> str | None:
    res = cli_json("bundle", "summary", "-t", target).get("resources", {}).get("genie_spaces", {})
    return (res.get(T.SPACE_RESOURCE) or {}).get("id") or None


def export_space(target: str, space_id: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        p = subprocess.run([CLI, "bundle", "generate", "genie-space", "--existing-id", space_id, "--key", "export",
                            "-s", f"{tmp}/src", "-d", f"{tmp}/resources", "--force", "-t", target],
                           cwd=T.PROJECT_DIR, capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit(f"`databricks bundle generate genie-space` failed:\n{p.stderr.strip()}")
        return json.loads(Path(tmp, "src", "export.geniespace.json").read_text(encoding="utf-8"))


def space_at(ref: str, path: Path = T.SPACE_FILE) -> dict | None:
    """The definition at a git ref, or None when the ref (or git) is not there."""
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=T.PROJECT_DIR, capture_output=True, text=True)
    if top.returncode != 0:
        return None
    rel = path.resolve().relative_to(Path(top.stdout.strip()).resolve()).as_posix()
    old = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=T.PROJECT_DIR, capture_output=True, text=True)
    return T.space_from_text(old.stdout) if old.returncode == 0 else None


def plan(base: dict, current: dict, live: dict) -> tuple[dict | None, list[str], list[str]]:
    """(new definition or None if there is nothing to sync, the UI edits, conflicts)."""
    edits = T.diff(base, live)
    if not edits:
        return None, [], []
    merged, conflicts = T.merge3(base, current, live)
    return merged, edits, conflicts


def sync(target: str, base_ref: str | None = None, path: Path = T.SPACE_FILE) -> dict | None:
    space_id = deployed_space_id(target)
    if not space_id:
        return None
    live = T.from_target(export_space(target, space_id), target)
    current = T.load_space(path) if path.exists() else {}
    ref = base_ref or f"genie-deployed-{target}"
    base = space_at(ref, path)
    if base is None:
        ref, base = "HEAD (no deploy tag yet)", space_at("HEAD", path) or current
    merged, edits, conflicts = plan(base, current, live)
    if merged is not None:
        T.save_space(merged, path)
    return {"base": ref, "edits": edits, "conflicts": conflicts, "written": merged is not None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", "--target", default="dev")
    ap.add_argument("--base", help="git ref of the version deployed to the target (default: genie-deployed-<target>)")
    ap.add_argument("--space-id", help="import this space instead (replaces the file)")
    ap.add_argument("--from-catalog", help="import: catalog the space uses (default: the target's)")
    ap.add_argument("--from-schema", help="import: schema the space uses (default: the target's)")
    args = ap.parse_args(argv)
    if args.space_id:                                          # import: the space becomes the definition
        s = T.settings(args.target)
        space = T.neutralise(export_space(args.target, args.space_id), args.from_catalog or s["catalog"],
                             args.from_schema or s.get("schema"))
        before = T.load_space() if T.SPACE_FILE.exists() else {}
        T.save_space(space)
        changes = T.diff(before, space)
        print(f"imported {args.space_id} into {T.SPACE_FILE.relative_to(T.PROJECT_DIR)}: {T.count_changes(changes)} change(s)")
        print("\n".join(changes))
        return 0
    result = sync(args.target, args.base)
    if result is None:
        print(f"the space is not deployed to {args.target} yet: nothing to sync")
        return 0
    if not result["written"]:
        print(f"no edits in the {args.target} Genie UI since the last deploy ({result['base']}): nothing to sync")
        return 0
    print(f"{T.count_changes(result['edits'])} edit(s) made in the {args.target} Genie UI since {result['base']}, "
          f"applied to {T.SPACE_FILE.relative_to(T.PROJECT_DIR)}:")
    print("\n".join(result["edits"]))
    for c in result["conflicts"]:
        print(f"WARNING conflict: {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
