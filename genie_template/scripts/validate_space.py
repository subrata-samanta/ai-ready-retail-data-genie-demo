"""Checks on the space definition that need no workspace (every pull request runs them).

    python scripts/validate_space.py                          static checks + canonical form
    python scripts/validate_space.py --fix                    rewrite space/genie_space.yml in canonical form
    python scripts/validate_space.py --diff-against <ref>     what changed versus a git ref (pull requests)
    python scripts/validate_space.py --release-notes <from> <to> [--out notes.md]
                                                              Markdown change list between two refs (releases)

`databricks bundle validate -t <target>` checks the bundle; this checks the space's content.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genie_tools as T                                        # noqa: E402


def _summary(text: str) -> None:
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def space_at(ref: str, path: Path = T.SPACE_FILE) -> dict:
    """The space definition as it was at a git ref ({} if the ref is empty or the file did not exist there)."""
    if not ref:
        return {}
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=T.PROJECT_DIR, capture_output=True,
                         text=True).stdout.strip()
    rel = path.resolve().relative_to(Path(top).resolve()).as_posix() if top else path.name
    old = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=T.PROJECT_DIR, capture_output=True, text=True)
    return T.space_from_text(old.stdout) if old.returncode == 0 else {}


def release_notes(old_ref: str, new_ref: str) -> str:
    changes = T.diff(space_at(old_ref), space_at(new_ref))
    new = space_at(new_ref)
    return (f"## Genie space changes {old_ref or '(first release)'} → {new_ref}\n\n"
            f"Content {T.content_hash(new)}: {T.summary(new)}\n\n"
            "```diff\n" + ("\n".join(changes) or "no change to the space content") + "\n```\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=str(T.SPACE_FILE))
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--diff-against", metavar="GIT_REF")
    ap.add_argument("--release-notes", nargs=2, metavar=("FROM", "TO"))
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    path = Path(args.file)

    if args.release_notes:
        notes = release_notes(*args.release_notes)
        Path(args.out).write_text(notes, encoding="utf-8") if args.out else print(notes)
        return 0
    space = T.load_space(path)
    if args.diff_against:
        changes = T.diff(space_at(args.diff_against, path), space)
        print(f"changes versus {args.diff_against} ({T.count_changes(changes)}):")
        print("\n".join(changes) or "  (none)")
        _summary("### Genie space changes\n\n```diff\n" + ("\n".join(changes) or "no changes") + "\n```")
        return 0
    if args.fix:
        T.save_space(space, path)
        print(f"rewrote {path} in canonical form")
    errors, warnings = T.validate(space)
    if not T.is_canonical(path):
        errors.append(f"{path.name} is not in canonical form: run python scripts/validate_space.py --fix")
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
