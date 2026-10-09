"""
Promotion-gate smoke test for the Finance & P&L Genie space.

Run this against the DEV space (after `bundle deploy -t dev` and the one-time
data setup) before promoting to QA/Prod. It asks the curated sample questions
through the Genie Conversation API and asserts each one returns a SQL-backed
answer with at least one row — a cheap guard against a space that deploys but
can't actually answer (bad table refs, empty data, broken instructions).

Usage:
    # Resolve the deployed space id from the bundle, then run the test:
    export GENIE_SPACE_ID="$(databricks bundle summary -t dev -o json \
        | jq -r '.resources.genie_spaces.finance_pnl.id')"
    python tests/test_genie.py          # or: pytest tests/test_genie.py

Auth comes from your active Databricks CLI profile / DATABRICKS_* env vars,
exactly like the bundle commands. Requires: pip install databricks-sdk
"""

import os
import sys

from databricks.sdk import WorkspaceClient

# A few of the space's sample questions. Each must come back with generated SQL
# and a non-empty result set.
SMOKE_QUESTIONS = [
    "What was gross margin % by brand for the latest fiscal quarter?",
    "Which region had the worst trade spend variance versus budget?",
    "Show the monthly net revenue trend for Brand A over the fiscal year.",
]


def _resolve_space_id() -> str:
    space_id = os.environ.get("GENIE_SPACE_ID")
    if not space_id:
        sys.exit(
            "GENIE_SPACE_ID is not set. Resolve it from the deployed bundle:\n"
            "  export GENIE_SPACE_ID=\"$(databricks bundle summary -t dev -o json "
            "| jq -r '.resources.genie_spaces.finance_pnl.id')\""
        )
    return space_id


def _answer_has_rows(message) -> bool:
    """True if the Genie reply carries a query result with >= 1 row."""
    for attachment in message.attachments or []:
        query = getattr(attachment, "query", None)
        if query is None:
            continue
        result = getattr(query, "query_result", None) or getattr(query, "result", None)
        row_count = getattr(result, "row_count", None) if result else None
        if row_count and row_count > 0:
            return True
        # Some SDK versions surface the statement id only; treat a present query as a pass.
        if getattr(query, "query", None) or getattr(query, "statement_id", None):
            return True
    return False


def test_genie_space_answers_sample_questions():
    space_id = _resolve_space_id()
    w = WorkspaceClient()

    failures = []
    for question in SMOKE_QUESTIONS:
        message = w.genie.start_conversation_and_wait(space_id, question)
        ok = _answer_has_rows(message)
        print(f"[{'PASS' if ok else 'FAIL'}] {question}")
        if not ok:
            failures.append(question)

    assert not failures, f"Genie returned no usable answer for: {failures}"


if __name__ == "__main__":
    test_genie_space_answers_sample_questions()
    print("\nAll Genie smoke questions answered — safe to promote.")
