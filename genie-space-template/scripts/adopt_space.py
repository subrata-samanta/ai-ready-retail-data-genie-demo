"""Bring an existing Genie space under this bundle: export it into src/, keep this repository's resource
settings, put its tables on the dev catalog and, optionally, let a target adopt the space in place.

    python scripts/adopt_space.py --space-id <id>                         # export only
    python scripts/adopt_space.py --space-id <id> --bind dev              # export + your dev adopts it
    python scripts/adopt_space.py --space-id <id> --from-target prod -p myprofile

The space id is the last part of the space's URL: https://<workspace>/genie/rooms/<space-id>.

What it does:
  1. `databricks bundle generate genie-space --existing-id <id> --key genie_space` in the workspace of
     --from-target (default dev). That writes src/genie_space.geniespace.json and a resource YAML.
  2. Keeps this repository's resource YAML instead of the generated one (warehouse by name, no
     parent_path, title per environment, permissions), and copies the live title and description into
     databricks.yml where they are still CHANGE_ME.
  3. Rewrites the space's catalog to the dev catalog when it reads another one (for example a space that
     reads the prod catalog today), so the JSON follows the "one JSON, dev catalog" rule.
  4. With --bind <target>: `databricks bundle deployment bind`, so that target's next deploy updates this
     very space (same id, URL and conversation history) instead of creating a new one; then shows
     `databricks bundle plan` for it. The binding is stored in the deploying identity's bundle folder,
     so bind qa or prod as the identity that deploys them: the CI service principal. The easy way is
     the workflow's `adopt_space_id` input; with -p <a profile of that service principal> works too.

Fill in databricks.yml first (everything except the title and description).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

import yaml

import space_lib as L


def cli(args: list[str], profile: str | None, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["databricks", *args] + (["-p", profile] if profile else [])
    print("$", " ".join(cmd), flush=True)
    p = subprocess.run(cmd, cwd=L.ROOT, text=True, capture_output=True)
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    if check and p.returncode:
        raise SystemExit(f"command failed ({p.returncode}): {' '.join(cmd)}")
    return p


def fill_placeholder(variable: str, value: str) -> bool:
    """Replace `default: "CHANGE_ME ..."` of one variable in databricks.yml, keeping every comment."""
    lines = L.BUNDLE_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    inside = False
    for i, line in enumerate(lines):
        if line.strip() == f"{variable}:" and line.startswith("  ") and not line.startswith("   "):
            inside = True
        elif inside and line.strip().startswith("default:") and L.PLACEHOLDER in line.split("#", 1)[0]:
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{indent}default: {json.dumps(value, ensure_ascii=False)}\n"
            L.BUNDLE_FILE.write_text("".join(lines), encoding="utf-8")
            return True
        elif inside and line.startswith("  ") and not line.startswith("   ") and line.strip().endswith(":"):
            inside = False
    return False


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--space-id", required=True, help="id of the existing Genie space (from its URL)")
    ap.add_argument("--from-target", default="dev", help="target whose workspace holds the space (default dev)")
    ap.add_argument("--bind", metavar="TARGET", help="target that adopts the space in place (dev, qa or prod)")
    ap.add_argument("-p", "--profile", help="Databricks CLI profile (default: DATABRICKS_CONFIG_PROFILE / DEFAULT)")
    args = ap.parse_args(argv)

    left = [x for x in L.placeholders_left()
            if not any(k in x for k in ('"CHANGE_ME space title"', '"CHANGE_ME space description"'))]
    if left:
        raise SystemExit("Fill in databricks.yml first:\n  " + "\n  ".join(left))

    # 1. export the space; keep our resource YAML
    ours = L.RESOURCE_FILE.read_text(encoding="utf-8")
    try:
        cli(["bundle", "generate", "genie-space", "--existing-id", args.space_id, "--key", L.KEY, "--force",
             "-t", args.from_target], args.profile)
        generated = yaml.safe_load(L.RESOURCE_FILE.read_text(encoding="utf-8"))
    finally:
        L.RESOURCE_FILE.write_text(ours, encoding="utf-8")
    live = generated["resources"]["genie_spaces"][L.KEY]
    print(f"\nExported '{live.get('title')}' into {L.SPACE_FILE.relative_to(L.ROOT)}; "
          f"kept {L.RESOURCE_FILE.relative_to(L.ROOT)} as it is in this repository.")

    # 2. title and description
    for var, value in (("space_title", live.get("title")), ("space_description", live.get("description"))):
        if value and fill_placeholder(var, value.strip()):
            print(f"databricks.yml: {var} = {value.strip()[:70]!r}")

    # 3. one JSON, dev catalog
    space = L.load_space()
    dev = L.catalog_for("dev")
    found = L.catalogs_in(space)
    blob = L.SPACE_FILE.read_text(encoding="utf-8")
    if len(found) == 1 and found != {dev}:
        (source,) = found
        blob, n = L.rewrite_catalog(blob, source, dev)
        print(f"The space read catalog {source}; rewrote {n} reference(s) to the dev catalog {dev}. "
              f"The same tables must exist in {dev}.")
    elif len(found) > 1:
        print(f"WARNING: the space reads several catalogs {sorted(found)}; only {dev} is rewritten per target. "
              f"Move its data sources into one catalog, or extend scripts/set_catalog.py.")
    space, changes = L.fix(json.loads(blob))
    L.SPACE_FILE.write_text(L.dump_space(space), encoding="utf-8")
    for c in changes:
        print(f"fixed: {c}")

    # 4. adopt in place
    if args.bind:
        if args.bind != "dev":
            print(f"NOTE: binding {args.bind} as the identity of this CLI profile. CI only sees the binding if that "
                  f"is the service principal that deploys {args.bind} (otherwise use the workflow's adopt_space_id).")
        cli(["bundle", "deployment", "bind", L.KEY, args.space_id, "--auto-approve", "-t", args.bind], args.profile)
        original = L.SPACE_FILE.read_text(encoding="utf-8")
        try:
            if args.bind != "dev":                    # plan what CI would deploy: the target's catalog
                subprocess.run([sys.executable, str(L.ROOT / "scripts" / "set_catalog.py"), args.bind],
                               cwd=L.ROOT, check=True)
            cli(["bundle", "plan", "-t", args.bind], args.profile, check=False)
        finally:
            L.SPACE_FILE.write_text(original, encoding="utf-8")
        print(f"\nTarget {args.bind} now owns space {args.space_id}: its next deploy updates it in place.")
        if args.bind == "dev":
            print("Note: dev runs in development mode, so the next deploy renames it '[dev <you>] ...'.")

    print("\nNext: python scripts/check_space.py, review `git diff`, commit.")


if __name__ == "__main__":
    main(sys.argv[1:])
