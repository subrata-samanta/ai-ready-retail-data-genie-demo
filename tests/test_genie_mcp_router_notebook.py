"""Run notebooks/Genie_MCP_Router_Demo.ipynb from top to bottom, offline.

The notebook connects to two Genie spaces through their managed MCP servers and lets an LLM route each question.
Here it runs against tests/fake_genie_mcp.py: two MCP servers built with the official `mcp` SDK (one answering with
JSON, one with server-sent events), the Genie spaces API and a keyword-routing chat endpoint. Every code cell runs
in order, as "Run all" would, and the test checks what the text promises. Skipped when `mcp` or `uvicorn` is missing.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTEBOOK = ROOT / "notebooks" / "Genie_MCP_Router_Demo.ipynb"
sys.path.insert(0, str(ROOT / "tests"))


def _code_cells() -> list[str]:
    nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


def test_genie_mcp_router_notebook_cells_are_well_formed():
    nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    assert nb["nbformat"] == 4
    for i, src in enumerate(_code_cells()):
        ast.parse(src, f"code cell {i}")
        assert "%pip" not in src and "pip install" not in src, "the demo must not install anything"
    for cell in nb["cells"]:
        assert not cell.get("outputs"), "saved without outputs: they would show the stand-in's data"


def _run(env: dict, echo_on_error: bool = True) -> tuple[str, dict]:
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    ns = {"__name__": "__notebook__"}
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            for i, src in enumerate(_code_cells()):
                exec(compile(src, f"{NOTEBOOK.name}[code cell {i}]", "exec"), ns)
    except Exception:
        if echo_on_error:
            print(out.getvalue())
        raise
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return out.getvalue(), ns


def test_genie_mcp_router_notebook_end_to_end_against_stand_in():
    try:
        import mcp.server.mcpserver  # noqa: F401
        import uvicorn  # noqa: F401
        from fake_genie_mcp import SALES_ID, STOCK_ID, TOKEN, FakeGenieWorkspace
    except ImportError:
        print("      (mcp 2.x or uvicorn not installed: Genie MCP router notebook run skipped)")
        return
    ws = FakeGenieWorkspace.start()
    base = {"DATABRICKS_HOST": ws.host, "DATABRICKS_TOKEN": TOKEN,
            }
    try:
        # space 1 as a Genie UI link, space 2 as a bare ID; no hints, no endpoint: the notebook picks one
        text, ns = _run({**base, "GENIE_SPACE_ID_1": f"{ws.host}/genie/rooms/{SALES_ID}?o=123",
                         "GENIE_SPACE_ID_2": STOCK_ID, "GENIE_SPACE_HINT_1": "", "GENIE_SPACE_HINT_2": "",
                         "LLM_ENDPOINT": ""})

        # section 2-4: both MCP servers answered tools/list (one over JSON, one over SSE)
        assert ns["SPACE_IDS"] == {"space_1": SALES_ID, "space_2": STOCK_ID}
        assert [s.title for s in ns["SPACES"]] == ["FreshCart Sales Analytics", "FreshCart Inventory"]
        assert [s.ask_tool["name"] for s in ns["SPACES"]] == [f"query_space_{SALES_ID}", f"query_space_{STOCK_ID}"]
        assert all(s.poll_tool for s in ns["SPACES"])
        assert ns["SPACES"][0].sample_questions == ["What were net sales by region last week?"]
        assert ns["SPACES"][1].sample_questions == []          # configuration not readable: no crash
        # section 5: the LLM sees one tool per space, described by the space's title and description
        assert len(ns["TOOLS"]) == 2 and "out-of-stock" in ns["TOOLS"][1]["function"]["description"]
        # section 6: Claude picked over Llama and the embeddings endpoint
        assert ns["LLM_ENDPOINT"] == "databricks-claude-sonnet-4-5"
        assert "test reply: 'OK'" in text

        # section 8: every question routed as expected: space 1, space 2, both, none
        score = ns["scorecard"]
        assert list(score["expected"]) == ["space_1", "space_2", "space_1, space_2", "none"], score
        assert list(score["routed right"]) == ["yes"] * 4, score
        assert "4 of 4 questions routed as expected" in text
        # the client polled until Genie finished, and the answers carry Genie's numbers
        assert "COMPLETED in" in text and "(2 x poll_response_" in text
        answers = [r["answer"] for r in ns["RESULTS"]]
        assert "West led net sales" in answers[0] and "Dairy" in answers[1]
        assert "Toronto Queen St" in answers[2] and "Dairy" in answers[2]
        assert "outside both Genie spaces" in answers[3]
        assert "Toronto Queen St" in text and "182450.00" in text          # Genie's rows were shown as tables
        assert f"open in Genie: {ws.host}/genie/rooms/{SALES_ID}/chats/" in text
        # section 9: the wire messages are shown
        assert '"method": "tools/call"' in text and "the reply that carried the answer" in text
        assert "Set MY_QUESTION above" in text

        calls = ws.state.tool_calls
        assert (SALES_ID, f"poll_response_{SALES_ID}") in calls and (STOCK_ID, f"poll_response_{STOCK_ID}") in calls
        requests = ws.state.llm_requests
        assert all(r.get("tools") for r in requests if any(m["role"] == "tool" for m in r["messages"]))

        # a second run: an MCP URL as the ID, a hint and an explicit endpoint
        text2, ns2 = _run({**base, "GENIE_SPACE_ID_1": SALES_ID,
                           "GENIE_SPACE_ID_2": f"{ws.host}/api/2.0/mcp/genie/{STOCK_ID}",
                           "GENIE_SPACE_HINT_1": "Promotions and loyalty too.", "GENIE_SPACE_HINT_2": "",
                           "LLM_ENDPOINT": "databricks-meta-llama-3-3-70b-instruct"})
        assert ns2["LLM_ENDPOINT"] == "databricks-meta-llama-3-3-70b-instruct"
        assert "Note from the user: Promotions and loyalty too." in ns2["TOOLS"][0]["function"]["description"]
        assert "4 of 4 questions routed as expected" in text2
    finally:
        ws.stop()


def test_genie_mcp_router_notebook_stops_with_a_clear_message_without_ids():
    try:
        from fake_genie_mcp import TOKEN, FakeGenieWorkspace
    except ImportError:
        return
    ws = FakeGenieWorkspace.start()
    try:
        env = {"DATABRICKS_HOST": ws.host, "DATABRICKS_TOKEN": TOKEN,
               "DATABRICKS_CONFIG_FILE": str(ROOT / "tests" / "no-such-databrickscfg"),
               "GENIE_SPACE_ID_1": "", "GENIE_SPACE_ID_2": ""}
        try:
            _run(env, echo_on_error=False)
        except ValueError as err:
            assert "genie_space_id_1, genie_space_id_2" in str(err)
        else:
            raise AssertionError("the notebook ran without Genie space IDs")
    finally:
        ws.stop()
