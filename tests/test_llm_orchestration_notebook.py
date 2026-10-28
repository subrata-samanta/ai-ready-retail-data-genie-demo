"""Run notebooks/LLM_Orchestration_Two_Genie_MCP_Demo.ipynb from top to bottom, offline.

The notebook routes each question with an LLM (JSON: A, B, BOTH or NONE) to two Genie spaces reached through their
managed MCP servers, polls Genie until it has answered, and has the LLM write the final answer. Here it runs against
tests/fake_genie_mcp.py with the installed MCP Python SDK (the notebook supports mcp 1.x and 2.x). The %pip cell is
skipped; every other code cell runs in order, as "Run all" would. Skipped when mcp, uvicorn or openai is missing.
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
NOTEBOOK = ROOT / "notebooks" / "LLM_Orchestration_Two_Genie_MCP_Demo.ipynb"
sys.path.insert(0, str(ROOT / "tests"))


def _code_cells() -> list[str]:
    nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


def test_llm_orchestration_notebook_cells_are_well_formed():
    nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    assert nb["nbformat"] == 4
    cells = _code_cells()
    assert cells[0].startswith("%pip install") and "dbutils.library.restartPython()" in cells[1]
    for i, src in enumerate(cells[1:], 1):
        ast.parse(src, f"code cell {i}")      # also rejects a top-level await, which not every runtime runs
    for cell in nb["cells"]:
        assert not cell.get("outputs"), "saved without outputs"


def _run(config: dict, echo_on_error: bool = True) -> tuple[str, dict]:
    ns = {"__name__": "__notebook__"}
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            for i, src in enumerate(_code_cells()):
                if src.startswith("%pip"):
                    continue
                if src.startswith("GENIE_A_ID"):
                    for old, new in config.items():
                        src = src.replace(old, new)
                exec(compile(src, f"{NOTEBOOK.name}[code cell {i}]", "exec"), ns)
    except Exception:
        if echo_on_error:
            print(out.getvalue())
        raise
    return out.getvalue(), ns


@contextlib.contextmanager
def _workspace():
    from fake_genie_mcp import TOKEN, FakeGenieWorkspace
    ws = FakeGenieWorkspace.start()
    saved = {k: os.environ.get(k) for k in ("DATABRICKS_HOST", "DATABRICKS_TOKEN")}
    os.environ.update({"DATABRICKS_HOST": ws.host, "DATABRICKS_TOKEN": TOKEN})
    try:
        yield ws
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ws.stop()


def _deps() -> bool:
    try:
        import mcp  # noqa: F401
        import openai  # noqa: F401
        import uvicorn  # noqa: F401
        from mcp.server.mcpserver import MCPServer  # noqa: F401
    except ImportError:
        print("      (mcp 2.x, uvicorn or openai not installed: LLM orchestration notebook run skipped)")
        return False
    return True


def test_llm_orchestration_notebook_end_to_end_against_stand_in():
    if not _deps():
        return
    from fake_genie_mcp import SALES_ID, STOCK_ID
    with _workspace() as ws:
        text, ns = _run({"<GENIE_SPACE_A_ID>": f"{ws.host}/genie/rooms/{SALES_ID}?o=1",
                         "<GENIE_SPACE_B_ID>": STOCK_ID})

        # sections 3-5: IDs (one given as a URL), the tools of both MCP servers, the spaces' descriptions
        assert (ns["GENIE_A_ID"], ns["GENIE_B_ID"]) == (SALES_ID, STOCK_ID)
        assert ns["SPACES"]["A"]["query_tool"]["name"] == f"query_space_{SALES_ID}"
        assert ns["SPACES"]["B"]["poll_tool"]["name"] == f"poll_response_{STOCK_ID}"
        assert ns["SPACES"]["A"]["name"] == "FreshCart Sales Analytics"
        assert "out-of-stock" in ns["router_system_prompt"]()
        # section 6: no endpoint configured, so a Foundation Model endpoint was picked (Claude first)
        assert ns["ORCHESTRATOR_ENDPOINT"] == "databricks-claude-sonnet-4-5"

        # section 9: A, B, BOTH, NONE, each routed as expected
        results = ns["DEMO_RESULTS"]
        assert [r["expected"] for r in results] == ["A", "B", "BOTH", "NONE"], results
        assert [r["as expected"] for r in results] == ["yes"] * 4, results
        # the router reply came fenced with a sentence before it, and was still parsed
        assert "ROUTING:  BOTH" in text
        # every Genie call polled until COMPLETED, and the answer reached the synthesis step
        assert text.count("COMPLETED in") == 4 and "2 poll(s)" in text
        assert "West led net sales" in text and "Dairy (4.1%)" in text and "Toronto Queen St" in text
        assert f"open in Genie: {ws.host}/genie/rooms/{SALES_ID}/chats/" in text
        assert "Set MY_QUESTION above" in text
        polls = [c for c in ws.state.tool_calls if c[1].startswith("poll_response")]
        assert len(polls) == 8

        # own demo questions and a configured endpoint replace the generated ones
        text2, ns2 = _run({"<GENIE_SPACE_A_ID>": SALES_ID, "<GENIE_SPACE_B_ID>": STOCK_ID,
                           'ORCHESTRATOR_ENDPOINT = ""': 'ORCHESTRATOR_ENDPOINT = "databricks-meta-llama-3-3-70b-instruct"',
                           '    # ("Which products are most often out of stock?", "B"),':
                           '    ("Which products are most often out of stock?", "B"),'})
        assert ns2["ORCHESTRATOR_ENDPOINT"] == "databricks-meta-llama-3-3-70b-instruct"
        assert [(r["question"], r["routed"], r["as expected"]) for r in ns2["DEMO_RESULTS"]] == \
            [("Which products are most often out of stock?", "B", "yes")]


def test_llm_orchestration_notebook_stops_with_a_clear_message_without_ids():
    if not _deps():
        return
    with _workspace():
        try:
            _run({}, echo_on_error=False)
        except ValueError as err:
            assert "Please configure in section 2: GENIE_A_ID, GENIE_B_ID" in str(err)
        else:
            raise AssertionError("the notebook ran without Genie space IDs")
