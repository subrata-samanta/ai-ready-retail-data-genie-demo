"""Tests for Genie space version control, promotion and rollback (genie_cicd).

Everything runs against SimulatedWorkspace, so no Databricks workspace is needed. The adapter for
the real Databricks SDK is checked against the SDK's own method signatures when it is installed.
"""
from __future__ import annotations

import copy
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from freshcart import config as FC, pipeline                                  # noqa: E402
from genie_cicd import bootstrap, ops, spec as S                              # noqa: E402
from genie_cicd.history import HistoryStore                                   # noqa: E402
from genie_cicd.workspace import ConflictError, SimulatedWorkspace            # noqa: E402

CONFIG = ops.load_config()
SPACE = S.load(CONFIG.space_file)


def _warehouse():
    if not (FC.WAREHOUSE_DIR / "gold.db").exists():
        pipeline.run(full_refresh=True, verbose=False)


class _Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.t += timedelta(minutes=1)
        return self.t


def _world():
    """Three simulated workspaces (dev, qa, prod), each with its own history."""
    _warehouse()
    root = Path(tempfile.mkdtemp(prefix="genie_cicd_test_"))
    clock = _Clock()
    out = {}
    for name in ("dev", "qa", "prod"):
        env = CONFIG.env(name)
        ws = SimulatedWorkspace(root / name, env.catalog)
        out[name] = (env, ws, HistoryStore(ws, env.history_root, clock=clock))
    return out


def _quiet(*_a, **_k):
    pass


def _break_example(space):
    """The classic mistake: the trusted 'net sales by region' example now sums gross sales."""
    for e in space["instructions"]["example_question_sqls"]:
        if "net sales and margin by region" in S.sql_text(e["question"]):
            e["sql"] = [line.replace("MEASURE(net_sales)", "MEASURE(gross_sales)") for line in e["sql"]]


# ------------------------------------------------------------------------------------- spec
def test_space_file_is_canonical_and_valid():
    text = CONFIG.space_file.read_text(encoding="utf-8")
    assert S.dumps(SPACE) == text, "space file is not in canonical form"
    errors, warnings = S.validate(SPACE, forbidden_catalogs=CONFIG.catalogs)
    assert not errors, errors
    assert not warnings, warnings


def test_bootstrap_is_deterministic():
    assert S.content_hash(bootstrap.build()) == S.content_hash(bootstrap.build())


def test_render_and_neutralise_round_trip():
    for env in CONFIG.envs.values():
        rendered = S.render(SPACE, env.catalog)
        assert S.TOKEN not in S.dumps(rendered)
        assert S.content_hash(S.neutralise(rendered, env.catalog)) == S.content_hash(SPACE)


def test_hard_coded_catalog_is_rejected():
    bad = copy.deepcopy(SPACE)
    bad["data_sources"]["tables"][0]["identifier"] = "freshcart_dev.gold.dim_store"
    errors, _ = S.validate(bad, forbidden_catalogs=CONFIG.catalogs)
    assert any("freshcart_dev" in e for e in errors), errors


def test_diff_names_the_changed_example_and_line():
    bad = copy.deepcopy(SPACE)
    _break_example(bad)
    changes = S.diff(SPACE, bad)
    assert changes[0].startswith("~ changed example SQL: What were net sales and margin by region")
    assert any("gross_sales" in c for c in changes)


def test_every_sql_runs_on_the_local_warehouse():
    _warehouse()
    assert ops.check_sql_locally(SPACE) == []


# ------------------------------------------------------------------------------- lifecycle
def test_full_lifecycle_dev_qa_prod_with_gate_drift_and_rollback():
    w = _world()
    (dev, dws, dh), (qa, qws, qh), (prod, pws, ph) = w["dev"], w["qa"], w["prod"]

    # v1 to dev; a UI edit in dev is captured by a snapshot
    r1 = ops.deploy(dws, dev, SPACE, dh, config=CONFIG, version="v1", log=_quiet)
    assert ops.snapshot(dws, dev, dh) is None                              # nothing changed yet
    sid = r1["space_id"]
    dws.ui_edit(sid, lambda s: s["config"]["sample_questions"].append(
        {"id": "0" * 31 + "1", "question": ["Which stores opened in FY2025?"]}))
    snap = ops.snapshot(dws, dev, dh)
    assert snap and snap["changes"] == ["+ added sample question: Which stores opened in FY2025?"]

    # promote dev -> qa (dev has no gate) and pass the qa gate
    ops.promote(dws, dev, dh, qws, qa, qh, config=CONFIG, log=_quiet)
    assert ops.gate(qws, qa, qh, log=_quiet)["passed"]
    ops.promote(qws, qa, qh, pws, prod, ph, config=CONFIG, log=_quiet)
    assert ops.smoke(pws, prod, log=_quiet)["passed"]
    good_hash = ph.latest("prod", ("deployed",))["content_hash"]

    # a mistake made in dev reaches qa and is stopped by the gate; prod promotion is refused
    v2 = copy.deepcopy(SPACE)
    _break_example(v2)
    ops.deploy(dws, dev, v2, dh, config=CONFIG, version="v2", log=_quiet)
    ops.promote(dws, dev, dh, qws, qa, qh, config=CONFIG, log=_quiet)
    res = ops.gate(qws, qa, qh, log=_quiet)
    assert not res["passed"] and res["num_bad"] == 1
    try:
        ops.promote(qws, qa, qh, pws, prod, ph, config=CONFIG, log=_quiet)
        raise AssertionError("promotion should have been refused")
    except ops.GateFailed:
        pass

    # someone edits prod in the UI: the next deploy refuses (drift), the edit is in the backup
    prod_id = ops.find_space(pws, prod)["space_id"]
    pws.ui_edit(prod_id, lambda s: s["instructions"]["text_instructions"][0]["content"].append("\nAlways answer in French."))
    try:
        ops.deploy(pws, prod, SPACE, ph, config=CONFIG, version="v1b", log=_quiet)
        raise AssertionError("drift should have been detected")
    except ops.DriftError as e:
        assert any("French" in c for c in e.changes)
    assert "French" in S.dumps(ops.live_neutral(pws, prod)[1])               # refused: prod untouched

    # a bad version forced into prod is rolled back with one call
    ops.deploy(pws, prod, v2, ph, config=CONFIG, version="v2", allow_drift=True, log=_quiet)
    assert "French" in S.dumps(ph.latest("prod", ("backup",))["space"])      # the overwritten edit is kept
    ops.rollback(pws, prod, ph, to="previous", config=CONFIG, log=_quiet)
    _, live = ops.live_neutral(pws, prod)
    assert S.content_hash(live) == good_hash
    assert prod_id == ops.find_space(pws, prod)["space_id"]                   # same space: conversations are kept


def test_dependencies_must_exist_in_the_target_catalog():
    w = _world()
    qa = w["qa"][0]
    ws = SimulatedWorkspace(Path(tempfile.mkdtemp()), qa.catalog, missing_objects={"freshcart_qa.gold.dim_product"})
    try:
        ops.deploy(ws, qa, SPACE, HistoryStore(ws, qa.history_root), config=CONFIG, log=_quiet)
        raise AssertionError("missing table should block the deploy")
    except ops.DeployError as e:
        assert "freshcart_qa.gold.dim_product" in str(e)


def test_stale_etag_is_rejected():
    w = _world()
    dev, ws, h = w["dev"]
    rec = ops.deploy(ws, dev, SPACE, h, config=CONFIG, log=_quiet)
    old = ws.get_space(rec["space_id"])["etag"]
    ws.ui_edit(rec["space_id"], lambda s: None)
    try:
        ws.update_space(rec["space_id"], title="x", description="", warehouse_id="", serialized_space=SPACE, etag=old)
        raise AssertionError("stale etag accepted")
    except ConflictError:
        pass


def test_rollback_previous_walks_back_through_releases():
    w = _world()
    env, ws, h = w["qa"]
    versions = []
    for i in range(3):
        sp = copy.deepcopy(SPACE)
        sp["config"]["sample_questions"][0]["question"] = [f"Question version {i}"]
        ops.deploy(ws, env, sp, h, config=CONFIG, version=f"v{i}", allow_drift=True, log=_quiet)
        versions.append(S.content_hash(sp))
    for expected in (versions[1], versions[0]):
        ops.rollback(ws, env, h, to="previous", config=CONFIG, log=_quiet)
        assert S.content_hash(ops.live_neutral(ws, env)[1]) == expected


# ---------------------------------------------------------------------------- real adapter
def test_databricks_adapter_matches_sdk_signatures():
    try:
        from unittest import mock

        from databricks.sdk.service import catalog, dashboards, files
    except ImportError:
        print("      (databricks-sdk not installed: adapter signature check skipped)")
        return
    from genie_cicd.workspace import DatabricksWorkspace

    w = mock.Mock()
    w.genie = mock.create_autospec(dashboards.GenieAPI, instance=True)
    w.tables = mock.create_autospec(catalog.TablesAPI, instance=True)
    w.functions = mock.create_autospec(catalog.FunctionsAPI, instance=True)
    w.files = mock.create_autospec(files.FilesAPI, instance=True)
    space = dashboards.GenieSpace(space_id="abc", title="t", etag="1", serialized_space=S.to_wire(SPACE))
    w.genie.get_space.return_value = space
    w.genie.create_space.return_value = space
    w.genie.list_spaces.return_value = dashboards.GenieListSpacesResponse(spaces=[space])
    ws = DatabricksWorkspace(client=w)

    assert ws.list_spaces()[0]["space_id"] == "abc"
    assert ws.get_space("abc")["serialized_space"]["version"] == 2
    ws.create_space(title="t", description="d", warehouse_id="wh", parent_path="/p", serialized_space=SPACE)
    ws.update_space("abc", title="t", description="d", warehouse_id="wh", serialized_space=SPACE, etag="1")
    w.genie.update_space.assert_called_once()
    assert w.genie.update_space.call_args.kwargs["etag"] == "1"
    w.tables.exists.return_value = catalog.TableExistsResponse(table_exists=True)
    assert ws.object_exists("freshcart_qa.gold.dim_store", "table")
    run = dashboards.GenieEvalRunResponse(eval_run_id="r1", eval_run_status=dashboards.EvaluationStatusType.DONE,
                                          num_questions=1, num_correct=1, num_needs_review=0)
    w.genie.genie_create_eval_run.return_value = run
    w.genie.genie_get_eval_run.return_value = run
    w.genie.genie_list_eval_results.return_value = dashboards.GenieListEvalResultsResponse(eval_results=[
        dashboards.GenieEvalResult(result_id="x", space_id="abc", benchmark_question_id="q", question="Q")])
    w.genie.genie_get_eval_result_details.return_value = dashboards.GenieEvalResultDetails(
        result_id="x", space_id="abc", benchmark_question_id="q", assessment=dashboards.GenieEvalAssessment.GOOD)
    res = ws.run_benchmarks("abc")
    assert res["num_correct"] == 1 and res["results"][0]["assessment"] == "GOOD"
