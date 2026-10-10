"""Point the space JSON at a target's catalog before deploying to that target.

The JSON is written with the dev catalog, exactly as `bundle generate` exports it from the dev space, and
bundles do not substitute variables inside that file. This rewrites every three-part name
<dev catalog>.<schema>.<object> to <target catalog>.<schema>.<object>, reading both catalogs from
databricks.yml. CI runs it on a fresh checkout; if you run it locally, don't commit the result
(`git checkout src/` undoes it).

    python scripts/set_catalog.py qa
"""
from __future__ import annotations

import sys

import space_lib as L


def main(argv: list[str]) -> None:
    if len(argv) != 1:
        raise SystemExit("usage: python scripts/set_catalog.py <target>")
    target = argv[0]
    dev, catalog = L.catalog_for("dev"), L.catalog_for(target)
    blob = L.SPACE_FILE.read_text(encoding="utf-8")
    new, n = L.rewrite_catalog(blob, dev, catalog)
    L.SPACE_FILE.write_text(new, encoding="utf-8")
    left = L.catalogs_in(L.load_space()) - {catalog}
    if left:
        raise SystemExit(f"the space still reads catalog(s) {sorted(left)} after the rewrite")
    print(f"Space for target {target} now reads catalog {catalog} ({n} reference(s) rewritten)")


if __name__ == "__main__":
    main(sys.argv[1:])
