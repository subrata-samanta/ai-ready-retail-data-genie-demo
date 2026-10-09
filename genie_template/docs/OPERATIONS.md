# Operating the Genie space

This guide covers how this project is developed, released, monitored and kept healthy. For a step-by-step
explanation of the CI/CD process from scratch (version history, raising pull requests, syncing the dev Genie UI,
promotion dev → qa → prod), read `notebooks/Genie_CICD_Guide`. It is the same for every
project made from the template. The names below (`gate_min_accuracy`, `alert_emails`, ...) are settings in
`genie.config.yml`.

## 1. Lifecycle

```text
 DEVELOP ──────────────► REVIEW ─────────────► RELEASE ───────────────────────────► OPERATE ─────────► IMPROVE
 sandbox / dev Genie UI   pull request          main → dev → qa → approval → prod      nightly quality    failed & thumbs-down
 space/genie_space.yml    genie-ci: tests,      drift gate, quality gate, smoke,       + usage jobs,      questions become
                          SQL on dev data,      tag + GitHub release                   dashboard, alerts  benchmarks and examples
                          readiness, change list
```

| Stage | Who | Where | Leaves behind |
|---|---|---|---|
| Develop | developers | `sandbox` target (own copy), or the dev space in the Genie UI | a branch, or a `genie/dev-sync` pull request + `genie-dev-snapshot-*` tag |
| Review | developers, code owners | pull request | the change list, readiness review and SQL check in the check summary |
| Release | GitHub Actions; an approver for prod | `genie-release` | backups (artifacts), qa quality report, tag `genie-prod-*`, GitHub release notes |
| Operate | technical owner | dashboard, alerts, e-mails | rows in `<catalog>.<monitoring_schema>.genie_*` |
| Improve | business and technical owner | usage dashboard → pull request | new benchmarks, instructions, example SQL |

## 2. Roles

| Role | Setting | Can |
|---|---|---|
| Business owner | `business_owner` | decides what the space must answer; approves benchmark changes |
| Technical owner | `technical_owner` | runs this guide; receives alerts (`alert_emails`, `alert_subscribers`) |
| Developers | `developers_group` | change the space through pull requests; edit the dev space in the UI (CAN_MANAGE in dev, CAN_VIEW in qa and prod) |
| Approvers | required reviewers of the GitHub environment `prod` | approve each prod deployment, after reading the qa quality report |
| Users | `users_group` | ask questions (CAN_RUN), with their own data permissions |
| Deployers | `deployers_group`: one service principal per environment | the only identities that deploy dev, qa and prod |

## 3. Environments and promotion rules

| | sandbox | dev | qa | prod |
|---|---|---|---|---|
| Deployed by | you, by hand | GitHub Actions | GitHub Actions | GitHub Actions, after approval |
| When | any time | every merge to `main` | after dev | after qa passed and an approver approved |
| Gates | — | — | drift, health, benchmarks ≥ `gate_min_accuracy`, smoke | drift, smoke (auto-restore on failure) |
| UI edits | yours | synced to git hourly | blocked: release stops | blocked: release stops |
| Schedules, alerts | — | — | — | quality and usage jobs (`monitor_cron`), SQL alerts |

Rules: the same commit is deployed to every environment. Nothing reaches qa or prod except through `main`.
Nobody edits qa or prod in the UI.

## 4. Release (approver's checklist)

`genie-release` stops at **prod** and waits. Before approving, open the run and check:

1. **qa quality report** (run summary): accuracy at or above the gate. Read every wrong answer: is it a real
   regression or an ambiguous benchmark?
2. **What changes**: the change list of the pull request(s) since the last release (the previous GitHub release
   and this run's commit).
3. **No drift warning** in qa.
4. **Timing**: not during a business-critical window.

After prod deploys, the smoke test runs. If it fails, the previous release is restored automatically and the run
fails. A successful release creates the tag `genie-prod-<UTC>-<sha>` and a GitHub release with notes.

**Hotfix**: there is no shortcut past qa. Open a small pull request, merge it, and approve as above. The release
takes as long as the gate, usually minutes. If prod is broken *now*, roll back first (section 6), then fix.

## 5. Versions

| Question | Answer |
|---|---|
| What is in prod? | the tag `genie-deployed-prod` (moved by every deploy and rollback) and the latest `genie-prod-*` release; the notebooks prove it by comparing content hashes |
| What changed in a release? | its GitHub release notes; `python scripts/validate_space.py --release-notes <old> <new>` |
| What did the dev UI look like on day X? | the `genie-dev-snapshot-*` tag of that day |
| Which version produced a quality result? | the `version` (git commit) column of every monitoring table |
| What did prod look like just before a deploy? | the `genie-prod-pre-deploy-<run>` artifact of that release run (kept 400 days) |

## 6. Rollback

Run **genie-rollback** (Actions tab):

| Field | Value |
|---|---|
| environment | `prod` (or qa, dev) |
| to | `previous` (the release before the latest), or any tag or commit |
| what | `space`: only the space's content, with today's settings. `bundle`: everything as it was: settings, jobs, dashboard, alerts |
| reason | kept in the run log |

The rollback waits for the same approval as a release, backs up the live space, deploys, and smoke-tests. Then
**fix forward**: revert the bad change on `main` with a pull request. Otherwise the next release brings it back.

## 7. Monitoring and alerts

| Signal | Source | Threshold (`genie.config.yml`) | Who hears |
|---|---|---|---|
| Benchmark accuracy | quality job, nightly in prod → `genie_quality_runs`, `genie_quality_results` | `gate_min_accuracy` | job e-mail (`alert_emails`); SQL alert *accuracy below the gate* (`alert_subscribers`) |
| Tables readable, not empty, fresh | quality job → `genie_table_health` | `max_data_age_hours` | job e-mail |
| Monitoring itself running | SQL alert *monitoring stale* | no quality run for 26 h | `alert_subscribers` |
| Failed answers, thumbs down | usage job, nightly → `genie_usage_messages` | `alert_max_failed_share` of the last day | SQL alert *failing answers* |
| Job too slow | job health rule | over 1 hour | `alert_emails` |
| Deployments | GitHub Actions runs, environments, releases | any failure | GitHub notifications |

The **dashboard** `<space title> · monitoring` (deployed with the bundle) shows quality, data health and usage
for its environment. The SQL alerts run where `alerts_pause_status` is `UNPAUSED` (prod).

## 8. Runbook

| Signal | Meaning | Do this |
|---|---|---|
| `genie-ci` red | a test, the definition, the readiness review or a SQL on dev data failed | the check names the item; `make fix` for canonical form |
| Release stopped: **drift** (qa/prod) | someone edited the space in the UI | adopt: `python scripts/sync_from_workspace.py -t <env>` and open a pull request; or overwrite: run `genie-release` with *allow_drift* (the edit stays in the backup artifact) |
| Release stopped: **quality gate** | accuracy below the gate, or a table unhealthy | the qa report lists each wrong answer with Genie's SQL; add context (instructions, examples, column descriptions) or fix an ambiguous benchmark; check `genie_table_health` |
| Release stopped: plan **recreates** the space | a change Genie cannot apply in place | users would lose their conversations; avoid it. If it is intended: remove `lifecycle.prevent_destroy` (prod) and release with *allow_destroy* |
| Prod **smoke test** failed | Genie does not answer in prod | the previous release was restored; reproduce in qa |
| Alert **accuracy below the gate** | answers degraded on live data | compare `version` with the last good run: a release → roll back; the data changed → check health, update instructions |
| Alert **monitoring stale** | the nightly job did not run | Jobs UI: `<space title> · quality`; fix the failure (permissions, warehouse) and rerun |
| Alert **failing answers** | users are failing | dashboard *Usage* → *Questions to review*; turn them into benchmarks and examples |
| Job e-mail: health check failed | a table is missing, empty or stale | tell the data owner; Genie keeps working, but on old data |

## 9. Regular care

| When | What |
|---|---|
| Weekly | review the open `genie/dev-sync` pull request (merge or close); read *Questions to review* |
| Monthly | accuracy trend: raise `gate_min_accuracy` when the space is consistently better; add 2–3 benchmarks from real questions |
| Quarterly | access review: members of `users_group`, `developers_group`, approvers; rotate the service principals' OAuth secrets (re-run notebook parts B2 and B3) or move to OIDC |
| Each CLI release | update `DATABRICKS_CLI_VERSION` in the workflows in a pull request; the checks validate it |

## 10. Go-live checklist

`python scripts/readiness.py` checks most of this; every pull request shows it.

- [ ] Owners named (`business_owner`, `technical_owner`, `support_contact`), groups and catalogs are the project's
- [ ] At least 10 benchmarks, covering every kind of question; the smoke question is one of them
- [ ] qa gate passes at `gate_min_accuracy` ≥ 0.6
- [ ] `alert_emails` and `alert_subscribers` set for prod; `max_data_age_hours` matches the data's load interval
- [ ] Approvers set on the GitHub environment `prod`; branch protection on `main` requires `checks`
- [ ] Users know where to ask for help (`support_contact`), and the space description says what it cannot answer

## 11. Retiring the space

1. Tell the users, and remove `users_group` from the space.
2. In a pull request, remove `lifecycle.prevent_destroy`. Then run `databricks bundle destroy -t <env>` from a
   checkout, as each environment's deployer, or delete the resources by hand.
3. Archive the repository. The tags and releases keep every version.
