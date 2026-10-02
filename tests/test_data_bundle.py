"""Tests for the data bundle in databricks/ (pipeline, refresh job, SQL runner).

The SQL itself runs only on Databricks; these tests check what can be checked offline: every file splits into
statements, every placeholder has a value, no catalog name is hard-coded, the job's files exist, the bundle's
environments use the same catalogs as the Genie bundle, and (with sqlglot installed) every job statement parses
as Databricks SQL and every MERGE ... UPDATE SET * selects exactly the target table's columns.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "databricks"
sys.path.insert(0, str(DATA / "src"))
import run_sql as R                                                # noqa: E402

PARAMS = {"catalog": "freshcart_qa", "as_of_date": "2026-09-27"}
JOB = yaml.safe_load((DATA / "resources" / "freshcart_refresh.yml").read_text())["resources"]["jobs"]["freshcart_refresh"]
PIPELINE = yaml.safe_load((DATA / "resources" / "freshcart_pipeline.yml").read_text())["resources"]["pipelines"]["freshcart_pipeline"]


def job_sql_files() -> list[Path]:
    files = []
    for task in JOB["tasks"]:
        for arg in (task.get("spark_python_task") or {}).get("parameters", []):
            if arg.startswith("${workspace.file_path}/"):
                files.append(DATA / arg[len("${workspace.file_path}/"):])
    return files


def pipeline_sql_files() -> list[Path]:
    return [(DATA / "resources" / lib["file"]["path"]).resolve() for lib in PIPELINE["libraries"]]


def test_every_sql_file_belongs_to_the_job_or_the_pipeline():
    used = {p.resolve() for p in job_sql_files()} | set(pipeline_sql_files())
    on_disk = {p.resolve() for p in DATA.rglob("*.sql")}
    assert used == on_disk, f"not run by the bundle: {on_disk - used}; missing: {used - on_disk}"


def test_job_tasks_point_at_existing_files():
    for task in JOB["tasks"]:
        py = task.get("spark_python_task")
        if py:
            assert (DATA / "resources" / py["python_file"]).resolve().exists(), py["python_file"]
    assert all(p.exists() for p in job_sql_files())


def test_placeholders_all_have_values():
    for path in job_sql_files():
        for statement in R.split_statements(path.read_text()):
            assert "${" not in R.render(statement, PARAMS), path
    config = set(PIPELINE["configuration"])
    for path in pipeline_sql_files():
        used = set(R.PLACEHOLDER.findall(path.read_text()))
        assert used <= config, f"{path.name} uses {used - config}, which the pipeline does not set"


def test_no_hard_coded_catalog():
    pattern = re.compile(r"\bfreshcart(_dev|_qa)?\.(landing|bronze|silver|gold|semantic|governance)\b|/Volumes/freshcart")
    for path in DATA.rglob("*.sql"):
        hits = [line for line in path.read_text().splitlines() if pattern.search(line)]
        assert not hits, f"{path.relative_to(ROOT)} names a catalog: {hits[:3]}"


def test_environments_match_the_genie_bundle():
    data = yaml.safe_load((DATA / "databricks.yml").read_text())["targets"]
    genie = yaml.safe_load((ROOT / "genie_bundle" / "databricks.yml").read_text())["targets"]
    for target, cfg in data.items():
        assert cfg["variables"]["catalog"] == genie[target]["variables"]["catalog"], target


def test_splitter_handles_strings_comments_and_dollar_blocks():
    sql = """-- a comment; with a semicolon
CREATE VIEW v AS $$ a: 'x;y' $$;
SELECT 'it''s; fine', "q;" /* c; */ FROM t;  -- trailing
-- only a comment;
"""
    assert R.split_statements(sql) == ["CREATE VIEW v AS $$ a: 'x;y' $$",
                                       "SELECT 'it''s; fine', \"q;\" /* c; */ FROM t"]
    try:
        R.render("SELECT ${missing}", PARAMS)
        raise AssertionError("an unknown placeholder must be refused")
    except ValueError:
        pass


def test_runner_runs_every_statement_in_order():
    class Spark:
        statements = []

        def sql(self, s):
            self.statements.append(s)

    spark = Spark()
    files = [str(DATA / "00_setup.sql"), str(DATA / "governance" / "00_functions.sql")]
    assert R.main(["--catalog", "freshcart_qa", "--as-of-date", "2026-09-27", *files], spark=spark) == 0
    assert len(spark.statements) == 9 and spark.statements[0].startswith("CREATE SCHEMA IF NOT EXISTS freshcart_qa.")


def test_job_sql_parses_and_merges_match_the_gold_tables():
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        print("      (sqlglot not installed: Databricks SQL parse check skipped)")
        return
    columns = {}
    for path in job_sql_files():
        for statement in R.split_statements(path.read_text()):
            if "WITH METRICS" in statement:                     # metric view YAML: generated from semantic/*.yaml,
                continue                                        # which the local engine and its tests read
            tree = sqlglot.parse_one(R.render(statement, PARAMS), read="databricks")
            assert tree is not None, statement[:80]
            if isinstance(tree, exp.Create) and tree.kind == "TABLE":
                columns[tree.this.this.name] = [c.name for c in tree.this.expressions if isinstance(c, exp.ColumnDef)]
            if isinstance(tree, exp.Merge) and "UPDATE SET *" in statement:
                source = tree.args["using"].this
                while isinstance(source, exp.Union):
                    source = source.this
                names = [s.alias_or_name for s in source.selects]
                assert names == columns[tree.this.name], (tree.this.name, names, columns[tree.this.name])
    assert len(columns) == 7


def test_job_scripts_do_not_exit_on_success():
    """On Databricks a Python script task runs inside IPython: sys.exit(0) raises SystemExit and fails the task."""
    scripts = []
    for bundle in (DATA, ROOT / "genie_bundle"):
        for path in (bundle / "resources").glob("*.yml"):
            for job in (yaml.safe_load(path.read_text()).get("resources", {}).get("jobs") or {}).values():
                for task in job["tasks"]:
                    if task.get("spark_python_task"):
                        scripts.append((bundle / "resources" / task["spark_python_task"]["python_file"]).resolve())
    assert len(set(scripts)) == 2, scripts
    for script in set(scripts):
        code = script.read_text()
        assert "sys.exit(main())" not in code and "sys.exit(0)" not in code, f"{script.name} exits on success"


def test_grant_to_a_non_account_group_is_skipped_with_a_warning():
    class Spark:
        def __init__(self):
            self.statements = []

        def sql(self, s):
            self.statements.append(s)
            if s.startswith("GRANT"):
                raise RuntimeError("[ErrorClass=PRINCIPAL_DOES_NOT_EXIST.PRINCIPAL_DOES_NOT_EXIST] Could not find principal")

    out = []
    n = R.run_files(Spark(), [DATA / "governance" / "01_security.sql"], "freshcart_dev", echo=out.append)
    assert n == 2 and any("freshcart-business-users is not an account group" in line for line in out)
    assert out[-1].startswith("WARNING: 5 grant(s) skipped for freshcart-business-users")

    class Broken(Spark):
        def sql(self, s):
            raise RuntimeError("[TABLE_OR_VIEW_NOT_FOUND] gold.fct_sales_line")

    try:
        R.run_files(Broken(), [DATA / "governance" / "01_security.sql"], "freshcart_dev", echo=out.append)
        raise AssertionError("any other error must stop the run")
    except RuntimeError:
        pass


def test_sql_functions_keep_parameters_out_of_aggregates():
    """Inside a SQL function a parameter is an outer reference; Databricks rejects an aggregate that mixes it
    with table columns (AGGREGATE_FUNCTION_MIXED_OUTER_LOCAL_REFERENCES). Use parameters only in WHERE filters."""
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        print("      (sqlglot not installed: check skipped)")
        return
    checked = 0
    for path in job_sql_files():
        for statement in R.split_statements(path.read_text()):
            m = re.match(r"CREATE OR REPLACE FUNCTION\s+\S+?\((.*?)\)\s*RETURNS TABLE", statement, re.S)
            if not m:
                continue
            params = set(re.findall(r"^\s*(\w+)\s+\w+", m.group(1), re.M))
            body = R.render(statement.split("\nRETURN\n", 1)[1], PARAMS)
            for agg in sqlglot.parse_one(body, read="databricks").find_all(exp.AggFunc):
                used = {c.name for c in agg.find_all(exp.Column)} & params
                assert not used, f"{path.name}: aggregate {agg.sql()[:80]} uses parameter(s) {used}"
            checked += 1
    assert checked == 1, "expected the like-for-like function"
