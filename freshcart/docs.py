"""Render the documentation from docs_src/ into docs/ (and README.md) using REAL pipeline output.

Every table and number in the docs is produced by running SQL against the local warehouse,
so the docs can never drift from the data. Directives inside the templates:

  <!--table [limit=N] [metric] [params=json]    SQL follows on the next lines (Databricks-style
  SELECT ...                                    SQL if `metric`), closed by -->.
  -->                                           Rendered as a markdown table.
  <!--file path [lines=A-B] [lang=sql]-->       Include a file (or a line range) as a code block.
  <!--raw path [lines=N]-->                     First N lines of a raw source file.
  <!--quality--> <!--benchmarks--> <!--dictionary--> <!--feeds--> <!--examples-->
  {{n: SQL}}  {{usd: SQL}}  {{pct: SQL}}  {{v: SQL}}   inline values

    python -m freshcart.docs
"""
from __future__ import annotations

import json
import re


from . import benchmarks, bronze, config as C, contracts, metrics, quality
from .db import connect, render_sql

FORMATS = metrics.measure_formats()
EXTRA_PCT = {"lfl_growth_pct", "out_of_stock_rate", "promo_sales_share", "gross_margin_pct", "return_rate",
             "online_share"}


def fmt_cell(col: str, v, truncate=True):
    if v is None:
        return "NULL"
    spec = FORMATS.get(col)
    if isinstance(v, float):
        if (spec and spec.get("type") == "percentage") or col in EXTRA_PCT:
            places = (spec or {}).get("decimal_places", {}).get("places", 1)
            return f"{v * 100:.{places}f}%"
        if spec and spec.get("type") == "currency":
            places = spec.get("decimal_places", {}).get("places", 0)
            return f"${v:,.{places}f}"
        if col.endswith("_usd") or col.endswith("_local") or col in ("net_sales_ty", "net_sales_ly"):
            return f"{v:,.2f}"
        if "rate" in col or "fx" in col:
            return f"{v:.4f}"
        return f"{v:,.3f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else f"{v:,.0f}"
    if isinstance(v, int) and abs(v) >= 1000 and not col.endswith("_id") and "year" not in col and not col.startswith("_"):
        return f"{v:,}"
    s = str(v)
    return s if (len(s) <= 110 or not truncate) else s[:107] + "..."


def md_table(cols, rows, truncate=True) -> str:
    esc = lambda s: str(s).replace("|", "\\|")                                   # noqa: E731
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        out.append("| " + " | ".join(esc(fmt_cell(c, v, truncate)) for c, v in zip(cols, r)) + " |")
    return "\n".join(out)


def _run(con, sql, metric=False, params=None):
    if metric:
        return metrics.run(con, sql, params)
    cur = con.execute(render_sql(metrics.map_catalog(sql)), params or {})
    return [c[0] for c in cur.description], cur.fetchall()


def _inline(con, m):
    kind, sql = m.group(1), m.group(2).strip()
    v = con.execute(render_sql(sql)).fetchone()[0]
    if kind == "n":
        return f"{v:,}"
    if kind == "usd":
        return f"${v:,.2f}"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    return str(v)


def _file_block(arg: str) -> str:
    parts = arg.split()
    path, opts = C.ROOT / parts[0], dict(p.split("=", 1) for p in parts[1:])
    text = path.read_text(encoding="utf-8").splitlines()
    if "lines" in opts:
        a, b = (int(x) for x in opts["lines"].split("-"))
        text = text[a - 1:b]
    lang = opts.get("lang", {"sql": "sql", "yml": "yaml", "yaml": "yaml", "py": "python", "md": "markdown"}
                    .get(path.suffix.lstrip("."), ""))
    link = f"[`{parts[0]}`](../{parts[0]})"
    return f"{link}\n\n```{lang}\n" + "\n".join(text) + "\n```"


def _raw_block(arg: str) -> str:
    parts = arg.split()
    path, opts = C.RAW_DIR / parts[0], dict(p.split("=", 1) for p in parts[1:])
    n = int(opts.get("lines", 6))
    with open(path, encoding="utf-8") as f:
        lines = [next(f).rstrip("\n") for _ in range(n)]
    if path.suffix == ".ndjson":
        lines = [json.dumps(json.loads(lines[0]), indent=2)]
    return f"`data/raw/{parts[0]}`\n\n```{'json' if path.suffix == '.ndjson' else 'text'}\n" + "\n".join(lines) + "\n```"


def _quality(con) -> str:
    rules = md_table(["layer", "rule (must be 0)", "violations", "status"],
                     [(l, n, v, "PASS" if v == 0 else "FAIL") for l, n, v in quality.run_rules(con)])
    finds = md_table(["what the pipeline found and handled", "rows"], quality.run_findings(con))
    return f"**Rules**\n\n{rules}\n\n**Findings**\n\n{finds}"


def _benchmarks(con) -> str:
    blocks = []
    for i, (b, cols, rows) in enumerate(benchmarks.run_benchmarks(con), start=1):
        head = f"**#{i} · {b['question']}**  \nTests: {b['tests']}. Correct when: {b['correct_when']}."
        if cols is None:
            blocks.append(head + "\n\n_No single expected result: graded by review or an Agent-mode benchmark._")
        else:
            blocks.append(head + f"\n\n```sql\n{b['expected_sql'].strip()}\n```\n\nExpected answer:\n\n" +
                          md_table(cols, rows[:10]))
    return "\n\n".join(blocks)


def _examples(con) -> str:
    blocks = []
    sql_by_title = {e["title"]: e["sql"] for e in benchmarks.load_examples()}
    for title, params, cols, rows in benchmarks.run_examples(con):
        p = f" (run here with {params})" if params else ""
        blocks.append(f"#### \"{title}\"\n\n```sql\n{sql_by_title[title]}\n```\n\nResult{p}:\n\n{md_table(cols, rows[:8])}")
    return "\n\n".join(blocks)


def _genie_sources() -> str:
    """The space's data sources and trusted functions, from the bundle (the only definition)."""
    space = benchmarks.load_space()
    ds, ins = space.get("data_sources") or {}, space.get("instructions") or {}
    rows = [(f"`{t['identifier']}`", "metric view", "") for t in ds.get("metric_views", [])]
    for t in ds.get("tables", []):
        cfg = t.get("column_configs", [])
        hidden = [c["column_name"] for c in cfg if c.get("exclude")]
        matched = [c["column_name"] for c in cfg if c.get("enable_entity_matching")]
        notes = "; ".join(x for x in (f"entity matching on {', '.join(matched)}" if matched else "",
                                      f"hidden: {', '.join(hidden)}" if hidden else "") if x)
        rows.append((f"`{t['identifier']}`", "table", notes))
    rows += [(f"`{f['identifier']}`", "trusted function", "") for f in ins.get("sql_functions", [])]
    return md_table(["object (`${var.catalog}` = the environment's catalog)", "kind", "column settings"], rows)


def _genie_expressions() -> str:
    snippets = (benchmarks.load_space().get("instructions") or {}).get("sql_snippets") or {}
    rows = [(kind.rstrip("s").capitalize(), s["display_name"], f"`{''.join(s['sql'])}`", ", ".join(s.get("synonyms", [])))
            for kind in ("filters", "expressions", "measures") for s in snippets.get(kind, [])]
    return md_table(["type", "name", "code", "synonyms"], rows, truncate=False)


def _genie_instructions() -> str:
    ins = (benchmarks.load_space().get("instructions") or {}).get("text_instructions", [])
    return "\n\n".join("```markdown\n" + "".join(t["content"]).strip() + "\n```" for t in ins)


def _dictionary() -> str:
    out = []
    for c in contracts.all_contracts():
        keys = f"Primary key: `{', '.join(c['primary_key'])}`"
        fks = "; ".join(f"`{fk['column']}` → `{fk['references']}.{fk['ref_column']}`" for fk in c.get("foreign_keys", []))
        out.append(f"### gold.{c['table']}\n\n{c['comment']}\n\n{keys}" + (f"  \nForeign keys: {fks}" if fks else "") +
                   "\n\n" + md_table(["column", "type", "nullable", "comment"],
                                     [(f"`{x['name']}`", x["type"], "no" if x.get("nullable") is False else "yes",
                                       x.get("comment", "")) for x in c["columns"]], truncate=False))
    return "\n\n".join(out)


def _feeds(con) -> str:
    rows = []
    for table, pattern, fmt, desc in bronze.feed_catalog():
        files = len(list(C.RAW_DIR.glob(pattern)))
        n = con.execute(f"SELECT COUNT(*) FROM bronze.{table}").fetchone()[0]
        rows.append((f"`data/raw/{pattern}`", fmt, files, f"`bronze.{table}`", n, desc))
    return md_table(["files", "format", "count", "bronze table", "rows", "what it is"], rows)


def render_text(con, text: str) -> str:
    def table(m):
        head, sql = m.group(1).split(), m.group(2)
        opts = dict(h.split("=", 1) for h in head if "=" in h)
        metric = "metric" in head
        params = json.loads(opts["params"]) if "params" in opts else None
        cols, rows = _run(con, sql, metric, params)
        limit = int(opts.get("limit", 20))
        more = f"\n\n_{len(rows) - limit:,} more rows not shown._" if len(rows) > limit else ""
        return md_table(cols, rows[:limit]) + more

    text = re.sub(r"<!--table([^\n]*)\n(.*?)-->", table, text, flags=re.S)
    text = re.sub(r"<!--translate\n(.*?)-->",
                  lambda m: "```sql\n" + metrics.translate(con, m.group(1)).strip() + "\n```", text, flags=re.S)
    text = re.sub(r"<!--file (.*?)-->", lambda m: _file_block(m.group(1).strip()), text)
    text = re.sub(r"<!--raw (.*?)-->", lambda m: _raw_block(m.group(1).strip()), text)
    text = text.replace("<!--quality-->", _quality(con) if "<!--quality-->" in text else "")
    text = text.replace("<!--benchmarks-->", _benchmarks(con) if "<!--benchmarks-->" in text else "")
    text = text.replace("<!--examples-->", _examples(con) if "<!--examples-->" in text else "")
    text = text.replace("<!--genie-sources-->", _genie_sources() if "<!--genie-sources-->" in text else "")
    text = text.replace("<!--genie-expressions-->", _genie_expressions() if "<!--genie-expressions-->" in text else "")
    text = text.replace("<!--genie-instructions-->", _genie_instructions() if "<!--genie-instructions-->" in text else "")
    text = text.replace("<!--dictionary-->", _dictionary() if "<!--dictionary-->" in text else "")
    text = text.replace("<!--feeds-->", _feeds(con) if "<!--feeds-->" in text else "")
    text = re.sub(r"\{\{(n|usd|pct|v):\s*(.*?)\}\}", lambda m: _inline(con, m), text, flags=re.S)
    return text


def main():
    con = connect()
    C.DOCS_DIR.mkdir(exist_ok=True)
    banner = ("<!-- GENERATED by `python -m freshcart.docs` from docs_src/. Edit the template, not this file. "
              "Every table and number below comes from running the pipeline. -->\n\n")
    for src in sorted(C.DOCS_SRC_DIR.glob("*.md")):
        target = C.ROOT / "README.md" if src.name == "README.md" else C.DOCS_DIR / src.name
        out = render_text(con, src.read_text(encoding="utf-8"))
        if target.name == "README.md":
            out = out.replace("](../", "](")
        target.write_text(banner + out, encoding="utf-8")
        print(f"rendered {target.relative_to(C.ROOT)}")


if __name__ == "__main__":
    main()
