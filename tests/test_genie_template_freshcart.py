"""The Genie project template, used for FreshCart: is the template (and its notebook) correct for FreshCart's data
and pipeline?

The FreshCart project is made from the template by examples/freshcart_genie_project/make_project.py (the
FreshCart genie.config.yml + FreshCart's space from genie_bundle/). Then:

  * the project passes its own checks (tests, validation, readiness) and deploys exactly what genie_bundle deploys;
  * every example and benchmark SQL of the space runs on FreshCart's data (the local warehouse that run_demo.py
    builds, the same tables and metric views the data bundle builds on Databricks), for dev, qa and prod;
  * the bundle, with the real Databricks CLI against the stand-in workspace: every target, deploy, the quality gate on
    FreshCart's 17 benchmarks, the smoke test, the usage job, the approver's report, a sync with no churn;
  * the template notebook, top to bottom with apply_changes = yes, for the FreshCart project: deployers in the
    row-level-security group, grants on gold and semantic, no example data, every SQL run on FreshCart data
    (Spark answers from the local warehouse and fails on errors), release, UI sync, drift, rollback;
  * the notebook's import path: the FreshCart space, prototyped in dev, imported as the first version, gives
    exactly the same file as make_project.py.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
EXAMPLE = ROOT / "examples" / "freshcart_genie_project"
SPACE_PATH = "space/genie_space.yml"
PROJECT_MODULES = ("genie_tools", "check_sql", "readiness", "genie_quality", "genie_usage", "build_notebooks")


def _make(tmp: Path) -> Path:
    spec = importlib.util.spec_from_file_location("make_freshcart_project", EXAMPLE / "make_project.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.make(tmp / "freshcart-genie")


def _tools(project: Path):
    spec = importlib.util.spec_from_file_location(f"genie_tools_{uuid.uuid4().hex}", project / "scripts" / "genie_tools.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@contextlib.contextmanager
def _own_modules():
    """The notebook imports genie_tools & co. from the project it runs in: hide the copies other tests imported."""
    saved = {k: sys.modules.pop(k) for k in PROJECT_MODULES if k in sys.modules}
    path = list(sys.path)
    try:
        yield
    finally:
        for k in PROJECT_MODULES:
            sys.modules.pop(k, None)
        sys.modules.update(saved)
        sys.path[:] = path


def _cli():
    from test_genie_bundle import _cli as find
    return find()


# ----------------------------------------------------------------------------------------- the project
def test_freshcart_project_passes_its_checks_and_deploys_what_genie_bundle_deploys():
    import yaml
    with tempfile.TemporaryDirectory() as tmp:
        project = _make(Path(tmp))                     # make() also asserts: same content as genie_bundle, every env
        for step in (["tests/run_tests.py"], ["scripts/validate_space.py"], ["scripts/readiness.py", "--strict"]):
            p = subprocess.run([sys.executable, *step], cwd=project, capture_output=True, text=True)
            assert p.returncode == 0, f"{step}\n{p.stdout[-3000:]}\n{p.stderr[-2000:]}"
        T = _tools(project)
        s = T.settings("prod")
        assert (T.project_name(), s["catalog"], T.space_title("dev")) == \
            ("freshcart-genie", "freshcart", "FreshCart Sales & Stock Assistant [DEV]")
        assert s["deployer_extra_groups"] == ["fc_all_regions"]
        space = T.load_space(project / SPACE_PATH)
        assert T.summary(space)["benchmark"] == 17 and T.summary(space)["metric view"] == 2
        assert sorted(T.data_sources(T.for_target(space, "qa"))) == [
            "freshcart_qa.gold.dim_product", "freshcart_qa.gold.dim_store", "freshcart_qa.semantic.fn_like_for_like_sales",
            "freshcart_qa.semantic.inventory_metrics", "freshcart_qa.semantic.sales_metrics"]
    # the FreshCart config is the template's, filled in: same settings, nothing left out or added
    template = yaml.safe_load((ROOT / "genie_template" / "genie.config.yml").read_text())
    freshcart = yaml.safe_load((EXAMPLE / "genie.config.yml").read_text())
    assert set(freshcart["variables"]) == set(template["variables"])
    assert set(freshcart["targets"]) == set(template["targets"])


def test_every_freshcart_query_runs_on_freshcart_data():
    """Every example (with its parameter defaults) and benchmark answer, rendered for each environment, runs on the
    FreshCart warehouse; the benchmarks return rows."""
    from freshcart import benchmarks, config as C, pipeline
    from freshcart.db import connect
    if not (C.WAREHOUSE_DIR / "gold.db").exists():
        pipeline.run(full_refresh=True, verbose=False)
    with tempfile.TemporaryDirectory() as tmp:
        project = _make(Path(tmp))
        T = _tools(project)
        sys.path.insert(0, str(project / "scripts"))
        with _own_modules():
            sys.path.insert(0, str(project / "scripts"))
            import check_sql
            con = connect()
            try:
                for env in T.ENVIRONMENTS:
                    items = check_sql.statements(T.for_target(T.load_space(project / SPACE_PATH), env))
                    assert len(items) == 6 + 17 and all(i["params"] is not None for i in items), env
                    for i in items:
                        params = {n: (int(v) if t == "INTEGER" else v) for n, (v, t) in i["params"].items()}
                        cols, rows = benchmarks.run_sql(con, i["sql"], params)
                        assert cols, (env, i["label"])
                        if i["label"].startswith("benchmark"):
                            assert rows, f"{env}: {i['label']} returns no rows on FreshCart data"
            finally:
                con.close()


def test_freshcart_project_bundle_with_the_real_databricks_cli():
    cli = _cli()
    if cli is None:
        print("      (Databricks CLI 1.14+ not found: FreshCart template bundle test skipped)")
        return
    from fake_workspace import FakeWorkspace, FreshCartEvaluator
    ws = FakeWorkspace.start(evaluator=FreshCartEvaluator())
    with tempfile.TemporaryDirectory() as tmp:
        project = _make(Path(tmp))
        T = _tools(project)
        env = {**os.environ, "DATABRICKS_HOST": ws.host, "DATABRICKS_TOKEN": "test", "DATABRICKS_CLI": cli}
        env.pop("DATABRICKS_CONFIG_PROFILE", None)

        def run(*args, ok=True):
            p = subprocess.run(list(args), cwd=project, env=env, capture_output=True, text=True)
            assert (p.returncode == 0) == ok, f"{args}\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}"
            return p.stdout

        try:
            for target in T.targets():
                res = json.loads(run(cli, "bundle", "validate", "-t", target, "-o", "json"))["resources"]
                space = json.loads(res["genie_spaces"]["genie_space"]["serialized_space"])
                catalog = T.settings(target)["catalog"]
                assert {i.split(".")[0] for i in T.data_sources(space)} == {catalog}, target
                assert res["genie_spaces"]["genie_space"]["title"].endswith(T.space_title(target))
            run(cli, "bundle", "deploy", "-t", "qa")
            assert all("freshcart_qa.genie_monitoring." in a["query_text"] for a in ws.state.alerts.values())
            # the release's qa steps, on FreshCart's 17 benchmarks
            run(cli, "bundle", "run", "genie_quality", "-t", "qa")
            report = run(sys.executable, "scripts/quality_report.py", "-t", "qa")
            assert "GATE PASSED" in report, report
            m = re.search(r"(\d+)/(\d+) graded correct \((\d+)%\)", report)
            assert m and int(m.group(3)) >= 60 and int(m.group(2)) >= 4, report
            run(cli, "bundle", "run", "genie_quality", "-t", "qa", "--params", "mode=smoke")
            smoke = run(sys.executable, "scripts/quality_report.py", "-t", "qa")
            assert "What were net sales and margin by region last week?" in smoke and "SMOKE TEST PASSED" in smoke
            run(cli, "bundle", "run", "genie_usage", "-t", "qa")
            # exporting the deployed space gives back the file in git, unchanged: syncs cause no churn
            before = (project / SPACE_PATH).read_text()
            out = run(sys.executable, "scripts/sync_from_workspace.py", "-t", "qa")
            assert "0 change(s)" in out and (project / SPACE_PATH).read_text() == before, out
        finally:
            ws.stop()


# ------------------------------------------------------------------------------------------- the notebook
class StrictSpark:
    """Spark for the notebook: SELECTs run on the FreshCart warehouse (with named parameters) and fail on errors,
    like Spark; every other statement is recorded."""

    def __init__(self):
        from test_databricks_notebook import _Spark
        self._base = _Spark()
        self.statements, self.queries = self._base.statements, []

    def sql(self, statement: str, args: dict | None = None):
        from test_databricks_notebook import _DF
        from freshcart import benchmarks
        from freshcart.db import connect
        head = statement.lstrip().split(None, 1)[0].upper()
        if head not in ("SELECT", "WITH"):
            return self._base.sql(statement)
        self.statements.append(statement)
        con = connect()
        try:
            rows = benchmarks.run_sql(con, statement, args or {})[1]
        finally:
            con.close()
        self.queries.append((statement, args))
        return _DF([tuple(r) for r in rows])

    def table(self, name):
        return self._base.table(name)


def _stand_ins(ws, holder, replacements, project: Path, T, token: str, files: dict | None = None, seed_dev=None):
    from fake_github import FakeGitHub
    for name in ("freshcart-business-users", "freshcart-genie-developers", "freshcart-genie-deployers", "fc_all_regions"):
        gid = str(9800 + len(ws.state.scim["Groups"]))    # what the FreshCart end-to-end notebook created
        ws.state.scim["Groups"][gid] = {"id": gid, "displayName": name, "members": [], "meta": {"resourceType": "Group"}}
    if files is None:
        files = {f.relative_to(project).as_posix(): f.read_text(encoding="utf-8") for f in project.rglob("*")
                 if f.is_file() and "__pycache__" not in f.parts}
    g = FakeGitHub("freshcart", "freshcart-genie", files, token=token, space_path=SPACE_PATH, check_names=("checks",))
    holder["g"] = g
    replacements['GITHUB_API = "https://api.github.com"'] = f"GITHUB_API = {g.url!r}"
    if seed_dev:
        seed_dev(ws)
    deployed_etag = {}

    def find(env):
        return next((s for s in ws.state.spaces.values() if s["title"] == T.space_title(env)), None)

    def on_deploy(env, text, allow_drift):
        rendered = T.for_target(T.space_from_text(text), env)
        with ws.state.lock:
            sp = find(env)
            if sp and env != "dev" and not allow_drift and deployed_etag.get(env) not in (None, sp["etag"]):
                return False
            serialized = json.dumps(rendered, sort_keys=True, separators=(",", ":"))
            if sp is None:
                sid = uuid.uuid4().hex
                sp = ws.state.spaces[sid] = {"space_id": sid, "title": T.space_title(env), "description": "",
                                             "warehouse_id": "wh-0001", "parent_path": "/Workspace/x",
                                             "serialized_space": serialized, "etag": "1"}
            else:
                sp.update(serialized_space=serialized, etag=str(int(sp["etag"]) + 1))
            deployed_etag[env] = sp["etag"]
        return True

    def on_dev_sync():
        sp = find("dev")
        return T.dumps_space(T.from_target(json.loads(sp["serialized_space"]), "dev")) if sp else None

    g.on_deploy, g.on_dev_sync = on_deploy, on_dev_sync
    return g


def _run(project: Path, widgets: dict, **kw):
    from test_databricks_notebook import run_notebook
    from test_genie_bundle import _cli as find
    cli = find()
    try:
        import nacl  # noqa: F401
    except ImportError:
        cli = None
    if cli is None:
        return None
    T = _tools(project)
    holder, replacements = {}, {}
    spark = StrictSpark()
    notebook = project / "notebooks" / "Genie_Project_Template_source.py"
    seed, files, until = kw.get("seed_dev"), kw.get("files"), kw.get("until")
    try:
        with _own_modules():
            ns, out, ws = run_notebook(cli, widgets, replacements, notebook=notebook, spark=spark, until=until,
                                       setup=lambda ws: _stand_ins(ws, holder, replacements, project, T,
                                                                   widgets["github_token"], files, seed))
    finally:
        if "g" in holder:
            holder["g"].stop()
    return ns, out, ws, holder["g"], T, spark


def test_template_notebook_for_freshcart_end_to_end():
    from freshcart import config as C, pipeline
    if not (C.WAREHOUSE_DIR / "gold.db").exists():
        pipeline.run(full_refresh=True, verbose=False)
    with tempfile.TemporaryDirectory() as tmp:
        project = _make(Path(tmp))
        result = _run(project, {"apply_changes": "yes", "github_repo": "freshcart/freshcart-genie",
                                "github_token": "ghp-freshcart"})          # example_data keeps its default: yes
        if result is None:
            print("      (Databricks CLI 1.14+ or PyNaCl not found: FreshCart template notebook run skipped)")
            return
        ns, out, ws, g, T, spark = result
        on_main = T.load_space(project / SPACE_PATH)
    statements = "\n".join(spark.statements)

    # B2: FreshCart's data is used, never the example data; deployers see every region; grants on gold and semantic
    assert "example_data = yes is ignored" in out and "samples.tpch" not in statements
    groups = {grp["displayName"]: {m["value"] for m in grp["members"]} for grp in ws.state.scim["Groups"].values()}
    sps = {sp["displayName"]: sp for sp in ws.state.scim["ServicePrincipals"].values()}
    for env, catalog in (("dev", "freshcart_dev"), ("qa", "freshcart_qa"), ("prod", "freshcart")):
        sp = sps[f"freshcart-genie-deployer-{env}"]
        assert sp["id"] in groups["freshcart-genie-deployers"] and sp["id"] in groups["fc_all_regions"], env
        for schema in ("gold", "semantic"):
            assert f"GRANT USE SCHEMA, SELECT, EXECUTE ON SCHEMA `{catalog}`.`{schema}` TO `{sp['applicationId']}`" in statements
            assert f"GRANT USE SCHEMA, SELECT, EXECUTE ON SCHEMA `{catalog}`.`{schema}` TO `freshcart-business-users`" in statements
        assert f"CREATE TABLE IF NOT EXISTS `{catalog}`.`genie_monitoring`.`genie_usage_messages`" in statements
    # C: all 23 queries ran on FreshCart data, the parameterised examples with their default values
    assert "23/23 queries ran against freshcart_dev" in out, out[out.find("queries ran") - 200:][:400]
    assert sum(1 for q, a in spark.queries if a) == 2, "both parameterised examples ran with their defaults"
    assert any("freshcart_dev.semantic.sales_metrics" in q for q, _ in spark.queries)
    # D: a FreshCart benchmark question was promoted and released to prod on prod's catalog
    feature = next(p for p in g.pulls.values() if p["head"]["ref"].startswith("feature/genie-"))
    assert feature["merged"] and "released to prod" in out
    shipped = T.space_from_text(g.file_at("main"))
    added = T.diff(on_main, shipped)
    assert len(added) == 1 and added[0].startswith("+ added sample question: "), added
    assert added[0].split(": ", 1)[1] in {" ".join(b["question"]) for b in on_main["benchmarks"]["questions"]}
    prod = next(s for s in ws.state.spaces.values() if s["title"] == "FreshCart Sales & Stock Assistant")
    assert "freshcart.semantic.sales_metrics" in prod["serialized_space"] and "freshcart_dev" not in prod["serialized_space"]
    assert "prod: live version genie-prod-" in out
    # F: the UI edit in dev came back as exactly one change, in environment-neutral form
    synced = T.space_from_text(g.file_at("genie/dev-sync"))
    assert T.diff(shipped, synced) == ["+ added sample question: Edited in the dev Genie UI (template notebook, part F)"]
    assert "${var.catalog}.${var.schema}.sales_metrics" in g.file_at("genie/dev-sync")
    # G and H: drift blocked then overwritten; the rollback restored the previous release
    assert "blocked by the drift gate, as intended" in out
    assert out.split("prod now runs", 1)[1].split("\n", 1)[0].endswith(": True")
    # J: the readiness review of the FreshCart project
    assert "ready, with open items: 0 MUST failed, 2 SHOULD open" in out


def test_template_notebook_imports_the_freshcart_prototype():
    """A fresh copy of the template (still the example space) + the FreshCart config; the FreshCart space was built by
    hand in dev. Importing it gives, byte for byte, the file make_project.py writes, and it is shipped."""
    from freshcart import config as C, pipeline
    if not (C.WAREHOUSE_DIR / "gold.db").exists():
        pipeline.run(full_refresh=True, verbose=False)
    import yaml
    freshcart = yaml.safe_load((ROOT / "genie_bundle" / "resources" / "freshcart_assistant.space.yml").read_text())
    prototype = json.dumps(freshcart["variables"]["freshcart_assistant_space"]["default"]).replace("${var.catalog}",
                                                                                                    "freshcart_dev")
    sid = uuid.uuid4().hex

    def seed(ws):                                            # the space someone built in the dev Genie UI
        ws.state.spaces[sid] = {"space_id": sid, "title": "FreshCart prototype", "description": "",
                                "warehouse_id": "wh-0001", "parent_path": "/Workspace/Users/dana",
                                "serialized_space": prototype, "etag": "7"}

    with tempfile.TemporaryDirectory() as tmp:
        project = _make(Path(tmp))
        expected = (project / SPACE_PATH).read_text()
        # the repository and the Git folder start as a fresh copy of the template: the example space
        (project / SPACE_PATH).write_text((ROOT / "genie_template" / SPACE_PATH).read_text())
        result = _run(project, {"apply_changes": "yes", "github_repo": "freshcart/freshcart-genie",
                                "github_token": "ghp-import", "example_data": "no", "import_space_id": sid},
                      seed_dev=seed, until="Part E")
        if result is None:
            print("      (Databricks CLI 1.14+ or PyNaCl not found: FreshCart import run skipped)")
            return
        ns, out, ws, g, T, spark = result
    assert f"imported 'FreshCart prototype' ({sid})" in out
    assert "23/23 queries ran against freshcart_dev" in out
    pr = next(p for p in g.pulls.values() if p["head"]["ref"].startswith("feature/genie-"))
    assert pr["title"] == "Genie: first version of the space (imported)" and pr["merged"]
    assert g.file_at("main") == expected, "the import differs from make_project.py's conversion"
    assert "released to prod" in out
    prod = next(s for s in ws.state.spaces.values() if s["title"] == "FreshCart Sales & Stock Assistant")
    assert "freshcart.semantic.inventory_metrics" in prod["serialized_space"]
