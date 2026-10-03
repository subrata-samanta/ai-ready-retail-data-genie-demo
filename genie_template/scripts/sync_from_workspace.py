"""Bring a Genie space from the workspace into git (space/genie_space.yml).

    python scripts/sync_from_workspace.py -t dev
        edits made in the dev Genie UI -> git (genie-dev-sync runs this hourly)
    python scripts/sync_from_workspace.py -t dev --space-id <id> --from-catalog <c> [--from-schema <s>]
        import any existing space (for example the one you prototyped by hand) as the project's first version

Steps, with the Databricks CLI:
  1. `databricks bundle summary -t <target>`  -> the id of the space this bundle deployed (unless --space-id)
  2. `databricks bundle generate genie-space --existing-id <id>` -> the live space as JSON
  3. real names -> ${var.catalog}.${var.schema}. ; write space/genie_space.yml canonically; print the changes

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


def sync(target: str, space_id: str | None = None, from_catalog: str | None = None, from_schema: str | None = None,
         path: Path = T.SPACE_FILE) -> list[str] | None:
    space_id = space_id or deployed_space_id(target)
    if not space_id:
        return None
    s = T.settings(target)
    live = export_space(target, space_id)
    space = T.neutralise(live, from_catalog or s["catalog"], from_schema or s.get("schema"))
    before = T.load_space(path) if path.exists() else {}
    T.save_space(space, path)
    return T.diff(before, space)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", "--target", default="dev")
    ap.add_argument("--space-id", help="default: the space this bundle deployed to the target")
    ap.add_argument("--from-catalog", help="catalog the exported space uses (default: the target's)")
    ap.add_argument("--from-schema", help="schema the exported space uses (default: the target's)")
    args = ap.parse_args(argv)
    changes = sync(args.target, args.space_id, args.from_catalog, args.from_schema)
    if changes is None:
        print(f"the space is not deployed to {args.target} yet: nothing to sync")
        return 0
    print(f"synced into {T.SPACE_FILE.relative_to(T.PROJECT_DIR)}: {T.count_changes(changes)} change(s)")
    print("\n".join(changes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
