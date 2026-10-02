"""Tests for the Genie space bundle (genie_bundle/).

Unit tests need only PyYAML (+ databricks-sdk for the gate tests). When the Databricks CLI (1.14+)
is installed, the integration test also runs the real `databricks bundle validate / deploy / plan /
generate` against tests/fake_workspace.py, an in-memory stand-in for a workspace.
"""
from __future__ import annotations

import copy
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
BUNDLE = REPO / "genie_bundle"
sys.path.insert(0, str(BUNDLE / "scripts"))
sys.path.insert(0, str(BUNDLE / "src"))
sys.path.insert(0, str(REPO / "tests"))

import check_drift                                                         # noqa: E402
import space_tools as T                                                    # noqa: E402

SPACE = T.load_space()


def _break_example(space):
    for e in space["instructions"]["example_question_sqls"]:
        if "net sales and margin by region" in T.text(e["question"]):
            e["sql"] = [line.replace("MEASURE(net_sales)", "MEASURE(gross_sales)") for line in e["sql"]]


# --------------------------------------------------------------------------- the definition
def test_space_definition_is_canonical_and_valid():
    assert T.is_canonical(), "run genie_bundle/scripts/validate_space.py --fix"
    errors, warnings = T.validate(SPACE)
    assert not errors, errors
    assert not warnings, warnings


def test_every_sql_in_the_space_runs_locally():
    assert T.check_sql_locally(SPACE) == []


def test_catalog_variable_round_trip_for_every_target():
    for target, catalog in T.TARGET_CATALOGS.items():
        rendered = T.render(SPACE, catalog)
        assert T.CATALOG_REF not in json.dumps(rendered)
        assert T.content_hash(T.neutralise(rendered, catalog)) == T.content_hash(SPACE), target


def test_target_catalogs_match_databricks_yml():
    cfg = yaml.safe_load((BUNDLE / "databricks.yml").read_text())
    from_yml = {t: v["variables"]["catalog"] for t, v in cfg["targets"].items()}
    assert from_yml == T.TARGET_CATALOGS


def test_hard_coded_catalog_is_rejected():
    bad = copy.deepcopy(SPACE)
    bad["data_sources"]["tables"][0]["identifier"] = "freshcart_qa.gold.dim_store"
    assert any("freshcart_qa" in e for e in T.validate(bad)[0])


def test_diff_names_the_changed_example_and_the_line():
    bad = copy.deepcopy(SPACE)
    _break_example(bad)
    changes = T.diff(SPACE, bad)
    assert changes[0] == "~ changed example SQL: What were net sales and margin by region last week?"
    assert any("gross_sales" in c for c in changes)


def test_resource_uses_the_space_variable():
    res = yaml.safe_load((BUNDLE / "resources" / "freshcart_assistant.genie_space.yml").read_text())
    gs = res["resources"]["genie_spaces"][T.SPACE_KEY]
    assert gs["serialized_space"] == "${var.%s}" % T.SPACE_VAR
    assert "file_path" not in gs                    # file contents are not variable-resolved by bundles


# ----------------------------------------------------------------------------- drift script
def _plan(etag_old, etag_remote, action="update", live=None):
    entry = {"action": action, "changes": {"etag": {"old": etag_old, "remote": etag_remote}},
             "remote_state": {"serialized_space": json.dumps(live or T.render(SPACE, "freshcart"))}}
    return {"plan": {"resources.genie_spaces.freshcart_assistant": entry,
                     "resources.genie_spaces.freshcart_assistant.permissions": {"action": "skip"}}}


def _run_drift(plan, *extra):
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp, "plan.json")
        p.write_text(json.dumps(plan))
        code = check_drift.main([str(p), "-t", "prod", "--backup-dir", tmp, *extra])
        return code, [T.load_space(f) for f in Path(tmp).glob("*.yml")]


def test_drift_gate():
    assert _run_drift(_plan("3", "3", "skip"))[0] == 0                     # untouched since the last deploy
    edited = T.render(copy.deepcopy(SPACE), "freshcart")
    edited["config"]["sample_questions"][0]["question"] = ["edited in the UI"]
    code, backups = _run_drift(_plan("3", "4", live=edited))
    assert code == 2 and len(backups) == 1                                 # refused, and the live state saved
    assert ["edited in the UI"] in [q["question"] for q in backups[0]["config"]["sample_questions"]]
    assert _run_drift(_plan("3", "4", live=edited), "--allow-drift")[0] == 0
    assert _run_drift(_plan("3", "3", "recreate"))[0] == 3                 # would lose conversations
    assert _run_drift(_plan(None, None, "create"))[0] == 0                 # first deployment


# ------------------------------------------------------------------------------ gate script
def _sdk_mocks(assessments, sql="SELECT 1"):
    from unittest import mock
    from databricks.sdk.service import dashboards as D
    w = mock.Mock()
    w.genie = mock.create_autospec(D.GenieAPI, instance=True)
    graded = [a for a in assessments if a != "NEEDS_REVIEW"]
    run = D.GenieEvalRunResponse(eval_run_id="r", eval_run_status=D.EvaluationStatusType.DONE,
                                 num_questions=len(assessments), num_correct=graded.count("GOOD"),
                                 num_needs_review=len(assessments) - len(graded))
    w.genie.genie_create_eval_run.return_value = run
    w.genie.genie_get_eval_run.return_value = run
    w.genie.genie_list_eval_results.return_value = D.GenieListEvalResultsResponse(eval_results=[
        D.GenieEvalResult(result_id=str(i), space_id="s", benchmark_question_id=str(i), question=f"q{i}")
        for i in range(len(assessments))])
    w.genie.genie_get_eval_result_details.side_effect = lambda s, r, rid: D.GenieEvalResultDetails(
        result_id=rid, space_id=s, benchmark_question_id=rid, assessment=D.GenieEvalAssessment(assessments[int(rid)]))
    w.genie.start_conversation_and_wait.return_value = D.GenieMessage(
        space_id="s", conversation_id="c", content="q", message_id="m", status=D.MessageStatus.COMPLETED,
        attachments=[D.GenieAttachment(query=D.GenieQueryAttachment(query=sql))] if sql else [])
    return w


def test_gate_script_pass_fail_and_smoke():
    try:
        import genie_quality_gate as G
    except ImportError:
        print("      (databricks-sdk not installed: gate script tests skipped)")
        return
    args = ["--space-id", "s", "--min-graded", "2"]
    assert G.main(args, client=_sdk_mocks(["GOOD", "GOOD", "NEEDS_REVIEW"])) == 0
    assert G.main(args, client=_sdk_mocks(["GOOD", "BAD", "NEEDS_REVIEW"])) == 1
    assert G.main(args, client=_sdk_mocks(["GOOD", "NEEDS_REVIEW", "NEEDS_REVIEW"])) == 1   # too few graded
    assert G.main(["--space-id", "s", "--mode", "smoke"], client=_sdk_mocks([], sql="SELECT 1")) == 0
    assert G.main(["--space-id", "s", "--mode", "smoke"], client=_sdk_mocks([], sql=None)) == 1


# ----------------------------------------------------------------- real CLI, fake workspace
def _cli() -> str | None:
    cli = os.environ.get("DATABRICKS_CLI") or shutil.which("databricks")
    if not cli:
        return None
    out = subprocess.run([cli, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"v(\d+)\.(\d+)", out)
    return cli if m and (int(m.group(1)), int(m.group(2))) >= (1, 14) else None


def test_bundle_with_the_real_databricks_cli():
    cli = _cli()
    if cli is None:
        print("      (Databricks CLI 1.14+ not found: bundle integration test skipped)")
        return
    from fake_workspace import FakeWorkspace, FreshCartEvaluator
    ws = FakeWorkspace.start(evaluator=FreshCartEvaluator())
    tmp = Path(tempfile.mkdtemp(prefix="genie_bundle_test_"))
    bundle = tmp / "genie_bundle"
    shutil.copytree(BUNDLE, bundle, ignore=shutil.ignore_patterns(".databricks", "__pycache__"))
    env = {**os.environ, "DATABRICKS_HOST": ws.host, "DATABRICKS_TOKEN": "test", "DATABRICKS_CLI": cli}
    env.pop("DATABRICKS_CONFIG_PROFILE", None)

    def run(*args, ok=True):
        p = subprocess.run(list(args), cwd=bundle, env=env, capture_output=True, text=True)
        assert (p.returncode == 0) == ok, f"{args}\n{p.stdout}\n{p.stderr}"
        return p.stdout

    try:
        for target, catalog in T.TARGET_CATALOGS.items():
            cfg = json.loads(run(cli, "bundle", "validate", "-t", target, "-o", "json"))
            space = json.loads(cfg["resources"]["genie_spaces"][T.SPACE_KEY]["serialized_space"])
            assert {t["identifier"].split(".")[0] for t in space["data_sources"]["tables"]} == {catalog}, target

        run(cli, "bundle", "deploy", "-t", "qa")
        assert "0 to add, 0 to change, 0 to delete" in run(cli, "bundle", "plan", "-t", "qa")
        space_id = json.loads(run(cli, "bundle", "summary", "-t", "qa", "-o", "json"))["resources"]["genie_spaces"][T.SPACE_KEY]["id"]

        gate = [sys.executable, "src/genie_quality_gate.py", "--space-id", space_id, "--min-graded", "4"]
        run(*gate)                                                       # the correct space passes
        ws.ui_edit(space_id, _break_example)                             # someone breaks it in the UI
        plan = run(cli, "bundle", "plan", "-t", "qa", "-o", "json")
        Path(tmp, "plan.json").write_text(plan)
        p = subprocess.run([sys.executable, "scripts/check_drift.py", str(tmp / "plan.json"), "-t", "qa"],
                           cwd=bundle, env=env, capture_output=True, text=True)
        assert p.returncode == 2 and "gross_sales" in p.stdout, p.stdout
        assert "GATE FAILED" in run(*gate, ok=False)                     # and the gate catches it

        run(sys.executable, "scripts/sync_from_workspace.py", "-t", "qa")  # adopt the edit into "git"
        synced = T.load_space(bundle / "resources" / "freshcart_assistant.space.yml")
        assert any("gross_sales" in line for e in synced["instructions"]["example_question_sqls"] for line in e["sql"])
        assert T.validate(synced)[0] == []                               # catalog written as ${var.catalog}
    finally:
        ws.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def test_benchmark_notes_refer_to_existing_benchmarks():
    notes = yaml.safe_load((BUNDLE / "benchmark_notes.yml").read_text())["notes"]
    ids = {b["id"] for b in SPACE["benchmarks"]["questions"]}
    assert set(notes) <= ids, f"notes for benchmarks that no longer exist: {set(notes) - ids}"


def test_version_1_column_fields_are_rejected():
    """Databricks refuses get_example_values / build_value_dictionary in a version 2 space (400 on create)."""
    bad = json.loads(json.dumps(SPACE))
    bad["data_sources"]["tables"][0]["column_configs"][0]["get_example_values"] = True
    errors = T.validate(bad)[0]
    assert any("'get_example_values' is a version 1 field; version 2 uses 'enable_format_assistance'" in e
               for e in errors), errors


def test_gate_prints_genie_sql_for_wrong_answers():
    import contextlib
    import io
    from types import SimpleNamespace as NS
    sys.path.insert(0, str(BUNDLE / "src"))
    import genie_quality_gate as G
    run = NS(eval_run_id="r1", eval_run_status="DONE", num_questions=2, num_correct=1, num_needs_review=0)
    details = {"a": NS(assessment="GOOD", assessment_reasons=[], actual_response=None, expected_response=None),
               "b": NS(assessment="BAD", assessment_reasons=["RESULT_EXTRA_ROWS"],
                       actual_response=NS(response="SELECT snapshot_date, MEASURE(on_hand_units)\nFROM inventory_metrics"),
                       expected_response=NS(response="SELECT MEASURE(on_hand_units) FROM inventory_metrics"))}
    genie = NS(genie_create_eval_run=lambda space: run, genie_get_eval_run=lambda space, rid: run,
               genie_list_eval_results=lambda space, rid, page_size, page_token: NS(
                   eval_results=[NS(result_id="a", question="q ok"), NS(result_id="b", question="q stock")],
                   next_page_token=None),
               genie_get_eval_result_details=lambda space, rid, res: details[res])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        res = G.gate(NS(genie=genie), "s", min_accuracy=0.95, max_bad=0, min_graded=1)
    assert not res["passed"]
    assert "BAD  q stock" in out.getvalue() and "Genie's SQL:" in out.getvalue()
    assert "         SELECT snapshot_date, MEASURE(on_hand_units)" in out.getvalue()
