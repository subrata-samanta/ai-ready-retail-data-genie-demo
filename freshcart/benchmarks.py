"""Run the Genie space's example queries and benchmark answers on the local warehouse.

The Genie space has a single definition: genie_bundle/resources/freshcart_assistant.space.yml (the
Declarative Automation Bundle deploys it to every environment). This module reads it, so the docs, the
tests and CI all check exactly what is deployed. Genie itself runs on Databricks, so this does not test
Genie. It does three useful things:
  1. proves every example and benchmark SQL is valid against the demo data
  2. records the expected answer for each benchmark (shown in docs/07-genie-agent.md)
  3. lets you compare: ask Genie the question on Databricks, check it matches this answer

    python -m freshcart.benchmarks
"""
from __future__ import annotations

import re

import yaml

from . import config as C
from . import metrics
from .db import connect

SPACE_FILE = C.GENIE_BUNDLE_DIR / "resources" / "freshcart_assistant.space.yml"
NOTES_FILE = C.GENIE_BUNDLE_DIR / "benchmark_notes.yml"
SPACE_VAR = "freshcart_assistant_space"

# ${var.catalog} (bundle) or an environment's catalog -> the local catalog name the metric engine maps
_CATALOG = re.compile(r"(\$\{var\.catalog\}|\b(?:freshcart_dev|freshcart_qa|freshcart))\.(?=(?:gold|semantic|silver)\.)")


def local_sql(sql: str) -> str:
    return _CATALOG.sub("freshcart.", sql)


def run_sql(con, sql: str, params: dict | None = None):
    """Run Genie SQL (any environment's catalog) on the local warehouse. Returns (columns, rows)."""
    return metrics.run(con, local_sql(sql), params or {})


def _text(lines) -> str:
    return "".join(lines) if isinstance(lines, list) else str(lines or "")


def load_space(path=SPACE_FILE) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["variables"][SPACE_VAR]["default"]


def load_examples(space: dict | None = None) -> list[dict]:
    space = space or load_space()
    out = []
    for e in (space.get("instructions") or {}).get("example_question_sqls", []):
        params = {p["name"]: (p.get("default_value") or {}).get("values", [None])[0] for p in e.get("parameters", [])}
        out.append({"id": e["id"], "title": _text(e["question"]), "sql": _text(e["sql"]).strip(), "params": params})
    return out


def load_benchmarks(space: dict | None = None) -> list[dict]:
    """The space's benchmarks with their reviewer notes, then the review-only questions."""
    space = space or load_space()
    notes = yaml.safe_load(NOTES_FILE.read_text(encoding="utf-8")) if NOTES_FILE.exists() else {}
    note_by_id = notes.get("notes") or {}
    rank = {k: i for i, k in enumerate(note_by_id)}
    examples = {e["title"] for e in load_examples(space)}
    out = []
    for b in (space.get("benchmarks") or {}).get("questions", []):
        question = _text(b["question"])
        note = note_by_id.get(b["id"]) or ({"tests": "Regression check of the trusted example query",
                                             "correct_when": "same result as the example query"}
                                            if question in examples else {"tests": "", "correct_when": ""})
        out.append({"id": b["id"], "question": question, "expected_sql": _text(b["answer"][0]["content"]).strip()
                    if b.get("answer") else None, **note})
    out.sort(key=lambda b: (rank.get(b["id"], len(rank)), b["question"]))
    return out + [{"id": None, "expected_sql": None, **r} for r in notes.get("review_only") or []]


def run_benchmarks(con, space: dict | None = None):
    results = []
    for b in load_benchmarks(space):
        if not b.get("expected_sql"):
            results.append((b, None, None))
            continue
        cols, rows = run_sql(con, b["expected_sql"])
        results.append((b, cols, rows))
    return results


def run_examples(con, space: dict | None = None):
    return [(e["title"], e["params"], *run_sql(con, e["sql"], e["params"])) for e in load_examples(space)]


def sql_failures(con, space: dict | None = None) -> list[str]:
    """Every example or benchmark whose SQL does not run on the local warehouse."""
    failures = []
    for e in load_examples(space):
        try:
            run_sql(con, e["sql"], e["params"])
        except Exception as ex:                                     # noqa: BLE001
            failures.append(f"example {e['title']!r} fails locally: {ex}")
    for b in load_benchmarks(space):
        if b.get("expected_sql"):
            try:
                run_sql(con, b["expected_sql"])
            except Exception as ex:                                 # noqa: BLE001
                failures.append(f"benchmark {b['question']!r} fails locally: {ex}")
    return failures


def main():
    con = connect()
    for i, (b, cols, rows) in enumerate(run_benchmarks(con), start=1):
        status = "review" if cols is None else f"{len(rows)} row(s)"
        print(f"#{i:>2} {b['question']:<62} {status}")
        if rows:
            print(f"     {cols} -> {rows[0]}")
    print()
    for title, params, cols, rows in run_examples(con):
        print(f"{title:<62} {len(rows)} row(s) {params or ''}")


if __name__ == "__main__":
    main()
