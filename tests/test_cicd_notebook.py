"""Run notebooks/FreshCart_CICD_Dev_to_Prod_with_DAB_and_GitHub_source.py from top to bottom, offline.

Every Python cell runs with apply_changes = yes against two stand-ins: tests/fake_workspace.py (Databricks, with
the real Databricks CLI for the hand-over) and tests/fake_github.py (GitHub, which simulates the release, sync and
rollback workflows; its "deployments" update the stand-in workspace through on_deploy, which also reports drift
like check_drift.py does). Skipped when no Databricks CLI or PyNaCl is installed.
"""
from __future__ import annotations

import json
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "genie_bundle" / "scripts"))

NOTEBOOK = ROOT / "notebooks" / "FreshCart_CICD_Dev_to_Prod_with_DAB_and_GitHub_source.py"
SPACE_PATH = "genie_bundle/resources/freshcart_assistant.space.yml"
TITLES = {"dev": "FreshCart Sales & Stock Assistant [DEV]", "qa": "FreshCart Sales & Stock Assistant [QA]",
          "prod": "FreshCart Sales & Stock Assistant"}
CATALOGS = {"dev": "freshcart_dev", "qa": "freshcart_qa", "prod": "freshcart"}


def _space_from_text(text: str) -> dict:
    import space_tools as T
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "space.yml"
        p.write_text(text)
        return T.load_space(p)


def _text_from_space(space: dict) -> str:
    import space_tools as T
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "space.yml"
        T.save_space(space, p)
        return p.read_text()


def test_cicd_notebook_cells_are_well_formed():
    from test_databricks_notebook import cells
    kinds = [k for k, _ in cells(NOTEBOOK)]
    assert kinds.count("pip") == 1 and kinds.index("pip") < kinds.index("py")
    compile(NOTEBOOK.read_text(encoding="utf-8"), str(NOTEBOOK), "exec")
    from freshcart import databricks_notebook as N
    source, target = N.paths("FreshCart_CICD_Dev_to_Prod_with_DAB_and_GitHub")
    assert target.read_text(encoding="utf-8") == N.render(source), "run: python -m freshcart.databricks_notebook"


def test_cicd_notebook_end_to_end_against_stand_ins():
    from test_genie_bundle import _cli
    cli = _cli()
    try:
        import nacl  # noqa: F401
    except ImportError:
        cli = None
    if cli is None:
        print("      (Databricks CLI 1.14+ or PyNaCl not found: CI/CD notebook run skipped)")
        return
    import space_tools as T
    from fake_github import FakeGitHub
    from test_databricks_notebook import run_notebook

    holder, deployed_etag, replacements = {}, {}, {}

    def setup(ws):
        for name in ("freshcart-genie-deployers", "fc_all_regions"):
            gid = str(9500 + len(ws.state.scim["Groups"]))
            ws.state.scim["Groups"][gid] = {"id": gid, "displayName": name, "members": [], "meta": {"resourceType": "Group"}}
        g = FakeGitHub("dana", "freshcart", {SPACE_PATH: (ROOT / SPACE_PATH).read_text()}, token="ghp-test-token")
        holder["g"], holder["ws"] = g, ws
        replacements['GITHUB_API = "https://api.github.com"'] = f"GITHUB_API = {g.url!r}"

        def find(env):
            return next((s for s in ws.state.spaces.values() if s["title"] == TITLES[env]), None)

        def on_deploy(env, text, allow_drift):
            rendered = T.render(_space_from_text(text), CATALOGS[env])
            with ws.state.lock:
                sp = find(env)
                if sp and env != "dev" and not allow_drift and deployed_etag.get(env) not in (None, sp["etag"]):
                    return False                                       # check_drift.py: edited outside the bundle
                serialized = json.dumps(rendered, sort_keys=True, separators=(",", ":"))
                if sp is None:
                    sid = uuid.uuid4().hex
                    sp = ws.state.spaces[sid] = {"space_id": sid, "title": TITLES[env], "description": "",
                                                 "warehouse_id": "wh-0001", "parent_path": "/Workspace/x",
                                                 "serialized_space": serialized, "etag": "1"}
                else:
                    sp.update(serialized_space=serialized, etag=str(int(sp["etag"]) + 1))
                deployed_etag[env] = sp["etag"]
            return True

        def on_dev_sync():
            sp = find("dev")
            return _text_from_space(T.neutralise(json.loads(sp["serialized_space"]), CATALOGS["dev"])) if sp else None

        g.on_deploy, g.on_dev_sync = on_deploy, on_dev_sync

    widgets = {"apply_changes": "yes", "github_repo": "dana/freshcart", "github_token": "ghp-test-token"}
    try:
        ns, out, ws = run_notebook(cli, widgets, replacements, setup=setup, notebook=NOTEBOOK)
    finally:
        if "g" in holder:
            holder["g"].stop()
    g = holder["g"]

    # B2: one service principal per environment, in both groups, with an OAuth secret
    sps = {sp["displayName"]: sp for sp in ws.state.scim["ServicePrincipals"].values()}
    assert set(sps) == {f"freshcart-deployer-{e}" for e in CATALOGS}
    for group in ws.state.scim["Groups"].values():
        if group["displayName"] in ("freshcart-genie-deployers", "fc_all_regions"):
            assert {m["value"] for m in group["members"]} >= {sp["id"] for sp in sps.values()}
    grants = "\n".join(ns["spark"].statements)
    for cat in CATALOGS.values():
        assert f"GRANT ALL PRIVILEGES ON CATALOG `{cat}`" in grants and f"CREATE CATALOG IF NOT EXISTS `{cat}`" in grants
    # B3: GitHub matches the architecture; secrets arrive decryptable and equal to the Databricks secrets
    assert g.default_branch == "main" and "main" in g.branches
    assert g.environments["prod"]["reviewers"] == [{"type": "User", "id": g.user_id}]
    assert g.settings["protection"]["required_status_checks"]["contexts"] == ["run-demo"]
    for env in CATALOGS:
        sp = sps[f"freshcart-deployer-{env}"]
        assert g.variables[env] == {"DATABRICKS_HOST": ws.host, "DATABRICKS_CLIENT_ID": sp["applicationId"]}
        assert g.secrets[env]["DATABRICKS_CLIENT_SECRET"] == ws.state.sp_secrets[sp["id"]][-1]
    assert ws.state.secret_scopes["freshcart"]["github_token"] == "ghp-test-token"
    # D: the pull request was checked, merged and released to prod with an approval and a tag
    feature = next(p for p in g.pulls.values() if p["head"]["ref"].startswith("feature/"))
    assert feature["merged"]
    assert "released to prod" in out
    prod = next(s for s in ws.state.spaces.values() if s["title"] == TITLES["prod"])
    assert "Which five stores sold the most last week?" in prod["serialized_space"]
    assert any(t.startswith("genie-prod-") for t in g.tags)
    # E: every environment's live space was identified
    assert "prod: live version genie-prod-" in out, "prod runs a tagged release"
    # F: the UI edit in dev became a sync pull request with a tagged snapshot
    sync = next(p for p in g.pulls.values() if p["head"]["ref"] == "genie/dev-sync")
    assert sync["state"] == "open" and any(t.startswith("genie-dev-snapshot-") for t in g.tags)
    assert "What were online sales by region last week?" in out
    # G: the prod edit blocked the release; the allow_drift release overwrote it
    assert "blocked by the drift gate, as intended" in out
    assert "Edited directly in prod" not in prod["serialized_space"]
    # H: the rollback put the previous release back in prod
    assert "prod now runs" in out and out.split("prod now runs", 1)[1].split("\n", 1)[0].endswith(": True")


def test_cicd_notebook_dry_run_changes_nothing():
    """The default (apply_changes = no) runs every cell, prints the planned changes and changes nothing."""
    from test_genie_bundle import _cli
    cli = _cli()
    try:
        import nacl  # noqa: F401
    except ImportError:
        cli = None
    if cli is None:
        print("      (Databricks CLI 1.14+ or PyNaCl not found: CI/CD notebook dry run skipped)")
        return
    from fake_github import FakeGitHub
    from test_databricks_notebook import run_notebook
    holder, replacements = {}, {}

    def setup(ws):
        for name in ("freshcart-genie-deployers", "fc_all_regions"):
            gid = str(9600 + len(ws.state.scim["Groups"]))
            ws.state.scim["Groups"][gid] = {"id": gid, "displayName": name, "members": [], "meta": {"resourceType": "Group"}}
        g = holder["g"] = FakeGitHub("dana", "freshcart", {SPACE_PATH: (ROOT / SPACE_PATH).read_text()}, token="tok")
        replacements['GITHUB_API = "https://api.github.com"'] = f"GITHUB_API = {g.url!r}"

    try:
        ns, out, ws = run_notebook(cli, {"github_repo": "dana/freshcart", "github_token": "tok"}, replacements,
                                   setup=setup, notebook=NOTEBOOK)
    finally:
        if "g" in holder:
            holder["g"].stop()
    g = holder["g"]
    assert ns["APPLY"] is False
    assert not ws.state.scim["ServicePrincipals"] and not ws.state.sp_secrets and not ws.state.spaces
    assert set(g.branches) == {"work"} and g.default_branch == "work"
    assert not g.environments and not g.variables and not g.secrets and not g.pulls and not g.runs
    assert "[dry run] create branch main" in out and "[dry run] protect main" in out
    assert "[dry run] open the pull request" in out and "+ added sample question" in out
