"""The Genie project template (genie_template/), tested as a project made from it would be.

  * its own tests (genie_template/tests: config, space definition, drift gate, quality job, wiring);
  * the bundle with the real Databricks CLI against the stand-in workspace (tests/fake_workspace.py): every target
    resolves from genie.config.yml; deploy, no-op plan, a UI edit is caught by the drift gate and adopted by the sync;
  * its notebook from top to bottom, with apply_changes = yes and as a dry run, against the stand-in workspace and
    the stand-in GitHub (tests/fake_github.py, which simulates the template's workflows).

The CLI and notebook parts are skipped when no Databricks CLI 1.14+ (or PyNaCl, for the notebook) is installed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "genie_template"
NOTEBOOK = TEMPLATE / "notebooks" / "Genie_Project_Template_source.py"
SPACE_PATH = "space/genie_space.yml"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(TEMPLATE / "scripts"))
import genie_tools as T                                        # noqa: E402


def _cli():
    from test_genie_bundle import _cli as find
    return find()


def test_template_own_tests_pass():
    p = subprocess.run([sys.executable, str(TEMPLATE / "tests" / "run_tests.py")], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-2000:]
    assert "All tests passed" in p.stdout


def test_template_bundle_with_the_real_databricks_cli():
    cli = _cli()
    if cli is None:
        print("      (Databricks CLI 1.14+ not found: template bundle test skipped)")
        return
    from fake_workspace import FakeWorkspace
    ws = FakeWorkspace.start()
    tmp = Path(tempfile.mkdtemp(prefix="genie_template_test_"))
    project = tmp / "project"
    shutil.copytree(TEMPLATE, project, ignore=shutil.ignore_patterns(".databricks", "__pycache__"))
    env = {**os.environ, "DATABRICKS_HOST": ws.host, "DATABRICKS_TOKEN": "test", "DATABRICKS_CLI": cli}
    env.pop("DATABRICKS_CONFIG_PROFILE", None)

    def run(*args, ok=True):
        p = subprocess.run(list(args), cwd=project, env=env, capture_output=True, text=True)
        assert (p.returncode == 0) == ok, f"{args}\n{p.stdout}\n{p.stderr}"
        return p.stdout

    try:
        for target in T.targets():
            cfg = json.loads(run(cli, "bundle", "validate", "-t", target, "-o", "json"))
            res = cfg["resources"]
            space = json.loads(res["genie_spaces"][T.SPACE_RESOURCE]["serialized_space"])
            s = T.settings(target)
            assert {i.rsplit(".", 1)[0] for i in T.data_sources(space)} == {f"{s['catalog']}.{s['schema']}"}, target
            assert res["genie_spaces"][T.SPACE_RESOURCE]["title"].endswith(T.space_title(target)), target
            params = {p["name"]: p["default"] for p in res["jobs"]["genie_quality"]["parameters"]}
            assert params["record_to"] == f"{s['catalog']}.{s['monitoring_schema']}", target
            assert ("schedule" in res["jobs"]["genie_quality"]) == (target == "prod"), target

        # a workspace.host in genie.config.yml merges with databricks.yml's root_path
        cfg_file = project / "genie.config.yml"
        text = cfg_file.read_text()
        cfg_file.write_text(text.replace("  prod:\n    # workspace:\n    #   host: https://<prod-workspace>.cloud.databricks.com",
                                         f"  prod:\n    workspace:\n      host: {ws.host}"))
        cfg = json.loads(run(cli, "bundle", "validate", "-t", "prod", "-o", "json"))
        assert cfg["workspace"]["host"] == ws.host and cfg["workspace"]["root_path"].endswith("/acme-orders-genie/prod")
        cfg_file.write_text(text)

        # a new project: nothing deployed to dev yet, so the sync has nothing to do (and does not fail the release)
        assert "not deployed to dev yet" in run(sys.executable, "scripts/sync_from_workspace.py", "-t", "dev")

        run(cli, "bundle", "deploy", "-t", "qa")
        assert "0 to add, 0 to change, 0 to delete" in run(cli, "bundle", "plan", "-t", "qa")
        sid = json.loads(run(cli, "bundle", "summary", "-t", "qa", "-o", "json"))["resources"]["genie_spaces"][T.SPACE_RESOURCE]["id"]
        ws.ui_edit(sid, lambda d: d["config"]["sample_questions"].append({"id": uuid.uuid4().hex, "question": ["UI edit"]}))
        Path(tmp, "plan.json").write_text(run(cli, "bundle", "plan", "-t", "qa", "-o", "json"))
        p = subprocess.run([sys.executable, "scripts/check_drift.py", str(tmp / "plan.json"), "-t", "qa", "--backup-dir",
                            str(tmp / "backup")], cwd=project, env=env, capture_output=True, text=True)
        assert p.returncode == 2 and "UI edit" in p.stdout, p.stdout
        out = run(sys.executable, "scripts/sync_from_workspace.py", "-t", "qa")
        assert "+ added sample question: UI edit" in out, out
        synced = T.load_space(project / SPACE_PATH)
        assert T.validate(synced)[0] == [], "the export is written with ${var.catalog}.${var.schema}"
        assert "acme_qa" not in (project / SPACE_PATH).read_text()
    finally:
        ws.stop()
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------------------------------- notebook
def _repo_files() -> dict[str, str]:
    """The template's files, as the root of a new repository."""
    out = {}
    for f in TEMPLATE.rglob("*"):
        if f.is_file() and "__pycache__" not in f.parts and ".databricks" not in f.parts:
            out[f.relative_to(TEMPLATE).as_posix()] = f.read_text(encoding="utf-8")
    return out


def _stand_ins(ws, holder, replacements, token):
    from fake_github import FakeGitHub
    for name in ("acme-genie-developers",):                     # one group exists already; the others are created
        gid = str(9700 + len(ws.state.scim["Groups"]))
        ws.state.scim["Groups"][gid] = {"id": gid, "displayName": name, "members": [], "meta": {"resourceType": "Group"}}
    g = FakeGitHub("acme", "orders-genie", _repo_files(), token=token, space_path=SPACE_PATH, check_names=("checks",))
    holder["g"], holder["ws"] = g, ws
    replacements['GITHUB_API = "https://api.github.com"'] = f"GITHUB_API = {g.url!r}"
    deployed_etag = {}

    def find(env):
        return next((s for s in ws.state.spaces.values() if s["title"] == T.space_title(env)), None)

    def on_deploy(env, text, allow_drift):
        rendered = T.for_target(T.space_from_text(text), env)
        with ws.state.lock:
            sp = find(env)
            if sp and env != "dev" and not allow_drift and deployed_etag.get(env) not in (None, sp["etag"]):
                return False                                       # check_drift.py: edited outside the bundle
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


def _requirements():
    cli = _cli()
    try:
        import nacl  # noqa: F401
    except ImportError:
        cli = None
    return cli


def test_template_notebook_is_well_formed():
    from test_databricks_notebook import cells
    kinds = [k for k, _ in cells(NOTEBOOK)]
    assert kinds.count("pip") == 1 and kinds.index("pip") < kinds.index("py")
    compile(NOTEBOOK.read_text(encoding="utf-8"), str(NOTEBOOK), "exec")


def test_template_notebook_end_to_end_against_stand_ins():
    cli = _requirements()
    if cli is None:
        print("      (Databricks CLI 1.14+ or PyNaCl not found: template notebook run skipped)")
        return
    from test_databricks_notebook import run_notebook
    holder, replacements = {}, {}
    widgets = {"apply_changes": "yes", "github_repo": "acme/orders-genie", "github_token": "ghp-template"}
    try:
        ns, out, ws = run_notebook(cli, widgets, replacements, notebook=NOTEBOOK,
                                   setup=lambda ws: _stand_ins(ws, holder, replacements, "ghp-template"))
    finally:
        if "g" in holder:
            holder["g"].stop()
    g = holder["g"]
    statements = "\n".join(ns["spark"].statements)

    # B2: groups, one service principal per environment (named after the project), catalogs, example data, grants
    names = {grp["displayName"] for grp in ws.state.scim["Groups"].values()}
    assert {"acme-genie-users", "acme-genie-developers", "acme-genie-deployers"} <= names
    sps = {sp["displayName"]: sp for sp in ws.state.scim["ServicePrincipals"].values()}
    assert {f"acme-orders-genie-deployer-{e}" for e in T.ENVIRONMENTS} <= set(sps)
    for env in T.ENVIRONMENTS:
        s, sp = T.settings(env), sps[f"acme-orders-genie-deployer-{env}"]
        assert f"CREATE CATALOG IF NOT EXISTS `{s['catalog']}`" in statements
        assert f"CREATE TABLE IF NOT EXISTS `{s['catalog']}`.`{s['schema']}`.`orders` AS SELECT * FROM samples.tpch.orders" in statements
        assert f"GRANT USE SCHEMA, SELECT, EXECUTE ON SCHEMA `{s['catalog']}`.`{s['schema']}` TO `{sp['applicationId']}`" in statements
        assert f"GRANT ALL PRIVILEGES ON SCHEMA `{s['catalog']}`.`{s['monitoring_schema']}` TO `{sp['applicationId']}`" in statements
        # B3: GitHub environments with the principal's id and its secret (decryptable, equal to Databricks')
        assert g.variables[env] == {"DATABRICKS_HOST": ws.host, "DATABRICKS_CLIENT_ID": sp["applicationId"]}
        assert g.secrets[env]["DATABRICKS_CLIENT_SECRET"] == ws.state.sp_secrets[sp["id"]][-1]
    assert g.default_branch == "main" and g.environments["prod"]["reviewers"] == [{"type": "User", "id": g.user_id}]
    assert g.settings["protection"]["required_status_checks"]["contexts"] == ["checks"]
    assert g.rulesets and g.rulesets[0]["conditions"]["ref_name"]["include"] == ["refs/tags/genie-prod-*"]
    # C: every SQL was run against dev; the sandbox was deployed as the user
    assert "queries ran against acme_dev" in out
    assert any(s["title"].startswith("[dev ") and s["title"].endswith("Acme Orders Assistant") for s in ws.state.spaces.values())
    # D: pull request, checks, merge, release to prod with an approval, a tag and a GitHub release
    feature = next(p for p in g.pulls.values() if p["head"]["ref"].startswith("feature/genie-"))
    assert feature["merged"] and "released to prod" in out
    prod = next(s for s in ws.state.spaces.values() if s["title"] == T.space_title("prod"))
    assert "acme_prod.tpch_demo.orders" in prod["serialized_space"] and "${var." not in prod["serialized_space"]
    assert any(t.startswith("genie-prod-") for t in g.tags) and g.releases
    # E: every environment's live version identified
    assert "prod: live version genie-prod-" in out
    # F: the UI edit in dev became a sync pull request with a snapshot tag
    sync = next(p for p in g.pulls.values() if p["head"]["ref"] == "genie/dev-sync")
    assert sync["state"] == "open" and any(t.startswith("genie-dev-snapshot-") for t in g.tags)
    assert "Edited in the dev UI" in g.file_at("genie/dev-sync") and "acme_dev" not in g.file_at("genie/dev-sync")
    # G: the prod edit blocked the release; allow_drift overwrote it
    assert "blocked by the drift gate, as intended" in out
    prod = next(s for s in ws.state.spaces.values() if s["title"] == T.space_title("prod"))
    assert "Edited directly in prod" not in prod["serialized_space"]
    # H: the rollback put the previous release back
    assert "prod now runs" in out and out.split("prod now runs", 1)[1].split("\n", 1)[0].endswith(": True")


def test_template_notebook_dry_run_changes_nothing():
    cli = _requirements()
    if cli is None:
        print("      (Databricks CLI 1.14+ or PyNaCl not found: template notebook dry run skipped)")
        return
    from test_databricks_notebook import run_notebook
    holder, replacements = {}, {}
    try:
        ns, out, ws = run_notebook(cli, {"github_repo": "acme/orders-genie", "github_token": "tok"}, replacements,
                                   notebook=NOTEBOOK, setup=lambda ws: _stand_ins(ws, holder, replacements, "tok"))
    finally:
        if "g" in holder:
            holder["g"].stop()
    g = holder["g"]
    assert ns["APPLY"] is False
    assert not ws.state.scim["ServicePrincipals"] and not ws.state.sp_secrets and not ws.state.spaces
    assert not [s for s in ns["spark"].statements if not s.lstrip().upper().startswith(("SELECT", "WITH"))], \
        "a dry run runs no DDL or grants"
    assert set(g.branches) == {"work"} and not g.environments and not g.pulls and not g.runs and not g.rulesets
    for expected in ("[dry run] create group acme-genie-users", "[dry run] create branch main", "[dry run] protect main",
                     "[dry run] protect the release tags", "[dry run] open the pull request",
                     "+ added sample question: Which nation has the most customers?",
                     "[dry run] GRANT USE CATALOG ON CATALOG `acme_prod`"):
        assert expected in out, expected
