"""Tests for the simple Genie-as-Code bundle (genie-as-code-review/).

Static tests need only PyYAML. When the Databricks CLI (1.14+) is installed, the integration test runs
the bundle's own GitHub Actions workflow (genie-as-code-review/.github/workflows/deploy.yml), job by job
and step by step, against tests/fake_workspace.py with the real CLI: deploy to dev, qa and prod, the
smoke test, the benchmark gate, and the `bundle generate` round trip.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SIMPLE = REPO / "genie-as-code-review"
SPACE_FILE = SIMPLE / "src" / "freshcart_assistant.geniespace.json"
WORKFLOW = SIMPLE / ".github" / "workflows" / "deploy.yml"
KEY = "freshcart_assistant"
DEV_CATALOG = "freshcart_dev"
sys.path.insert(0, str(REPO / "genie_bundle" / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

import space_tools as T                                                    # noqa: E402

SPACE = json.loads(SPACE_FILE.read_text(encoding="utf-8"))
_CATALOG_REF = re.compile(r"\b(\w+)\.(?:gold|semantic)\.\w+")


def _catalogs(space: dict) -> set[str]:
    return set(_CATALOG_REF.findall(json.dumps(space)))


def _examples(space: dict) -> set[str]:
    return {T.text(e["question"]) for e in space["instructions"]["example_question_sqls"]}


class _UniqueKeyLoader(yaml.SafeLoader):
    """GitHub rejects a workflow with a duplicate key (for example two `env:` in one step); so does this."""


def _no_duplicates(loader, node, deep=False):
    keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        raise yaml.constructor.ConstructorError(None, None, f"duplicate key(s) {dupes}", node.start_mark)
    return loader.construct_mapping(node, deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def _workflow() -> dict:
    wf = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    wf["on"] = wf.pop(True, wf.get("on"))                    # YAML 1.1 reads the key `on` as True
    return wf


def _job_runs(job: dict, event: str, target: str | None) -> bool:
    """Evaluate a job's `if:` (the simple expressions this workflow uses) for an event."""
    expr = job.get("if")
    if expr is None:
        return True
    py = expr.replace("&&", " and ").replace("||", " or ")
    ns = {"github": type("G", (), {"event_name": event}), "inputs": type("I", (), {"target": target})}
    return bool(eval(py, {"__builtins__": {}}, ns))           # noqa: S307 (our own workflow file)


def _jobs_for(event: str, target: str | None = None) -> list[str]:
    jobs = _workflow()["jobs"]
    running: list[str] = []
    for name, job in jobs.items():                           # jobs are written in dependency order
        needs = job.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        if all(n in running for n in needs) and _job_runs(job, event, target):
            running.append(name)
    return running


# ------------------------------------------------------------------------------- static checks
def test_simple_space_is_in_generate_format_and_valid():
    text = SPACE_FILE.read_text(encoding="utf-8")
    assert text == json.dumps(SPACE, indent=2, sort_keys=True, ensure_ascii=False) + "\n", \
        "keep the JSON exactly as `bundle generate` writes it (indent 2, sorted keys), so syncs give clean diffs"
    assert T.normalise(SPACE) == SPACE, "lists must be sorted by id / identifier / column_name"
    errors, warnings = T.validate(T.neutralise(SPACE, DEV_CATALOG))
    assert not errors, errors
    assert not warnings, warnings
    assert _catalogs(SPACE) == {DEV_CATALOG}, "the JSON is written with the dev catalog only"
    assert SPACE["benchmarks"]["questions"], "the QA gate needs benchmarks"


def test_simple_space_sql_runs_locally():
    assert T.check_sql_locally(SPACE) == []


def test_simple_bundle_catalogs_match_the_data_bundle():
    simple = yaml.safe_load((SIMPLE / "databricks.yml").read_text())["targets"]
    data = yaml.safe_load((REPO / "databricks" / "databricks.yml").read_text())["targets"]
    assert {t: v["variables"]["catalog"] for t, v in simple.items()} == \
           {t: v["variables"]["catalog"] for t, v in data.items()}
    assert simple["dev"]["variables"]["catalog"] == DEV_CATALOG


def test_simple_smoke_questions_have_trusted_examples():
    tree = ast.parse((SIMPLE / "tests" / "test_genie.py").read_text())
    smoke = next(ast.literal_eval(n.value) for n in tree.body
                 if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "SMOKE_QUESTIONS")
    assert smoke and set(smoke) <= _examples(SPACE)


def test_simple_resource_points_at_the_space_file():
    res = yaml.safe_load((SIMPLE / "resources" / f"{KEY}.genie_space.yml").read_text())
    gs = res["resources"]["genie_spaces"][KEY]
    assert (SIMPLE / "resources" / gs["file_path"]).resolve() == SPACE_FILE
    assert gs["warehouse_id"] == "${var.warehouse_id}"
    assert {"level": "CAN_RUN", "group_name": "${var.business_users_group}"} in gs["permissions"]


def test_simple_workflow_runs_the_right_jobs_for_each_trigger():
    wf = _workflow()
    assert set(wf["on"]) == {"pull_request", "push", "workflow_dispatch"}
    assert _jobs_for("pull_request") == ["validate"]
    assert _jobs_for("push") == ["validate", "deploy-dev", "deploy-qa"]
    assert _jobs_for("workflow_dispatch", "dev") == ["validate", "deploy-dev"]
    assert _jobs_for("workflow_dispatch", "qa") == ["validate", "deploy-dev", "deploy-qa"]
    assert _jobs_for("workflow_dispatch", "prod") == ["validate", "deploy-dev", "deploy-qa", "deploy-prod"]
    for name, job in wf["jobs"].items():
        for step in job["steps"]:
            if "jq" in step.get("run", ""):
                assert f".resources.genie_spaces.{KEY}.id" in step["run"], (name, step.get("name"))


# ----------------------------------------------------------------- real CLI, fake workspace
def _cli() -> str | None:
    cli = os.environ.get("DATABRICKS_CLI") or shutil.which("databricks")
    if not cli:
        return None
    out = subprocess.run([cli, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"v(\d+)\.(\d+)", out)
    return cli if m and (int(m.group(1)), int(m.group(2))) >= (1, 14) else None


class _Runner:
    """Runs the workflow's jobs like GitHub Actions does: every job on a fresh checkout, every `run:` step
    with bash -eo pipefail and the job's and step's env. `uses:` steps are provided by the test (the CLI and
    Python are on PATH). The fake workspace has no OAuth, so the service principal's client id and secret
    become a token."""

    def __init__(self, ws, cli: str, tmp: Path):
        self.ws, self.tmp, self.n = ws, tmp, 0
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "databricks").symlink_to(cli)
        self.base = {**os.environ, "PATH": os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent),
                                                            os.environ.get("PATH", "")])}
        self.base.pop("DATABRICKS_CONFIG_PROFILE", None)

    def checkout(self) -> Path:
        self.n += 1
        dest = self.tmp / f"checkout{self.n}"
        shutil.copytree(SIMPLE, dest, ignore=shutil.ignore_patterns(".databricks", "__pycache__"))
        return dest

    def env(self, *envs: dict) -> dict:
        out = dict(self.base)
        for e in envs:
            for k, v in (e or {}).items():
                v = re.sub(r"\$\{\{\s*vars\.\w+_DATABRICKS_HOST\s*\}\}", self.ws.host, str(v))
                out[k] = re.sub(r"\$\{\{\s*secrets\.\w+\s*\}\}", "fake-secret", v)
        if out.pop("DATABRICKS_CLIENT_ID", None):
            out.pop("DATABRICKS_CLIENT_SECRET", None)
            out["DATABRICKS_TOKEN"] = "fake-token"
        return out

    def step(self, checkout: Path, job: dict, step: dict, ok: bool = True) -> str:
        p = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]], cwd=checkout,
                           env=self.env(job.get("env"), step.get("env")), capture_output=True, text=True)
        out = p.stdout + p.stderr
        assert (p.returncode == 0) == ok, f"step {step.get('name') or step['run']!r}:\n{out}"
        return out

    def job(self, name: str) -> str:
        job = _workflow()["jobs"][name]
        checkout = self.checkout()
        return "".join(self.step(checkout, job, s) for s in job["steps"] if "run" in s)


def _space_catalogs(ws) -> dict[str, set[str]]:
    """Title -> catalogs read by that deployed space."""
    titles = [sp["title"] for sp in ws.spaces.values()]
    assert len(titles) == len(set(titles)), f"two spaces with the same title: {titles}"
    return {sp["title"]: _catalogs(json.loads(sp["serialized_space"])) for sp in ws.spaces.values()}


def _break_example(space):
    for e in space["instructions"]["example_question_sqls"]:
        if "net sales and margin by region" in T.text(e["question"]):
            e["sql"] = [line.replace("MEASURE(net_sales)", "MEASURE(gross_sales)") for line in e["sql"]]


def test_simple_bundle_workflow_against_the_fake_workspace():
    cli = _cli()
    if cli is None:
        print("      (Databricks CLI 1.14+ not found: simple bundle integration test skipped)")
        return
    from fake_workspace import FakeWorkspace, FreshCartEvaluator
    ws = FakeWorkspace.start(evaluator=FreshCartEvaluator())
    tmp = Path(tempfile.mkdtemp(prefix="simple_genie_test_"))
    try:
        r = _Runner(ws, cli, tmp)
        jobs = _workflow()["jobs"]

        # push to main: validate -> dev + smoke test -> qa + benchmark gate
        logs = {name: r.job(name) for name in _jobs_for("push")}
        assert "All Genie smoke questions answered" in logs["deploy-dev"]
        assert "Space for target qa now reads catalog freshcart_qa" in logs["deploy-qa"]
        assert "Gate passed" in logs["deploy-qa"]
        catalogs = _space_catalogs(ws)
        assert catalogs == {"[dev dana] FreshCart Sales & Stock Assistant": {"freshcart_dev"},
                            "FreshCart Sales & Stock Assistant [QA]": {"freshcart_qa"}}, catalogs

        # manual dispatch to prod: dev and qa are updated in place, prod is created on its own catalog
        logs = {name: r.job(name) for name in _jobs_for("workflow_dispatch", "prod")}
        assert "Gate passed" in logs["deploy-qa"] and "All Genie smoke questions answered" in logs["deploy-prod"]
        assert _space_catalogs(ws) == {"[dev dana] FreshCart Sales & Stock Assistant": {"freshcart_dev"},
                                       "FreshCart Sales & Stock Assistant [QA]": {"freshcart_qa"},
                                       "FreshCart Sales & Stock Assistant": {"freshcart"}}
        assert all({"group_name": "freshcart-business-users", "permission_level": "CAN_RUN"} in acl
                   for acl in ws.state.permissions.values())

        # someone breaks the QA space in the UI: the benchmark gate fails
        checkout = r.checkout()
        qa_id = json.loads(subprocess.run(["databricks", "bundle", "summary", "-t", "qa", "-o", "json"], cwd=checkout,
                                          env=r.env(jobs["deploy-qa"]["env"]), capture_output=True, text=True,
                                          check=True).stdout)["resources"]["genie_spaces"][KEY]["id"]
        ws.ui_edit(qa_id, _break_example)
        gate = next(s for s in jobs["deploy-qa"]["steps"] if s.get("name") == "Genie benchmark gate")
        out = r.step(checkout, jobs["deploy-qa"], gate)                     # 3 of 4 graded right: above 60%
        assert "❌ What were net sales and margin by region last week?" in out and "Gate passed" in out, out
        strict = {**gate, "env": {**gate["env"], "ACCURACY_THRESHOLD": "0.95"}}
        assert "GATE FAILED: accuracy 75% < threshold 95%" in r.step(checkout, jobs["deploy-qa"], strict, ok=False)

        # ... and a dev space without its trusted examples fails the smoke test
        dev_id = next(i for i, sp in ws.spaces.items() if sp["title"].startswith("[dev "))
        ws.ui_edit(dev_id, lambda s: s["instructions"].update(example_question_sqls=[]))
        smoke = next(s for s in jobs["deploy-dev"]["steps"] if s.get("name") == "Smoke test (conversation API)")
        assert "[FAIL]" in r.step(checkout, jobs["deploy-dev"], smoke, ok=False)

        # `bundle generate` round trip: a UI edit in dev comes back as a clean change to the JSON only
        def add_sample_question(space):
            space["instructions"] = json.loads(json.dumps(SPACE["instructions"]))     # undo the edit above
            space["config"]["sample_questions"].append(
                {"id": "ffffffffffffffffffffffffffffffff", "question": ["What is our online share by region?"]})

        checkout = r.checkout()
        ws.ui_edit(dev_id, add_sample_question)
        before = {p: p.read_bytes() for p in checkout.rglob("*") if p.is_file()}
        subprocess.run(["databricks", "bundle", "generate", "genie-space", "--resource", KEY, "--force", "-t", "dev"],
                       cwd=checkout, env=r.env(jobs["deploy-dev"]["env"]), capture_output=True, text=True, check=True)
        changed = {p.relative_to(checkout).as_posix() for p in checkout.rglob("*")
                   if p.is_file() and ".databricks" not in p.parts and before.get(p) != p.read_bytes()}
        assert changed == {"src/freshcart_assistant.geniespace.json"}, changed
        synced_text = (checkout / "src" / "freshcart_assistant.geniespace.json").read_text(encoding="utf-8")
        synced = json.loads(synced_text)
        assert synced_text == json.dumps(synced, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        assert {k: v for k, v in synced.items() if k != "config"} == {k: v for k, v in SPACE.items() if k != "config"}
        assert ["What is our online share by region?"] in [q["question"] for q in synced["config"]["sample_questions"]]
    finally:
        ws.stop()
        shutil.rmtree(tmp, ignore_errors=True)
