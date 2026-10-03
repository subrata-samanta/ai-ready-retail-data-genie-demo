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
**`space/genie_space.yml`**. Nothing else needs editing. **[`docs/OPERATIONS.md`](docs/OPERATIONS.md)** is the
operating model that comes with it: roles, the release and approver checklist, rollback, monitoring and alerts, a
runbook, regular care, and go-live and retirement checklists.

| Stage | What the template gives every project |
|---|---|
| **Develop** | a personal `sandbox` target. Dev Genie UI edits are synced to git as pull requests. `make check / fix / diff / sql` |
| **Review** | `genie-ci` on every pull request: project tests, validation, canonical form, every SQL run on dev data, readiness review, the change list, `bundle plan` for dev; plus a PR template and CODEOWNERS |
| **Release** | dev → qa (drift gate, health checks, benchmark gate, smoke test, quality report for the approver) → approval → prod (drift gate, smoke test with auto-restore). Then a tag and a GitHub release with notes, and links from each environment to its live space |
| **Version control** | release tags, dev UI snapshot tags, a pre-deploy backup of the live space, the deployed commit on every monitoring row |
| **Rollback** | `genie-rollback`: `to` = `previous` or any tag or commit; `what` = `space` (content only) or `bundle` (everything as it was), with approval |
| **Operate** | nightly quality and usage jobs in prod, Delta result tables, an AI/BI dashboard and three SQL alerts (accuracy below the gate, monitoring stale, failing answers), failure and slow-run e-mails, delete protection on the prod space |
| **Govern** | a service principal per environment, groups for users, developers and deployers, prod approvers, branch and tag protection, a production readiness review (`scripts/readiness.py`) |

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
├── databricks.yml                bundle wiring: includes, targets sandbox/dev/qa/prod, permissions, prod schedules,
│                                 prod space protected from deletion
├── resources/
│   ├── genie_space.yml               the Genie space: title, description, warehouse, CAN_RUN for users
│   ├── genie_quality.job.yml         quality job: health checks + benchmarks (gate), smoke test; nightly in prod
│   ├── genie_usage.job.yml           usage job: questions, failures, feedback; nightly in prod
│   ├── genie_monitoring.dashboard.yml  AI/BI dashboard: quality, data health, usage
│   └── genie_alerts.yml              SQL alerts: accuracy below the gate, monitoring stale, failing answers
├── src/                          runs in Databricks
│   ├── genie_quality.py          the quality job: records genie_quality_runs, genie_quality_results, genie_table_health
│   ├── genie_usage.py            the usage job: merges genie_usage_messages
│   └── genie_monitoring.lvdash.json   the dashboard's definition
├── scripts/                      run by CI and by you (`make help`)
│   ├── genie_tools.py            config + space library: load, render, neutralise, diff, validate, hash
│   ├── validate_space.py         static checks, canonical form, change list (PRs), release notes
│   ├── check_sql.py              every example and benchmark SQL on an environment's data
│   ├── readiness.py              production readiness review: MUST checks (block) and SHOULD checks (go-live)
│   ├── check_drift.py            drift gate + backup of the live space, from `bundle plan -o json`
│   ├── sync_from_workspace.py    dev UI edits -> git; import an existing space
│   ├── quality_report.py         the quality job's output in the GitHub run summary, for the approver
│   ├── space_url.py              the deployed space's link (GitHub environment URL)
│   └── build_notebooks.py        notebooks/*_source.py -> .ipynb
├── tests/                        your project's tests, no workspace needed: python tests/run_tests.py
├── notebooks/
│   ├── Genie_Project_Template_source.py   setup + walkthrough (Databricks source format)
│   └── Genie_Project_Template.ipynb       the same notebook, for reading on GitHub
├── docs/OPERATIONS.md            the operating model (roles, release, rollback, monitoring, runbook, checklists)
├── .github/
│   ├── workflows/
│   │   ├── genie-ci.yml          every pull request: tests, validation, SQL on dev data, readiness, change list, plan
│   │   ├── genie-release.yml     main -> dev -> qa (gate) -> approval -> prod; tag + GitHub release
│   │   ├── genie-dev-sync.yml    hourly: dev Genie UI -> pull request + snapshot tag (and its checks)
│   │   └── genie-rollback.yml    by hand: any tag or commit -> any environment
│   ├── pull_request_template.md  the reviewer's checklist
│   └── CODEOWNERS                who must review what (fill in your teams)
├── Makefile                      make check | fix | diff | sql | deploy | gate | sync | readiness ...
└── requirements-dev.txt
```

★ = what you edit for a new project.

## `genie.config.yml` at a glance

| Section | Setting | Example | Used for |
|---|---|---|---|
| 1 Project | `bundle.name`, `business_owner`, `technical_owner`, `support_contact` | `acme-orders-genie` | bundle, service principal names (`<name>-deployer-<env>`), deploy folder; owners in job descriptions and the runbook |
| 2 Space | `space_title`, `space_description` | `Acme Orders Assistant` | what users see; each environment adds `title_suffix` |
| 3 Data | `catalog` (per environment), `schema`, `warehouse_name` | `acme_dev` / `acme_qa` / `acme_prod`, `tpch_demo` | the space names tables `${var.catalog}.${var.schema}.<table>` |
| 4 Access | `users_group`, `developers_group`, `deployers_group`, `deployer_extra_groups` | `acme-genie-users` ... | CAN_RUN; CAN_MANAGE in dev / CAN_VIEW elsewhere; the CI/CD principals; groups they also need (for example the row-level-security group that sees every row) |
| 5 Quality gate | `gate_min_accuracy`, `gate_min_graded`, `gate_max_bad`, `smoke_question` | `0.60`, `3`, `-1` | qa must pass before prod; the smoke test after each prod deploy |
| 6 Monitoring | `monitoring_schema`, `max_data_age_hours`, `monitor_cron`, `monitor_timezone`, `usage_lookback_hours`, `alert_emails`, `alert_subscribers`, `alerts_pause_status`, `alert_max_failed_share` | `genie_monitoring`, `26`, `0 0 6 * * ?` | result tables, freshness check, nightly jobs in prod, failure e-mails, SQL alerts |
| 7 Environments | `targets.<env>`: `workspace.host`, `catalog`, `title_suffix`, `alerts_pause_status`, `alert_emails`, `alert_subscribers` | | per-environment values |

The file is a bundle file, so `databricks bundle validate -t <env>` checks it and shows every resolved value.
`python scripts/genie_tools.py` prints them without a workspace.

**Common layouts**
- **One workspace, one catalog per environment** (the default): set `catalog` per target.
- **One catalog, one schema per environment**: give every target the same `catalog` and its own `schema`. Give
  each target its own `monitoring_schema` too, or the environments share the monitoring tables (every row has its
  `target`; the alerts filter on it).
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
| **Monitoring** | the `genie_quality` job (tables readable, not empty, fresh; benchmarks) and the `genie_usage` job (every question, status, feedback), nightly in prod. Results go to `<catalog>.<monitoring_schema>.genie_*`. Also deployed: the dashboard `<title> · monitoring` and the SQL alerts. E-mails go to `alert_emails` and alert notifications to `alert_subscribers`. The notebook (part I) also shows the evaluation trend, audit events, job runs and workflow runs |
| **Secrets** | GitHub OIDC with a Databricks federation policy (nothing stored), or each service principal's OAuth secret as a GitHub environment secret |

## Everyday commands

`make help` lists them: `make check`, `make fix`, `make diff`, `make sql T=dev`, `make deploy`, `make gate`,
`make sync`, `make readiness`. Underneath:

```bash
python scripts/genie_tools.py                         # the project's settings per environment
python scripts/validate_space.py [--fix]              # check (and canonicalise) the space definition
python scripts/validate_space.py --diff-against main  # what your branch changes in the space
python tests/run_tests.py                             # the project's tests
python scripts/readiness.py                           # production readiness review
python scripts/check_sql.py -t dev                    # every example and benchmark SQL on dev data

databricks bundle validate -t sandbox                 # resolved configuration
databricks bundle deploy   -t sandbox                 # your own copy, to try a change
databricks bundle run genie_quality -t sandbox        # benchmarks + health on your copy
python scripts/sync_from_workspace.py -t sandbox      # bring edits from your copy's UI into the file
databricks bundle destroy  -t sandbox
```

## Status

The template was tested offline. The tests used the real Databricks CLI against a stand-in workspace, which ran
the quality and usage jobs. The notebook was run end to end against stand-ins of the workspace and of GitHub. The
template was also tested as a repository of its own, and with a different data layout. Its workflows pass
actionlint.

The first run in a real workspace and repository is where these still need to be confirmed:
- the GitHub settings calls;
- Unity Catalog grants;
- service-principal sign-in from GitHub Actions;
- how the dashboard renders;
- the SQL alerts' evaluation;
- the system-table queries.
