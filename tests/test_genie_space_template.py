"""Tests for the reusable Genie space template (genie-space-template/).

Static tests need only PyYAML. When the Databricks CLI (1.14+) is installed, the integration test plays a
company adopting the template: an existing space that reads the prod catalog is exported with
scripts/adopt_space.py, then the template's own GitHub Actions workflow runs job by job against
tests/fake_workspace.py: dev and QA on push, and a prod release that takes over the existing space in place.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "genie-space-template"
WORKFLOW = TEMPLATE / ".github" / "workflows" / "deploy.yml"
FRESHCART_SPACE = REPO / "genie-as-code-review" / "src" / "freshcart_assistant.geniespace.json"
sys.path.insert(0, str(REPO / "tests"))

from test_simple_genie_bundle import _UniqueKeyLoader, _cli                 # noqa: E402


def _lib(root: Path = TEMPLATE):
    """space_lib of a copy of the template (it reads databricks.yml and src/ next to itself)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"space_lib_{abs(hash(root))}", root / "scripts" / "space_lib.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _workflow() -> dict:
    wf = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    wf["on"] = wf.pop(True, wf.get("on"))
    return wf


def _jobs_for(event: str, target: str | None = None) -> list[str]:
    running: list[str] = []
    for name, job in _workflow()["jobs"].items():
        expr = (job.get("if") or "True").replace("&&", " and ").replace("||", " or ")
        ns = {"github": type("G", (), {"event_name": event}), "inputs": type("I", (), {"target": target})}
        needs = job.get("needs", [])
        if bool(eval(expr, {"__builtins__": {}}, ns)) and all(n in running for n in ([needs] if isinstance(needs, str) else needs)):  # noqa: S307
            running.append(name)
    return running


def _fill(root: Path, host: str = "https://adb-1.azuredatabricks.net") -> None:
    """What a user does in step 1: replace every CHANGE_ME except the title and description."""
    path = root / "databricks.yml"
    text = re.sub(r"https://CHANGE_ME-\w+\.cloud\.databricks\.com", host, path.read_text(encoding="utf-8"))
    for old, new in {"CHANGE_ME-genie": "sales-genie", "CHANGE_ME warehouse name": "Serverless Starter Warehouse",
                     "CHANGE_ME_dev_catalog": "freshcart_dev", "CHANGE_ME_qa_catalog": "freshcart_qa",
                     "CHANGE_ME_prod_catalog": "freshcart", "CHANGE_ME-business-users": "sales-genie-users"}.items():
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def _copy(tmp: Path, name: str = "repo") -> Path:
    dest = tmp / name
    shutil.copytree(TEMPLATE, dest, ignore=shutil.ignore_patterns(".databricks", "__pycache__"))
    return dest


def _run(root: Path, *args: str, env: dict | None = None, ok: bool = True) -> str:
    p = subprocess.run([sys.executable, *args], cwd=root, env=env, capture_output=True, text=True)
    out = p.stdout + p.stderr
    assert (p.returncode == 0) == ok, f"{args}:\n{out}"
    return out


# ------------------------------------------------------------------------------------------ static
def test_template_rewrites_only_three_part_names_of_the_catalog():
    L = _lib()
    sql = ("SELECT s.net, sales.amount FROM sales.gold.fct s JOIN `sales`.gold.dim d "
           "JOIN sales_dev2.gold.x JOIN other.sales.gold JOIN sales.semantic.mv")
    new, n = L.rewrite_catalog(sql, "sales", "sales_qa")
    assert n == 3, new
    assert new == ("SELECT s.net, sales.amount FROM sales_qa.gold.fct s JOIN `sales_qa`.gold.dim d "
                   "JOIN sales_dev2.gold.x JOIN other.sales.gold JOIN sales_qa.semantic.mv")
    assert L.rewrite_catalog(sql, "sales", "sales") == (sql, 0)


def test_template_has_placeholders_and_dev_qa_prod_targets():
    cfg = yaml.safe_load((TEMPLATE / "databricks.yml").read_text(encoding="utf-8"))
    assert cfg["bundle"]["engine"] == "direct"
    assert list(cfg["targets"]) == ["dev", "qa", "prod"]
    assert cfg["targets"]["dev"]["mode"] == "development"
    assert all(cfg["targets"][t]["mode"] == "production" for t in ("qa", "prod"))
    assert "CHANGE_ME" in (TEMPLATE / "databricks.yml").read_text(encoding="utf-8")
    res = yaml.safe_load((TEMPLATE / "resources" / "genie_space.genie_space.yml").read_text(encoding="utf-8"))
    space = res["resources"]["genie_spaces"]["genie_space"]
    assert space["file_path"] == "../src/genie_space.geniespace.json"
    assert space["warehouse_id"] == "${var.warehouse_id}" and "parent_path" not in space
    assert not (TEMPLATE / "src" / "genie_space.geniespace.json").exists()     # written by adopt_space.py


def test_template_checks_fail_on_placeholders_and_pass_once_filled():
    tmp = Path(tempfile.mkdtemp(prefix="genie_template_"))
    try:
        root = _copy(tmp)
        (root / "src" / "genie_space.geniespace.json").write_text(FRESHCART_SPACE.read_text(encoding="utf-8"))
        out = _run(root, "scripts/check_space.py", ok=False)
        assert "databricks.yml" in out and "fill this in" in out, out
        _fill(root)
        for var in ("space_title", "space_description"):
            text = (root / "databricks.yml").read_text(encoding="utf-8")
            (root / "databricks.yml").write_text(re.sub(rf'(\n  {var}:\n(?:    .*\n)*?    default: )"CHANGE_ME[^"]*"',
                                                        r'\1"Sales"', text), encoding="utf-8")
        out = _run(root, "scripts/check_space.py")
        assert "Space checks passed" in out and "AI-readiness" in out and "benchmark questions" in out, out

        # a hand edit: unsorted ids, a missing id, another catalog
        space = json.loads(FRESHCART_SPACE.read_text(encoding="utf-8"))
        space["config"]["sample_questions"].insert(0, {"id": "ffff", "question": ["Sales by store?"]})
        space["data_sources"]["tables"][0]["identifier"] = "prod_cat.gold.dim_product"
        (root / "src" / "genie_space.geniespace.json").write_text(json.dumps(space))
        out = _run(root, "scripts/check_space.py", ok=False)
        assert "not in `bundle generate` format" in out and "not 32 lowercase hex" in out and "prod_cat" in out, out
        out = _run(root, "scripts/check_space.py", "--fix", ok=False)          # ids and format fixed, catalog not
        assert "new id for sample question" in out and "Space checks passed" not in out and "prod_cat" in out, out

        # set_catalog.py: dev catalog -> the target's, every reference
        (root / "src" / "genie_space.geniespace.json").write_text(FRESHCART_SPACE.read_text(encoding="utf-8"))
        out = _run(root, "scripts/set_catalog.py", "prod")
        assert "now reads catalog freshcart " in out, out
        text = (root / "src" / "genie_space.geniespace.json").read_text(encoding="utf-8")
        assert "freshcart_dev." not in text and "freshcart.semantic.sales_metrics" in text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_template_workflow_runs_the_right_jobs_for_each_trigger():
    assert _jobs_for("pull_request") == ["validate"]
    assert _jobs_for("push") == ["validate", "deploy-dev", "deploy-qa"]
    assert _jobs_for("workflow_dispatch", "dev") == ["validate", "deploy-dev"]
    assert _jobs_for("workflow_dispatch", "prod") == ["validate", "deploy-dev", "deploy-qa", "deploy-prod"]
    jobs = _workflow()["jobs"]
    assert jobs["deploy-prod"]["environment"] == "prod"
    runs = [s.get("run", "") for s in jobs["deploy-prod"]["steps"]]
    order = [next(i for i, r in enumerate(runs) if key in r) for key in
             ("set_catalog.py prod", "deployment bind", "bundle plan", "bundle deploy -t prod", "smoke_test.py")]
    assert order == sorted(order), runs


# ------------------------------------------------------------------------------------- integration
class _Runner:
    """Runs the template workflow's jobs like GitHub Actions: each job on a fresh checkout of `repo`, each
    `run:` step with bash and the job's and step's env. The fake workspace has no OAuth: the client id and
    secret become a token."""

    def __init__(self, ws, cli: str, tmp: Path, repo: Path):
        self.ws, self.tmp, self.repo, self.n = ws, tmp, repo, 0
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "databricks").symlink_to(cli)
        self.base = {**os.environ, "PATH": os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent),
                                                            os.environ.get("PATH", "")])}
        for k in ("DATABRICKS_CONFIG_PROFILE", "DATABRICKS_HOST", "DATABRICKS_TOKEN"):
            self.base.pop(k, None)

    def env(self, inputs: dict, *envs: dict) -> dict:
        out = dict(self.base)
        for e in envs:
            for k, v in (e or {}).items():
                v = re.sub(r"\$\{\{\s*vars\.\w+_DATABRICKS_HOST\s*\}\}", self.ws.host, str(v))
                v = re.sub(r"\$\{\{\s*inputs\.(\w+)\s*\}\}", lambda m: inputs.get(m.group(1), ""), v)
                out[k] = re.sub(r"\$\{\{\s*secrets\.\w+\s*\}\}", "fake-secret", v)
        if out.pop("DATABRICKS_CLIENT_ID", None):
            out.pop("DATABRICKS_CLIENT_SECRET", None)
            out["DATABRICKS_TOKEN"] = "fake-token"
        return out

    def job(self, name: str, **inputs) -> str:
        job = _workflow()["jobs"][name]
        self.n += 1
        checkout = self.tmp / f"checkout{self.n}"
        shutil.copytree(self.repo, checkout, ignore=shutil.ignore_patterns(".databricks", "__pycache__"))
        out = ""
        for step in (s for s in job["steps"] if "run" in s):
            p = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]], cwd=checkout,
                               env=self.env(inputs, job.get("env"), step.get("env")), capture_output=True, text=True)
            out += p.stdout + p.stderr
            assert p.returncode == 0, f"{name} / {step.get('name') or step['run']!r}:\n{out}"
        return out


def test_template_adopts_an_existing_space_and_promotes_it():
    cli = _cli()
    if cli is None:
        print("      (Databricks CLI 1.14+ not found: template integration test skipped)")
        return
    from fake_workspace import FakeWorkspace, FreshCartEvaluator
    ws = FakeWorkspace.start(evaluator=FreshCartEvaluator())
    tmp = Path(tempfile.mkdtemp(prefix="genie_template_it_"))
    try:
        # the company's live space, built by hand in the UI, reading the prod catalog
        live_space = FRESHCART_SPACE.read_text(encoding="utf-8").replace("freshcart_dev.", "freshcart.")
        body = {"title": "Sales Assistant", "description": "Ask about sales and stock.", "warehouse_id": "wh-0001",
                "parent_path": "/Workspace/Users/dana@freshcart.example", "serialized_space": live_space}
        req = urllib.request.Request(f"{ws.host}/api/2.0/genie/spaces", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer x", "Content-Type": "application/json"})
        live_id = json.load(urllib.request.urlopen(req))["space_id"]

        repo = _copy(tmp)
        _fill(repo, ws.host)
        r = _Runner(ws, cli, tmp, repo)
        local = {**r.base, "DATABRICKS_HOST": ws.host, "DATABRICKS_TOKEN": "fake-token"}

        # adopt_space.py: export, keep our resource YAML, title/description, catalog moved to dev
        resource_before = (repo / "resources" / "genie_space.genie_space.yml").read_text(encoding="utf-8")
        out = _run(repo, "scripts/adopt_space.py", "--space-id", live_id, env=local)
        assert "rewrote" in out and "dev catalog freshcart_dev" in out, out
        assert (repo / "resources" / "genie_space.genie_space.yml").read_text(encoding="utf-8") == resource_before
        cfg = (repo / "databricks.yml").read_text(encoding="utf-8")
        assert 'default: "Sales Assistant"' in cfg and 'default: "Ask about sales and stock."' in cfg
        assert "CHANGE_ME" not in cfg.split("#")[0] and not _lib(repo).placeholders_left()
        exported = (repo / "src" / "genie_space.geniespace.json").read_text(encoding="utf-8")
        assert exported == FRESHCART_SPACE.read_text(encoding="utf-8")       # same space, now on the dev catalog
        assert "Space checks passed" in _run(repo, "scripts/check_space.py")
        # the stand-in answers only questions with a trusted example, so list those (as step 6 suggests)
        with (repo / "tests" / "smoke_questions.txt").open("a", encoding="utf-8") as f:
            f.write("What were net sales and margin by region last week?\n"
                    "Where are we out of stock most often in fresh?\n")

        # push to main: validate -> dev + smoke test -> QA + benchmark gate (new spaces)
        logs = {name: r.job(name) for name in _jobs_for("push")}
        assert "All Genie smoke questions answered" in logs["deploy-dev"]
        assert "now reads catalog freshcart_qa" in logs["deploy-qa"] and "Gate passed" in logs["deploy-qa"]
        titles = {sp["title"] for sp in ws.spaces.values()}
        assert titles == {"Sales Assistant", "[dev dana] Sales Assistant", "Sales Assistant [QA]"}, titles

        # first prod release: prod takes over the live space in place
        logs = {name: r.job(name, target="prod", adopt_space_id=live_id) for name in _jobs_for("workflow_dispatch", "prod")}
        assert "Successfully bound" in logs["deploy-prod"], logs["deploy-prod"]
        assert "All Genie smoke questions answered" in logs["deploy-prod"]
        assert len(ws.spaces) == 3, [sp["title"] for sp in ws.spaces.values()]
        prod = ws.spaces[live_id]
        assert prod["title"] == "Sales Assistant" and prod["warehouse_id"] == "wh-0001"
        assert "freshcart.semantic.sales_metrics" in prod["serialized_space"]
        assert "freshcart_dev." not in prod["serialized_space"]
        acl = ws.state.permissions.get(f"genie/{live_id}") or next(
            v for k, v in ws.state.permissions.items() if live_id in k)
        assert {"group_name": "sales-genie-users", "permission_level": "CAN_RUN"} in acl, acl

        # later releases: no adopt_space_id, still the same space
        r.job("deploy-prod", target="prod")
        assert len(ws.spaces) == 3 and live_id in ws.spaces

        # the daily loop: a UI edit in dev comes back with `bundle generate` as a change to the JSON only
        dev_id = next(i for i, sp in ws.spaces.items() if sp["title"].startswith("[dev "))
        ws.ui_edit(dev_id, lambda s: s["config"]["sample_questions"].append(
            {"id": "ffffffffffffffffffffffffffffffff", "question": ["What is our online share by region?"]}))
        before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".databricks" not in p.parts}
        subprocess.run(["databricks", "bundle", "generate", "genie-space", "--resource", "genie_space", "--force",
                        "-t", "dev"], cwd=repo, env=local, capture_output=True, text=True, check=True)
        changed = {p.relative_to(repo).as_posix() for p in repo.rglob("*")
                   if p.is_file() and ".databricks" not in p.parts and before.get(p) != p.read_bytes()}
        assert changed == {"src/genie_space.geniespace.json"}, changed
        assert "Space checks passed" in _run(repo, "scripts/check_space.py")
    finally:
        ws.stop()
        shutil.rmtree(tmp, ignore_errors=True)
