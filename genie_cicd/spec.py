"""The Genie space definition as code: load, normalise, make environment-neutral, diff, validate.

A Genie space (shown as a Genie Agent in the UI) is fully described by its *serialized space*:
the JSON that `GET /api/2.0/genie/spaces/{id}?include_serialized_space=true` returns and that
`POST /api/2.0/genie/spaces` / `PATCH /api/2.0/genie/spaces/{id}` accept. The same JSON is what
Declarative Automation Bundles store in a `.geniespace.json` file.

In git we keep that JSON in an *environment-neutral* form: every Unity Catalog reference to the
FreshCart catalog is written as `{{catalog}}.<schema>.<object>`. `render()` swaps the token for the
catalog of the target environment (freshcart_dev, freshcart_qa, freshcart) right before a deploy,
and `neutralise()` does the reverse when a space is exported. One file, every environment.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
from pathlib import Path

TOKEN = "{{catalog}}"
SCHEMAS = ("bronze", "silver", "gold", "semantic", "governance")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_TOKEN_REF = re.compile(r"\{\{catalog\}\}\.(" + "|".join(SCHEMAS) + r")\.(\w+)")

# Lists of objects are sorted by the first of these keys they carry. The Genie API stores the
# space as protobuf and expects collections in a stable order; sorting also keeps git diffs small.
# Lists of strings (SQL lines, instruction lines, question text) are never reordered.
_SORT_KEYS = ("id", "identifier", "column_name", "name")

# Collections whose items carry an `id`, with a readable label for change summaries.
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
_IDENTIFIER_SECTIONS = {
    ("data_sources", "tables"): "table",
    ("data_sources", "metric_views"): "metric view",
}


# ----------------------------------------------------------------------------------------- io
def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dumps(spec: dict) -> str:
    """Canonical text form: sorted keys, 2-space indent, one value per line, trailing newline."""
    return json.dumps(normalise(spec), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def save(spec: dict, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(dumps(spec), encoding="utf-8")


def parse(serialized_space: str | dict | None) -> dict:
    if serialized_space is None or serialized_space == "":
        return {}
    return json.loads(serialized_space) if isinstance(serialized_space, str) else copy.deepcopy(serialized_space)


def to_wire(spec: dict) -> str:
    """The compact string the API expects in `serialized_space`."""
    return json.dumps(normalise(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------------- normalise
def normalise(value):
    """Deterministic form of a space: object lists sorted by id/identifier/column_name/name."""
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


def content_hash(spec: dict) -> str:
    """Identity of a space's content. Two specs with the same hash behave identically."""
    return hashlib.sha256(dumps(spec).encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------- environment neutrality
def _map_strings(value, fn):
    if isinstance(value, dict):
        return {k: _map_strings(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_map_strings(v, fn) for v in value]
    return fn(value) if isinstance(value, str) else value


def _catalog_ref(catalog: str) -> re.Pattern:
    return re.compile(r"(?<![\w`.])`?" + re.escape(catalog) + r"`?\.(?=(" + "|".join(SCHEMAS) + r")\.)")


def neutralise(spec: dict, catalog: str) -> dict:
    """Replace `<catalog>.<schema>.` with `{{catalog}}.<schema>.` everywhere (exported -> git)."""
    pattern = _catalog_ref(catalog)
    return normalise(_map_strings(spec, lambda s: pattern.sub(TOKEN + ".", s)))


def render(spec: dict, catalog: str) -> dict:
    """Replace `{{catalog}}` with the target environment's catalog (git -> deploy)."""
    return normalise(_map_strings(spec, lambda s: s.replace(TOKEN, catalog)))


def referenced_objects(spec: dict) -> set[str]:
    """Every `{{catalog}}.schema.object` the space uses, in data sources, functions and any SQL."""
    found: set[str] = set()
    _map_strings(spec, lambda s: found.update(f"{TOKEN}.{m.group(1)}.{m.group(2)}" for m in _TOKEN_REF.finditer(s)) or s)
    return found


def declared_objects(spec: dict) -> dict[str, str]:
    """Objects attached to the space as data sources or trusted functions: identifier -> kind."""
    out = {}
    for path, kind in {**_IDENTIFIER_SECTIONS, ("instructions", "sql_functions"): "function"}.items():
        for item in _get(spec, path):
            if "identifier" in item:
                out[item["identifier"]] = kind
    return out


# ------------------------------------------------------------------------------------- diff
def _get(spec: dict, path: tuple) -> list:
    node = spec
    for key in path:
        node = node.get(key, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, list) else []


def _label(item: dict) -> str:
    for key in ("question", "display_name", "alias", "identifier", "title"):
        if key in item:
            v = item[key]
            return " ".join((" ".join(v) if isinstance(v, list) else str(v)).split())[:80]
    if "content" in item:
        text = " ".join((" ".join(item["content"]) if isinstance(item["content"], list) else str(item["content"])).split())
        return text.lstrip("# ")[:60] + "..."
    return item.get("id", "?")


def count_changes(changes: list[str]) -> int:
    """Number of changed items in a diff() result (detail lines are indented)."""
    return sum(1 for c in changes if not c.startswith(" "))


def _text(item: dict) -> list[str]:
    return json.dumps(item, indent=2, sort_keys=True, ensure_ascii=False).splitlines()


def diff(old: dict, new: dict) -> list[str]:
    """Human-readable change list between two specs, plus line diffs of changed items.

    This is what reviewers read in a pull request: "changed example SQL: What were net sales..."
    followed by exactly which SQL lines changed.
    """
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
                for line in difflib.unified_diff(_text(before[k]), _text(after[k]), lineterm="", n=1):
                    if not line.startswith(("---", "+++", "@@")):
                        out.append("      " + line)
    known = {p[0] for p in list(_ID_SECTIONS) + list(_IDENTIFIER_SECTIONS)}
    for key in sorted((old.keys() | new.keys()) - known):
        if old.get(key) != new.get(key):
            out.append(f"~ changed {key}: {old.get(key)!r} -> {new.get(key)!r}")
    return out


def summary(spec: dict) -> dict:
    """Counts per section, for a quick look at what a space contains."""
    out = {kind: len(_get(spec, path)) for path, kind in {**_IDENTIFIER_SECTIONS, **_ID_SECTIONS}.items()}
    return {k: v for k, v in out.items() if v}


# --------------------------------------------------------------------------------- validate
def validate(spec: dict, *, forbidden_catalogs: list[str] = ()) -> tuple[list[str], list[str]]:
    """Static checks that need no workspace. Returns (errors, warnings).

    Errors block a merge or deploy; warnings are shown to the reviewer.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(spec.get("version"), int):
        errors.append("missing integer 'version'")
    if not _get(spec, ("data_sources", "tables")) and not _get(spec, ("data_sources", "metric_views")):
        errors.append("the space has no data sources")

    # ids: unique across all instruction types, and across sample questions + benchmarks
    groups = {
        "instructions": [p for p in _ID_SECTIONS if p[0] == "instructions"],
        "questions": [("config", "sample_questions"), ("benchmarks", "questions")],
    }
    for group, paths in groups.items():
        seen: dict[str, str] = {}
        for path in paths:
            for item in _get(spec, path):
                i = item.get("id")
                if not i:
                    errors.append(f"{_ID_SECTIONS[path]} without an id: {_label(item)}")
                    continue
                if not _HEX32.match(str(i)):
                    warnings.append(f"{_ID_SECTIONS[path]} id {i!r} is not 32 lowercase hex characters")
                if i in seen:
                    errors.append(f"duplicate id {i} in {group} ({seen[i]} and {_ID_SECTIONS[path]})")
                seen[i] = _ID_SECTIONS[path]

    # environment neutrality: no hard-coded environment catalog may slip into git
    text = json.dumps(spec)
    for cat in forbidden_catalogs:
        if _catalog_ref(cat).search(text.replace("\\n", "\n")):
            errors.append(f"hard-coded catalog '{cat}.' found; use '{TOKEN}.' so the file works in every environment")
    for ident in declared_objects(spec):
        if not ident.startswith(TOKEN + "."):
            warnings.append(f"data source {ident} is outside the FreshCart catalog and is not environment-neutral")

    # SQL may only use objects the space actually has access to
    declared = set(declared_objects(spec))
    for ref in sorted(referenced_objects(spec) - declared):
        warnings.append(f"SQL references {ref}, which is not a data source or function of the space")

    for item in _get(spec, ("benchmarks", "questions")):
        for answer in item.get("answer", []):
            if answer.get("format") != "SQL" or not answer.get("content"):
                errors.append(f"benchmark {_label(item)!r} has an answer that is not SQL")
    for item in _get(spec, ("instructions", "example_question_sqls")):
        if not item.get("sql"):
            errors.append(f"example {_label(item)!r} has no SQL")
    return errors, warnings


def sql_text(lines) -> str:
    return "".join(lines) if isinstance(lines, list) else str(lines or "")


def text_lines(text: str) -> list[str]:
    """Split text the way Genie exports it: a list of lines, each keeping its newline."""
    lines = text.splitlines(keepends=True)
    if lines and lines[-1].endswith("\n"):
        lines[-1] = lines[-1].rstrip("\n")
    return lines
