"""Drift gate and pre-deploy backup, from `databricks bundle plan -o json`.

    databricks bundle plan -t prod -o json > plan.json
    python genie_bundle/scripts/check_drift.py plan.json -t prod --backup-dir backup/ [--allow-drift]

`bundle deploy` replaces the space's content with the bundle's. If someone edited the space in the
Genie UI since the last deployment, a non-interactive deploy would overwrite that edit without
asking. The plan shows it: the bundle remembers the space's etag from its last deployment, and
`changes.etag.old != changes.etag.remote` means the space was modified outside the bundle.

This script
  * writes the live space (`remote_state.serialized_space` in the plan) to --backup-dir, in
    environment-neutral form, so the exact state before the deploy can always be restored;
  * prints what was changed outside the bundle (live versus the last deployed definition);
  * exits 2 if there is drift, unless --allow-drift (then the edit is overwritten, and kept in the backup);
  * exits 3 if the plan would delete or recreate a Genie space (all its conversations would be lost),
    unless --allow-destroy.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import space_tools as T                                        # noqa: E402


def analyse(plan: dict, catalog: str) -> list[dict]:
    out = []
    for key, entry in (plan.get("plan") or {}).items():
        if not key.startswith("resources.genie_spaces.") or "." in key[len("resources.genie_spaces."):]:
            continue                                         # skip sub-resources such as .permissions
        remote = entry.get("remote_state") or {}
        etag = (entry.get("changes") or {}).get("etag") or {}
        live = json.loads(remote["serialized_space"]) if remote.get("serialized_space") else None
        out.append({
            "resource": key.split(".", 2)[2],
            "action": entry.get("action"),
            "drift": bool(etag) and etag.get("old") is not None and etag.get("old") != etag.get("remote"),
            "etag_deployed": etag.get("old"), "etag_live": etag.get("remote"),
            "live": T.neutralise(live, catalog) if live else None,
        })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan")
    ap.add_argument("-t", "--target", required=True)
    ap.add_argument("--catalog", help="default: the target's catalog from space_tools.TARGET_CATALOGS")
    ap.add_argument("--backup-dir")
    ap.add_argument("--allow-drift", action="store_true")
    ap.add_argument("--allow-destroy", action="store_true",
                    help="allow a plan that deletes or recreates a Genie space (its conversations are lost)")
    args = ap.parse_args(argv)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    catalog = args.catalog or T.TARGET_CATALOGS[args.target]
    local = T.load_space()
    drifted = False
    lines = []
    destructive = []
    for r in analyse(plan, catalog):
        print(f"{r['resource']}: plan action {r['action']}")
        if r["action"] in ("delete", "recreate"):
            destructive.append(r["resource"])
        if r["live"] is not None and args.backup_dir:
            out = Path(args.backup_dir) / f"{args.target}.{r['resource']}.live.space.yml"
            T.save_space(r["live"], out)
            print(f"  backup of the live space: {out} (content {T.content_hash(r['live'])})")
        if r["drift"]:
            drifted = True
            changes = T.diff(local, r["live"]) if r["live"] else []
            print(f"  DRIFT: edited outside the bundle since the last deploy (etag {r['etag_deployed']} -> {r['etag_live']})")
            print("  live space versus the definition in git:")
            print("\n".join("    " + c for c in changes) or "    (content identical; only the etag moved)")
            lines += changes
    if os.environ.get("GITHUB_STEP_SUMMARY") and drifted:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"### ⚠️ Drift on `{args.target}`\n\nThe space was edited outside the bundle.\n\n```diff\n"
                     + "\n".join(lines) + "\n```\n")
    if destructive and not args.allow_destroy:
        print(f"\nRefusing: the plan would {'/'.join(sorted({r['action'] for r in analyse(plan, catalog) if r['resource'] in destructive}))} "
              f"{', '.join(destructive)}. Users would lose their conversations. Rerun with allow_destroy if intended.")
        return 3
    if drifted and not args.allow_drift:
        print(f"\nRefusing to deploy over edits made in {args.target}. Adopt them (sync into git via a pull "
              "request) or rerun with allow_drift to overwrite them (they are kept in the backup).")
        return 2
    if drifted:
        print("\n--allow-drift: the deploy will overwrite the edits above (kept in the backup).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
