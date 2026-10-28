"""Run notebooks/Genie_CICD_Simple_Bundle_Walkthrough.ipynb from top to bottom, offline.

The notebook explains the simple bundle's CI/CD (genie-as-code-review/) and, in part 5, runs the bundle's own
workflow against three stand-in workspaces with the real Databricks CLI. This test executes every code cell in order
and checks the outcomes the text describes. Skipped when no Databricks CLI 1.14+ is installed.
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
NOTEBOOK = ROOT / "notebooks" / "Genie_CICD_Simple_Bundle_Walkthrough.ipynb"
sys.path.insert(0, str(ROOT / "tests"))


def _code_cells() -> list[str]:
    nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


def test_simple_cicd_notebook_cells_are_well_formed():
    nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    assert nb["nbformat"] == 4
    code = [c for c in nb["cells"] if c["cell_type"] == "code"]
    for i, cell in enumerate(code):
        ast.parse("".join(cell["source"]), f"code cell {i}")
        assert not any(o.get("output_type") == "error" for o in cell.get("outputs", [])), f"code cell {i} saved an error"
    counts = [c.get("execution_count") for c in code]
    assert counts == list(range(1, len(code) + 1)), "save the notebook after Restart & Run All"


def test_simple_cicd_notebook_end_to_end_against_stand_ins():
    from test_genie_bundle import _cli
    cli = _cli()
    if cli is None:
        print("      (Databricks CLI 1.14+ not found: simple CI/CD notebook run skipped)")
        return
    cwd, env_cli = os.getcwd(), os.environ.get("DATABRICKS_CLI")
    os.environ["DATABRICKS_CLI"] = cli
    ns = {"__name__": "__notebook__", "display": lambda *a, **k: None}
    out = io.StringIO()
    try:
        os.chdir(ROOT / "notebooks")
        with contextlib.redirect_stdout(out):
            for i, src in enumerate(_code_cells()):
                exec(compile(src, f"{NOTEBOOK.name}[code cell {i}]", "exec"), ns)
    finally:
        os.chdir(cwd)
        if env_cli is None:
            os.environ.pop("DATABRICKS_CLI", None)
        else:
            os.environ["DATABRICKS_CLI"] = env_cli
        for ws in ns.get("WS", {}).values():
            ws.stop()
    text = out.getvalue()

    # part 4: the JSON reads the dev catalog for every target until set_catalog.sh rewrites it
    assert "Space for target qa now reads catalog freshcart_qa" in text
    # 5.2 / 5.3: first release, then prod after approval
    assert "4/4 graded correct (100%)" in text and "approved by Ravi (release manager)" in text
    # 5.4: the UI edit came back through bundle generate and was graded in QA
    assert "the dev space now has 5 examples; git still has 4" in text
    assert "5/5 graded correct (100%)" in text
    # 5.5: the bad change passed the pull request and the smoke test, failed the QA gate, never reached prod
    assert "GATE FAILED: accuracy 40% < threshold 60%" in text
    # 5.6: rollback put the reviewed summary rule back in prod
    assert "prod is back to the reviewed rule: True" in text
    # 5.7: drift shows in the plan and the next release overwrites it
    assert "Plan: 0 to add, 1 to change" in text and "edit still live in prod: False" in text
    # 5.8: a broken JSON stops at validate
    assert "✘  Space JSON is valid" in text
    # the lab ends with every environment on the same version, each on its own catalog
    assert "lab cleaned up" in text
    final = ns["spaces"]().set_index("workspace")                  # the stand-ins' state outlives their servers
    assert final["catalog"].to_dict() == {"dev": "freshcart_dev", "qa": "freshcart_qa", "prod": "freshcart"}
    assert set(final["examples"]) == {5} and set(final["benchmarks"]) == {9}
