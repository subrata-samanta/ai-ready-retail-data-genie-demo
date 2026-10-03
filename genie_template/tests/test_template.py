"""Tests of the project: they need no workspace, and they hold for any project made from the template (they
check your genie.config.yml and space/genie_space.yml, not the example's content).

    python tests/run_tests.py        (or: python -m pytest tests)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
import genie_tools as T                                        # noqa: E402


# ------------------------------------------------------------------------------------ configuration
def test_config_defines_every_environment():
    assert re.fullmatch(r"[a-z][a-z0-9-]*", T.project_name()), "bundle.name: lowercase letters, digits and dashes"
    assert set(T.ENVIRONMENTS) <= set(T.targets()), "genie.config.yml needs the targets dev, qa and prod"
    places = {}
    for env in T.ENVIRONMENTS:
        s = T.settings(env)
        assert s.get("catalog"), f"{env}: set variables.catalog in genie.config.yml"
        assert s.get("schema"), f"{env}: set the schema variable"
        places[env] = (s["catalog"], s["schema"])
    assert len(set(places.values())) == 3, f"dev, qa and prod must use different data: {places}"
    titles = [T.space_title(e) for e in T.ENVIRONMENTS]
    assert len(set(titles)) == 3, f"each environment needs its own title (title_suffix): {titles}"


def test_every_variable_used_is_defined():
    defined = set(T.load_config().get("variables", {})) | {T.SPACE_VAR}
    used = set()
    for f in [ROOT / "databricks.yml", *sorted((ROOT / "resources").glob("*.yml"))]:
        used |= set(re.findall(r"\$\{var\.(\w+)\}", f.read_text(encoding="utf-8")))
    assert used <= defined, f"used in the bundle but not defined in genie.config.yml: {sorted(used - defined)}"


def test_gate_thresholds_are_sane():
    s = T.settings("qa")
    assert 0 < float(s["gate_min_accuracy"]) <= 1, "gate_min_accuracy is a share: 0.60 means 60%"
    assert int(s["gate_min_graded"]) >= 1
    assert s["smoke_question"].strip(), "set smoke_question"


# ---------------------------------------------------------------------------------- the space file
def test_space_definition_is_valid_and_canonical():
    space = T.load_space()
    errors, _ = T.validate(space)
    assert not errors, errors
    assert T.is_canonical(), "run: python scripts/validate_space.py --fix"


def test_render_and_neutralise_round_trip_for_every_environment():
    space = T.load_space()
    for env in T.targets():
        s = T.settings(env)
        rendered = T.for_target(space, env)
        blob = json.dumps(rendered)
        assert T.CATALOG_REF not in blob and T.SCHEMA_REF not in blob, env
        assert T.from_target(rendered, env) == T.normalise(space), f"{env}: export -> git is not the reverse"
        assert all(i.startswith(s["catalog"] + ".") for i in T.data_sources(rendered)
                   if i.count(".") == 2 and not i.startswith("samples.")), env


def test_hard_coded_catalog_is_rejected():
    space = T.copy_space(T.load_space())
    cat = T.settings("prod")["catalog"]
    space.setdefault("instructions", {}).setdefault("example_question_sqls", []).append(
        {"id": T.new_id("bad"), "question": ["bad"], "sql": [f"SELECT 1 FROM {cat}.some_schema.some_table"]})
    assert any("hard-coded catalog" in e for e in T.validate(space)[0])


def test_diff_names_what_changed():
    old = T.load_space()
    new = T.copy_space(old)
    new.setdefault("config", {}).setdefault("sample_questions", []).append({"id": T.new_id("q"), "question": ["Q?"]})
    changes = T.diff(old, new)
    assert changes == ["+ added sample question: Q?"], changes


# ---------------------------------------------------------------------------------- drift gate
def _plan(etag_old, etag_remote, action="update", live=None):
    live = live or T.for_target(T.load_space(), "prod")
    return {"plan": {f"resources.genie_spaces.{T.SPACE_RESOURCE}": {
        "action": action, "changes": {"etag": {"old": etag_old, "remote": etag_remote}},
        "remote_state": {"serialized_space": json.dumps(live)}}}}


def _drift(plan, *extra) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "plan.json"
        p.write_text(json.dumps(plan))
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_drift.py"), str(p), "-t", "prod",
                            "--backup-dir", str(Path(tmp) / "backup"), *extra], capture_output=True, text=True)
        backups = list((Path(tmp) / "backup").glob("*.yml"))
        assert backups, "the live space is always backed up"
        return r.returncode, r.stdout


def test_drift_gate():
    assert _drift(_plan("3", "3"))[0] == 0
    code, out = _drift(_plan("3", "5"))
    assert code == 2 and "DRIFT" in out
    assert _drift(_plan("3", "5"), "--allow-drift")[0] == 0
    assert _drift(_plan("3", "3", action="recreate"))[0] == 3
    assert _drift(_plan("3", "3", action="recreate"), "--allow-destroy")[0] == 0


# ------------------------------------------------------------------------------- quality job
class _GenieMock:
    def __init__(self, assessments, sql="SELECT 1", space=None):
        self.assessments, self.sql, self.space = assessments, sql, space or T.for_target(T.load_space(), "qa")

    def get_space(self, space_id, include_serialized_space=False):
        return SimpleNamespace(serialized_space=json.dumps(self.space))

    def genie_create_eval_run(self, space_id):
        good = sum(a == "GOOD" for a in self.assessments)
        review = sum(a == "NEEDS_REVIEW" for a in self.assessments)
        return SimpleNamespace(eval_run_id="e1", eval_run_status="DONE", num_questions=len(self.assessments),
                               num_correct=good, num_needs_review=review)

    def genie_list_eval_results(self, space_id, run_id, page_size=100, page_token=None):
        return SimpleNamespace(eval_results=[SimpleNamespace(result_id=str(i), question=f"q{i}")
                                             for i in range(len(self.assessments))], next_page_token=None)

    def genie_get_eval_result_details(self, space_id, run_id, result_id):
        return SimpleNamespace(assessment=self.assessments[int(result_id)], assessment_reasons=[],
                               actual_response=SimpleNamespace(response="SELECT 2"),
                               expected_response=SimpleNamespace(response="SELECT 1"))

    def start_conversation_and_wait(self, space_id, question, timeout=None):
        att = [SimpleNamespace(query=SimpleNamespace(query=self.sql))] if self.sql else []
        return SimpleNamespace(status="COMPLETED", attachments=att)


class _SparkMock:
    """Answers the health-check queries; fails for tables in `missing`; records what is written."""

    def __init__(self, missing=()):
        self.missing, self.statements, self.written = set(missing), [], {}

    def sql(self, statement):
        self.statements.append(statement)
        if any(m in statement.replace("`", "") for m in self.missing):
            raise RuntimeError("[TABLE_OR_VIEW_NOT_FOUND] not found")
        return SimpleNamespace(count=lambda: 1, collect=lambda: [])

    def createDataFrame(self, rows, ddl):
        spark = self

        class _W:
            def mode(self, m):
                return self

            def saveAsTable(self, name):
                spark.written.setdefault(name.rsplit(".", 1)[1], []).extend(rows)
        return SimpleNamespace(write=_W())


def _quality(assessments, mode="gate", spark=None, sql="SELECT 1", **kw):
    import genie_quality as Q
    argv = ["--space-id", "s1", "--mode", mode, "--min-accuracy", str(kw.get("min_accuracy", 0.6)),
            "--min-graded", "1", "--smoke-question", "How many?", "--record-to", "cat.mon", "--target", "qa"]
    return Q.main(argv, client=SimpleNamespace(genie=_GenieMock(assessments, sql)), spark=spark or _SparkMock())


def test_quality_gate_passes_and_fails_on_accuracy():
    assert _quality(["GOOD", "GOOD", "BAD", "NEEDS_REVIEW"]) == 0          # 2 of 3 graded = 67% >= 60%
    assert _quality(["GOOD", "BAD", "BAD"]) == 1                            # 33%
    assert _quality([], ) == 1                                              # nothing graded


def test_quality_gate_fails_when_a_table_is_unreadable_and_records_everything():
    first = next(iter(T.data_sources(T.for_target(T.load_space(), "qa"))))
    spark = _SparkMock(missing=[first])
    assert _quality(["GOOD", "GOOD"], spark=spark) == 1
    assert {"genie_quality_runs", "genie_quality_results", "genie_table_health"} <= set(spark.written)
    assert any("CREATE TABLE IF NOT EXISTS `cat`.`mon`.`genie_quality_runs`" in s for s in spark.statements)


def test_smoke_test_needs_sql():
    assert _quality([], mode="smoke") == 0
    assert _quality([], mode="smoke", sql=None) == 1


# ------------------------------------------------------------------------------- wiring
def test_workflows_refer_to_files_that_exist():
    for wf in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        text = wf.read_text(encoding="utf-8")
        for path in re.findall(r"python (scripts/\w+\.py)", text) + re.findall(r"(space/genie_space\.yml)", text):
            assert (ROOT / path).exists(), f"{wf.name} refers to {path}, which does not exist"
        for job in re.findall(r"bundle run (\w+)", text):
            assert any(f"    {job}:" in f.read_text() for f in (ROOT / "resources").glob("*.yml")), (wf.name, job)


def test_notebooks_are_up_to_date():
    import build_notebooks
    assert build_notebooks.main(["--check"]) == 0, "run: python scripts/build_notebooks.py"
