"""Shared helpers for the scripts in this folder. They never deploy anything: deployment is always
`databricks bundle deploy`.

    bundle_config()              databricks.yml, parsed
    catalog_for(target)          a target's `catalog` variable
    load_space() / dump_space()  the space JSON, in the exact format `bundle generate` writes
    catalogs_in(space)           catalogs used by the space's data sources and functions
    rewrite_catalog(text, a, b)  every three-part name a.<schema>.<object> -> b.<schema>.<object>
    validate(space)              static checks that need no workspace (errors, warnings)
    fix(space)                   adds missing ids and sorts id lists, as `bundle generate` does
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
KEY = "genie_space"                                       # the resource key in resources/*.yml
SPACE_FILE = ROOT / "src" / f"{KEY}.geniespace.json"
RESOURCE_FILE = ROOT / "resources" / f"{KEY}.genie_space.yml"
BUNDLE_FILE = ROOT / "databricks.yml"
PLACEHOLDER = "CHANGE_ME"

# Lists whose items carry a 32-hex id; Genie requires them sorted by id.
ID_SECTIONS = {
    ("config", "sample_questions"): "sample question",
    ("instructions", "text_instructions"): "text instruction",
    ("instructions", "example_question_sqls"): "example SQL",
    ("instructions", "sql_functions"): "trusted function",
    ("instructions", "join_specs"): "join",
    ("instructions", "sql_snippets", "filters"): "SQL filter",
    ("instructions", "sql_snippets", "expressions"): "SQL expression",
    ("instructions", "sql_snippets", "measures"): "SQL measure",
    ("benchmarks", "questions"): "benchmark",
}
SOURCE_SECTIONS = {("data_sources", "tables"): "table", ("data_sources", "metric_views"): "metric view"}
V1_COLUMN_FIELDS = {"get_example_values": "enable_format_assistance", "build_value_dictionary": "enable_entity_matching"}
GENIE_MAX_SOURCES = 30
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


# ------------------------------------------------------------------------------------ bundle config
def bundle_config() -> dict:
    return yaml.safe_load(BUNDLE_FILE.read_text(encoding="utf-8"))


def targets() -> list[str]:
    return list(bundle_config().get("targets") or {})


def catalog_for(target: str) -> str:
    cfg = bundle_config()
    if target not in (cfg.get("targets") or {}):
        raise SystemExit(f"unknown target {target!r}; databricks.yml has {targets()}")
    value = ((cfg["targets"][target] or {}).get("variables") or {}).get("catalog") \
        or (cfg.get("variables", {}).get("catalog") or {}).get("default")
    if not value:
        raise SystemExit(f"target {target!r} sets no `catalog` variable in databricks.yml")
    return value


def placeholders_left() -> list[str]:
    """Lines of databricks.yml that still hold CHANGE_ME (comments ignored)."""
    out = []
    for n, line in enumerate(BUNDLE_FILE.read_text(encoding="utf-8").splitlines(), 1):
        if PLACEHOLDER in line.split("#", 1)[0]:
            out.append(f"databricks.yml:{n}: {line.strip()}")
    return out


# ---------------------------------------------------------------------------------------- space io
def load_space(path: Path = SPACE_FILE) -> dict:
    if not path.exists():
        raise SystemExit(f"{path.relative_to(ROOT)} does not exist yet: run scripts/adopt_space.py first")
    return json.loads(path.read_text(encoding="utf-8"))


def dump_space(space: dict) -> str:
    """The format `databricks bundle generate genie-space` writes, so a sync gives a clean diff."""
    return json.dumps(space, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def get(space: dict, path: tuple) -> list:
    node = space
    for key in path:
        node = node.get(key, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, list) else []


def text(lines) -> str:
    return "".join(lines) if isinstance(lines, list) else str(lines or "")


def label(item: dict) -> str:
    for key in ("question", "identifier", "title", "content", "sql"):
        if item.get(key):
            return text(item[key]).strip().splitlines()[0][:70]
    return str(item.get("id", "?"))


# ---------------------------------------------------------------------------------------- catalogs
def source_identifiers(space: dict) -> dict[str, str]:
    """identifier -> kind, for every table, metric view and trusted function."""
    out = {}
    for path, kind in {**SOURCE_SECTIONS, ("instructions", "sql_functions"): "function"}.items():
        for item in get(space, path):
            if item.get("identifier"):
                out[item["identifier"]] = kind
    return out


def catalogs_in(space: dict) -> set[str]:
    return {ident.split(".")[0].strip("`") for ident in source_identifiers(space) if ident.count(".") >= 2}


def _three_part(catalog: str) -> re.Pattern:
    # `catalog`.schema.object or catalog.schema.object, never alias.column (two parts) or a longer name
    return re.compile(r"(?<![\w`.])(`?)" + re.escape(catalog) + r"\1(?=\.`?\w+`?\.`?\w)")


def rewrite_catalog(blob: str, old: str, new: str) -> tuple[str, int]:
    """Replace the catalog of every three-part name; returns the new text and the number of replacements."""
    if old == new:
        return blob, 0
    return _three_part(old).subn(lambda m: f"{m.group(1)}{new}{m.group(1)}", blob)


# ---------------------------------------------------------------------------------------- validate
def validate(space: dict, catalog: str | None = None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(space.get("version"), int):
        errors.append("missing integer 'version'")
    elif space["version"] >= 2:
        for t in get(space, ("data_sources", "tables")):
            for c in t.get("column_configs") or []:
                for old, new in V1_COLUMN_FIELDS.items():
                    if old in c:
                        errors.append(f"{t.get('identifier')}.{c.get('column_name')}: '{old}' is a version 1 "
                                      f"field; version 2 uses '{new}'")
    n_sources = sum(len(get(space, p)) for p in SOURCE_SECTIONS)
    if not n_sources:
        errors.append("the space has no data sources")
    elif n_sources > GENIE_MAX_SOURCES:
        errors.append(f"{n_sources} data sources; Genie allows at most {GENIE_MAX_SOURCES}")
    for path, kind in ID_SECTIONS.items():
        items = get(space, path)
        ids = [str(i.get("id", "")) for i in items]
        for item, i in zip(items, ids):
            if not _HEX32.match(i):
                errors.append(f"{kind} {label(item)!r}: id {i!r} is not 32 lowercase hex characters")
        if len(set(ids)) != len(ids):
            errors.append(f"duplicate ids among the {kind}s")
        if ids != sorted(ids):
            errors.append(f"{kind}s are not sorted by id")
    if catalog is not None:
        other = sorted(catalogs_in(space) - {catalog})
        if other:
            errors.append(f"data sources read catalog(s) {other}; the JSON must use the dev catalog {catalog!r} "
                          f"(scripts/set_catalog.py rewrites it for qa and prod)")
    for item in get(space, ("benchmarks", "questions")):
        if not any(a.get("format") == "SQL" and a.get("content") for a in item.get("answer", [])):
            errors.append(f"benchmark {label(item)!r} has no SQL answer, so the QA gate cannot grade it")
    for item in get(space, ("instructions", "example_question_sqls")):
        if not item.get("sql"):
            errors.append(f"example {label(item)!r} has no SQL")
    return errors, warnings


def readiness(space: dict) -> list[tuple[str, str, str]]:
    """(status, check, advice) rows: the practices that made the FreshCart space accurate."""
    n_sources = sum(len(get(space, p)) for p in SOURCE_SECTIONS)
    n_metric_views = len(get(space, ("data_sources", "metric_views")))
    n_text = sum(len(text(i.get("content"))) for i in get(space, ("instructions", "text_instructions")))
    n_examples = len(get(space, ("instructions", "example_question_sqls")))
    n_functions = len(get(space, ("instructions", "sql_functions")))
    n_snippets = sum(len(get(space, ("instructions", "sql_snippets", k))) for k in ("filters", "expressions", "measures"))
    n_samples = len(get(space, ("config", "sample_questions")))
    n_bench = len(get(space, ("benchmarks", "questions")))
    n_matched = sum(1 for t in get(space, ("data_sources", "tables")) for c in t.get("column_configs") or []
                    if c.get("enable_entity_matching"))

    def row(ok: bool, warn: bool, check: str, advice: str):
        return ("ok" if ok else "warn" if warn else "info", check, advice)

    return [
        row(n_sources <= 12, True, f"{n_sources} data sources",
            "keep a space focused: 5-12 curated tables or metric views for one business domain"),
        row(n_metric_views > 0, False, f"{n_metric_views} metric views",
            "define each KPI once in a Unity Catalog metric view (FreshCart: semantic.sales_metrics)"),
        row(n_text >= 300, True, f"{n_text} characters of general instructions",
            "describe the business: fiscal calendar, currency, definitions, when to ask a clarifying question"),
        row(n_examples >= 5, True, f"{n_examples} example SQL queries",
            "add a trusted example query for each of the most frequent questions"),
        row(n_functions + n_snippets > 0, False, f"{n_functions} trusted functions, {n_snippets} SQL snippets",
            "encode tricky logic (like-for-like, YoY) as a trusted function or SQL expression"),
        row(n_matched > 0, False, f"{n_matched} columns with entity matching",
            "turn on entity matching for names users type: stores, products, regions, customers"),
        row(n_samples >= 3, True, f"{n_samples} sample questions", "show users 3-5 questions the space answers well"),
        row(n_bench >= 10, True, f"{n_bench} benchmark questions",
            "write 10-30 benchmarks with expected SQL; the QA gate (tests/benchmark_gate.py) grades them"),
    ]


def fix(space: dict) -> tuple[dict, list[str]]:
    """Give items without a valid id a new one and sort every id list (what `bundle generate` produces)."""
    changes = []
    for path, kind in ID_SECTIONS.items():
        items = get(space, path)
        for item in items:
            if not _HEX32.match(str(item.get("id", ""))):
                item["id"] = uuid.uuid4().hex
                changes.append(f"new id for {kind} {label(item)!r}")
        if [i["id"] for i in items] != sorted(i["id"] for i in items):
            items.sort(key=lambda i: i["id"])
            changes.append(f"sorted the {kind}s by id")
    return space, changes
