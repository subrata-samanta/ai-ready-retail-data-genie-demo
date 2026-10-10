"""Promotion-gate smoke test for the Genie space.

Asks a few questions through the Genie Conversation API and checks each one returns a SQL-backed answer
with at least one row: a cheap guard against a space that deploys but can't answer (bad table names,
empty data, missing grants, broken instructions).

Questions come from tests/smoke_questions.txt, or, when that file lists none, from the first three
sample questions in the space JSON.

    export GENIE_SPACE_ID="$(databricks bundle summary -t dev -o json \
        | jq -r '.resources.genie_spaces.genie_space.id')"
    python tests/smoke_test.py

Auth comes from your Databricks CLI profile / DATABRICKS_* variables. Requires: pip install databricks-sdk
"""
import json
import os
import sys
from pathlib import Path

from databricks.sdk import WorkspaceClient

ROOT = Path(__file__).resolve().parent.parent


def smoke_questions() -> list[str]:
    listed = [line.strip() for line in (ROOT / "tests" / "smoke_questions.txt").read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    if listed:
        return listed
    space = json.loads((ROOT / "src" / "genie_space.geniespace.json").read_text(encoding="utf-8"))
    samples = ["".join(q["question"]) for q in space.get("config", {}).get("sample_questions", [])]
    if not samples:
        sys.exit("No smoke questions: list some in tests/smoke_questions.txt or add sample questions to the space.")
    return samples[:3]


def row_count(w: WorkspaceClient, message) -> int:
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


def main() -> None:
    space_id = os.environ.get("GENIE_SPACE_ID")
    if not space_id:
        sys.exit("GENIE_SPACE_ID is not set (see the usage at the top of this file).")
    w = WorkspaceClient()
    failures = []
    for question in smoke_questions():
        message = w.genie.start_conversation_and_wait(space_id, question)
        status = getattr(message.status, "value", message.status)
        rows = row_count(w, message) if status == "COMPLETED" else 0
        print(f"[{'PASS' if rows > 0 else 'FAIL'}] {question}  ({status}, {rows} row(s))")
        if rows == 0:
            failures.append(question)
    if failures:
        sys.exit(f"Genie returned no usable answer for: {failures}")
    print("\nAll Genie smoke questions answered.")


if __name__ == "__main__":
    main()
