"""The template's library: project configuration and the Genie space definition. Nothing here deploys;
deploying is always `databricks bundle deploy`.

Configuration (genie.config.yml + databricks.yml)
    project_name()               the bundle name
    targets()                    sandbox, dev, qa, prod (whatever the config defines)
    settings(target)             every variable resolved for a target, offline (lookups are left as None)

Space definition (space/genie_space.yml: the bundle variable `genie_space`)
    load_space() / save_space()  read / write it in canonical form (sorted, one line per SQL line: clean diffs)
    neutralise(space, cat, sch)  an exported space (acme_dev.sales.orders) -> ${var.catalog}.${var.schema}.orders
    render(space, cat, sch)      the reverse: what the bundle deploys to a target
    diff(old, new)               the change list reviewers read (pull requests, drift, release notes)
    validate(space)              static checks that need no workspace
    sql_statements(space)        every example and benchmark SQL, to run against an environment
    content_hash(space)          a short fingerprint: which version is live where

    python scripts/genie_tools.py                 print the project and every environment's settings
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_DIR / "genie.config.yml"
BUNDLE_FILE = PROJECT_DIR / "databricks.yml"
SPACE_FILE = PROJECT_DIR / "space" / "genie_space.yml"
SPACE_RESOURCE = "genie_space"                  # resources.genie_spaces.<this> in resources/genie_space.yml
SPACE_VAR = "genie_space"                       # the bundle variable holding the content
CATALOG_REF, SCHEMA_REF = "${var.catalog}", "${var.schema}"
ENVIRONMENTS = ("dev", "qa", "prod")            # deployed by CI/CD; sandbox is personal


# ===================================================================================== configuration
def _merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return {**a, **{k: _merge(a.get(k), v) if k in a else v for k, v in b.items()}}
    return b


def load_config() -> dict:
    """databricks.yml and genie.config.yml merged, as the bundle merges its included files."""
    out: dict = {}
    for f in (BUNDLE_FILE, CONFIG_FILE):
        if f.exists():
            out = _merge(out, yaml.safe_load(f.read_text(encoding="utf-8")) or {})
    return out


def project_name() -> str:
    return load_config()["bundle"]["name"]


def targets() -> list[str]:
    return list(load_config().get("targets", {}))


_REF_PATTERN = re.compile(r"\$\{(var|bundle)\.([\w]+)\}")


def settings(target: str) -> dict:
    """Variable values for a target: defaults, then the target's overrides, then ${var.x}, ${bundle.name} and
    ${bundle.target} substituted. A variable resolved by a lookup (warehouse_id) is None here."""
    cfg = load_config()
    if target not in cfg.get("targets", {}):
        raise KeyError(f"no target {target!r} in genie.config.yml (targets: {', '.join(cfg.get('targets', {}))})")
    values = {name: (None if "lookup" in (spec or {}) else (spec or {}).get("default"))
              for name, spec in (cfg.get("variables") or {}).items()}
    values.update((cfg["targets"][target] or {}).get("variables") or {})
    bundle = {"name": cfg["bundle"]["name"], "target": target}

    def sub(v, depth=0):
        if isinstance(v, str) and depth < 10:
            out = _REF_PATTERN.sub(lambda m: str((values if m.group(1) == "var" else bundle).get(m.group(2), m.group(0))), v)
            return sub(out, depth + 1) if out != v else out
        return v
    return {k: sub(v) for k, v in values.items()}


def space_title(target: str) -> str:
    s = settings(target)
    return s["space_title"] + (s.get("title_suffix") or "")


# ============================================================================= space: read and write
def load_space(path: Path = SPACE_FILE) -> dict:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return doc["variables"][SPACE_VAR]["default"]


_HEADER = """\
# Content of the Genie space: data sources, instructions, example SQL, SQL snippets, trusted functions,
# sample questions and benchmarks, in Databricks' serialized-space format (the bundle variable genie_space).
#
# Tables are written ${var.catalog}.${var.schema}.<table>; each environment's values come from genie.config.yml.
# Edit it in the dev Genie UI and let genie-dev-sync write this file, or edit it here in a pull request.
# Keep it canonical: python scripts/validate_space.py --fix
"""


class _Dumper(yaml.SafeDumper):
    """One line per SQL / instruction line: strings containing a newline are written double-quoted."""


_Dumper.add_representer(str, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style='"' if "\n" in v else None))


def dumps_space(space: dict) -> str:
    doc = {"variables": {SPACE_VAR: {"type": "complex", "description": "Serialized content of the Genie space",
                                     "default": normalise(space)}}}
    return _HEADER + yaml.dump(doc, Dumper=_Dumper, sort_keys=True, allow_unicode=True, width=1000,
                               default_flow_style=False)


def save_space(space: dict, path: Path = SPACE_FILE) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(dumps_space(space), encoding="utf-8")


def is_canonical(path: Path = SPACE_FILE) -> bool:
    return Path(path).read_text(encoding="utf-8") == dumps_space(load_space(path))


def space_from_text(text: str) -> dict:
    return yaml.safe_load(text)["variables"][SPACE_VAR]["default"]


# ========================================================================= space: normalise and map
_SORT_KEYS = ("id", "identifier", "column_name", "name")


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


def _q(name: str) -> str:
    return r"`?" + re.escape(name) + r"`?"


_START = r"(?<![\w`.{$])"


def neutralise(space: dict, catalog: str, schema: str | None = None) -> dict:
    """An exported space names real objects (acme_dev.sales.orders); in git they read
    ${var.catalog}.${var.schema}.orders, and other schemas of the catalog ${var.catalog}.<schema>.<object>."""
    patterns = []
    if schema:
        patterns.append((re.compile(_START + _q(catalog) + r"\." + _q(schema) + r"\."), f"{CATALOG_REF}.{SCHEMA_REF}."))
    patterns.append((re.compile(_START + _q(catalog) + r"\.(?=`?\w+`?\.)"), f"{CATALOG_REF}."))

    def fn(s):
        for p, repl in patterns:
            s = p.sub(repl, s)
        return s
    return normalise(_map_strings(space, fn))


def render(space: dict, catalog: str, schema: str | None = None) -> dict:
    def fn(s):
        s = s.replace(CATALOG_REF, catalog)
        return s.replace(SCHEMA_REF, schema) if schema is not None else s
    return normalise(_map_strings(space, fn))


def for_target(space: dict, target: str) -> dict:
    s = settings(target)
    return render(space, s["catalog"], s.get("schema"))


def from_target(space: dict, target: str) -> dict:
    s = settings(target)
    return neutralise(space, s["catalog"], s.get("schema"))


# ============================================================================= space: inspect
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
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _get(space: dict, path: tuple) -> list:
    node = space
    for key in path:
        node = node.get(key, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, list) else []


def text(lines) -> str:
    return "".join(lines) if isinstance(lines, list) else str(lines or "")


def data_sources(space: dict) -> dict[str, str]:
    """identifier -> 'table' | 'metric view' | 'function'."""
    out = {}
    for path, kind in {**_IDENTIFIER_SECTIONS, ("instructions", "sql_functions"): "function"}.items():
        for item in _get(space, path):
            if "identifier" in item:
                out[item["identifier"]] = kind
    return out


def schemas_used(space: dict) -> set[str]:
    """catalog.schema of every data source (as written: with ${var.catalog} / ${var.schema}, or rendered)."""
    return {ident.rsplit(".", 1)[0] for ident in data_sources(space) if ident.count(".") == 2}


_OBJECT_REF = re.compile(re.escape(CATALOG_REF) + r"\.(" + re.escape(SCHEMA_REF) + r"|\w+)\.(\w+)")


def referenced_objects(space: dict) -> set[str]:
    found: set[str] = set()
    for _, sql in sql_statements(space):
        found.update(m.group(0) for m in _OBJECT_REF.finditer(sql))
    return found


def sql_statements(space: dict) -> list[tuple[str, str]]:
    """(label, sql) for every example SQL and benchmark answer."""
    out = []
    for item in _get(space, ("instructions", "example_question_sqls")):
        out.append((f"example: {_label(item)}", text(item.get("sql"))))
    for item in _get(space, ("benchmarks", "questions")):
        for answer in item.get("answer", []):
            if answer.get("format") == "SQL":
                out.append((f"benchmark: {_label(item)}", text(answer.get("content"))))
    return out


def _label(item: dict) -> str:
    for key in ("question", "display_name", "alias", "identifier", "title"):
        if key in item:
            v = item[key]
            return " ".join((" ".join(v) if isinstance(v, list) else str(v)).split())[:80]
    if "content" in item:
        return " ".join(text(item["content"]).split()).lstrip("# ")[:60] + "..."
    return str(item.get("id", "?"))


def summary(space: dict) -> dict:
    out = {kind: len(_get(space, path)) for path, kind in {**_IDENTIFIER_SECTIONS, **_ID_SECTIONS}.items()}
    return {k: v for k, v in out.items() if v}


def new_id(*parts) -> str:
    """A 32-character hex id: stable when parts are given (reproducible files), random otherwise."""
    import uuid
    return hashlib.md5("|".join(map(str, parts)).encode()).hexdigest() if parts else uuid.uuid4().hex


# ================================================================================== space: diff
def diff(old: dict, new: dict) -> list[str]:
    """What changed, item by item, with the changed lines."""
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


# ============================================================================== space: validate
V1_COLUMN_FIELDS = {"get_example_values": "enable_format_assistance", "build_value_dictionary": "enable_entity_matching"}


def validate(space: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) of the definition, without a workspace."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(space.get("version"), int):
        errors.append("missing integer 'version' (use 2)")
    elif space["version"] >= 2:
        for t in _get(space, ("data_sources", "tables")):
            for c in t.get("column_configs") or []:
                for old, new in V1_COLUMN_FIELDS.items():
                    if old in c:
                        errors.append(f"{t.get('identifier')}.{c.get('column_name')}: '{old}' is a version 1 field; "
                                      f"version 2 uses '{new}'")
    if not _get(space, ("data_sources", "tables")) and not _get(space, ("data_sources", "metric_views")):
        errors.append("the space has no data sources (data_sources.tables or data_sources.metric_views)")
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
    blob = json.dumps(space).replace("\\n", "\n")
    for target in targets():
        s = settings(target)
        cat = s.get("catalog")
        if cat and re.search(_START + _q(cat) + r"\.`?\w+`?\.", blob):
            errors.append(f"hard-coded catalog '{cat}.' ({target}); write {CATALOG_REF}. so every environment works")
    for ident in data_sources(space):
        if not ident.startswith(CATALOG_REF + "."):
            warnings.append(f"data source {ident} does not use {CATALOG_REF}: it is the same object in every environment")
    for ref in sorted(referenced_objects(space) - set(data_sources(space))):
        warnings.append(f"SQL references {ref}, which is not a data source or function of the space")
    for item in _get(space, ("benchmarks", "questions")):
        for answer in item.get("answer", []):
            if answer.get("format") != "SQL" or not answer.get("content"):
                errors.append(f"benchmark {_label(item)!r} has an answer that is not SQL")
    for item in _get(space, ("instructions", "example_question_sqls")):
        if not item.get("sql"):
            errors.append(f"example {_label(item)!r} has no SQL")
    if not _get(space, ("benchmarks", "questions")):
        warnings.append("the space has no benchmarks: the quality gate cannot measure it")
    return errors, warnings


def copy_space(space: dict) -> dict:
    return copy.deepcopy(space)


def main() -> int:
    print(f"project {project_name()}  ({CONFIG_FILE.name})")
    for t in targets():
        s = settings(t)
        print(f"  {t:<8} title {space_title(t)!r:<40} data {s.get('catalog')}.{s.get('schema')}   "
              f"warehouse {s.get('warehouse_name')!r}   monitoring {s.get('catalog')}.{s.get('monitoring_schema')}")
    if SPACE_FILE.exists():
        space = load_space()
        print(f"space content {content_hash(space)}: {summary(space)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
