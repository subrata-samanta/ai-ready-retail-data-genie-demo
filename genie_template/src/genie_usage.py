"""Usage job of the Genie space: the task of the bundle job `genie_usage`.

Reads the conversations started in the last lookback_hours (all users) and, for each message:
    the question, who asked it and when, the status (COMPLETED, FAILED, ...), the error, whether Genie answered
    with SQL (and which), and the user's feedback (POSITIVE / NEGATIVE)
and merges them into <record_to>.genie_usage_messages, keyed by message: a re-run updates (a later thumbs down,
for example) and never duplicates. The dashboard and the SQL alert "failing answers" read the table.

Questions that failed or got a thumbs down are the best source of new benchmarks and example SQL.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

DDL = ("message_id STRING, conversation_id STRING, created_at TIMESTAMP, user_id STRING, question STRING, "
       "status STRING, error STRING, has_sql BOOLEAN, genie_sql STRING, feedback_rating STRING, target STRING, "
       "version STRING, collected_at TIMESTAMP")
TABLE = "genie_usage_messages"


def _value(x):
    return getattr(x, "value", x)


def _ts(ms) -> datetime | None:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).replace(tzinfo=None) if ms else None


def _pages(call, field: str, **kw):
    token = None
    while True:
        page = call(**kw, page_token=token) if token else call(**kw)
        yield from (getattr(page, field, None) or [])
        token = getattr(page, "next_page_token", None)
        if not token:
            return


def collect(w, space_id: str, lookback_hours: float, max_conversations: int = 5000) -> list[dict]:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=lookback_hours)
    rows = []
    for n, conv in enumerate(_pages(w.genie.list_conversations, "conversations", space_id=space_id,
                                    include_all=True, page_size=100)):
        if n >= max_conversations:
            print(f"stopped after {max_conversations} conversations")
            break
        started = _ts(conv.created_timestamp)
        if started and started < cutoff:
            continue
        for m in _pages(w.genie.list_conversation_messages, "messages", space_id=space_id,
                        conversation_id=conv.conversation_id, page_size=100):
            created = _ts(m.created_timestamp)
            if created and created < cutoff:
                continue
            sql = next((a.query.query for a in (m.attachments or []) if getattr(a, "query", None) and a.query.query), None)
            err = m.error
            rows.append({
                "message_id": m.message_id or m.id, "conversation_id": conv.conversation_id, "created_at": created,
                "user_id": str(m.user_id) if m.user_id is not None else None, "question": m.content,
                "status": str(_value(m.status) or ""),
                "error": (f"{_value(err.type) or ''}: {err.error or ''}".strip(": ") if err else None),
                "has_sql": bool(sql), "genie_sql": sql,
                "feedback_rating": str(_value(m.feedback.rating)) if m.feedback and m.feedback.rating else None,
            })
    return rows


def record(spark, location: str, rows: list[dict], stamp: dict) -> None:
    if spark is None or not location:
        print("not recorded (no Spark session or no record_to)")
        return
    cat, sch = location.split(".", 1)
    name = f"`{cat}`.`{sch}`.`{TABLE}`"
    spark.sql(f"CREATE TABLE IF NOT EXISTS {name} ({DDL})")
    if not rows:
        return
    cols = [c.split()[0] for c in DDL.split(", ")]
    spark.createDataFrame([tuple({**r, **stamp}.get(c) for c in cols) for r in rows], DDL) \
        .createOrReplaceTempView("genie_usage_batch")
    spark.sql(f"MERGE INTO {name} t USING genie_usage_batch s "
              "ON t.message_id = s.message_id AND t.target = s.target "
              "WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *")
    print(f"merged {len(rows)} message(s) into {location}.{TABLE}")


def summarise(rows: list[dict]) -> dict:
    failed = [r for r in rows if r["status"] != "COMPLETED"]
    negative = [r for r in rows if r["feedback_rating"] == "NEGATIVE"]
    return {"messages": len(rows), "users": len({r["user_id"] for r in rows}),
            "conversations": len({r["conversation_id"] for r in rows}), "failed": len(failed),
            "negative_feedback": len(negative), "without_sql": sum(1 for r in rows if not r["has_sql"]),
            "to_review": [r["question"] for r in failed + negative][:20]}


def main(argv=None, client=None, spark=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--space-id", required=True)
    ap.add_argument("--lookback-hours", type=float, default=48)
    ap.add_argument("--record-to", default="")
    ap.add_argument("--target", default="")
    ap.add_argument("--version", default="")
    args = ap.parse_args(argv)
    if client is None:
        from databricks.sdk import WorkspaceClient
        client = WorkspaceClient()
    if spark is None:
        try:
            from pyspark.sql import SparkSession
            spark = SparkSession.builder.getOrCreate()
        except Exception:                                # noqa: BLE001
            spark = None
    rows = collect(client, args.space_id, args.lookback_hours)
    s = summarise(rows)
    print(f"last {args.lookback_hours:g}h: {s['messages']} question(s) from {s['users']} user(s) in "
          f"{s['conversations']} conversation(s); {s['failed']} failed, {s['negative_feedback']} thumbs down, "
          f"{s['without_sql']} answered without SQL")
    for q in s["to_review"]:
        print(f"  review: {q}")
    record(spark, args.record_to, rows, {"target": args.target, "version": args.version,
                                         "collected_at": datetime.now(timezone.utc).replace(tzinfo=None)})
    return 0


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
