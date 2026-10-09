# FreshCart Sales & Stock Assistant — Genie Space as Code (simple version)

A Databricks Asset Bundle that manages the FreshCart **AI/BI Genie space as a versioned,
promotable resource**, with as few moving parts as possible: one JSON file, one resource, one
workflow. Author the space in **dev**, curate it in the UI, capture the changes back into source,
test it, and promote the *same artifact* to **QA** and **Prod**.

This is the lightweight alternative to [`../genie_bundle`](../genie_bundle/), which deploys the same
FreshCart space with more safeguards: a drift check before every deploy, the quality gate as a job
inside Databricks, an hourly sync of UI edits, and a rollback workflow. Start here; move to that one
when you need those safeguards.

```
databricks.yml                                bundle + engine:direct + dev/qa/prod targets
resources/freshcart_assistant.genie_space.yml the genie_spaces resource (-> file_path, warehouse, permissions)
src/freshcart_assistant.geniespace.json       serialized space: data sources, instructions, sample Qs,
                                              example SQL, trusted function, benchmarks
scripts/set_catalog.sh                        points the space at a target's catalog before deploying
tests/test_genie.py                           Conversation-API smoke test (promotion gate)
tests/benchmark_gate.py                       Genie benchmark evaluation gate (QA)
.github/workflows/deploy.yml                  CI/CD: validate -> dev -> QA (benchmark gate) -> prod
```

## What the space contains

The content is a subset of the full FreshCart space, so both bundles give the same answers:

- **Data sources:** the metric views `semantic.sales_metrics` and `semantic.inventory_metrics`, and the
  tables `gold.dim_store` and `gold.dim_product` (with entity matching on names, regions and categories).
- **Instructions:** the FreshCart business context (4-5-4 fiscal calendar, USD, when to ask a
  clarification question, how stock works).
- **4 trusted example queries** and the trusted function `semantic.fn_like_for_like_sales`.
- **4 sample questions** and **8 benchmark questions** with their expected SQL.

## Prerequisites

- **Databricks CLI ≥ 1.14** (tested here with 1.18 and 1.20) and the **`direct` deployment engine**
  (set in `databricks.yml`). The Terraform engine does **not** support Genie spaces.
- **The FreshCart data** in each target workspace: deploy and run the data bundle in
  [`../databricks`](../databricks/) first. It writes each environment to its own catalog:

  | Target | Catalog |
  |---|---|
  | dev | `freshcart_dev` |
  | qa | `freshcart_qa` |
  | prod | `freshcart` |

- A **serverless SQL warehouse** in each target workspace. The bundle resolves it by name via `lookup`;
  the default is `"Serverless Starter Warehouse"`. Change it in `databricks.yml` if yours differs.
- The group **`freshcart-business-users`** (change `business_users_group` if yours differs). It gets
  `CAN_RUN` on the space. Its members also need `SELECT` on the FreshCart tables, because Genie runs
  queries as the person asking (the data bundle's `governance/01_security.sql` grants this).
- For the tests: `pip install databricks-sdk`, and `jq` to read the bundle summary.

Authentication is not in `databricks.yml`: locally use `-p <profile>` (or `DATABRICKS_CONFIG_PROFILE`).
Uncomment and fill in `host:` under each target once you know your workspace URLs (safer).

## One JSON, three catalogs

The JSON is written with the **dev catalog** (`freshcart_dev.gold...`, `freshcart_dev.semantic...`),
exactly as `bundle generate` exports it from the dev space, so syncing UI edits gives a clean diff.
Bundles do **not** substitute variables inside a `file_path` file, so before deploying to qa or prod,
CI runs `scripts/set_catalog.sh <target>`. It reads the target's `catalog` variable from
`databricks.yml` and rewrites the catalog in its fresh checkout. Never commit a rewritten file; locally,
`git checkout src/` undoes it.

## Two ways to run this

Pick based on whether the Dev space already exists and needs to stay alive.

| | **A — Bundle-owned from day one** | **B — Adopt an existing Dev space** |
|---|---|---|
| Use when | Greenfield, or you can recreate Dev | A curated Dev space already exists and must stay running |
| Who owns Dev | The bundle (created by it) | The bundle (adopted via `bind`) |
| How QA/Prod are created | `deploy -t qa/prod` after Dev | Same — `deploy -t qa/prod` after bind |
| Source of truth | Git (bundle JSON) | Git (bundle JSON, seeded from `generate --existing-id`) |
| Dev history preserved | N/A — bundle created it | ✅ Yes — bind adopts the existing space in-place |

---

### Approach A — Bundle-owned from day one (the standard MLOps loop)

The bundle creates and owns the Dev space; you curate it, capture edits back to
source, and promote the *same artifact* forward.

```bash
# 1. Validate syntax (resource recognized, variables resolve)
databricks bundle validate -t dev

# 2. Deploy to dev (development mode names it "[dev <you>] FreshCart Sales & Stock Assistant")
databricks bundle deploy -t dev

# 3. Curate in the Databricks UI — refine instructions, add example SQL,
#    add benchmark questions.

# 4. Capture those UI edits back into source — bundles do NOT auto-sync them.
#    This rewrites only the JSON, in the same format, so the diff shows just your edits:
databricks bundle generate genie-space --resource freshcart_assistant --force
#    (add --watch to poll continuously while you iterate)

# 5. Review the diff like any code change, then commit:
git diff src/freshcart_assistant.geniespace.json

# 6. Smoke-test the dev space:
export GENIE_SPACE_ID="$(databricks bundle summary -t dev -o json \
  | jq -r '.resources.genie_spaces.freshcart_assistant.id')"
python tests/test_genie.py

# 7. Push to main: CI deploys dev and QA and runs the benchmark gate.
#    Promote to prod with a manual run of the workflow (target: prod).
```

---

### Approach B — Adopt an existing Dev space via bind

For a curated Dev space that is already live and must not be recreated. The bundle
**adopts it in-place** (same space ID, same URL, chat history preserved), then
promotes to QA/Prod as normal. `bundle deployment bind` works for `genie_spaces`
(tested here with CLI 1.18 and 1.20).

```bash
# 1. Pull the existing Dev space's definition into bundle source.
#    Writes src/freshcart_assistant.geniespace.json + resources/freshcart_assistant.genie_space.yml
databricks bundle generate genie-space --existing-id <dev-space-id> --key freshcart_assistant --force

# 2. Fix up the generated resource YAML (see "After generate" below).

# 3. Bind — the bundle adopts the existing space. Next deploy updates it in-place.
databricks bundle deployment bind freshcart_assistant <dev-space-id> --auto-approve
#    → "Successfully bound genie_space with an id '...'"

# 4. Check what the next deploy changes:
databricks bundle plan -t dev
#    → Plan: 0 to add, 1 to change, ...  The dev target runs in development mode, so the
#      deploy renames the space to "[dev <you>] ...". Expect that change; anything else is drift.

# 5. From here the loop is identical to Approach A.
```

**After `generate --existing-id` — three fixups before binding:**
1. **Warehouse** — `generate` writes Dev's literal `warehouse_id` into the YAML.
   Replace it with `${var.warehouse_id}` so QA/Prod resolve their own warehouse
   by name via the `lookup`.
2. **`parent_path`** — `generate` writes the space's current folder. Remove this line;
   the bundle uses the correct workspace path per target on deploy.
3. **Title and permissions** — `generate` writes the live space's title and description, and no
   permissions. Restore `title` (with `${var.title_suffix}`) and `permissions` from this
   repository's version of the YAML.

The table names in the JSON need no fixup as long as you export from the dev space
(catalog `freshcart_dev`); see "One JSON, three catalogs".

---

## CI/CD (`.github/workflows/deploy.yml`)

The workflow expects this folder to be the root of its repository (copy it out), with:

| GitHub setting | Value |
|---|---|
| Variables `DEV_DATABRICKS_HOST`, `QA_DATABRICKS_HOST`, `PROD_DATABRICKS_HOST` | workspace URLs |
| Secrets `DEV_/QA_/PROD_SP_CLIENT_ID`, `DEV_/QA_/PROD_SP_CLIENT_SECRET` | one service principal per environment (OAuth) |
| Environments `dev`, `qa`, `prod` | `prod` (and optionally `qa`) with required reviewers |

| Trigger | Jobs |
|---|---|
| Pull request | validate |
| Push to `main` | validate → dev + smoke test → QA + benchmark gate |
| Manual run, target `dev` / `qa` / `prod` | the same chain up to that target; prod also waits for approval and is smoke-tested |

Prod is only deployed by a manual run, and only after the QA gate passed on the same commit.
The gate fails when accuracy on the automatically graded benchmarks is below 60%
(`ACCURACY_THRESHOLD`) or when nothing could be graded; benchmarks Genie can't grade
(NEEDS_REVIEW) are listed but don't count.

## Testing

From the repository root, `python tests/run_tests.py` runs `tests/test_simple_genie_bundle.py`:

- the JSON is in `bundle generate`'s format, sorted, valid, and uses only the dev catalog;
- every example and benchmark SQL runs on the local FreshCart warehouse;
- the target catalogs match the data bundle's;
- the workflow runs the right jobs for each trigger;
- with the Databricks CLI installed, the workflow's own steps run job by job against a stand-in
  workspace (`tests/fake_workspace.py`): deploy to dev, qa and prod on their own catalogs, the smoke
  test, the benchmark gate (passing, and failing after a bad UI edit), and the `generate` round trip.

The stand-in workspace answers questions with the space's trusted example SQL; it does not test
Genie itself. Run the smoke test and the gate against a real workspace before relying on them.

Teardown: `databricks bundle destroy -t <target>` removes the bundle-managed space for that target.
The FreshCart data is managed by the data bundle in `../databricks`.

## Gotchas (the real-world caveats)

- **UI edits don't auto-sync.** They live in the workspace until you run
  `bundle generate genie-space --resource freshcart_assistant --force`. Make this step part of your
  workflow; skip it and curations are lost on the next deploy.
- **Variables are not resolved inside the JSON.** Hence the dev catalog in the file and
  `scripts/set_catalog.sh` for qa/prod.
- **IDs are required, validated, and must be sorted.** Every object in `sample_questions`,
  `text_instructions`, `example_question_sqls`, `sql_functions` and `benchmarks` needs a lowercase
  32-hex id (`uuid.uuid4().hex`), and id-bearing lists are sorted by id. `bundle generate` handles
  this; hand edits need care (the repository test checks it).
- **Version 2 spaces reject version 1 column fields** (`get_example_values`, `build_value_dictionary`);
  use `enable_format_assistance` and `enable_entity_matching`.
- **Development mode renames the space** to `[dev <deployer>] ...`, including a space adopted with `bind`.
- **`direct` engine + CLI ≥ 1.14** are hard requirements for this bundle.
