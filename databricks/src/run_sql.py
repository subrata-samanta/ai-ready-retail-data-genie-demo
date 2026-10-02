"""Run FreshCart Databricks SQL files with spark.sql, one statement at a time.

Used by the bundle job freshcart_refresh (every task except the pipeline) and by the end-to-end notebook.

    run_sql.py --catalog freshcart_dev --as-of-date 2026-09-27 00_setup.sql governance/00_functions.sql

The SQL files name the catalog ${catalog} and the day treated as today ${as_of_date}, so one set of files
serves every environment. Both are replaced before a statement runs; a statement that still contains a
${...} placeholder is refused rather than sent to Spark.
"""
from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_.-]+)\}")


def split_statements(sql: str) -> list[str]:
    """Split a SQL script on semicolons that are outside strings, comments, identifiers and $$ blocks.

    Comment-only fragments and comments before a statement are dropped; comments inside one are kept.
    """
    statements, buf = [], []
    i, n = 0, len(sql)
    while i < n:
        c, nxt = sql[i], sql[i + 1] if i + 1 < n else ""
        if c == "-" and nxt == "-":                                   # line comment
            j = sql.find("\n", i)
            j = n if j == -1 else j
            buf.append(sql[i:j])
            i = j
        elif c == "/" and nxt == "*":                                 # block comment
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            buf.append(sql[i:j])
            i = j
        elif c == "$" and nxt == "$":                                 # $$ ... $$ (metric view YAML)
            j = sql.find("$$", i + 2)
            if j == -1:
                raise ValueError("unterminated $$ block")
            buf.append(sql[i:j + 2])
            i = j + 2
        elif c in "'\"`":                                             # string or quoted identifier
            j = i + 1
            while j < n:
                if sql[j] == "\\" and c != "`":
                    j += 2
                    continue
                if sql[j] == c:
                    break
                j += 1
            if j >= n:
                raise ValueError(f"unterminated {c} quote starting at offset {i}")
            buf.append(sql[i:j + 1])
            i = j + 1
        elif c == ";":
            statements.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(c)
            i += 1
    statements.append("".join(buf))
    return [_without_leading_comments(s) for s in statements if _has_code(s)]


def _without_leading_comments(statement: str) -> str:
    s = statement.strip()
    while s.startswith(("--", "/*")):
        end = s.find("\n") if s.startswith("--") else s.find("*/") + 2
        s = s[end:].strip() if end > 1 else ""
    return s


def _has_code(fragment: str) -> bool:
    without = re.sub(r"--[^\n]*", "", fragment)
    without = re.sub(r"/\*.*?\*/", "", without, flags=re.S)
    return bool(without.strip())


def render(sql: str, params: dict[str, str]) -> str:
    """Replace ${key} for every key in params; fail if any placeholder is left."""
    out = PLACEHOLDER.sub(lambda m: params.get(m.group(1), m.group(0)), sql)
    left = sorted(set(PLACEHOLDER.findall(out)))
    if left:
        raise ValueError(f"no value for {', '.join('${' + k + '}' for k in left)}")
    return out


def summary(statement: str) -> str:
    code = "\n".join(line for line in statement.splitlines() if not line.strip().startswith("--"))
    return " ".join(code.split())[:110]


GRANTEE = re.compile(r"\bTO\s+`?([^`;]+?)`?\s*$", re.I | re.S)


def is_missing_grantee(statement: str, error: Exception) -> bool:
    """A GRANT to a principal Unity Catalog does not know (for example a workspace-local group)."""
    return statement.lstrip().upper().startswith("GRANT ") and "PRINCIPAL_DOES_NOT_EXIST" in str(error)


def run_files(spark, files, catalog: str, as_of_date: str = "today", echo=print) -> int:
    """Run every statement of every file, in order. Returns the number of statements run.

    One kind of failure is reported and skipped instead of stopping the run: a GRANT to a group that is not an
    account group. Unity Catalog can grant access only to account groups; without it, that group's members
    cannot read the data, but nothing else is affected (the owner and admins keep their access).
    """
    params = {"catalog": catalog, "as_of_date": as_of_date}
    count, skipped = 0, []
    for path in files:
        statements = [render(s, params) for s in split_statements(Path(path).read_text(encoding="utf-8"))]
        echo(f"== {path} ({len(statements)} statements)")
        for statement in statements:
            started = time.time()
            try:
                spark.sql(statement)
            except Exception as e:
                if is_missing_grantee(statement, e):
                    m = GRANTEE.search(statement)
                    skipped.append(m.group(1) if m else "?")
                    echo(f"   WARNING skipped: {summary(statement)}\n"
                         f"      {skipped[-1]} is not an account group, so Unity Catalog cannot grant it access.")
                    continue
                echo(f"   FAILED in {path}:\n{statement}")
                raise
            count += 1
            echo(f"   ok {time.time() - started:5.1f}s  {summary(statement)}")
    if skipped:
        echo(f"WARNING: {len(skipped)} grant(s) skipped for {', '.join(sorted(set(skipped)))}: create it as an account "
             "group (account console, or workspace Settings > Identity and access > Groups in an identity-federated "
             "workspace) and run the job again to apply them.")
    return count


def main(argv=None, spark=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--as-of-date", default="today",
                    help="day treated as today, YYYY-MM-DD; 'today' uses current_date()")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    if spark is None:
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
    n = run_files(spark, args.files, args.catalog, args.as_of_date)
    print(f"{n} statements run on catalog {args.catalog} (as of {args.as_of_date})")
    return 0


if __name__ == "__main__":
    # Do not end with sys.exit: a Databricks Python script task runs inside IPython, where exiting with code 0 still
    # raises SystemExit and marks the task as failed. A failing statement raises its own error, which fails the task.
    main()
