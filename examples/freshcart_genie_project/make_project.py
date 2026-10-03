"""Make the FreshCart Genie project from the template: a folder ready to become its own GitHub repository.

    python examples/freshcart_genie_project/make_project.py <out_dir>

  1. copies genie_template/ to <out_dir>
  2. replaces its genie.config.yml with the FreshCart one next to this script
  3. converts the FreshCart space (genie_bundle/resources/freshcart_assistant.space.yml) into the template's
     space/genie_space.yml: the same content, with the semantic schema written ${var.catalog}.${var.schema}
  4. checks the result: the space renders, for dev, qa and prod, to exactly what genie_bundle deploys

Then follow the template's README (new repository, Git folder, notebooks/Genie_Project_Template).
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
TEMPLATE = REPO / "genie_template"
FRESHCART_SPACE = REPO / "genie_bundle" / "resources" / "freshcart_assistant.space.yml"
FRESHCART_VAR = "freshcart_assistant_space"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make(out: Path) -> Path:
    import yaml
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} exists and is not empty")
    shutil.copytree(TEMPLATE, out, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".databricks", "__pycache__", "backup", "plan*.json"))
    shutil.copyfile(HERE / "genie.config.yml", out / "genie.config.yml")
    T = _module(f"genie_tools_{abs(hash(str(out)))}", out / "scripts" / "genie_tools.py")
    freshcart = yaml.safe_load(FRESHCART_SPACE.read_text(encoding="utf-8"))["variables"][FRESHCART_VAR]["default"]
    # genie_bundle writes every table ${var.catalog}.<schema>.<object>; rendered for dev and neutralised again with the
    # template's rules, the configured schema (semantic) becomes ${var.catalog}.${var.schema}
    space = T.from_target(T.render(freshcart, T.settings("dev")["catalog"]), "dev")
    T.save_space(space, out / "space" / "genie_space.yml")
    for env in T.ENVIRONMENTS:                                # the deployed content is unchanged
        catalog = T.settings(env)["catalog"]
        assert json.dumps(T.for_target(space, env), sort_keys=True) == \
            json.dumps(T.render(freshcart, catalog), sort_keys=True), f"{env}: content differs from genie_bundle"
    return out


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__)
        return 2
    out = make(Path(args[0]))
    print(f"made the FreshCart Genie project in {out}")
    print("next: copy it to the root of a new GitHub repository and follow its README.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
