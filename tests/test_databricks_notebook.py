"""Run the end-to-end Databricks notebook from top to bottom, offline.

The notebook's source is notebooks/FreshCart_End_to_End_on_Databricks_source.py; the .ipynb next to it is
generated from it (freshcart/databricks_notebook.py) and must be up to date.

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
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTEBOOK = ROOT / "notebooks" / "FreshCart_End_to_End_on_Databricks_source.py"
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

    def toPandas(self):
        import pandas as pd
        return pd.DataFrame(self._rows)


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


class _Secrets:
    def __init__(self, scopes: dict):
        self.scopes = scopes

    def get(self, scope, key):
        return self.scopes[scope][key]


class _DBUtils:
    def __init__(self, values, secret_scopes=None):
        self.widgets = _Widgets(values)
        self.secrets = _Secrets(secret_scopes if secret_scopes is not None else {})
        self.library = type("L", (), {"restartPython": staticmethod(lambda: None)})()
        self.notebook = type("N", (), {"entry_point": None})()       # context lookups fall back to cwd


def run_notebook(cli: str, widgets: dict, extra_replacements: dict | None = None, until: str | None = None,
                 setup=None, notebook: Path = NOTEBOOK) -> tuple[dict, str, object]:
    """Run the notebook's Python cells in order (until the markdown cell containing `until`, if given).
    `setup(workspace)` runs first, to prepare the stand-in workspace."""
    from fake_workspace import FakeWorkspace, FreshCartEvaluator
    ws = FakeWorkspace.start(evaluator=FreshCartEvaluator())
    if setup:
        setup(ws)
    tmp = Path(tempfile.mkdtemp(prefix="e2e_notebook_"))
    work, volumes = tmp / "work", tmp / "Volumes"
    (work / "cli").mkdir(parents=True)
    shutil.copy(cli, work / "cli" / "databricks")
    replacements = {'WORK = Path("/tmp/freshcart")': f'WORK = Path({str(work)!r})',
                    'VOLUMES = Path("/Volumes")': f'VOLUMES = Path({str(volumes)!r})',
                    **(extra_replacements or {})}
    spark = _Spark()
    ns = {"spark": spark, "dbutils": _DBUtils(widgets, ws.state.secret_scopes), "display": lambda *a, **k: None,
          "displayHTML": lambda *a, **k: None, "__name__": "__notebook__"}
    out = io.StringIO()
    saved = {k: os.environ.get(k) for k in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_CONFIG_PROFILE")}
    os.environ.update({"DATABRICKS_HOST": ws.host, "DATABRICKS_TOKEN": "notebook-token"})
    os.environ.pop("DATABRICKS_CONFIG_PROFILE", None)
    cwd = os.getcwd()
    os.chdir(ROOT / "notebooks")
    try:
        for i, (kind, src) in enumerate(cells(notebook)):
            if until and kind == "md" and until in src:
                break
            if kind != "py":
                continue
            for old, new in replacements.items():
                src = src.replace(old, new)
            with contextlib.redirect_stdout(out):
                try:
                    exec(compile(src, f"{notebook.name}[cell {i}]", "exec"), ns)
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


def test_ipynb_is_generated_from_the_source():
    from freshcart import databricks_notebook as N
    assert N.TARGET.read_text(encoding="utf-8") == N.render(), "run: python -m freshcart.databricks_notebook"
    nb = json.loads(N.TARGET.read_text(encoding="utf-8"))
    assert [c["cell_type"] for c in nb["cells"]] == ["markdown" if k == "md" else "code" for k, _ in cells()]
    assert nb["cells"][0]["source"][0].startswith("# FreshCart on Databricks")
    assert any(c["source"] and c["source"][0].startswith("%sql") for c in nb["cells"])


def test_notebook_end_to_end_against_the_stand_in_workspace():
    from test_genie_bundle import _cli
    cli = _cli()
    if cli is None:
        print("      (Databricks CLI 1.14+ not found: notebook run skipped)")
        return
    def leftover_local_group(ws):                 # what an earlier version of the notebook created
        ws.state.scim["Groups"]["6001"] = {"id": "6001", "displayName": "freshcart-business-users", "members": [],
                                           "meta": {"resourceType": "WorkspaceGroup"}}

    ns, out, ws = run_notebook(cli, {"promote": "yes", "cleanup": "yes"},
                               {"CREATE_SERVICE_PRINCIPALS = False": "CREATE_SERVICE_PRINCIPALS = True"},
                               setup=leftover_local_group)
    statements = "\n".join(ns["spark"].statements)
    # steps 4-6: account groups (the workspace-local leftover replaced), catalogs, setup SQL, raw files
    groups = {g["displayName"]: g for g in ws.state.scim["Groups"].values()}
    assert {"fc_all_regions", "freshcart-business-users", "fc_crm_admins"} <= set(groups)
    assert all(g["meta"]["resourceType"] == "Group" for g in groups.values()), "all groups are account groups"
    assert "replaced the workspace-local group freshcart-business-users" in out
    assert set(ns["GROUP_KIND"].values()) == {"account"}
    me = "1001"
    assert all(any(m["value"] == me for m in groups[g]["members"]) for g in ns["GROUPS"])
    assert not any(m["value"] == me for m in groups["fc_crm_admins"].get("members", []))
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


def test_notebook_falls_back_to_workspace_groups_without_the_identity_api():
    from test_genie_bundle import _cli
    cli = _cli()
    if cli is None:
        print("      (Databricks CLI 1.14+ not found: notebook run skipped)")
        return

    def no_identity_api(ws):
        ws.state.identity_api = False

    ns, out, ws = run_notebook(cli, {}, until="## Step 5", setup=no_identity_api)
    assert set(ns["GROUP_KIND"].values()) == {"workspace"}
    assert "Unity Catalog grants to them will be skipped" in out
    groups = {g["displayName"]: g for g in ws.state.scim["Groups"].values()}
    assert all(any(m["value"] == "1001" for m in groups[g]["members"]) for g in ns["GROUPS"])
