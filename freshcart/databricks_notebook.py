"""Generate the .ipynb version of the end-to-end Databricks notebook from its Databricks source file.

    notebooks/FreshCart_End_to_End_on_Databricks_source.py   (Databricks source format: clean diffs in reviews)
        -> notebooks/FreshCart_End_to_End_on_Databricks.ipynb (renders on GitHub; opens in Databricks and Jupyter)

    python -m freshcart.databricks_notebook

Both files hold the same cells; tests/test_databricks_notebook.py fails if the .ipynb is out of date.
(They need different names: in a Databricks Git folder X.py and X.ipynb would both be the notebook X.)
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config as C

SOURCE = C.ROOT / "notebooks" / "FreshCart_End_to_End_on_Databricks_source.py"
TARGET = C.ROOT / "notebooks" / "FreshCart_End_to_End_on_Databricks.ipynb"
HEADER = "# Databricks notebook source"
SEPARATOR = "\n# COMMAND ----------\n"


def cells(source: Path = SOURCE) -> list[tuple[str, str]]:
    """(kind, text) per cell: kind is markdown or code; magic cells (%sql, %pip) keep their magic line."""
    out = []
    for chunk in source.read_text(encoding="utf-8").split(SEPARATOR):
        chunk = chunk.replace(HEADER + "\n", "", 1).strip("\n")
        if not chunk:
            continue
        lines = chunk.splitlines()
        if all(line.startswith("# MAGIC") for line in lines):
            text = [line[len("# MAGIC "):] if line.startswith("# MAGIC ") else "" for line in lines]
            if text[0].strip() == "%md":
                out.append(("markdown", "\n".join(text[1:])))
            else:
                out.append(("code", "\n".join(text)))
        else:
            out.append(("code", chunk))
    return out


def to_ipynb(source: Path = SOURCE) -> dict:
    nb_cells = []
    for kind, text in cells(source):
        cell = {"cell_type": kind, "metadata": {}, "source": text.splitlines(keepends=True)}
        if kind == "code":
            cell.update(execution_count=None, outputs=[])
        nb_cells.append(cell)
    return {
        "cells": nb_cells,
        "metadata": {
            "application/vnd.databricks.v1+notebook": {"language": "python",
                                                       "notebookName": TARGET.stem},
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 4,
    }


def render(source: Path = SOURCE) -> str:
    return json.dumps(to_ipynb(source), indent=1, ensure_ascii=False) + "\n"


def main() -> None:
    TARGET.write_text(render(), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(C.ROOT)}")


if __name__ == "__main__":
    main()
