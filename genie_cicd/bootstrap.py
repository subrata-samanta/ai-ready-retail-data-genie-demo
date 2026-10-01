"""Compile the reviewed Genie design in genie/ into a first serialized space (version 1 of the file).

    python -m genie_cicd bootstrap        # writes genie/space/freshcart_assistant.geniespace.json

Use this once, to start version control from the design in genie/agent_config.yaml. After that the
`.geniespace.json` file is the source of truth: changes are made in the dev space (UI) or in the
file, and flow through `pull` -> pull request -> CI -> QA -> prod.

Field names follow the serialized-space format Databricks publishes in its bundle examples and CLI
tests (version, config.sample_questions, data_sources.tables + column_configs,
instructions.text_instructions / example_question_sqls (+ parameters) / sql_functions /
sql_snippets, benchmarks.questions). Three areas are taken from Databricks solution guidance and
should be confirmed on your workspace with one round trip (deploy to dev, then `pull`):
data_sources.metric_views, the column_configs flags `exclude` / `enable_entity_matching`, and the
`synonyms` / `instruction` fields of sql_snippets. The export Databricks returns is authoritative:
commit what `pull` writes.

Ids are derived from stable keys (md5 of kind + key), so re-running bootstrap gives the same ids and
benchmark history stays comparable.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from . import spec as S

ROOT = Path(__file__).resolve().parent.parent
GENIE_DIR = ROOT / "genie"
DEFAULT_OUT = GENIE_DIR / "space" / "freshcart_assistant.geniespace.json"
SOURCE_CATALOG = "freshcart"      # the catalog name the design files are written against

_TYPE_HINT = {"string": "STRING", "integer": "INTEGER", "int": "INTEGER", "double": "DOUBLE", "date": "DATE"}

# Concrete values used to turn the two parameterized examples into regression benchmarks.
EXAMPLE_PARAMS = {
    "03_store_week_by_week.sql": {"store_name": "FreshCart Boston Seaport", "fiscal_year": "2026"},
    "06_product_across_regions.sql": {"product_search": "chips"},
}


def _id(kind: str, key: str) -> str:
    return hashlib.md5(f"{kind}:{key}".encode("utf-8")).hexdigest()


def _neutral(text: str) -> str:
    return re.sub(r"\b" + SOURCE_CATALOG + r"\.(?=(" + "|".join(S.SCHEMAS) + r")\.)", S.TOKEN + ".", text)


def _strip_header(sql: str) -> str:
    """Example files start with -- Title / -- Usage guidance / -- Parameters comments; Genie keeps
    those as separate fields, so only the SQL body goes into `sql`."""
    lines = sql.splitlines()
    while lines and (lines[0].startswith("--") or not lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines).strip().rstrip(";")


def build(genie_dir: Path = GENIE_DIR) -> dict:
    cfg = yaml.safe_load((genie_dir / "agent_config.yaml").read_text(encoding="utf-8"))
    bench = yaml.safe_load((genie_dir / "benchmarks.yaml").read_text(encoding="utf-8"))["benchmarks"]

    hidden = {t: set(cols) for t, cols in (cfg.get("hidden_columns") or {}).items()}
    matched = {t: set(cols) for t, cols in (cfg.get("entity_matching", {}).get("enabled") or {}).items()}

    tables, metric_views = [], []
    for src in cfg["data_sources"]:
        ident = _neutral(src["object"])
        if ".semantic." in ident:
            metric_views.append({"identifier": ident})
            continue
        cols = sorted(hidden.get(src["object"], set()) | matched.get(src["object"], set()))
        entry = {"identifier": ident}
        if cols:
            entry["column_configs"] = []
            for c in cols:
                cc = {"column_name": c}
                if c in hidden.get(src["object"], set()):
                    cc["exclude"] = True
                if c in matched.get(src["object"], set()):
                    cc.update(get_example_values=True, build_value_dictionary=True, enable_entity_matching=True)
                entry["column_configs"].append(cc)
        tables.append(entry)

    # General instructions, plus the usage guidance of each example (kept as text so it reaches Genie)
    text = (genie_dir / cfg["general_instructions_file"]).read_text(encoding="utf-8").strip()
    guidance = [f"- {e['title']} {e['usage_guidance']}" for e in cfg["example_queries"] if e.get("usage_guidance")]
    if guidance:
        text += "\n\n## Guidance for the example queries\n" + "\n".join(guidance)
    text_instructions = [{"id": _id("text", "general"), "content": S.text_lines(_neutral(text))}]

    examples, regression = [], []
    for e in cfg["example_queries"]:
        body = _neutral(_strip_header((genie_dir / e["file"]).read_text(encoding="utf-8")))
        item = {"id": _id("example", e["file"]), "question": [e["title"]], "sql": S.text_lines(body)}
        name = Path(e["file"]).name
        if e.get("parameters"):
            item["parameters"] = []
            for p in e["parameters"]:
                param = {"name": p["name"], "type_hint": _TYPE_HINT[p["type"].lower()], "description": [p["comment"]]}
                if p["name"] in EXAMPLE_PARAMS.get(name, {}):
                    param["default_value"] = {"values": [EXAMPLE_PARAMS[name][p["name"]]]}
                item["parameters"].append(param)
        examples.append(item)
        # Every trusted example is also a regression benchmark: Genie must keep answering it the same way.
        if not e.get("parameters"):
            regression.append({"id": _id("benchmark", "example:" + e["file"]), "question": [e["title"]],
                               "answer": [{"format": "SQL", "content": S.text_lines(body)}]})

    snippets = {"filters": [], "expressions": []}
    for x in cfg.get("sql_expressions", []):
        kind = "filters" if x["type"] == "filter" else "expressions"
        item = {"id": _id("snippet", x["name"]), "display_name": x["name"], "sql": [x["code"]],
                "synonyms": x.get("synonyms", []), "instruction": [x["instructions"]]}
        if kind == "expressions":
            item["alias"] = re.sub(r"\W+", "_", x["name"].lower()).strip("_")
        snippets[kind].append(item)

    benchmarks = regression + [
        {"id": _id("benchmark", str(b["id"])), "question": [b["question"]],
         "answer": [{"format": "SQL", "content": S.text_lines(_neutral(b["expected_sql"].strip()))}]}
        for b in bench if b.get("expected_sql")
    ]

    space = {
        "version": 2,
        "config": {"sample_questions": [{"id": _id("sample", q), "question": [q]} for q in cfg["common_questions"]]},
        "data_sources": {"tables": tables, "metric_views": metric_views},
        "instructions": {
            "text_instructions": text_instructions,
            "example_question_sqls": examples,
            "sql_functions": [{"id": _id("function", f), "identifier": _neutral(f)} for f in cfg.get("functions", [])],
            "sql_snippets": {k: v for k, v in snippets.items() if v},
        },
        "benchmarks": {"questions": benchmarks},
    }
    return S.normalise(space)


def main(out: Path = DEFAULT_OUT) -> Path:
    S.save(build(), out)
    print(f"wrote {out.relative_to(ROOT)}  (content hash {S.content_hash(S.load(out))})")
    return out


if __name__ == "__main__":
    main()
