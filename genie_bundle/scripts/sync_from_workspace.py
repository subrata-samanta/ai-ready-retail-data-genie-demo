"""Bring edits made in the Genie UI into git (the bundle's space definition).

    python genie_bundle/scripts/sync_from_workspace.py -t dev
    python genie_bundle/scripts/sync_from_workspace.py -t sandbox        your personal copy

Steps, all with the Databricks CLI:
  1. `databricks bundle summary -t <target>`   -> the id of the space this bundle deployed
  2. `databricks bundle generate genie-space --existing-id <id>` -> the live space as JSON
  3. replace the target's catalog (freshcart_dev.) with ${var.catalog}.
  4. compare it with the version last deployed to the target: the git tag genie-deployed-<target>, which the release
     and rollback workflows move after every deploy (--base to choose another ref; HEAD if there is none). No
     difference: nothing to sync. Otherwise only those UI edits are applied, item by item, onto
     resources/freshcart_assistant.space.yml as it is now, so changes merged since that deploy are never undone.
     (--space-id <id> replaces the file with that space instead: an import.)

Why not `bundle generate --resource ... --force` alone? It writes a .geniespace.json with the
target's real catalog names, and a file referenced by file_path is not variable-resolved, so the
same file could not be deployed to qa and prod. The space is therefore kept as a bundle variable
with ${var.catalog}, and this script converts the export into that form.
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
import space_tools as T                                        # noqa: E402

CLI = os.environ.get("DATABRICKS_CLI", "databricks")


def cli_json(*args) -> dict:
    p = subprocess.run([CLI, *args, "-o", "json"], cwd=T.BUNDLE_DIR, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"`databricks {' '.join(args)}` failed:\n{p.stderr.strip()}")
    out = p.stdout
    return json.loads(out[out.find("{"):])


def target_catalog(target: str) -> str:
    return cli_json("bundle", "validate", "-t", target)["variables"]["catalog"]["value"]


def deployed_space_id(target: str) -> str | None:
    """None when this identity has not deployed the space to the target yet (the first release)."""
    res = cli_json("bundle", "summary", "-t", target).get("resources", {}).get("genie_spaces", {})
    return (res.get(T.SPACE_KEY) or {}).get("id") or None


def export_space(target: str, space_id: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        p = subprocess.run([CLI, "bundle", "generate", "genie-space", "--existing-id", space_id, "--key", "export",
                            "-s", f"{tmp}/src", "-d", f"{tmp}/resources", "--force", "-t", target],
                           cwd=T.BUNDLE_DIR, capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit(f"`databricks bundle generate genie-space` failed:\n{p.stderr.strip()}")
        return json.loads(Path(tmp, "src", "export.geniespace.json").read_text(encoding="utf-8"))


def space_at(ref: str, path: Path = T.SPACE_FILE) -> dict | None:
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=T.BUNDLE_DIR, capture_output=True, text=True)
    if top.returncode != 0:
        return None
    rel = path.resolve().relative_to(Path(top.stdout.strip()).resolve()).as_posix()
    old = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=T.BUNDLE_DIR, capture_output=True, text=True)
    if old.returncode != 0:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
        fh.write(old.stdout)
    return T.load_space(Path(fh.name))


def sync(target: str, space_id: str | None = None, path: Path = T.SPACE_FILE, base_ref: str | None = None):
    """None if nothing is deployed; otherwise {base, edits, conflicts, written}."""
    imported = space_id is not None
    space_id = space_id or deployed_space_id(target)
    if not space_id:
        return None                              # nothing deployed yet: nothing to sync
    live = T.neutralise(export_space(target, space_id), target_catalog(target))
    current = T.load_space(path) if path.exists() else {}
    if imported:                                 # an import replaces the file
        T.save_space(live, path)
        return {"base": "the file", "edits": T.diff(current, live), "conflicts": [], "written": True}
    ref = base_ref or f"genie-deployed-{target}"
    base = space_at(ref, path)
    if base is None:
        ref, base = "HEAD (no deploy tag yet)", space_at("HEAD", path) or current
    edits = T.diff(base, live)
    if not edits:
        return {"base": ref, "edits": [], "conflicts": [], "written": False}
    merged, conflicts = T.merge3(base, current, live)
    T.save_space(merged, path)
    return {"base": ref, "edits": edits, "conflicts": conflicts, "written": True}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", "--target", default="dev")
    ap.add_argument("--space-id", help="import this space instead (replaces the file)")
    ap.add_argument("--base", help="git ref of the version deployed to the target (default: genie-deployed-<target>)")
    args = ap.parse_args(argv)
    result = sync(args.target, args.space_id, base_ref=args.base)
    if result is None:
        print(f"the space is not deployed to {args.target} yet: nothing to sync")
        return 0
    if not result["written"]:
        print(f"no edits in the {args.target} Genie UI since the last deploy ({result['base']}): nothing to sync")
        return 0
    print(f"synced {args.target} into {T.SPACE_FILE.relative_to(T.REPO_ROOT)} (base {result['base']}): "
          f"{T.count_changes(result['edits'])} change(s)")
    print("\n".join(result["edits"]))
    for c in result["conflicts"]:
        print(f"WARNING conflict: {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
