"""Helpers for the Genie space definition in the bundle. They never deploy anything: deployment is
always `databricks bundle deploy`. They read and write
resources/freshcart_assistant.space.yml, which defines the bundle variable `freshcart_assistant_space`
(the space's serialized content) with tables written as `${var.catalog}.<schema>.<object>`.

    load_space() / save_space()   read / write the definition (canonical: sorted, stable diffs)
    neutralise(space, catalog)    an exported space (freshcart_dev.gold...) -> ${var.catalog}.gold...
    render(space, catalog)        the reverse, what the bundle does for a target
    diff(old, new)                readable change list for pull requests and drift reports
    validate(space)               static checks that need no workspace
    check_sql_locally(space)      run every example and benchmark SQL on the local FreshCart warehouse
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
from pathlib import Path

import yaml

BUNDLE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BUNDLE_DIR.parent
SPACE_KEY = "freshcart_assistant"
SPACE_VAR = f"{SPACE_KEY}_space"
SPACE_FILE = BUNDLE_DIR / "resources" / f"{SPACE_KEY}.space.yml"
CATALOG_REF = "${var.catalog}"
# each target's catalog, read from databricks.yml (the only place it is written)
TARGET_CATALOGS = {t: cfg["variables"]["catalog"] for t, cfg in
                   yaml.safe_load((BUNDLE_DIR / "databricks.yml").read_text(encoding="utf-8"))["targets"].items()}
SCHEMAS = ("bronze", "silver", "gold", "semantic", "governance")

_HEADER = """\
# Content of the Genie space `{key}`: data sources, instructions, example SQL, SQL expressions,
# trusted functions, sample questions and benchmarks, in Databricks' serialized-space format.
#
# Tables are written as ${{var.catalog}}.<schema>.<object>; the bundle fills in each target's catalog.
# Change the space in the dev Genie UI and let genie-dev-sync (or scripts/sync_from_workspace.py)
# write this file, or edit it here and deploy to dev first. Keep it canonical: scripts/validate_space.py.
"""

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_SORT_KEYS = ("id", "identifier", "column_name", "name")
_ID_SECTIONS = {
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
_IDENTIFIER_SECTIONS = {("data_sources", "tables"): "table", ("data_sources", "metric_views"): "metric view"}
_REF = re.compile(re.escape(CATALOG_REF) + r"\.(" + "|".join(SCHEMAS) + r")\.(\w+)")


# ------------------------------------------------------------------------------------------- io
def load_space(path: Path = SPACE_FILE) -> dict:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return doc["variables"][SPACE_VAR]["default"]


class _Dumper(yaml.SafeDumper):
    """One line per SQL / instruction line: strings containing a newline are written double-quoted."""


def _str(dumper, value):
    style = '"' if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)


def dumps_space(space: dict) -> str:
    doc = {"variables": {SPACE_VAR: {
        "type": "complex",
        "description": "Serialized content of the FreshCart Genie space (see the header of this file)",
        "default": normalise(space),
    }}}
    return _HEADER.format(key=SPACE_KEY) + yaml.dump(doc, Dumper=_Dumper, sort_keys=True, allow_unicode=True,
                                                     width=1000, default_flow_style=False)


def save_space(space: dict, path: Path = SPACE_FILE) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(dumps_space(space), encoding="utf-8")


def is_canonical(path: Path = SPACE_FILE) -> bool:
    return Path(path).read_text(encoding="utf-8") == dumps_space(load_space(path))


# ------------------------------------------------------------------------------------ normalise
def normalise(value):
    """Lists of objects sorted by id / identifier / column_name / name; lists of strings untouched."""
    if isinstance(value, dict):
        return {k: normalise(v) for k, v in value.items()}
    if isinstance(value, list):
        items = [normalise(v) for v in value]
        if items and all(isinstance(v, dict) for v in items):
            for key in _SORT_KEYS:
                if all(key in v for v in items):
                    return sorted(items, key=lambda v: str(v[key]))
        return items
    return value


def content_hash(space: dict) -> str:
    return hashlib.sha256(json.dumps(normalise(space), sort_keys=True).encode()).hexdigest()[:12]


def _map_strings(value, fn):
    if isinstance(value, dict):
        return {k: _map_strings(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_map_strings(v, fn) for v in value]
    return fn(value) if isinstance(value, str) else value


def _catalog_pattern(catalog: str) -> re.Pattern:
    return re.compile(r"(?<![\w`.{])`?" + re.escape(catalog) + r"`?\.(?=(" + "|".join(SCHEMAS) + r")\.)")


def neutralise(space: dict, catalog: str) -> dict:
    """An exported space reads `freshcart_dev.gold.x`; in git it must read `${var.catalog}.gold.x`."""
    pattern = _catalog_pattern(catalog)
    return normalise(_map_strings(space, lambda s: pattern.sub(CATALOG_REF + ".", s)))


def render(space: dict, catalog: str) -> dict:
    return normalise(_map_strings(space, lambda s: s.replace(CATALOG_REF, catalog)))


def _get(space: dict, path: tuple) -> list:
    node = space
    for key in path:
        node = node.get(key, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, list) else []


def text(lines) -> str:
    return "".join(lines) if isinstance(lines, list) else str(lines or "")


def referenced_objects(space: dict) -> set[str]:
    found: set[str] = set()
    _map_strings(space, lambda s: found.update(f"{CATALOG_REF}.{m.group(1)}.{m.group(2)}" for m in _REF.finditer(s)) or s)
    return found


def declared_objects(space: dict) -> dict[str, str]:
    out = {}
    for path, kind in {**_IDENTIFIER_SECTIONS, ("instructions", "sql_functions"): "function"}.items():
        for item in _get(space, path):
            if "identifier" in item:
                out[item["identifier"]] = kind
    return out


# ------------------------------------------------------------------------------------------ diff
def _label(item: dict) -> str:
    for key in ("question", "display_name", "alias", "identifier", "title"):
        if key in item:
            v = item[key]
            return " ".join((" ".join(v) if isinstance(v, list) else str(v)).split())[:80]
    if "content" in item:
        return " ".join(text(item["content"]).split()).lstrip("# ")[:60] + "..."
    return str(item.get("id", "?"))


def diff(old: dict, new: dict) -> list[str]:
    """What changed, item by item, with the changed lines: what a reviewer reads."""
    old, new = normalise(old or {}), normalise(new or {})
    out: list[str] = []
    sections = [(p, k, "id") for p, k in _ID_SECTIONS.items()] + [(p, k, "identifier") for p, k in _IDENTIFIER_SECTIONS.items()]
    for path, kind, key in sections:
        before = {i.get(key): i for i in _get(old, path)}
        after = {i.get(key): i for i in _get(new, path)}
        for k in sorted(after.keys() - before.keys(), key=str):
            out.append(f"+ added {kind}: {_label(after[k])}")
        for k in sorted(before.keys() - after.keys(), key=str):
            out.append(f"- removed {kind}: {_label(before[k])}")
        for k in sorted(before.keys() & after.keys(), key=str):
            if before[k] != after[k]:
                out.append(f"~ changed {kind}: {_label(after[k])}")
                a = json.dumps(before[k], indent=2, sort_keys=True).splitlines()
                b = json.dumps(after[k], indent=2, sort_keys=True).splitlines()
                out += ["      " + line for line in difflib.unified_diff(a, b, lineterm="", n=1)
                        if not line.startswith(("---", "+++", "@@"))]
    known = {p[0] for p in list(_ID_SECTIONS) + list(_IDENTIFIER_SECTIONS)}
    for key in sorted((old.keys() | new.keys()) - known):
        if old.get(key) != new.get(key):
            out.append(f"~ changed {key}: {old.get(key)!r} -> {new.get(key)!r}")
    return out


def count_changes(changes: list[str]) -> int:
    return sum(1 for c in changes if not c.startswith(" "))


def summary(space: dict) -> dict:
    out = {kind: len(_get(space, path)) for path, kind in {**_IDENTIFIER_SECTIONS, **_ID_SECTIONS}.items()}
    return {k: v for k, v in out.items() if v}


# -------------------------------------------------------------------------------------- validate
V1_COLUMN_FIELDS = {"get_example_values": "enable_format_assistance", "build_value_dictionary": "enable_entity_matching"}


def validate(space: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(space.get("version"), int):
        errors.append("missing integer 'version'")
    elif space["version"] >= 2:                  # Genie rejects version 1 column fields in a version 2 space
        for t in _get(space, ("data_sources", "tables")):
            for c in t.get("column_configs") or []:
                for old, new in V1_COLUMN_FIELDS.items():
                    if old in c:
                        errors.append(f"{t.get('identifier')}.{c.get('column_name')}: '{old}' is a version 1 field; "
                                      f"version 2 uses '{new}'")
    if not _get(space, ("data_sources", "tables")) and not _get(space, ("data_sources", "metric_views")):
        errors.append("the space has no data sources")
    groups = {"instructions": [p for p in _ID_SECTIONS if p[0] == "instructions"],
              "questions": [("config", "sample_questions"), ("benchmarks", "questions")]}
    for group, paths in groups.items():
        seen: dict = {}
        for path in paths:
            for item in _get(space, path):
                i = item.get("id")
                if not i:
                    errors.append(f"{_ID_SECTIONS[path]} without an id: {_label(item)}")
                    continue
                if not _HEX32.match(str(i)):
                    warnings.append(f"{_ID_SECTIONS[path]} id {i!r} is not 32 lowercase hex characters")
                if i in seen:
                    errors.append(f"duplicate id {i} in {group} ({seen[i]} and {_ID_SECTIONS[path]})")
                seen[i] = _ID_SECTIONS[path]
    blob = json.dumps(space)
    for target, cat in TARGET_CATALOGS.items():
        if _catalog_pattern(cat).search(blob.replace("\\n", "\n")):
            errors.append(f"hard-coded catalog '{cat}.' found; write {CATALOG_REF}. so every target works")
    if "{{catalog}}" in blob:
        errors.append("found '{{catalog}}'; this bundle uses " + CATALOG_REF)
    for ident in declared_objects(space):
        if not ident.startswith(CATALOG_REF + "."):
            warnings.append(f"data source {ident} does not use {CATALOG_REF} and is the same in every target")
    for ref in sorted(referenced_objects(space) - set(declared_objects(space))):
        warnings.append(f"SQL references {ref}, which is not a data source or function of the space")
    for item in _get(space, ("benchmarks", "questions")):
        for answer in item.get("answer", []):
            if answer.get("format") != "SQL" or not answer.get("content"):
                errors.append(f"benchmark {_label(item)!r} has an answer that is not SQL")
    for item in _get(space, ("instructions", "example_question_sqls")):
        if not item.get("sql"):
            errors.append(f"example {_label(item)!r} has no SQL")
    return errors, warnings


def check_sql_locally(space: dict) -> list[str]:
    """Run every example and benchmark SQL on the local SQLite warehouse (built if missing)."""
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from freshcart import benchmarks, config as C, pipeline
    from freshcart.db import connect
    if not (C.WAREHOUSE_DIR / "gold.db").exists():
        pipeline.run(full_refresh=True, verbose=False)
    return benchmarks.sql_failures(connect(), space)


def copy_space(space: dict) -> dict:
    return copy.deepcopy(space)
