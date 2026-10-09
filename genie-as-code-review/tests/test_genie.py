"""
Promotion-gate smoke test for the FreshCart Genie space.

Run this against a deployed space (after `bundle deploy` and the FreshCart data bundle) before
promoting it. It asks a few of the space's questions through the Genie Conversation API and asserts
each one returns a SQL-backed answer with at least one row — a cheap guard against a space that
deploys but can't actually answer (bad table refs, empty data, missing grants, broken instructions).

Usage:
    # Resolve the deployed space id from the bundle, then run the test:
    export GENIE_SPACE_ID="$(databricks bundle summary -t dev -o json \
        | jq -r '.resources.genie_spaces.freshcart_assistant.id')"
    python tests/test_genie.py          # or: pytest tests/test_genie.py

Auth comes from your active Databricks CLI profile / DATABRICKS_* env vars,
exactly like the bundle commands. Requires: pip install databricks-sdk
"""

import os
import sys

from databricks.sdk import WorkspaceClient

# Questions that have a trusted example query in the space. Each must come back with generated SQL
# and a non-empty result set.
SMOKE_QUESTIONS = [
    "What were net sales and margin by region last week?",
    "Which categories rely most on promotions this quarter?",
    "Where are we out of stock most often in fresh?",
]


def _resolve_space_id() -> str:
    space_id = os.environ.get("GENIE_SPACE_ID")
    if not space_id:
        sys.exit(
            "GENIE_SPACE_ID is not set. Resolve it from the deployed bundle:\n"
            "  export GENIE_SPACE_ID=\"$(databricks bundle summary -t dev -o json "
            "| jq -r '.resources.genie_spaces.freshcart_assistant.id')\""
        )
    return space_id


def _row_count(w: WorkspaceClient, message) -> int:
    """Rows returned by the first SQL answer in a Genie reply (0 when there is none)."""
    for attachment in message.attachments or []:
        if attachment.query is None or not attachment.query.query:
            continue
        result = w.genie.get_message_attachment_query_result(
            message.space_id, message.conversation_id, message.message_id, attachment.attachment_id
        ).statement_response
        if result is None:
            return 0
        if result.manifest and result.manifest.total_row_count is not None:
            return result.manifest.total_row_count
        return len(result.result.data_array or []) if result.result else 0
    return 0


def test_genie_space_answers_sample_questions():
    space_id = _resolve_space_id()
    w = WorkspaceClient()

    failures = []
    for question in SMOKE_QUESTIONS:
        message = w.genie.start_conversation_and_wait(space_id, question)
        status = getattr(message.status, "value", message.status)
        rows = _row_count(w, message) if status == "COMPLETED" else 0
        ok = rows > 0
        print(f"[{'PASS' if ok else 'FAIL'}] {question}  ({status}, {rows} row(s))")
        if not ok:
            failures.append(question)

    assert not failures, f"Genie returned no usable answer for: {failures}"


if __name__ == "__main__":
    test_genie_space_answers_sample_questions()
    print("\nAll Genie smoke questions answered — safe to promote.")
