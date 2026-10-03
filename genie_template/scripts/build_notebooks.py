"""Write the .ipynb of every notebook from its Databricks source file.

    notebooks/<Name>_source.py  (Databricks source format: clean diffs in reviews)
      -> notebooks/<Name>.ipynb  (renders on GitHub; opens in Databricks and Jupyter)

    python scripts/build_notebooks.py           write them
    python scripts/build_notebooks.py --check   exit 1 if one is out of date (CI)

They need different names: in a Databricks Git folder X.py and X.ipynb would both be the notebook X.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

NOTEBOOKS = Path(__file__).resolve().parent.parent / "notebooks"
HEADER = "# Databricks notebook source"
SEPARATOR = "\n# COMMAND ----------\n"


def cells(source: Path) -> list[tuple[str, str]]:
    out = []
    for chunk in source.read_text(encoding="utf-8").split(SEPARATOR):
        chunk = chunk.replace(HEADER + "\n", "", 1).strip("\n")
        if not chunk:
            continue
        lines = chunk.splitlines()
        if all(line.startswith("# MAGIC") for line in lines):
            text = [line[len("# MAGIC "):] if line.startswith("# MAGIC ") else "" for line in lines]
            out.append(("markdown", "\n".join(text[1:])) if text[0].strip() == "%md" else ("code", "\n".join(text)))
        else:
            out.append(("code", chunk))
    return out


def render(source: Path) -> str:
    nb_cells = []
    for kind, text in cells(source):
        cell = {"cell_type": kind, "metadata": {}, "source": text.splitlines(keepends=True)}
        if kind == "code":
            cell.update(execution_count=None, outputs=[])
        nb_cells.append(cell)
    nb = {"cells": nb_cells,
          "metadata": {"application/vnd.databricks.v1+notebook": {
              "language": "python", "notebookName": source.stem.removesuffix("_source")},
              "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
              "language_info": {"name": "python"}},
          "nbformat": 4, "nbformat_minor": 4}
    return json.dumps(nb, indent=1, ensure_ascii=False) + "\n"


def main(argv=None) -> int:
    check = "--check" in (argv if argv is not None else sys.argv[1:])
    stale = []
    for source in sorted(NOTEBOOKS.glob("*_source.py")):
        target = source.with_name(source.name.removesuffix("_source.py") + ".ipynb")
        text = render(source)
        if check:
            if not target.exists() or target.read_text(encoding="utf-8") != text:
                stale.append(target.name)
        else:
            target.write_text(text, encoding="utf-8")
            print(f"wrote {target.name}")
    if stale:
        print("out of date (run python scripts/build_notebooks.py): " + ", ".join(stale))
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
