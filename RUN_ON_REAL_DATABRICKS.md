# Run it on a real Databricks workspace, then do the same for your own Genie space

Two parts:

- **Part 1** runs the FreshCart demo end to end in a real workspace, so you can see every piece working.
- **Part 2** applies the same approach to your company's Genie space, with the template in
  [`genie-space-template/`](genie-space-template/).

---

## Part 1 — FreshCart on a real workspace (about 1 hour)

### What you need

- A workspace with **Unity Catalog** and **serverless compute** (a new trial workspace has both).
- To be a **workspace admin** who may **create catalogs**: the notebook creates groups, catalogs and a volume.
  In a company workspace where you can't, ask an admin for a sandbox workspace, or for a catalog plus the groups
  listed in [docs/09](docs/09-run-on-databricks.md) and run the CLI route below.

### Steps

1. **Add the repository as a Git folder.** *Workspace* → your user folder → *Create* → *Git folder* → URL
   `https://github.com/subrata-samanta/ai-ready-retail-data-genie-demo`, provider GitHub, branch `main`.
   (Private repository: connect GitHub first in *Settings* → *Linked accounts*.)
2. **Open** `notebooks/FreshCart_End_to_End_on_Databricks` from that folder and attach **Serverless** compute.
3. **Leave the widgets at their defaults** for the first run: environment `dev`, catalog and warehouse blank,
   as-of date `2026-09-27`, promote `no`, clean up `no`.
4. **Run all.** 20 to 40 minutes, mostly the pipeline and the benchmark evaluation. Every cell can be re-run.

### What "working as expected" looks like

| Notebook step | Check |
|---|---|
| 8 refresh job | the job run succeeds; the `monitor` task's health checks pass |
| 10 reference comparison | every benchmark answer on Databricks matches the local, tested reference |
| 11 deploy Genie bundle | prints a link to the space `[dev <you>] FreshCart Sales & Stock Assistant` |
| 12 ask Genie | questions come back with SQL and rows |
| 13 quality gate | the benchmark accuracy is at or above 60% and the job passes |

Then open the space and ask, for example, *"What were net sales and margin by region last week?"* or
*"Which categories rely most on promotions this quarter?"*. The expected answers to all benchmark questions are in
[docs/07-genie-agent.md](docs/07-genie-agent.md#benchmarks-the-expected-answers).

Optional, in the same notebook: step 14 (catch a UI edit and roll it back), `promote = yes` (qa and prod), step 16
(service principals for CI/CD), `cleanup = yes` (removes everything again).

### The same from a laptop (CLI route)

```bash
pip install -r requirements.txt databricks-sdk
databricks auth login --host https://<workspace> -p freshcart        # CLI >= 1.14
export DATABRICKS_CONFIG_PROFILE=freshcart
# once, as an admin: catalog, landing volume, raw files (groups: see docs/09)
databricks catalogs create freshcart_dev
databricks schemas create landing freshcart_dev
databricks volumes create freshcart_dev landing raw MANAGED
databricks fs cp -r data/raw dbfs:/Volumes/freshcart_dev/landing/raw
# data bundle, then the simple Genie bundle with its smoke test and benchmark gate
(cd databricks && databricks bundle deploy -t dev && databricks bundle run freshcart_refresh -t dev)
cd genie-as-code-review
databricks bundle deploy -t dev
export GENIE_SPACE_ID="$(databricks bundle summary -t dev -o json | jq -r '.resources.genie_spaces.freshcart_assistant.id')"
python tests/test_genie.py && python tests/benchmark_gate.py
```

If your warehouse is not called "Serverless Starter Warehouse", change `lookup.warehouse` in
`genie-as-code-review/databricks.yml` (and `genie_bundle/databricks.yml`).

---

## Part 2 — the same approach for your company's Genie space

The approach has two halves, and both matter: **AI-ready data** (what made FreshCart's answers accurate) and
**the space as code** (what makes changes safe to ship).

### Step 1 — Decide your environments

| Question | Typical answer | Where it goes |
|---|---|---|
| How many workspaces? | 1 (shared) or 3 (dev, qa, prod) | `workspace.host` per target in `databricks.yml` |
| Which catalog per environment? | e.g. `sales_dev`, `sales_qa`, `sales` (or one for all) | `catalog` per target |
| Which space do users use today? | usually the one that becomes **prod** | adopted in step 9 |
| Who may ask questions? | an account group, e.g. `sales-genie-users` | `business_users_group` |

### Step 2 — Make the data AI-ready (FreshCart's design decisions, applied to your tables)

1. **Model for questions:** a star schema (facts + dimensions) at a declared grain, business names, decoded codes.
2. **Comments on every table and column**, keys declared with `RELY` (Genie uses both to pick columns and joins).
3. **Each KPI defined once** in a Unity Catalog **metric view**; tricky logic (YoY, like-for-like) as a **trusted function**.
4. **Govern in the data:** grants for the users' group, row filters on facts, no personal data in what Genie reads.

[`genie-space-template/sql/uc_metadata_template.sql`](genie-space-template/sql/uc_metadata_template.sql) has the SQL
for each of these; [`databricks/`](databricks/) is FreshCart's full version.

### Step 3 — Create your repository from the template

```bash
mkdir sales-genie && cp -r ai-ready-retail-data-genie-demo/genie-space-template/. sales-genie/
cd sales-genie && git init -b main
pip install pyyaml databricks-sdk                     # and the Databricks CLI >= 1.14
```

### Step 4 — Log in and fill in `databricks.yml`

```bash
databricks auth login --host https://<dev-workspace>  -p dev
databricks auth login --host https://<prod-workspace> -p prod     # if prod is another workspace
```

Replace every `CHANGE_ME` except the title and description: bundle name, hosts, catalogs, warehouse name, group.

### Step 5 — Export your existing space into code

The space id is the last part of its URL (`https://<workspace>/genie/rooms/<space-id>`).

```bash
python scripts/adopt_space.py --space-id <space-id> --from-target prod -p prod    # where the space lives
```

This writes `src/genie_space.geniespace.json`, copies the title and description into `databricks.yml`, and moves
the tables onto your dev catalog. Commit the result: it is your baseline.

### Step 6 — Check it

```bash
python scripts/check_space.py                         # format, ids, catalogs + AI-readiness scorecard
python scripts/check_space.py --live dev -p dev   # comments and keys in Unity Catalog
```

Close the gaps the scorecard shows. Most valuable first: **10-30 benchmark questions with expected SQL** (the QA
gate needs them), example SQL for the top questions, general instructions about your business.

### Step 7 — Deploy your dev copy and test it

```bash
databricks bundle deploy -t dev -p dev
export GENIE_SPACE_ID="$(databricks bundle summary -t dev -p dev -o json | jq -r '.resources.genie_spaces.genie_space.id')"
DATABRICKS_CONFIG_PROFILE=dev python tests/smoke_test.py
DATABRICKS_CONFIG_PROFILE=dev python tests/benchmark_gate.py
```

Curate the dev space in the UI, then `databricks bundle generate genie-space --resource genie_space --force -t dev -p dev`
and commit the JSON diff. Repeat until the gate passes comfortably.

### Step 8 — Set up CI/CD

1. **One service principal per environment** (*Settings* → *Identity and access* → *Service principals*), each with an
   **OAuth secret**, workspace access, `CAN_USE` on the warehouse and `USE CATALOG` / `USE SCHEMA` / `SELECT` on its catalog.
2. **A GitHub repository** with the variables, secrets and environments in the
   [template README](genie-space-template/README.md#github-settings); `prod` with required reviewers.
3. Push to `main`: CI validates, deploys dev and QA, and runs the benchmark gate.

### Step 9 — First prod release

1. The current space's owner shares it with the **prod service principal**, `CAN_MANAGE`.
2. Check its sharing list and add every group that must keep access to `resources/genie_space.genie_space.yml`.
3. *Actions* → *Deploy Genie Space* → *Run workflow*: target `prod`, **`adopt_space_id`** = the space's id.
   After approval, prod takes over the space in place (same URL, history kept), deploys and smoke-tests it.

From then on: edit in dev → `bundle generate` → pull request → merge (dev + QA gate) → run the workflow for prod.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `genie_spaces is not supported` | CLI older than 1.14, or `engine: direct` missing |
| `lookup ... warehouse not found` | the warehouse name in `databricks.yml` differs in that workspace |
| smoke test FAIL with 0 rows | the deployer lacks `SELECT`, the tables are empty in that catalog, or a table name differs |
| benchmark gate "no benchmark could be graded" | the space has no benchmarks with SQL answers |
| a second prod space appeared | prod was deployed before `adopt_space_id` bound the old one; destroy the new one, re-run with the id |
| UI changes vanished after a deploy | they were never captured with `bundle generate` |
| `config host mismatch` | `DATABRICKS_HOST` differs from the target's `workspace.host` |
