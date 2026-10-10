# Genie space as code — template for your own space

The FreshCart approach ([`../genie-as-code-review`](../genie-as-code-review/)) made reusable for **any** Genie space:
one JSON file holds the space, one bundle resource deploys it, one GitHub workflow promotes it
**dev → QA (benchmark gate) → prod (approval)**. Nothing in here is FreshCart-specific.

Copy this folder to the root of a new repository, fill in `databricks.yml`, and run `scripts/adopt_space.py` to pull
in the space you already have. The full step-by-step guide, including running the FreshCart demo on a real workspace
first, is [`../RUN_ON_REAL_DATABRICKS.md`](../RUN_ON_REAL_DATABRICKS.md).

```text
databricks.yml                         bundle, variables, targets dev / qa / prod   <- fill in every CHANGE_ME
resources/genie_space.genie_space.yml  the Genie space resource (title, warehouse, permissions per target)
src/genie_space.geniespace.json        the space itself: data sources, instructions, examples, benchmarks
                                       (written by adopt_space.py and `bundle generate`, not by hand)
scripts/adopt_space.py                 once: export your existing space into src/, optionally bind it
scripts/check_space.py                 checks + AI-readiness scorecard; --live <target> checks UC metadata
scripts/set_catalog.py                 points the JSON at a target's catalog (CI runs it before qa/prod)
scripts/space_lib.py                   helpers shared by the scripts
tests/smoke_test.py                    asks a few questions through the Conversation API (dev and prod)
tests/smoke_questions.txt              the questions it asks (blank = the space's sample questions)
tests/benchmark_gate.py                runs the space's benchmarks, fails below 60% accuracy (QA)
sql/uc_metadata_template.sql           comments, keys, metric view, trusted function, grants, row filter
.github/workflows/deploy.yml           CI/CD: validate -> dev -> QA -> prod
```

## Prerequisites

- **Databricks CLI ≥ 1.14** (the workflow pins 1.18) and **Python 3.10+** with `pip install pyyaml databricks-sdk`.
  Genie spaces need the `direct` deployment engine, set in `databricks.yml`; the Terraform engine does not support them.
- **A SQL warehouse** in each target workspace, found by name (`warehouse_id.lookup`).
- **The data** the space reads in each environment's catalog. The JSON is written with the dev catalog and rewritten
  for qa and prod, so the same `schema.table` names must exist in every catalog. Only one catalog for everything?
  Give every target the same `catalog`.
- **An account group** for the business users (`business_users_group`), with `USE CATALOG`, `USE SCHEMA` and
  `SELECT` on the data: Genie runs every query as the person asking.

## Steps

```bash
# 0. Log in (one profile per workspace; the same one for all targets if you have one workspace)
databricks auth login --host https://<dev-workspace> -p dev

# 1. Fill in databricks.yml: bundle name, hosts, catalogs, warehouse name, group (not the title/description)

# 2. Export your existing space (id = the last part of its URL .../genie/rooms/<id>)
python scripts/adopt_space.py --space-id <id> -p dev                      # space lives in dev
python scripts/adopt_space.py --space-id <id> --from-target prod -p prod  # space lives in prod

# 3. Checks and the AI-readiness scorecard; --live also checks comments and keys in Unity Catalog
python scripts/check_space.py
python scripts/check_space.py --live dev -p dev

# 4. Your own dev copy of the space, then smoke-test it
databricks bundle deploy -t dev -p dev
export GENIE_SPACE_ID="$(databricks bundle summary -t dev -p dev -o json | jq -r '.resources.genie_spaces.genie_space.id')"
DATABRICKS_CONFIG_PROFILE=dev python tests/smoke_test.py
DATABRICKS_CONFIG_PROFILE=dev python tests/benchmark_gate.py      # needs benchmarks in the space

# 5. Commit, push, set up GitHub (below); merging to main deploys dev and QA and runs the gate
```

## The daily loop

1. Improve the space in your **dev** copy in the Genie UI: instructions, example SQL, benchmarks, entity matching.
2. Bring the edits back into git (bundles never sync them on their own; skip this and the next deploy undoes them):
   `databricks bundle generate genie-space --resource genie_space --force -t dev -p dev`
3. `python scripts/check_space.py`, review `git diff src/`, open a pull request (CI runs the checks).
4. Merge: CI deploys dev, then QA, and runs the benchmark gate.
5. Release: *Actions* → *Deploy Genie Space* → *Run workflow*, target `prod`; a prod reviewer approves.

**Rollback:** revert the commit and release again, or run the workflow on an older tag or commit.

## GitHub settings

| Setting | Value |
|---|---|
| Variables `DEV_DATABRICKS_HOST`, `QA_DATABRICKS_HOST`, `PROD_DATABRICKS_HOST` | workspace URLs (the same three times for one workspace); the same as in `databricks.yml` |
| Secrets `DEV_/QA_/PROD_SP_CLIENT_ID`, `DEV_/QA_/PROD_SP_CLIENT_SECRET` | an OAuth secret of one service principal per environment |
| Environments `dev`, `qa`, `prod` | `prod` (and optionally `qa`) with required reviewers |
| Branch protection on `main` | require the *Validate* check and a review |

Each service principal needs, in its workspace: workspace access, `CAN_USE` on the warehouse, and `USE CATALOG`,
`USE SCHEMA`, `SELECT` on its catalog (the smoke test and the benchmark gate run queries as the deployer).

## Adopting the space your users already use

Today's space keeps its id, URL and conversation history if prod **takes it over** instead of creating a new one:

1. The space's owner shares it with the **prod service principal**, `CAN_MANAGE` (Share dialog in the space).
2. The first prod release runs with the workflow input **`adopt_space_id`** = the space's id. The prod job binds the
   space to the bundle, prints `bundle plan -t prod`, then deploys over it.
3. Later releases leave `adopt_space_id` empty.

The deployment state that remembers this lives in the deployer's bundle folder, which is why prod is bound by CI's
service principal and not from a laptop. Your **dev** space can be adopted from a laptop:
`python scripts/adopt_space.py --space-id <id> --bind dev -p dev` (development mode then renames it `[dev <you>] ...`).

Before the first prod release, check the space's current sharing list: the bundle's `permissions` set it, so add any
group that should keep access to `resources/genie_space.genie_space.yml`.

## Gotchas

- **UI edits don't sync on their own**; run `bundle generate` (step 2 of the loop) before every pull request.
- **Variables are not substituted inside the JSON**: it keeps the dev catalog and CI runs `scripts/set_catalog.py`.
  Locally, `git checkout src/` undoes that script.
- **Every id is 32 lowercase hex characters and id lists are sorted**; `bundle generate` does this,
  `python scripts/check_space.py --fix` repairs hand edits.
- **Version 2 spaces reject version 1 column fields** (`get_example_values`, `build_value_dictionary`).
- **At most 30 data sources per space**; fewer, curated tables or metric views answer better.
- **NEEDS_REVIEW benchmarks don't count** towards the gate's accuracy; the gate fails if none could be graded.
- **Teardown:** `databricks bundle destroy -t <target>` deletes that target's space (an adopted one too).
