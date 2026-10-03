# Genie project template

A reusable starting point for running **any** Databricks Genie space as a governed product. It uses git, GitHub
Actions and Declarative Automation Bundles (DAB). One definition lives in git and is promoted
**dev → qa → prod** by a pipeline. Around it you get:

- version control, with a tag and release notes for every release;
- pull-request checks, a benchmark quality gate and a prod approval;
- drift protection and backups before every deploy;
- one-click rollback;
- nightly monitoring, with results in Delta tables and failure e-mails.

Everything that differs between projects is in **one config file, `genie.config.yml`**. The space's content is in
**`space/genie_space.yml`**. Nothing else needs editing.

## Start a new project in five steps

1. **Create the repository.** Make a new GitHub repository and copy the contents of this folder to its **root**
   (GitHub only runs workflows from the root's `.github/workflows`).
2. **Edit `genie.config.yml`.** Change the lines marked `# <- EDIT`: project name, space title and description,
   one catalog per environment, the schema, the SQL warehouse, the three groups, the gate thresholds, the
   smoke-test question, monitoring and alert e-mails.
3. **Add the repository to Databricks as a Git folder.** Open `notebooks/Genie_Project_Template` from it and run
   it with `apply_changes = no`. It prints everything it would change.
4. **Run the notebook again with `apply_changes = yes`.** It does the following:
   - creates the groups and a service principal per environment, and grants them the data and the warehouse;
   - sets up the GitHub repository: environments, approvals, secrets, branch and tag protection;
   - imports your prototype space (widget `import_space_id`) or keeps the example;
   - checks every SQL against dev;
   - ships the first release to prod.
5. **Work the normal way from then on.** Change the space in a pull request, or in the dev Genie UI (the change
   comes back to git as a pull request). Merge to release, approve prod, and watch the monitoring tables.

To try the template as it is, keep the example. It is an "orders assistant" on the TPC-H sample data, which the
notebook copies into each environment's catalog.

## Folder structure

```text
<your repository>/
├── genie.config.yml            ★ THE config file (see the table below)
├── space/
│   └── genie_space.yml         ★ the space's content: tables, instructions, example SQL, SQL snippets,
│                                 trusted functions, sample questions, benchmarks (environment-neutral)
├── databricks.yml                bundle wiring: includes, targets sandbox/dev/qa/prod, permissions, prod schedule
├── resources/
│   ├── genie_space.yml           the Genie space resource: title, description, warehouse, CAN_RUN for users
│   └── genie_quality.job.yml     the quality job: health checks + benchmarks (gate), smoke test, nightly monitor
├── src/
│   └── genie_quality.py          the job's code: runs in Databricks, records results in Delta tables
├── scripts/
│   ├── genie_tools.py            config + space library: load, render, neutralise, diff, validate, hash
│   ├── validate_space.py         static checks, canonical form, change list (PRs), release notes
│   ├── check_drift.py            drift gate + backup of the live space, from `bundle plan -o json`
│   ├── sync_from_workspace.py    dev UI edits -> git; import an existing space
│   └── build_notebooks.py        notebooks/*_source.py -> .ipynb
├── tests/                        your project's tests, no workspace needed: python tests/run_tests.py
├── notebooks/
│   ├── Genie_Project_Template_source.py   setup + walkthrough (Databricks source format)
│   └── Genie_Project_Template.ipynb       the same notebook, for reading on GitHub
├── .github/workflows/
│   ├── genie-ci.yml              every pull request: tests, validation, change list, bundle plan (dev)
│   ├── genie-release.yml         main -> dev -> qa (gate) -> approval -> prod; tag + GitHub release
│   ├── genie-dev-sync.yml        hourly: dev Genie UI -> pull request + snapshot tag
│   └── genie-rollback.yml        by hand: any tag or commit -> any environment
└── requirements-dev.txt
```

★ = what you edit for a new project.

## `genie.config.yml` at a glance

| Section | Setting | Example | Used for |
|---|---|---|---|
| 1 Project | `bundle.name` | `acme-orders-genie` | bundle, service principal names (`<name>-deployer-<env>`), deploy folder |
| 2 Space | `space_title`, `space_description` | `Acme Orders Assistant` | what users see; each environment adds `title_suffix` |
| 3 Data | `catalog` (per environment), `schema`, `warehouse_name` | `acme_dev` / `acme_qa` / `acme_prod`, `tpch_demo` | the space names tables `${var.catalog}.${var.schema}.<table>` |
| 4 Access | `users_group`, `developers_group`, `deployers_group` | `acme-genie-users` ... | CAN_RUN; CAN_MANAGE in dev / CAN_VIEW elsewhere; the CI/CD principals |
| 5 Quality gate | `gate_min_accuracy`, `gate_min_graded`, `gate_max_bad`, `smoke_question` | `0.60`, `3`, `-1` | qa must pass before prod; the smoke test after each prod deploy |
| 6 Monitoring | `monitoring_schema`, `max_data_age_hours`, `monitor_cron`, `monitor_timezone`, `alert_emails` | `genie_monitoring`, `26`, `0 0 6 * * ?` | result tables, freshness check, nightly run in prod, failure e-mails |
| 7 Environments | `targets.<env>`: `workspace.host`, `catalog`, `title_suffix`, `alert_emails` | | per-environment values |

The file is a bundle file, so `databricks bundle validate -t <env>` checks it and shows every resolved value.
`python scripts/genie_tools.py` prints them without a workspace.

**Common layouts**
- **One workspace, one catalog per environment** (the default): set `catalog` per target.
- **One catalog, one schema per environment**: give every target the same `catalog` and its own `schema`.
- **A workspace per environment**: set `workspace.host` per target, and `DATABRICKS_HOST` per GitHub environment
  (the notebook sets it to the workspace it runs in).
- **Several Genie spaces**: one repository per space (recommended), each made from this template.

## How it works

| Need | How the template does it |
|---|---|
| **Environments** | `sandbox` (each developer's own copy, deployed by hand), `dev`, `qa`, `prod` (deployed only by GitHub Actions, each as its own service principal) |
| **Promotion** | merge to `main` runs `genie-release`: dev → qa → approval → prod, always the same commit |
| **Version control** | every change is a reviewed commit. Every prod release is a tag `genie-prod-<UTC>-<sha>` plus a GitHub release with the change list. Every dev UI state is a tag `genie-dev-snapshot-<time>`. Every quality result records the deployed commit |
| **Gates** | `genie-ci` on pull requests. In qa: drift gate, health checks, benchmark gate, smoke test. Then a person approves prod |
| **UI edits** | dev: exported to git hourly as a pull request. qa/prod: the release refuses to overwrite them (exit 2) until you adopt them or rerun with `allow_drift` |
| **Safety** | the live space is backed up (build artifact) before every deploy. A plan that would delete or recreate the space is refused unless `allow_destroy`. A failed prod smoke test restores the previous release |
| **Rollback** | `genie-rollback`: `to` = `previous` or any tag or commit; `what` = `space` (content only) or `bundle` (everything as it was) |
| **Monitoring** | the `genie_quality` job: health (tables readable, not empty, fresh) and benchmarks; nightly in prod. Results go to `<catalog>.<monitoring_schema>.genie_quality_runs`, `genie_quality_results` and `genie_table_health`; failure e-mails go to `alert_emails`. The notebook (part I) also shows the evaluation trend, usage, audit events, job runs and workflow runs |
| **Secrets** | GitHub OIDC with a Databricks federation policy (nothing stored), or each service principal's OAuth secret as a GitHub environment secret |

## Everyday commands

```bash
python scripts/genie_tools.py                         # the project's settings per environment
python scripts/validate_space.py [--fix]              # check (and canonicalise) the space definition
python scripts/validate_space.py --diff-against main  # what your branch changes in the space
python tests/run_tests.py                             # the project's tests

databricks bundle validate -t sandbox                 # resolved configuration
databricks bundle deploy   -t sandbox                 # your own copy, to try a change
databricks bundle run genie_quality -t sandbox        # benchmarks + health on your copy
python scripts/sync_from_workspace.py -t sandbox      # bring edits from your copy's UI into the file
databricks bundle destroy  -t sandbox
```

## Status

The template was tested offline. The tests used the real Databricks CLI against a stand-in workspace, and the
notebook was run end to end against stand-ins of the workspace and of GitHub. Its workflows pass actionlint.

The first run in a real workspace and repository is where these still need to be confirmed:
- the GitHub settings calls;
- Unity Catalog grants;
- service-principal sign-in from GitHub Actions;
- the system-table queries.
