# Finance & P&L Analytics — Genie Space as Code

A Databricks Asset Bundle that manages an **AI/BI Genie space as a versioned,
promotable resource**. The point is the lifecycle, not the chart: author the
space in **dev**, curate it in the UI, capture the changes back into source,
test it, and promote the *same artifact* to **QA** and **Prod** — all governed
and reproducible across many workspaces.

```
databricks.yml                          bundle + engine:direct + dev/qa/prod targets
resources/finance_pnl.genie_space.yml   the genie_spaces resource (-> file_path, warehouse)
src/finance_pnl.geniespace.json         serialized space: tables, instructions, sample Qs, golden SQL
tests/test_genie.py                     Conversation-API smoke test (promotion gate)
.github/workflows/deploy.yml            CI/CD: validate -> dev -> QA (benchmark gate) -> prod
```

## Prerequisites

- **Databricks CLI ≥ 1.3.0** — `genie_spaces` resources require it.
- The **`direct` deployment engine** (set in `databricks.yml`). The Terraform
  engine does **not** support Genie spaces.
- A **serverless SQL warehouse** in each target workspace. The bundle resolves it
  by name via `lookup` — the default is `"Shared Endpoint"`; change it in
  `databricks.yml` if yours differs.
- Unity Catalog privileges on the `finance_demo.pnl` catalog/schema.
- For the test: `pip install databricks-sdk` (and `jq` to read the bundle summary).

Fill in the real workspace hosts/profiles under `targets:` in `databricks.yml`
before deploying (they ship as `<your-…-workspace>` placeholders).

## Data prerequisite (per target workspace)

The bundle deploys **only** the Genie space — it does not create tables. The
`finance_demo.pnl` schema and its tables (`gl_actuals`, `budget_plan`,
`product_hierarchy`, `cost_centers`) must already exist in each target workspace
you deploy to.

> The catalog/schema names must be **identical across dev/QA/prod**, because the
> Genie space references fully-qualified table names that aren't templated. If you
> rename them, update the `identifier` values in `src/finance_pnl.geniespace.json`.

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

# 2. Deploy to dev (development mode prefixes the name per user)
databricks bundle deploy -t dev

# 3. Curate in the Databricks UI — refine instructions, add data sources,
#    add benchmark questions, certify answers.

# 4. Capture those UI edits back into source — DABs do NOT auto-sync them:
databricks bundle generate genie-space --resource finance_pnl --force
#    (add --watch to poll continuously while you iterate)

# 5. Review the diff like any code change, then commit:
git diff src/finance_pnl.geniespace.json

# 6. Gate promotion on the smoke test against the dev space:
export GENIE_SPACE_ID="$(databricks bundle summary -t dev -o json \
  | jq -r '.resources.genie_spaces.finance_pnl.id')"
python tests/test_genie.py

# 7. Promote the same artifact downstream:
databricks bundle deploy -t qa
databricks bundle deploy -t prod
```

---

### Approach B — Adopt an existing Dev space via bind

For a curated Dev space that is already live and must not be recreated. The bundle
**adopts it in-place** — same space ID, same URL, chat history preserved — then
promotes to QA/Prod as normal.

> **Note:** `bundle deployment bind` for `genie_spaces` works as of CLI v1.5.0,
> confirmed by testing. The official docs list does not yet include `genie_spaces`
> but the command succeeds in practice.

```bash
# 1. Pull the existing Dev space's definition into bundle source.
#    Writes src/<key>.geniespace.json + resources/<key>.genie_space.yml
databricks bundle generate genie-space --existing-id <dev-space-id> --key finance_pnl

# 2. Fix up the generated resource YAML (see "After generate" below).

# 3. Bind — the bundle adopts the existing space. Next deploy updates it in-place.
databricks bundle deployment bind finance_pnl <dev-space-id> --auto-approve
#    → "Successfully bound genie_space with id '...'"

# 4. Confirm zero drift between source and the live space:
databricks bundle plan
#    → Plan: 0 to add, 0 to change, 0 to delete, 1 unchanged

# 5. From here the loop is identical to Approach A:
#    Curate in UI → generate --resource finance_pnl --force → commit → test → promote
databricks bundle deploy -t qa
databricks bundle deploy -t prod
```

**After `generate` — two fixups before binding:**
1. **Warehouse** — `generate` bakes Dev's literal `warehouse_id` into the YAML.
   Replace it with `${var.warehouse_id}` so QA/Prod resolve their own warehouse
   by name via the `lookup`.
2. **`parent_path`** — `generate` writes a hardcoded path pointing at your personal
   `.bundle` dev directory. Remove this line entirely; the bundle will use the
   correct workspace path per target on deploy.
3. **Table identifiers** — these live in the JSON and aren't templated. If QA/Prod
   use the **same** catalog/schema names → no change. If **different** → find/replace
   in `src/finance_pnl.geniespace.json` before deploying downstream.

---

Teardown: `databricks bundle destroy -t <target>` removes the bundle-managed space
for that target. Drop the `finance_demo.pnl` schema separately if you also
want to remove the underlying tables.

## Gotchas (call these out — they're the real-world caveats)

- **`bundle deployment bind` works for Genie spaces (CLI ≥ 1.5.0), despite the docs.**
  The official bind docs omit `genie_spaces` from the supported list, but the command
  succeeds. Use Approach B to adopt an existing space rather than recreating it.
- **UI edits don't auto-sync.** They live in the workspace until you run
  `bundle generate genie-space --resource <key> --force`. Make this step part of your
  workflow — skip it and curations get lost on the next deploy.
- **`generate` bakes in a hardcoded `parent_path` and `warehouse_id`.** Always clean
  these up after generating: remove `parent_path`, replace `warehouse_id` with
  `${var.warehouse_id}`.
- **IDs are required, validated, and must be sorted.** Every object in
  `sample_questions`, `text_instructions`, `example_question_sqls`, and `benchmarks`
  needs a lowercase 32-hex UUID (`uuid.uuid4().hex`). All id-bearing lists must be
  sorted ascending by id. `bundle generate` handles this automatically — hand-editing
  the JSON requires care.
- **Table identifiers must resolve in every target** — hence identical catalog/schema
  names across dev/QA/prod (or tokenize them yourself before deploying downstream).
- **`direct` engine + CLI ≥ 1.3.0** are hard requirements for `genie_spaces`.
