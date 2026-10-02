"""Run notebooks/FreshCart_End_to_End_on_Databricks.py from top to bottom, offline.

Every Python cell runs as written, against the stand-in workspace (tests/fake_workspace.py) with the real
Databricks CLI: both bundles are validated, deployed, run and destroyed for dev, qa and prod; Genie is asked
questions; the quality gate runs; the drift, sync and rollback loop of step 14 runs; service principals are
created. What cannot run offline is replaced:
  * spark: SQL statements are recorded; SELECT queries run on the local SQLite warehouse, so the benchmark
    comparison of step 10 compares real answers;
  * /Volumes and /tmp/freshcart: temporary folders;
  * the CLI download: the CLI found on this machine (DATABRICKS_CLI or databricks, 1.14+);
  * dbutils, display, displayHTML: minimal stand-ins.
Skipped when no Databricks CLI is installed.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTEBOOK = ROOT / "notebooks" / "FreshCart_End_to_End_on_Databricks.py"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def cells(path: Path = NOTEBOOK) -> list[tuple[str, str]]:
    """(kind, source) per cell; kind is md, sql, pip or py."""
    out = []
    for chunk in path.read_text(encoding="utf-8").split("\n# COMMAND ----------\n"):
        chunk = chunk.replace("# Databricks notebook source\n", "").strip("\n")
        if not chunk:
            continue
        first = chunk.splitlines()[0] if chunk else ""
        kind = {"# MAGIC %md": "md", "# MAGIC %sql": "sql", "# MAGIC %pip": "pip"}.get(" ".join(first.split()[:3]), "py")
        out.append((kind, chunk))
    return out


class _DF:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def collect(self):
        return self._rows

    def count(self):
        return len(self._rows)

    def limit(self, n):
        return _DF(self._rows[:n])

    def option(self, *a):
        return self

    def csv(self, path):
        assert Path(path).exists(), path
        return self


class _Spark:
    """Records statements; answers SELECTs from the local SQLite warehouse."""

    def __init__(self):
        from freshcart import benchmarks
        from freshcart.db import connect
        self.statements, self._benchmarks, self._connect = [], benchmarks, connect
        self.read = _DF()

    def sql(self, statement: str):
        self.statements.append(statement)
        head = statement.lstrip().split(None, 1)[0].upper()
        if head == "SHOW" and "CATALOGS" in statement.upper():
            return _DF([("catalog",)])
        if head in ("SELECT", "WITH"):
            con = self._connect()
            try:
                return _DF([tuple(r) for r in self._benchmarks.run_sql(con, statement)[1]])
            except Exception:                                             # noqa: BLE001
                return _DF()
            finally:
                con.close()
        return _DF()

    def table(self, name):
        return _DF()


class _Widgets:
    def __init__(self, values):
        self.values, self.defaults = values, {}

    def dropdown(self, name, default, choices, label=None):
        self.defaults[name] = default

    def text(self, name, default, label=None):
        self.defaults[name] = default

    def get(self, name):
        return self.values.get(name, self.defaults[name])


class _DBUtils:
    def __init__(self, values):
        self.widgets = _Widgets(values)
        self.library = type("L", (), {"restartPython": staticmethod(lambda: None)})()
        self.notebook = type("N", (), {"entry_point": None})()       # context lookups fall back to cwd


def run_notebook(cli: str, widgets: dict, extra_replacements: dict | None = None) -> tuple[dict, str, object]:
    from fake_workspace import FakeWorkspace, FreshCartEvaluator
    ws = FakeWorkspace.start(evaluator=FreshCartEvaluator())
    tmp = Path(tempfile.mkdtemp(prefix="e2e_notebook_"))
    work, volumes = tmp / "work", tmp / "Volumes"
    (work / "cli").mkdir(parents=True)
    shutil.copy(cli, work / "cli" / "databricks")
    replacements = {'WORK = Path("/tmp/freshcart")': f'WORK = Path({str(work)!r})',
                    'VOLUMES = Path("/Volumes")': f'VOLUMES = Path({str(volumes)!r})',
                    **(extra_replacements or {})}
    spark = _Spark()
    ns = {"spark": spark, "dbutils": _DBUtils(widgets), "display": lambda *a, **k: None,
          "displayHTML": lambda *a, **k: None, "__name__": "__notebook__"}
    out = io.StringIO()
    saved = {k: os.environ.get(k) for k in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_CONFIG_PROFILE")}
    os.environ.update({"DATABRICKS_HOST": ws.host, "DATABRICKS_TOKEN": "notebook-token"})
    os.environ.pop("DATABRICKS_CONFIG_PROFILE", None)
    cwd = os.getcwd()
    os.chdir(ROOT / "notebooks")
    try:
        for i, (kind, src) in enumerate(cells()):
            if kind != "py":
                continue
            for old, new in replacements.items():
                src = src.replace(old, new)
            with contextlib.redirect_stdout(out):
                try:
                    exec(compile(src, f"{NOTEBOOK.name}[cell {i}]", "exec"), ns)
                except Exception as e:
                    raise AssertionError(f"cell {i} failed: {e}\n--- output ---\n{out.getvalue()[-4000:]}") from e
    finally:
        os.chdir(cwd)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ws.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    return ns, out.getvalue(), ws


def test_notebook_cells_are_well_formed():
    kinds = [k for k, _ in cells()]
    assert kinds.count("pip") == 1 and kinds.index("pip") < kinds.index("py"), "the %pip cell must come first"
    for kind, src in cells():
        if kind in ("md", "sql", "pip"):
            assert all(line.startswith("# MAGIC") for line in src.splitlines()), src[:80]
    compile(NOTEBOOK.read_text(encoding="utf-8"), str(NOTEBOOK), "exec")
    # every repository path the notebook names exists
    for rel in set(re.findall(r'REPO / ((?:"[^"]+" / )*"[^"]+")', NOTEBOOK.read_text())):
        parts = re.findall(r'"([^"]+)"', rel)
        assert (ROOT.joinpath(*parts)).exists(), parts


def test_notebook_end_to_end_against_the_stand_in_workspace():
    from test_genie_bundle import _cli
    cli = _cli()
    if cli is None:
        print("      (Databricks CLI 1.14+ not found: notebook run skipped)")
        return
    ns, out, ws = run_notebook(cli, {"promote": "yes", "cleanup": "yes"},
                               {"CREATE_SERVICE_PRINCIPALS = False": "CREATE_SERVICE_PRINCIPALS = True"})
    statements = "\n".join(ns["spark"].statements)
    # steps 4-6: groups, catalogs, setup SQL with the catalog filled in, raw files in the volume
    assert {"fc_all_regions", "freshcart-business-users"} <= {g["displayName"] for g in ws.state.scim["Groups"].values()}
    for cat in ("freshcart_dev", "freshcart_qa", "freshcart"):
        assert f"CREATE CATALOG IF NOT EXISTS `{cat}`" in statements
        assert f"CREATE VOLUME IF NOT EXISTS {cat}.landing.raw" in statements
    assert "${" not in statements
    assert "uploaded" in out
    # step 10: every benchmark answer compared, all equal
    assert "of 17 benchmark answers match the local reference" in out, out[-2000:]
    assert "17 of 17 benchmark answers match" in out
    # steps 11-13: Genie space deployed, questions answered, gate passed
    assert ns["GATE_PASSED"] is True
    assert "GATE PASSED" in out
    # step 14: drift detected, captured, rolled back
    assert "check_drift.py exit code 2" in out
    assert re.search(r"^\+ +- Which store sold the most last week\?$", out, re.M), "the sync diff shows the edit"
    assert "sample question present = False" in out
    # step 15: qa gate and prod smoke passed; step 16: service principals; step 17: everything removed
    assert "gate in qa: PASSED" in out and "smoke in prod: PASSED" in out
    assert len(ws.state.scim["ServicePrincipals"]) == 3
    assert "Files: 6 uploaded" in out and "Files: 8 uploaded" not in out, "drift files must stay out of the bundle"
    assert "No active deployment found" not in out
    assert not ws.state.spaces and not ws.state.pipelines and not ws.state.jobs, "cleanup left resources behind"
