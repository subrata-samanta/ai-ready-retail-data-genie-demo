"""Quality job of the Genie space: the task of the bundle job `genie_quality`.

    mode=gate   1. health: every table of the space is readable and not empty (and, with max_data_age_hours > 0,
                   was changed within that many hours)
                2. benchmarks: Genie's benchmark evaluation; fail unless
                   - accuracy on the automatically graded benchmarks >= min_accuracy,
                   - at least min_graded benchmarks could be graded, and
                   - at most max_bad answers are wrong (-1: no separate limit)
    mode=smoke  ask smoke_question; fail unless Genie answers with SQL

Every result is appended to <record_to>.genie_quality_runs (one row per run), genie_quality_results (one row
per benchmark) and genie_table_health (one row per table check), with the target and the deployed git commit,
so quality can be followed over time and across releases. A failure exits non-zero: the job run fails, its
failure e-mail goes out, and `databricks bundle run` fails the release pipeline.

Runs as the job's identity inside Databricks; locally it uses the standard Databricks environment variables.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone


def _value(x):
    return getattr(x, "value", x)


def _response(r) -> str:
    text = getattr(r, "response", None)
    return text if isinstance(text, str) else ""


def _spark():
    try:
        from pyspark.sql import SparkSession
        return SparkSession.builder.getOrCreate()
    except Exception:                                    # noqa: BLE001  (no Spark: run locally)
        return None


# ------------------------------------------------------------------------------------------- health
def space_tables(w, space_id: str) -> list[tuple[str, str]]:
    """(identifier, kind) of the deployed space's data sources, read from the workspace."""
    sp = w.genie.get_space(space_id, include_serialized_space=True)
    content = json.loads(sp.serialized_space or "{}")
    ds = content.get("data_sources", {})
    return ([(t["identifier"], "table") for t in ds.get("tables", [])] +
            [(m["identifier"], "metric view") for m in ds.get("metric_views", [])])


def health(spark, tables: list[tuple[str, str]], max_age_hours: float = 0) -> list[dict]:
    rows = []

    def add(table, check, passed, observed="", detail=""):
        rows.append({"table_name": table, "check_name": check, "passed": bool(passed), "observed": str(observed),
                     "detail": detail[:500]})
    for ident, kind in tables:
        quoted = ".".join(f"`{p.strip('`')}`" for p in ident.split("."))
        try:
            if kind == "metric view":
                spark.sql(f"DESCRIBE TABLE {quoted}").collect()
                add(ident, "readable", True)
                continue
            has_rows = spark.sql(f"SELECT 1 FROM {quoted} LIMIT 1").count() > 0
            add(ident, "readable", True)
            add(ident, "not_empty", has_rows, "rows" if has_rows else "no rows")
        except Exception as e:                           # noqa: BLE001
            add(ident, "readable", False, detail=str(e).splitlines()[0] if str(e) else type(e).__name__)
            continue
        if max_age_hours and max_age_hours > 0:
            try:
                last = spark.sql(f"DESCRIBE DETAIL {quoted}").collect()[0]["lastModified"]
                age = (datetime.now(timezone.utc) - last.replace(tzinfo=last.tzinfo or timezone.utc)).total_seconds() / 3600
                add(ident, "fresh", age <= max_age_hours, f"{age:.1f}h", f"limit {max_age_hours:g}h")
            except Exception as e:                       # noqa: BLE001  (a view has no Delta history)
                add(ident, "fresh", True, "n/a", f"not checked: {str(e).splitlines()[0][:120] if str(e) else e}")
    return rows


# --------------------------------------------------------------------------------------- benchmarks
def run_benchmarks(w, space_id: str, timeout_s: int = 1800, poll_s: int = 10) -> dict:
    run = w.genie.genie_create_eval_run(space_id)
    deadline = time.time() + timeout_s
    while _value(run.eval_run_status) in (None, "NOT_STARTED", "RUNNING"):
        if time.time() > deadline:
            raise TimeoutError(f"benchmark run {run.eval_run_id} did not finish within {timeout_s}s")
        time.sleep(poll_s)
        run = w.genie.genie_get_eval_run(space_id, run.eval_run_id)
    results, token = [], None
    while True:
        page = w.genie.genie_list_eval_results(space_id, run.eval_run_id, page_size=100, page_token=token)
        for r in page.eval_results or []:
            d = w.genie.genie_get_eval_result_details(space_id, run.eval_run_id, r.result_id)
            results.append({"question": r.question, "assessment": _value(d.assessment) or "NEEDS_REVIEW",
                            "reason": "; ".join(str(_value(x)) for x in (d.assessment_reasons or [])),
                            "genie_sql": _response(d.actual_response), "expected_sql": _response(d.expected_response)})
        token = page.next_page_token
        if not token:
            break
    return {"eval_run_id": run.eval_run_id, "status": _value(run.eval_run_status),
            "num_questions": run.num_questions or len(results), "num_correct": run.num_correct or 0,
            "num_needs_review": run.num_needs_review or 0,
            "num_bad": sum(1 for r in results if r["assessment"] == "BAD"), "results": results}


def evaluate(res: dict, *, min_accuracy: float, max_bad: int, min_graded: int) -> dict:
    graded = res["num_questions"] - res["num_needs_review"]
    accuracy = res["num_correct"] / graded if graded else 0.0
    reasons = []
    if res["status"] != "DONE":
        reasons.append(f"the benchmark run ended with status {res['status']}")
    if 0 <= max_bad < res["num_bad"]:
        reasons.append(f"{res['num_bad']} benchmark(s) answered incorrectly (allowed {max_bad})")
    if graded < min_graded:
        reasons.append(f"only {graded} benchmark(s) could be graded (need {min_graded})")
    if accuracy < min_accuracy:
        reasons.append(f"accuracy {accuracy:.0%} is below {min_accuracy:.0%}")
    return {**res, "graded": graded, "accuracy": accuracy, "passed": not reasons, "reasons": reasons}


def smoke(w, space_id: str, question: str, timeout_s: int = 600) -> dict:
    msg = w.genie.start_conversation_and_wait(space_id, question, timeout=timedelta(seconds=timeout_s))
    sql = next((a.query.query for a in (msg.attachments or []) if a.query and a.query.query), None)
    status = _value(msg.status)
    passed = status == "COMPLETED" and bool(sql)
    print(f"smoke test: {question!r} -> {status}{' with SQL' if sql else ' without SQL'}")
    if sql:
        print("  " + sql.replace("\n", "\n  "))
    print("SMOKE TEST PASSED" if passed else "SMOKE TEST FAILED")
    return {"status": status, "sql": sql, "passed": passed}


# ------------------------------------------------------------------------------------------ record
TABLES = {
    "genie_quality_runs": "run_id STRING, checked_at TIMESTAMP, target STRING, version STRING, space_id STRING, "
                          "mode STRING, passed BOOLEAN, accuracy DOUBLE, num_correct INT, graded INT, num_bad INT, "
                          "num_needs_review INT, unhealthy_checks INT, eval_run_id STRING, reasons STRING",
    "genie_quality_results": "run_id STRING, checked_at TIMESTAMP, target STRING, version STRING, question STRING, "
                             "assessment STRING, reason STRING, genie_sql STRING, expected_sql STRING",
    "genie_table_health": "run_id STRING, checked_at TIMESTAMP, target STRING, version STRING, table_name STRING, "
                          "check_name STRING, passed BOOLEAN, observed STRING, detail STRING",
}


def record(spark, location: str, rows: dict[str, list[dict]]) -> None:
    """Append rows to the monitoring tables at location (catalog.schema), creating them when missing.
    Recording never fails the job: a warning is printed instead."""
    if spark is None or not location:
        print("results not recorded (no Spark session or no record_to)")
        return
    try:
        cat, sch = location.split(".", 1)
        try:                                             # the setup creates the schema; creating it needs a
            spark.sql(f"DESCRIBE SCHEMA `{cat}`.`{sch}`").collect()   # catalog privilege the job may not have
        except Exception:                                # noqa: BLE001
            spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{cat}`.`{sch}`")
        for table, ddl in TABLES.items():
            name = f"`{cat}`.`{sch}`.`{table}`"
            spark.sql(f"CREATE TABLE IF NOT EXISTS {name} ({ddl})")
            if rows.get(table):
                cols = [c.split()[0] for c in ddl.split(", ")]
                spark.createDataFrame([tuple(r.get(c) for c in cols) for r in rows[table]], ddl) \
                    .write.mode("append").saveAsTable(f"{cat}.{sch}.{table}")
        print(f"recorded in {location}: " + ", ".join(f"{t} +{len(r)}" for t, r in rows.items() if r))
    except Exception as e:                               # noqa: BLE001
        print(f"WARNING: results not recorded in {location}: {str(e).splitlines()[0] if str(e) else e}")


# -------------------------------------------------------------------------------------------- main
def run(w, args, spark=None) -> bool:
    stamp = {"run_id": uuid.uuid4().hex[:12], "checked_at": datetime.now(timezone.utc).replace(tzinfo=None),
             "target": args.target, "version": args.version}
    rows = {t: [] for t in TABLES}
    summary = {**stamp, "space_id": args.space_id, "mode": args.mode}
    if args.mode == "smoke":
        res = smoke(w, args.space_id, args.smoke_question)
        summary.update(passed=res["passed"], reasons="" if res["passed"] else f"smoke: {res['status']}, no SQL")
    else:
        checks = health(spark, space_tables(w, args.space_id), args.max_data_age_hours) if spark is not None else []
        bad = [c for c in checks if not c["passed"]]
        print(f"health: {len(checks) - len(bad)}/{len(checks)} check(s) passed" if checks else
              "health: skipped (no Spark session)")
        for c in bad:
            print(f"  FAILED {c['check_name']} {c['table_name']}: {c['observed']} {c['detail']}")
        rows["genie_table_health"] = [{**stamp, **c} for c in checks]
        res = evaluate(run_benchmarks(w, args.space_id), min_accuracy=args.min_accuracy, max_bad=args.max_bad,
                       min_graded=args.min_graded)
        print(f"benchmark run {res['eval_run_id']}: {res['num_correct']}/{res['graded']} graded correct "
              f"({res['accuracy']:.0%}), {res['num_bad']} wrong, {res['num_needs_review']} need review")
        for r in res["results"]:
            if r["assessment"] == "BAD":
                print(f"  BAD  {r['question']}\n       {r['reason']}")
                if r.get("genie_sql"):
                    print("       Genie's SQL:\n" + "\n".join("         " + line for line in r["genie_sql"].splitlines()))
        reasons = res["reasons"] + ([f"{len(bad)} health check(s) failed"] if bad else [])
        summary.update(passed=not reasons, accuracy=res["accuracy"], num_correct=res["num_correct"], graded=res["graded"],
                       num_bad=res["num_bad"], num_needs_review=res["num_needs_review"], unhealthy_checks=len(bad),
                       eval_run_id=res["eval_run_id"], reasons="; ".join(reasons))
        rows["genie_quality_results"] = [{**stamp, **r} for r in res["results"]]
        print("GATE PASSED" if not reasons else "GATE FAILED: " + "; ".join(reasons))
    rows["genie_quality_runs"] = [summary]
    record(spark, args.record_to, rows)
    return summary["passed"]


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--space-id", required=True)
    ap.add_argument("--mode", choices=["gate", "smoke"], default="gate")
    ap.add_argument("--min-accuracy", type=float, default=0.60)
    ap.add_argument("--min-graded", type=int, default=1)
    ap.add_argument("--max-bad", type=int, default=-1, help="-1: no separate limit on wrong answers")
    ap.add_argument("--smoke-question", default="")
    ap.add_argument("--max-data-age-hours", type=float, default=0)
    ap.add_argument("--record-to", default="", help="catalog.schema of the monitoring tables ('' = don't record)")
    ap.add_argument("--target", default="")
    ap.add_argument("--version", default="")
    return ap.parse_args(argv)


def main(argv=None, client=None, spark=None) -> int:
    args = parse(argv)
    if client is None:
        from databricks.sdk import WorkspaceClient
        client = WorkspaceClient()
    return 0 if run(client, args, spark if spark is not None else _spark()) else 1


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)        # only on failure: a non-zero exit fails the job task (and the release)
