"""Checks on the Genie space definition that need no workspace (run on every pull request).

    python genie_bundle/scripts/validate_space.py            static checks
    python genie_bundle/scripts/validate_space.py --sql      + run every example and benchmark SQL locally
    python genie_bundle/scripts/validate_space.py --fix      rewrite the file in canonical form
    python genie_bundle/scripts/validate_space.py --diff-against <git ref>   readable change list

`databricks bundle validate -t <target>` checks the bundle itself; this checks the space's content.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import space_tools as T                                        # noqa: E402


def _summary(text: str) -> None:
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=str(T.SPACE_FILE))
    ap.add_argument("--sql", action="store_true")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--diff-against", metavar="GIT_REF")
    args = ap.parse_args(argv)
    path = Path(args.file)
    space = T.load_space(path)

    if args.diff_against:
        rel = path.resolve().relative_to(T.REPO_ROOT).as_posix()
        old = subprocess.run(["git", "show", f"{args.diff_against}:{rel}"], cwd=T.REPO_ROOT, capture_output=True, text=True)
        base = {}
        if old.returncode == 0:
            with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
                fh.write(old.stdout)
            base = T.load_space(Path(fh.name))
        changes = T.diff(base, space)
        print(f"changes versus {args.diff_against} ({T.count_changes(changes)}):")
        print("\n".join(changes) or "  (none)")
        _summary("### Genie space changes\n\n```diff\n" + ("\n".join(changes) or "no changes") + "\n```")
        return 0

    if args.fix:
        T.save_space(space, path)
        print(f"rewrote {path} in canonical form")
    errors, warnings = T.validate(space)
    if not T.is_canonical(path):
        errors.append(f"{path.name} is not in canonical form: run validate_space.py --fix")
    if args.sql:
        errors += T.check_sql_locally(space)
    print(f"space content {T.content_hash(space)}: {T.summary(space)}")
    for e in errors:
        print("ERROR  ", e)
    for w in warnings:
        print("WARNING", w)
    print("OK" if not errors else f"{len(errors)} error(s)")
    _summary(f"### Genie space validation: {'✅' if not errors else '❌'}\n\n"
             + "\n".join(f"- ❌ {e}" for e in errors) + "\n" + "\n".join(f"- ⚠️ {w}" for w in warnings))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
