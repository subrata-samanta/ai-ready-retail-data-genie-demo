"""Bring edits made in the Genie UI into git (the bundle's space definition).

    python genie_bundle/scripts/sync_from_workspace.py -t dev
    python genie_bundle/scripts/sync_from_workspace.py -t sandbox        your personal copy

Steps, all with the Databricks CLI:
  1. `databricks bundle summary -t <target>`   -> the id of the space this bundle deployed
  2. `databricks bundle generate genie-space --existing-id <id>` -> the live space as JSON
  3. replace the target's catalog (freshcart_dev.) with ${var.catalog}. and write
     resources/freshcart_assistant.space.yml in canonical form; print what changed

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


def deployed_space_id(target: str) -> str:
    res = cli_json("bundle", "summary", "-t", target)["resources"]["genie_spaces"][T.SPACE_KEY]
    if not res.get("id"):
        raise SystemExit(f"the space is not deployed to {target} yet: run `databricks bundle deploy -t {target}`")
    return res["id"]


def export_space(target: str, space_id: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        p = subprocess.run([CLI, "bundle", "generate", "genie-space", "--existing-id", space_id, "--key", "export",
                            "-s", f"{tmp}/src", "-d", f"{tmp}/resources", "--force", "-t", target],
                           cwd=T.BUNDLE_DIR, capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit(f"`databricks bundle generate genie-space` failed:\n{p.stderr.strip()}")
        return json.loads(Path(tmp, "src", "export.geniespace.json").read_text(encoding="utf-8"))


def sync(target: str, space_id: str | None = None, path: Path = T.SPACE_FILE) -> list[str]:
    catalog = target_catalog(target)
    live = export_space(target, space_id or deployed_space_id(target))
    space = T.neutralise(live, catalog)
    before = T.load_space(path) if path.exists() else {}
    T.save_space(space, path)
    return T.diff(before, space)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", "--target", default="dev")
    ap.add_argument("--space-id", help="default: the space this bundle deployed to the target")
    args = ap.parse_args(argv)
    changes = sync(args.target, args.space_id)
    print(f"synced {args.target} into {T.SPACE_FILE.relative_to(T.REPO_ROOT)}: {T.count_changes(changes)} change(s)")
    print("\n".join(changes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
