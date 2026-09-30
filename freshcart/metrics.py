"""A small, local stand-in for Unity Catalog metric views.

Databricks metric views let you define a measure once (in YAML) and query it at any grain:

    SELECT region, MEASURE(net_sales) AS net_sales
    FROM   freshcart.semantic.sales_metrics
    WHERE  is_last_completed_fiscal_week
    GROUP BY ALL

SQLite has no metric views, so this module reads the SAME YAML files in semantic/ and turns a
query like the one above into plain SQLite SQL. That lets the demo run the Genie example
queries and benchmarks locally, unchanged, and show exactly what a metric view computes.

Supported (the subset the demo uses):
  * fields and measures, measures composed with MEASURE(other_measure)
  * FILTER (WHERE ...) inside measures
  * window measures with range: current, semiadditive: last  (stock on hand)
  * SELECT <fields and MEASURE(x) [AS alias]> FROM <metric view> [WHERE ...] [GROUP BY ALL]
    [ORDER BY ...] [LIMIT n], with :named parameters
Anything else is passed through with catalog names mapped (freshcart.gold.x -> gold.x).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from . import config as C

KEYWORDS = {
    "sum", "count", "avg", "min", "max", "distinct", "filter", "where", "case", "when", "then", "else",
    "end", "and", "or", "not", "null", "is", "in", "as", "true", "false", "like", "between", "nullif",
    "coalesce", "cast", "measure", "on", "by",
}
IDENT = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`|[A-Za-z_][A-Za-z0-9_]*|.", re.S)


def local_name(uc_name: str) -> str:
    """freshcart.gold.fct_sales_line -> gold.fct_sales_line"""
    parts = uc_name.split(".")
    return ".".join(parts[1:]) if len(parts) == 3 and parts[0] == C.CATALOG else uc_name


def map_catalog(sql: str) -> str:
    return re.sub(rf"\b{C.CATALOG}\.(bronze|silver|gold|semantic)\.", r"\1.", sql)


@dataclass
class MetricView:
    name: str
    spec: dict
    source_columns: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, name: str, con) -> "MetricView":
        spec = yaml.safe_load((C.SEMANTIC_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
        src = local_name(spec["source"])
        schema, table = src.split(".")
        cols = [r[1] for r in con.execute(f"PRAGMA {schema}.table_info({table})")]
        return cls(name, spec, cols)

    # ---- helpers ------------------------------------------------------------------------
    @property
    def fields(self) -> dict:
        return {f["name"]: f for f in self.spec.get("fields", self.spec.get("dimensions", []))}

    @property
    def measures(self) -> dict:
        return {m["name"]: m for m in self.spec.get("measures", [])}

    def _qualify(self, expr: str, to_alias: bool) -> str:
        """Unqualified names that are source columns refer to the source table (the Databricks
        rule). to_alias=True rewrites them to the flattened column names used in mv_rows."""
        toks = [m.group(0) for m in IDENT.finditer(expr)]
        out, i = [], 0
        while i < len(toks):
            tok = toks[i]
            if tok == "source" and i + 2 < len(toks) and toks[i + 1] == ".":        # explicit source.col
                col = toks[i + 2]
                out.append(f'"__src_{col}"' if to_alias else f"source.{col}")
                i += 3
                continue
            nxt = next((t for t in toks[i + 1:] if not t.isspace()), "")
            is_ident = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tok) is not None
            if (is_ident and (i == 0 or toks[i - 1] != ".") and nxt not in (".", "(")
                    and tok.lower() not in KEYWORDS and tok in self.source_columns):
                out.append(f'"__src_{tok}"' if to_alias else f"source.{tok}")
            else:
                out.append(tok)
            i += 1
        return "".join(out)

    def expand_measure(self, name: str, depth=0) -> str:
        if depth > 10:
            raise ValueError("measure references are nested too deeply")
        m = self.measures[name]
        if m.get("window") and depth > 0:
            raise ValueError(f"composing window measure {name} is not supported locally")
        expr = re.sub(r"MEASURE\(\s*(\w+)\s*\)",
                      lambda g: f"({self.expand_measure(g.group(1), depth + 1)})", m["expr"], flags=re.I)
        return self._qualify(expr, to_alias=True)

    def rows_cte(self) -> str:
        """One row per source row with every field computed: what the metric view 'looks like'."""
        cols = [f'  {self._qualify(f["expr"], to_alias=False)} AS "{n}"' for n, f in self.fields.items()]
        cols += [f'  source.{c} AS "__src_{c}"' for c in self.source_columns]
        joins = "\n".join(
            # PyYAML follows YAML 1.1, where an unquoted `on:` key is read as the boolean True
            f'LEFT JOIN {local_name(j["source"])} AS {j["name"]} ON {j.get("on") or j.get(True)}'
            for j in self.spec.get("joins", []))
        return (f"SELECT\n" + ",\n".join(cols) + f"\nFROM {local_name(self.spec['source'])} AS source\n{joins}")

    # ---- query --------------------------------------------------------------------------
    def query_sql(self, dims: list[tuple[str, str]], measures: list[tuple[str, str]],
                  where: str | None = None, order_by: str | None = None, limit: str | None = None) -> str:
        for d, _ in dims:
            if d not in self.fields:
                raise ValueError(f"{d} is not a field of {self.name}")
        where_sql = f"WHERE {where}" if where else ""
        dim_cols = [f'"{d}"' for d, _ in dims]
        group = f"GROUP BY {', '.join(dim_cols)}" if dims else ""
        plain = [(m, a) for m, a in measures if not self.measures[m].get("window")]
        windowed = [(m, a) for m, a in measures if self.measures[m].get("window")]

        ctes = [f"mv_rows AS (\n{self.rows_cte()}\n)"]
        agg_cols = [f'"{d}"' for d, _ in dims] + [f'{self.expand_measure(m)} AS "{a}"' for m, a in plain]
        if agg_cols:
            ctes.append(f"agg AS (\n  SELECT {', '.join(agg_cols)}\n  FROM mv_rows {where_sql}\n  {group}\n)")
        else:                                   # only window measures and no grouping: one row
            ctes.append("agg AS (SELECT 1 AS __one)")
        joins = []
        for i, (m, a) in enumerate(windowed):
            w = self.measures[m]["window"][0]
            if w.get("range") != "current" or w.get("semiadditive") != "last":
                raise ValueError("only range: current / semiadditive: last is supported locally")
            order = w["order"]
            part = f"PARTITION BY {', '.join(dim_cols)}" if dims else ""
            by_day = ", ".join(dim_cols + [f'"{order}"'])
            ctes.append(
                f"w{i} AS (\n  SELECT {', '.join(dim_cols + [f'{chr(34)}{order}{chr(34)} AS __ord'])}, "
                f"{self.expand_measure(m)} AS v\n  FROM mv_rows {where_sql}\n  GROUP BY {by_day}\n),\n"
                f"w{i}_last AS (\n  SELECT * FROM (SELECT *, MAX(__ord) OVER ({part}) AS __mx FROM w{i}) "
                f"WHERE __ord = __mx\n)")
            on = " AND ".join(f'agg.{c} IS w{i}_last.{c}' for c in dim_cols) or "1 = 1"
            joins.append((f"LEFT JOIN w{i}_last ON {on}", f'w{i}_last.v AS "{a}"'))

        select = [f'agg."{d}" AS "{a}"' for d, a in dims]
        wcols = {a: col for (m, a), (_, col) in zip(windowed, joins)}
        for m, a in measures:
            select.append(wcols[a] if a in wcols else f'agg."{a}"')
        sql = "WITH " + ",\n".join(ctes) + f"\nSELECT {', '.join(select)}\nFROM agg\n" + "\n".join(j for j, _ in joins)
        if order_by:
            sql += f"\nORDER BY {order_by}"
        if limit:
            sql += f"\nLIMIT {limit}"
        return sql


# --------------------------------------------------------------------------------------
# Databricks-style SQL -> SQLite
# --------------------------------------------------------------------------------------
METRIC_VIEWS = {"sales_metrics", "inventory_metrics"}
_Q = re.compile(r"^\s*SELECT\s+(?P<items>.+?)\s+FROM\s+(?P<src>[\w\.]+)(?P<rest>.*)$", re.S | re.I)
_REST = re.compile(r"^(?:\s+WHERE\s+(?P<where>.+?))?(?:\s+GROUP\s+BY\s+ALL)?"
                   r"(?:\s+ORDER\s+BY\s+(?P<order>.+?))?(?:\s+LIMIT\s+(?P<limit>\d+))?\s*;?\s*$", re.S | re.I)


def _strip_comments(sql: str) -> str:
    """Remove -- line comments that are not inside a string literal."""
    out = []
    for line in sql.splitlines():
        in_str, cut = False, len(line)
        for i, ch in enumerate(line):
            if ch == "'":
                in_str = not in_str
            elif not in_str and line.startswith("--", i):
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


def _split_top(s: str) -> list[str]:
    parts, depth, cur, quote = [], 0, "", None
    for ch in s:
        if quote:
            cur += ch
            quote = None if ch == quote else quote
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
            continue
        cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def translate(con, sql: str) -> str:
    sql = _strip_comments(sql).strip()
    fn = re.match(r"^SELECT\s+\*\s+FROM\s+[\w\.]*fn_like_for_like_sales\(\s*(\d+|:\w+)\s*\)\s*;?$", sql, re.I | re.S)
    if fn:
        body = (C.SEMANTIC_DIR / "fn_like_for_like_sales.local.sql").read_text(encoding="utf-8")
        arg = fn.group(1)
        return body.replace(":p_fiscal_year", arg if not arg.isdigit() else arg)
    m = _Q.match(sql)
    src = m.group("src").split(".")[-1] if m else None
    if not m or src not in METRIC_VIEWS:
        return map_catalog(sql).replace(" ILIKE ", " LIKE ")
    rest = _REST.match(m.group("rest"))
    if not rest:
        raise ValueError(f"cannot parse the query tail: {m.group('rest')!r}")
    mv = MetricView.load(src, con)
    dims, measures = [], []
    for item in _split_top(m.group("items")):
        mm = re.match(r"^MEASURE\(\s*(\w+)\s*\)(?:\s+AS\s+(\w+))?$", item, re.I)
        if mm:
            measures.append((mm.group(1), mm.group(2) or mm.group(1)))
            continue
        fm = re.match(r"^(\w+)(?:\s+AS\s+(\w+))?$", item, re.I)
        if not fm:
            raise ValueError(f"unsupported select item for a metric view: {item}")
        dims.append((fm.group(1), fm.group(2) or fm.group(1)))
    where = rest.group("where")
    if where:
        where = where.replace(" ILIKE ", " LIKE ")
    return mv.query_sql(dims, measures, where, rest.group("order"), rest.group("limit"))


def run(con, sql: str, params: dict | None = None):
    """Run Databricks-style SQL locally. Returns (columns, rows)."""
    local = translate(con, sql)
    cur = con.execute(local, params or {})
    return [c[0] for c in cur.description], cur.fetchall()


def measure_formats() -> dict:
    """measure/alias name -> format spec, used to pretty-print results in the docs."""
    out = {}
    for name in METRIC_VIEWS:
        spec = yaml.safe_load((C.SEMANTIC_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
        for m in spec.get("measures", []):
            if m.get("format"):
                out[m["name"]] = m["format"]
    return out
