"""Run every example SQL and benchmark answer of the space against an environment's data, on its SQL warehouse.

    python scripts/check_sql.py -t dev

Catches a broken query (a renamed column, a missing table, a typo) before anything is deployed: genie-ci runs it
on every pull request with the dev credentials. Parameterised examples run with their default values; one without
defaults is skipped. Uses the standard Databricks authentication (environment variables or a profile).
Exit code 1 if any query fails.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genie_tools as T                                        # noqa: E402

_PARAM = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


def _value(x):
    return getattr(x, "value", x)


def statements(space: dict) -> list[dict]:
    """[{label, sql, params: {name: (value, type)} | None (= has parameters without defaults)}]."""
    defaults = {}
    for ex in space.get("instructions", {}).get("example_question_sqls", []):
        for p in ex.get("parameters") or []:
            values = (p.get("default_value") or {}).get("values") or []
            if values:
                defaults[(T.text(ex.get("sql")), p["name"])] = (str(values[0]), p.get("type_hint") or "STRING")
    out = []
    for label, sql in T.sql_statements(space):
        names = sorted(set(_PARAM.findall(sql)))
        params = {n: defaults.get((sql, n)) for n in names}
        out.append({"label": label, "sql": sql, "params": None if any(v is None for v in params.values()) else params})
    return out


def run(w, warehouse_id: str, items: list[dict], timeout_s: int = 120) -> list[dict]:
    from databricks.sdk.service.sql import StatementParameterListItem
    results = []
    for it in items:
        if it["params"] is None:
            results.append({**it, "result": "skipped (parameters without default values)"})
            continue
        params = [StatementParameterListItem(name=n, value=v, type=t) for n, (v, t) in it["params"].items()]
        resp = w.statement_execution.execute_statement(statement=it["sql"], warehouse_id=warehouse_id, row_limit=5,
                                                       wait_timeout="30s", parameters=params or None)
        deadline = time.time() + timeout_s
        while _value(resp.status.state) in ("PENDING", "RUNNING") and time.time() < deadline:
            time.sleep(3)
            resp = w.statement_execution.get_statement(resp.statement_id)
        state = _value(resp.status.state)
        if state == "SUCCEEDED":
            results.append({**it, "result": "ok"})
        else:
            err = getattr(resp.status, "error", None)
            results.append({**it, "result": f"FAILED ({state}): {getattr(err, 'message', '') or ''}".strip()})
    return results


def warehouse_by_name(w, name: str) -> str:
    for wh in w.warehouses.list():
        if wh.name == name:
            return wh.id
    raise SystemExit(f"no SQL warehouse named {name!r} (warehouse_name in genie.config.yml)")


def main(argv=None, client=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", "--target", default="dev")
    args = ap.parse_args(argv)
    if client is None:
        from databricks.sdk import WorkspaceClient
        client = WorkspaceClient()
    s = T.settings(args.target)
    rendered = T.for_target(T.load_space(), args.target)
    results = run(client, warehouse_by_name(client, s["warehouse_name"]), statements(rendered))
    failed = [r for r in results if r["result"].startswith("FAILED")]
    for r in results:
        print(f"{'FAIL' if r in failed else ' ok '}  {r['label']}: {r['result']}")
    print(f"{len(results) - len(failed)}/{len(results)} queries ran on {s['catalog']}.{s['schema']} ({args.target})")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"### SQL of the space against {args.target}: {'✅' if not failed else '❌'}\n\n"
                     + "\n".join(f"- {'❌' if r in failed else '✅'} {r['label']}: {r['result']}" for r in results) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
