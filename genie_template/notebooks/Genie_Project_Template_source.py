# Databricks notebook source
# MAGIC %md
# MAGIC # Genie project template: from a prototype to a governed product
# MAGIC
# MAGIC This notebook takes **any** Genie space to production with **git, GitHub Actions and Declarative Automation Bundles
# MAGIC (DAB)**: one definition in git, promoted dev → qa → prod by a pipeline, with version control, gates, approvals,
# MAGIC rollback and monitoring. Nothing in it is specific to one project: everything that differs is read from
# MAGIC **`genie.config.yml`**.
# MAGIC
# MAGIC **How to use it for a new project**
# MAGIC 1. Create a GitHub repository from this template folder (copy the folder's contents to the repository root).
# MAGIC 2. Edit **`genie.config.yml`** (project name, space title, catalogs, groups, gate, monitoring; lines marked `# <- EDIT`).
# MAGIC 3. Put your space's content in **`space/genie_space.yml`**: keep the example to try the template, or import the space
# MAGIC    you prototyped in the Genie UI (part C does it for you).
# MAGIC 4. Add the repository to Databricks as a **Git folder**, open this notebook from it, run it top to bottom: first with
# MAGIC    `apply_changes = no` (prints every change it would make), then with `yes`.
# MAGIC
# MAGIC | Part | What it does | Changes something? |
# MAGIC |---|---|---|
# MAGIC | A | Architecture, folder structure, where every setting lives | no |
# MAGIC | B | One-time setup: groups, service principals, catalogs, monitoring tables and grants; the GitHub repository (environments, approvals, secrets, branch and tag protection) | yes |
# MAGIC | C | The first version of your space: import it from the Genie UI or keep the example; check every SQL against dev; try it in your sandbox | yes |
# MAGIC | D | Ship it: branch → pull request → checks → merge → release dev → qa (gate) → approval → prod | yes |
# MAGIC | E | Version control: releases, tags, what is live where, compare versions | no |
# MAGIC | F | Edits made in the dev Genie UI become versions in git | yes |
# MAGIC | G | Drift protection: an edit made in prod blocks the release | yes |
# MAGIC | H | Roll back prod | yes |
# MAGIC | I | Post-production monitoring: the dashboard, SQL alerts, quality history, wrong answers, table health, usage and feedback, jobs, deployments | no |
# MAGIC | J | Production readiness review, runbook, and the checklist for the next project | no |
# MAGIC
# MAGIC **Before you start**
# MAGIC * A Databricks workspace with Unity Catalog and a SQL warehouse; you are a workspace admin and may create catalogs
# MAGIC   (or an admin created the three catalogs of `genie.config.yml` for you).
# MAGIC * The data of your space exists in each environment's catalog (or use the example data: TPC-H, created in part B).
# MAGIC * A GitHub repository made from this template, and a **fine-grained personal access token** for it with repository
# MAGIC   permissions *Administration, Contents, Pull requests, Actions, Secrets, Variables, Environments*: read and write;
# MAGIC   *Deployments, Checks, Commit statuses*: read.
# MAGIC * Serverless compute for this notebook.

# COMMAND ----------

# MAGIC %md
# MAGIC # Part A · Architecture
# MAGIC
# MAGIC ## A1 · The system on one page
# MAGIC
# MAGIC ```text
# MAGIC  People                    GitHub: the source of truth                             Databricks
# MAGIC  ------                    ---------------------------                             ----------
# MAGIC                   +-------------------------------------------------------+
# MAGIC  developer -----> | feature branch --> pull request                       |
# MAGIC  (Git folder,     |                      | genie-ci: tests, space valid,  |
# MAGIC   sandbox target) |                      |   SQL on dev data, readiness,  |
# MAGIC                   |                      |   change list, bundle plan     |
# MAGIC                   |                      | review + merge                 |
# MAGIC                   |                      v                                |    one service principal
# MAGIC                   | main --push--> genie-release                          |    per environment
# MAGIC                   |                 1 dev-snapshot (UI edits -> git)      |
# MAGIC                   |                 2 dev   ------------------------------|-->  <catalog dev>   [DEV] space
# MAGIC                   |                 3 qa    drift gate, deploy,          -|-->  <catalog qa>    [QA]  space
# MAGIC                   |                         quality job (health+benchmarks), smoke
# MAGIC  approver ------> |                   [approval: environment prod]        |
# MAGIC                   |                 4 prod  drift gate, deploy, smoke    -|-->  <catalog prod>        space
# MAGIC                   |                         tag genie-prod-<time>-<sha>   |     nightly quality + usage jobs
# MAGIC                   |                         + GitHub release (notes)      |     -> <catalog>.<monitoring>.genie_*
# MAGIC                   |                                                       |     -> dashboard, SQL alerts, e-mails
# MAGIC                   |                                                       |
# MAGIC  Genie UI edit -> | genie-dev-sync (hourly): dev space -> PR + snapshot tag|
# MAGIC  (dev)            | genie-rollback (by hand): any tag / commit -> any env |
# MAGIC                   +-------------------------------------------------------+
# MAGIC                      sign-in: GitHub OIDC (federation policy) or the service principal's OAuth secret
# MAGIC ```
# MAGIC
# MAGIC ## A2 · Folder structure
# MAGIC
# MAGIC ```text
# MAGIC <your repository>/
# MAGIC ├── genie.config.yml            ★ THE config file: project name, title, catalogs, schema, warehouse, groups,
# MAGIC │                                 gate thresholds, monitoring, alert e-mails; per-environment values (section 7)
# MAGIC ├── space/
# MAGIC │   └── genie_space.yml         ★ the space's content: tables, instructions, example SQL, snippets, benchmarks
# MAGIC │                                 (written ${var.catalog}.${var.schema}.<table>: one file for every environment)
# MAGIC ├── databricks.yml                bundle wiring: includes, targets sandbox/dev/qa/prod, permissions, prod schedules,
# MAGIC │                                 prod space protected from deletion
# MAGIC ├── resources/
# MAGIC │   ├── genie_space.yml               the Genie space (title, warehouse, CAN_RUN for users)
# MAGIC │   ├── genie_quality.job.yml         quality job: health checks + benchmarks (gate), smoke test; nightly in prod
# MAGIC │   ├── genie_usage.job.yml           usage job: questions, failures, feedback; nightly in prod
# MAGIC │   ├── genie_monitoring.dashboard.yml  AI/BI dashboard: quality, data health, usage
# MAGIC │   └── genie_alerts.yml              SQL alerts: accuracy below the gate, monitoring stale, failing answers
# MAGIC ├── src/                          runs in Databricks
# MAGIC │   ├── genie_quality.py          the quality job (records genie_quality_runs, _results, genie_table_health)
# MAGIC │   ├── genie_usage.py            the usage job (merges genie_usage_messages)
# MAGIC │   └── genie_monitoring.lvdash.json   the dashboard's definition
# MAGIC ├── scripts/                      run by CI and by you (`make help` lists them)
# MAGIC │   ├── genie_tools.py            config + space library (load, render, neutralise, diff, validate, hash)
# MAGIC │   ├── validate_space.py         static checks, canonical form, change list, release notes
# MAGIC │   ├── check_sql.py              every example and benchmark SQL on an environment's data
# MAGIC │   ├── readiness.py              production readiness review (MUST / SHOULD checks)
# MAGIC │   ├── check_drift.py            drift gate + backup, from `bundle plan -o json`
# MAGIC │   ├── sync_from_workspace.py    dev UI edits -> git; import an existing space
# MAGIC │   ├── quality_report.py         the quality job's output in the GitHub run summary (for the approver)
# MAGIC │   ├── space_url.py              link to the deployed space (GitHub environment URL)
# MAGIC │   └── build_notebooks.py        notebooks/*_source.py -> .ipynb
# MAGIC ├── tests/                        tests of your project (no workspace needed): python tests/run_tests.py
# MAGIC ├── notebooks/
# MAGIC │   ├── Genie_Project_Template_source.py   this notebook (Databricks source)
# MAGIC │   ├── Genie_Project_Template.ipynb       the same, for reading on GitHub
# MAGIC │   └── Genie_CICD_Guide (.py / .ipynb)    the guide: how the whole CI/CD process works (read-only)
# MAGIC ├── docs/OPERATIONS.md            operating model: roles, release, rollback, monitoring, runbook, go-live checklist
# MAGIC ├── .github/
# MAGIC │   ├── workflows/
# MAGIC │   │   ├── genie-ci.yml          pull requests: tests, validation, SQL on dev data, readiness, change list, plan
# MAGIC │   │   ├── genie-release.yml     main -> dev -> qa -> approval -> prod, tag + GitHub release
# MAGIC │   │   ├── genie-dev-sync.yml    hourly: dev Genie UI -> pull request + snapshot tag
# MAGIC │   │   └── genie-rollback.yml    by hand: any version -> any environment
# MAGIC │   ├── pull_request_template.md  the reviewer's checklist
# MAGIC │   └── CODEOWNERS                who must review what (fill in your teams)
# MAGIC ├── Makefile                      make check | fix | diff | sql | deploy | gate | sync | readiness ...
# MAGIC ├── requirements-dev.txt
# MAGIC └── README.md
# MAGIC ```
# MAGIC
# MAGIC ★ = what you edit for a new project. Nothing else needs to change.
# MAGIC
# MAGIC ## A3 · Principles
# MAGIC
# MAGIC | Principle | How |
# MAGIC |---|---|
# MAGIC | **Everything is code** | The space (content and settings), its permissions, the quality job and its schedule are files in git. The workspace is an output. |
# MAGIC | **One definition, many environments** | The space names tables `${var.catalog}.${var.schema}.<table>`; each target fills in its own values. Every environment gets the same commit. |
# MAGIC | **Promotion by pipeline, not by people** | Only GitHub Actions deploys dev, qa and prod, each as its own service principal. People merge pull requests and approve prod. |
# MAGIC | **Gates before prod** | Pull request checks; in qa the health checks, the benchmark gate (`gate_min_accuracy`) and a smoke test; then a human approval. |
# MAGIC | **Nothing is lost** | UI edits in dev are exported to git (snapshot tags). UI edits in qa/prod block the release (drift gate) instead of being overwritten. The live space is backed up before every deploy. |
# MAGIC | **Every version can come back** | Each prod release is a tag and a GitHub release with notes; `genie-rollback` deploys any tag or commit. |
# MAGIC | **You hear about problems first** | Nightly quality and usage jobs in prod, results in Delta tables, a dashboard, SQL alerts, failure e-mails, GitHub notifications. |
# MAGIC | **Standard operations** | The same roles, release checklist, runbook and go-live checklist for every project: `docs/OPERATIONS.md`. |

# COMMAND ----------

# MAGIC %md
# MAGIC # Part B · One-time setup
# MAGIC
# MAGIC ## B1 · Parameters, connections and the project's configuration
# MAGIC
# MAGIC | Widget | Meaning |
# MAGIC |---|---|
# MAGIC | github_repo | `owner/name` of the repository made from the template |
# MAGIC | project_folder | the template's folder inside the repository; empty when it is the repository root (recommended: GitHub only runs workflows from the root) |
# MAGIC | github_token_secret | where the token is kept: `<secret scope>/<key>` in Databricks secrets |
# MAGIC | github_token | paste the token **once**: it is stored in the secret above; then clear this widget |
# MAGIC | auth_mode | how GitHub Actions signs in to Databricks: `secret` (an OAuth secret per service principal, automated here) or `oidc` (no secret; an account admin adds a federation policy, see B3) |
# MAGIC | reviewers | GitHub logins who approve prod deployments, comma separated (blank = you) |
# MAGIC | example_data | `yes` copies the TPC-H sample tables into each environment, for the example space |
# MAGIC | import_space_id | id of an existing Genie space to import as the first version (part C); blank = keep `space/genie_space.yml` |
# MAGIC | apply_changes | `no` prints what would change; `yes` changes it |

# COMMAND ----------

# MAGIC %pip install --quiet "databricks-sdk>=0.145.0" pyyaml pynacl

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("github_repo", "", "01 GitHub repository (owner/name)")
dbutils.widgets.text("project_folder", "", "02 project folder in the repository")
dbutils.widgets.text("github_token_secret", "genie/github_token", "03 token secret (scope/key)")
dbutils.widgets.text("github_token", "", "04 paste token once (then clear)")
dbutils.widgets.dropdown("auth_mode", "secret", ["secret", "oidc"], "05 GitHub sign-in to Databricks")
dbutils.widgets.text("reviewers", "", "06 prod approvers (GitHub logins)")
dbutils.widgets.dropdown("example_data", "yes", ["yes", "no"], "07 create the example data")
dbutils.widgets.text("import_space_id", "", "08 import this Genie space (id)")
dbutils.widgets.dropdown("apply_changes", "no", ["no", "yes"], "09 apply changes")

REPO_NAME = dbutils.widgets.get("github_repo").strip()
FOLDER = dbutils.widgets.get("project_folder").strip().strip("/")
AUTH_MODE = dbutils.widgets.get("auth_mode")
EXAMPLE_DATA = dbutils.widgets.get("example_data") == "yes"
IMPORT_ID = dbutils.widgets.get("import_space_id").strip()
APPLY = dbutils.widgets.get("apply_changes") == "yes"
if not REPO_NAME or "/" not in REPO_NAME:
    raise ValueError("Set the widget github_repo to owner/name of your repository.")
print(f"repository {REPO_NAME}{'/' + FOLDER if FOLDER else ''}   sign-in {AUTH_MODE}   apply_changes={APPLY}")

# COMMAND ----------

import base64, json, os, platform, re, shutil, subprocess, sys, tempfile, time, urllib.request, uuid, zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from databricks.sdk import WorkspaceClient
from databricks.sdk.service import iam, iamv2
from databricks.sdk.service import sql as dbsql

GITHUB_API = "https://api.github.com"
WORK = Path("/tmp/genie_template")
ENVIRONMENTS = ["dev", "qa", "prod"]


def find_project() -> Path:
    """The project folder: the Git folder directory with genie.config.yml (this notebook is in notebooks/)."""
    for c in [Path.cwd().parent, Path.cwd()]:
        if (c / "genie.config.yml").exists():
            return c
    raise RuntimeError("Open this notebook from the project's Git folder (genie.config.yml not found).")


PROJECT_DIR = find_project()
sys.path.insert(0, str(PROJECT_DIR / "scripts"))
import genie_tools as T                          # the template's config and space library

PROJECT = T.project_name()
CFG = {env: T.settings(env) for env in ENVIRONMENTS}
TITLE = {env: T.space_title(env) for env in ENVIRONMENTS}
SPACE = T.load_space()                           # the definition in the Git folder (environment-neutral)
in_repo = lambda rel: f"{FOLDER}/{rel}" if FOLDER else rel     # noqa: E731
SPACE_PATH = in_repo("space/genie_space.yml")

w = WorkspaceClient()
me = w.current_user.me()
HOST = w.config.host.rstrip("/")
print(f"project {PROJECT} ({PROJECT_DIR}); Databricks {HOST} as {me.user_name}")
display(pd.DataFrame([{"environment": e, "space title": TITLE[e], "data": f"{c['catalog']}.{c['schema']}",
                       "warehouse": c["warehouse_name"], "monitoring": f"{c['catalog']}.{c['monitoring_schema']}",
                       "gate": f">= {float(c['gate_min_accuracy']):.0%}", "alerts": ", ".join(c.get("alert_emails") or []) or "-"}
                      for e, c in CFG.items()]))
print(f"space content {T.content_hash(SPACE)}: {T.summary(SPACE)}")

# COMMAND ----------

# MAGIC %md
# MAGIC The token is read from Databricks secrets, never from the notebook. The first time, paste it into the widget
# MAGIC **github_token**: this cell stores it in the secret scope; then clear the widget.

# COMMAND ----------

SCOPE, KEY = dbutils.widgets.get("github_token_secret").split("/", 1)
pasted = dbutils.widgets.get("github_token").strip()
if pasted:
    if SCOPE not in {s.name for s in w.secrets.list_scopes()}:
        w.secrets.create_scope(SCOPE)
    w.secrets.put_secret(SCOPE, KEY, string_value=pasted)
    print(f"Stored the token in the secret {SCOPE}/{KEY}. Now clear the 'github_token' widget.")
TOKEN = dbutils.secrets.get(SCOPE, KEY)
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28"}


def gh(method: str, path: str, ok=(200, 201, 202, 204), **kw):
    """Call the GitHub REST API; path starts with /. Returns the JSON body ({} when there is none)."""
    r = requests.request(method, GITHUB_API + path, headers=HEADERS, timeout=60, **kw)
    if r.status_code not in ok:
        raise RuntimeError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:400]}")
    return r.json() if r.content else {}


def gh_get(path: str, default=None):
    """GET that returns `default` when the object does not exist (404)."""
    r = requests.get(GITHUB_API + path, headers=HEADERS, timeout=60)
    if r.status_code == 404:
        return default
    if r.status_code != 200:
        raise RuntimeError(f"GitHub GET {path} -> {r.status_code}: {r.text[:400]}")
    return r.json()


def change(what: str, method: str, path: str, **kw):
    """A change to GitHub: made when apply_changes = yes, otherwise only printed."""
    if not APPLY:
        body = kw.get("json")
        print(f"  [dry run] {what}: {method} {path}" + (f"  {json.dumps(body)[:160]}" if body else ""))
        return None
    result = gh(method, path, **kw)
    print(f"  done: {what}")
    return result


R = f"/repos/{REPO_NAME}"
user = gh("GET", "/user")
repo = gh("GET", R)
if not repo.get("permissions", {}).get("admin"):
    raise RuntimeError(f"{user['login']} is not an admin of {REPO_NAME}: the setup needs admin rights.")
REVIEWERS = [r.strip() for r in dbutils.widgets.get("reviewers").split(",") if r.strip()] or [user["login"]]
if FOLDER:
    print(f"note: the project is in {FOLDER}/; GitHub runs workflows only from the repository root's .github/workflows")
print(f"GitHub {REPO_NAME} as {user['login']} (admin); default branch {repo['default_branch']}; prod approvers {REVIEWERS}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## B2 · Databricks: groups, service principals, catalogs and grants
# MAGIC
# MAGIC | What | Name (from `genie.config.yml`) | Why |
# MAGIC |---|---|---|
# MAGIC | Groups | `users_group`, `developers_group`, `deployers_group` | permissions are given to groups, never to people |
# MAGIC | One service principal per environment | `<project>-deployer-dev`, `-qa`, `-prod` | each pipeline stage signs in as its own identity: a leaked qa credential cannot touch prod |
# MAGIC | Extra groups of the deployers | `deployer_extra_groups` (e.g. the row-level-security group that sees every row) | the quality gate must see all the data the benchmarks ask about |
# MAGIC | Catalogs and schemas | `catalog` per environment, `schema`, `monitoring_schema` | created once by you (CI never creates catalogs) |
# MAGIC | Monitoring tables | `genie_quality_runs`, `genie_quality_results`, `genie_table_health`, `genie_usage_messages` | created empty now, so the dashboard and alerts work before the first job run |
# MAGIC | Grants | deployer: read the space's data, own the monitoring schema; users: read the space's data | Genie answers with the asking user's own permissions |
# MAGIC | Warehouse | `CAN_USE` for the deployers and the users | the space and the quality job run on it |
# MAGIC
# MAGIC **Account groups.** Unity Catalog can grant data access only to *account* groups. The cell creates the groups as
# MAGIC account groups through the workspace's identity API and falls back to workspace groups (and says so) where that API
# MAGIC is not offered; data grants to workspace groups are then skipped.
# MAGIC
# MAGIC With **example_data = yes**, each environment gets the TPC-H sample tables (`samples.tpch`) in `<catalog>.<schema>`,
# MAGIC which is what the example space uses. It is ignored as soon as your space no longer uses those tables.

# COMMAND ----------

PATCH = [iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP]
GROUPS = {"users": CFG["prod"]["users_group"], "developers": CFG["prod"]["developers_group"],
          "deployers": CFG["prod"]["deployers_group"]}
GROUP_KIND = {}


def workspace_view(name: str):
    """(id, kind) of group `name` as this workspace sees it, or None; kind is 'account' or 'workspace'."""
    for g in w.groups.list(filter=f'displayName eq "{name}"', attributes="id,displayName,meta"):
        return g.id, "workspace" if (g.meta and g.meta.resource_type == "WorkspaceGroup") else "account"
    return None


def ensure_group(name: str) -> str | None:
    found = workspace_view(name)
    if found:
        GROUP_KIND[name] = found[1]
        return found[0]
    if not APPLY:
        print(f"[dry run] create group {name}")
        return None
    try:
        group = next((g for g in w.workspace_iam_v2.list_groups_proxy() if g.group_name == name), None) \
            or w.workspace_iam_v2.create_group_proxy(iamv2.Group(group_name=name))
        try:
            w.workspace_iam_v2.create_workspace_assignment_proxy(iamv2.WorkspaceAssignment(
                principal_id=int(group.group_id), principal_type=iamv2.PrincipalType.GROUP))
        except Exception as e:                   # noqa: BLE001  (already assigned)
            if "already" not in str(e).lower():
                raise
        for _ in range(30):
            if (workspace_view(name) or (None, ""))[1] == "account":
                break
            time.sleep(2)
        GROUP_KIND[name] = "account"
        print(f"created account group {name}")
        return group.group_id
    except Exception as e:                       # noqa: BLE001
        GROUP_KIND[name] = "workspace"
        print(f"could not create the account group {name} ({str(e).splitlines()[0][:100]}); using a workspace group")
        return w.groups.create(display_name=name).id


def add_member(group_id: str, principal_id: str) -> None:
    if principal_id in {m.value for m in (w.groups.get(group_id).members or [])}:
        return
    try:
        w.workspace_iam_v2.create_direct_group_member_proxy(int(group_id), iamv2.DirectGroupMember(principal_id=int(principal_id)))
    except Exception:                            # noqa: BLE001
        w.groups.patch(group_id, schemas=PATCH, operations=[
            iam.Patch(op=iam.PatchOp.ADD, path="members", value=[{"value": principal_id}])])


def sql(statement: str, quiet_errors: bool = False):
    """Run SQL when apply_changes = yes (print it otherwise); errors are reported, not raised."""
    if not APPLY:
        print(f"  [dry run] {statement}")
        return None
    try:
        return spark.sql(statement)
    except Exception as e:                       # noqa: BLE001
        if not quiet_errors:
            print(f"  could not run: {statement}\n    {str(e).splitlines()[0][:160]}")
        return None


GROUP_IDS = {role: ensure_group(name) for role, name in GROUPS.items()}
if APPLY and GROUP_IDS["developers"]:
    add_member(GROUP_IDS["developers"], me.id)
display(pd.DataFrame([{"role": r, "group": GROUPS[r], "kind": GROUP_KIND.get(GROUPS[r], "(to create)")} for r in GROUPS]))

# COMMAND ----------

def warehouse_id(name: str) -> str:
    whs = list(w.warehouses.list())
    match = [x for x in whs if x.name == name]
    if not match:
        raise RuntimeError(f"no SQL warehouse named {name!r} (warehouse_name in genie.config.yml); found {[x.name for x in whs]}")
    return match[0].id


EXAMPLE_TABLES = ["customer", "orders", "nation", "region"]
sys.path.insert(0, str(PROJECT_DIR / "src"))
import genie_quality, genie_usage                # the jobs' code: the monitoring tables' definitions
MONITORING_TABLES = {**genie_quality.TABLES, genie_usage.TABLE: genie_usage.DDL}
USES_EXAMPLE = {f"{T.CATALOG_REF}.{T.SCHEMA_REF}.{t}" for t in EXAMPLE_TABLES} <= set(T.data_sources(SPACE))
if EXAMPLE_DATA and not USES_EXAMPLE:
    print("example_data = yes is ignored: the space does not use the example tables (your own data is used)")
DEPLOYERS, SECRETS = {}, {}                      # env -> service principal; env -> OAuth secret (memory only)
for env in ENVIRONMENTS:
    c = CFG[env]
    cat, sch, mon = c["catalog"], c["schema"], c["monitoring_schema"]
    print(f"--- {env}: {cat}")
    name = f"{PROJECT}-deployer-{env}"
    sp = next(iter(w.service_principals.list(filter=f'displayName eq "{name}"')), None)
    if sp is None and APPLY:
        sp = w.service_principals.create(display_name=name)
        print(f"  created service principal {name}")
    if sp is not None:
        DEPLOYERS[env] = sp
        if APPLY and GROUP_IDS["deployers"]:
            add_member(GROUP_IDS["deployers"], sp.id)
        for g in c.get("deployer_extra_groups") or []:
            found = workspace_view(g)
            if not found:
                print(f"  WARNING: group {g} (deployer_extra_groups) does not exist; create it and re-run this cell")
            elif APPLY:
                add_member(found[0], sp.id)
            else:
                print(f"  [dry run] add {name} to {g}")
    else:
        extra = ", ".join(c.get("deployer_extra_groups") or [])
        print(f"  [dry run] create service principal {name}, add it to {GROUPS['deployers']}" + (f", {extra}" if extra else ""))
    sql(f"CREATE CATALOG IF NOT EXISTS `{cat}`")
    sql(f"CREATE SCHEMA IF NOT EXISTS `{cat}`.`{mon}`")
    for table, ddl in MONITORING_TABLES.items():
        sql(f"CREATE TABLE IF NOT EXISTS `{cat}`.`{mon}`.`{table}` ({ddl})")
    if EXAMPLE_DATA and USES_EXAMPLE:
        sql(f"CREATE SCHEMA IF NOT EXISTS `{cat}`.`{sch}`")
        for t in EXAMPLE_TABLES:
            sql(f"CREATE TABLE IF NOT EXISTS `{cat}`.`{sch}`.`{t}` AS SELECT * FROM samples.tpch.{t}")
    data_schemas = sorted(s for s in T.schemas_used(T.for_target(SPACE, env)) if s.split(".")[0] == cat)
    principal = f"`{sp.application_id}`" if sp is not None else f"`<{name}>`"
    users = f"`{GROUPS['users']}`"
    sql(f"GRANT USE CATALOG ON CATALOG `{cat}` TO {principal}")
    sql(f"GRANT ALL PRIVILEGES ON SCHEMA `{cat}`.`{mon}` TO {principal}")
    for s in data_schemas:
        q = ".".join(f"`{p}`" for p in s.split("."))
        sql(f"GRANT USE SCHEMA, SELECT, EXECUTE ON SCHEMA {q} TO {principal}")
        if GROUP_KIND.get(GROUPS["users"], "account") == "account":
            sql(f"GRANT USE CATALOG ON CATALOG `{cat}` TO {users}")
            sql(f"GRANT USE SCHEMA, SELECT, EXECUTE ON SCHEMA {q} TO {users}")
        else:
            print(f"  skipped data grants to the workspace group {GROUPS['users']} (Unity Catalog needs an account group)")
    if APPLY and sp is not None:
        wh = warehouse_id(c["warehouse_name"])
        w.warehouses.update_permissions(wh, access_control_list=[
            dbsql.WarehouseAccessControlRequest(service_principal_name=sp.application_id,
                                                permission_level=dbsql.WarehousePermissionLevel.CAN_USE),
            dbsql.WarehouseAccessControlRequest(group_name=GROUPS["users"],
                                                permission_level=dbsql.WarehousePermissionLevel.CAN_USE)])
        if AUTH_MODE == "secret":
            SECRETS[env] = w.service_principal_secrets_proxy.create(service_principal_id=sp.id).secret
        print(f"  {name} ({sp.application_id}): grants, warehouse CAN_USE" + (", OAuth secret" if env in SECRETS else ""))

display(pd.DataFrame([{"environment": e, "service principal": s.display_name, "application id": s.application_id,
                       "catalog": CFG[e]["catalog"]} for e, s in DEPLOYERS.items()]))

# COMMAND ----------

# MAGIC %md
# MAGIC ## B3 · GitHub: branch, environments, approvals, secrets, protection
# MAGIC
# MAGIC | Setting | Value | Why |
# MAGIC |---|---|---|
# MAGIC | Branch `main` | created from the default branch and made the default | releases run from `main` only |
# MAGIC | Environments `dev`, `qa`, `prod` | variables `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`; secret `DATABRICKS_CLIENT_SECRET` (auth_mode `secret`) | each release job signs in as its environment's service principal |
# MAGIC | `prod` | required reviewers; deployments from `main` only | a person approves every prod change |
# MAGIC | `qa` | deployments from `main` only | qa tests exactly what will reach prod |
# MAGIC | Branch protection on `main` | pull request required, check `checks` must pass, no force pushes | nothing reaches `main` untested |
# MAGIC | Tag ruleset `genie-prod-*` | tags cannot be deleted or moved | a release, once made, stays what it was: the basis of rollback |
# MAGIC | Workflow permissions | Actions may open pull requests | `genie-dev-sync` opens the sync pull requests |
# MAGIC
# MAGIC Secrets are encrypted here with the environment's public key before they are sent (libsodium sealed box); nobody
# MAGIC can read them back. With auth_mode **`oidc`** nothing secret is stored: an account admin adds a federation policy
# MAGIC per service principal (commands printed below; or the account console: *User management* → *Service principals* →
# MAGIC the principal → *Credentials & secrets* → *Federation policies*).

# COMMAND ----------

from nacl import encoding, public


def sealed(public_key_b64: str, value: str) -> str:
    box = public.SealedBox(public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder()))
    return base64.b64encode(box.encrypt(value.encode())).decode()


if not gh_get(f"{R}/branches/main"):
    head = gh("GET", f"{R}/git/ref/heads/{repo['default_branch']}")["object"]["sha"]
    change("create branch main", "POST", f"{R}/git/refs", json={"ref": "refs/heads/main", "sha": head})
if repo["default_branch"] != "main":
    change("make main the default branch", "PATCH", R, json={"default_branch": "main"})

reviewer_ids = [{"type": "User", "id": gh("GET", f"/users/{login}")["id"]} for login in REVIEWERS]
for env in ENVIRONMENTS:
    body = {"deployment_branch_policy": None}
    if env in ("qa", "prod"):
        body["deployment_branch_policy"] = {"protected_branches": False, "custom_branch_policies": True}
    if env == "prod":
        body["reviewers"] = reviewer_ids
    change(f"environment {env}", "PUT", f"{R}/environments/{env}", json=body)
    if env in ("qa", "prod") and APPLY:
        policies = gh("GET", f"{R}/environments/{env}/deployment-branch-policies").get("branch_policies", [])
        if not any(p["name"] == "main" for p in policies):
            change(f"{env}: deploy from main only", "POST", f"{R}/environments/{env}/deployment-branch-policies",
                   json={"name": "main", "type": "branch"})
    values = {"DATABRICKS_HOST": HOST, "DATABRICKS_CLIENT_ID": DEPLOYERS[env].application_id if env in DEPLOYERS
              else f"<application id of {PROJECT}-deployer-{env}>"}
    for name, value in values.items():
        if APPLY and gh_get(f"{R}/environments/{env}/variables/{name}") is not None:
            change(f"{env}: variable {name}", "PATCH", f"{R}/environments/{env}/variables/{name}",
                   json={"name": name, "value": value})
        else:
            change(f"{env}: variable {name}", "POST", f"{R}/environments/{env}/variables", json={"name": name, "value": value})
    if AUTH_MODE == "secret" and env in SECRETS:
        key = gh("GET", f"{R}/environments/{env}/secrets/public-key")
        change(f"{env}: secret DATABRICKS_CLIENT_SECRET", "PUT", f"{R}/environments/{env}/secrets/DATABRICKS_CLIENT_SECRET",
               json={"encrypted_value": sealed(key["key"], SECRETS[env]), "key_id": key["key_id"]})

change("protect main", "PUT", f"{R}/branches/main/protection", json={
    "required_status_checks": {"strict": False, "contexts": ["checks"]},
    "enforce_admins": False,
    "required_pull_request_reviews": {"required_approving_review_count": 0},
    "restrictions": None, "allow_force_pushes": False, "allow_deletions": False})
rulesets = gh_get(f"{R}/rulesets", default=[]) or []
if not any(r.get("name") == "genie releases" for r in rulesets):
    change("protect the release tags", "POST", f"{R}/rulesets", json={
        "name": "genie releases", "target": "tag", "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/tags/genie-prod-*"], "exclude": []}},
        "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}, {"type": "update"}]})
change("let Actions open pull requests", "PUT", f"{R}/actions/permissions/workflow",
       json={"default_workflow_permissions": "read", "can_approve_pull_request_reviews": True})

if AUTH_MODE == "oidc":
    print("\nFederation policies (account admin), one per environment:")
    for env, sp in DEPLOYERS.items():
        policy = {"oidc_policy": {"issuer": "https://token.actions.githubusercontent.com",
                                  "audiences": ["<your Databricks account id>"],
                                  "subject": f"repo:{REPO_NAME}:environment:{env}"}}
        print(f"databricks account service-principal-federation-policy create {sp.id} --json '{json.dumps(policy)}'")

# COMMAND ----------

# MAGIC %md
# MAGIC **Switch the Git folder to `main`** (Git dialog → branch → `main`). From now on changes reach `main` only through
# MAGIC pull requests, and the release pipeline is the only way into dev, qa and prod.

# COMMAND ----------

# MAGIC %md
# MAGIC # Part C · The first version of your space
# MAGIC
# MAGIC The space's content lives in `space/genie_space.yml`, environment-neutral: the dev catalog and schema in an export
# MAGIC are replaced by `${var.catalog}` and `${var.schema}`.
# MAGIC
# MAGIC * **import_space_id empty**: the file in git is the first version (the template's example, or what you put there).
# MAGIC * **import_space_id set**: the space with that id (for example the prototype you built in the Genie UI) is read
# MAGIC   through the API and converted; its tables must be in the dev catalog and schema of `genie.config.yml`, or another
# MAGIC   catalog of the same name pattern (they are replaced by the variables). Part D commits it through a pull request.
# MAGIC
# MAGIC Then every example and benchmark SQL is run against dev, so a broken query is caught before any deploy.

# COMMAND ----------

NEW_SPACE = T.copy_space(SPACE)
if IMPORT_ID:
    imported = w.genie.get_space(IMPORT_ID, include_serialized_space=True)
    live = json.loads(imported.serialized_space)
    NEW_SPACE = T.neutralise(live, CFG["dev"]["catalog"], CFG["dev"]["schema"])
    print(f"imported {imported.title!r} ({IMPORT_ID}): {T.summary(NEW_SPACE)}")
    print("\n".join(T.diff(SPACE, NEW_SPACE)[:60]) or "(identical to the file in git)")
errors, warnings = T.validate(NEW_SPACE)
for e in errors:
    print("ERROR  ", e)
for x in warnings:
    print("WARNING", x)
print("definition OK" if not errors else f"{len(errors)} error(s): fix them before shipping (part D will refuse)")

# COMMAND ----------

import check_sql                                 # the same queries and parameter defaults genie-ci uses


def typed(value: str, type_hint: str):
    t = (type_hint or "STRING").upper()
    return int(value) if t in ("INT", "INTEGER", "BIGINT", "LONG", "SMALLINT") else \
        float(value) if t in ("DOUBLE", "FLOAT", "DECIMAL") else value


rendered = T.for_target(NEW_SPACE, "dev")
results = []
for item in check_sql.statements(rendered):
    label = item["label"]
    if item["params"] is None:
        results.append({"query": label, "result": "skipped (parameters without default values)"})
        continue
    try:
        args = {n: typed(v, t) for n, (v, t) in item["params"].items()}
        df = spark.sql(item["sql"], args=args) if args else spark.sql(item["sql"])
        n = len(df.limit(5).collect())
        results.append({"query": label, "result": f"ok ({n} row(s) shown)"})
    except Exception as e:                       # noqa: BLE001
        results.append({"query": label, "result": "FAILED: " + str(e).splitlines()[0][:160]})
display(pd.DataFrame(results))
failed = [r for r in results if r["result"].startswith("FAILED")]
print(f"{len(results) - len(failed)}/{len(results)} queries ran against {CFG['dev']['catalog']}" +
      ("" if not failed else ": fix the failing ones (or the data) before shipping"))

# COMMAND ----------

# MAGIC %md
# MAGIC ### C2 · Try it in your own sandbox (optional)
# MAGIC
# MAGIC The `sandbox` target is each developer's private copy: deployed as you, titled `[dev <you>] ...`, in your user
# MAGIC folder, on dev data. It is how you try a change before opening a pull request. This cell deploys it with the
# MAGIC Databricks CLI (downloaded once) and prints the link; `databricks bundle destroy -t sandbox` removes it.

# COMMAND ----------

CLI_VERSION = "1.18.0"


def cli() -> Path:
    exe = WORK / "cli" / "databricks"
    if not exe.exists():
        arch = {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]
        url = f"https://github.com/databricks/cli/releases/download/v{CLI_VERSION}/databricks_cli_{CLI_VERSION}_linux_{arch}.zip"
        exe.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, exe.parent / "cli.zip")
        with zipfile.ZipFile(exe.parent / "cli.zip") as z:
            z.extract("databricks", exe.parent)
        exe.chmod(0o755)
    return exe


def bundle(*args, space: dict | None = None) -> str:
    """Run `databricks bundle ...` as you, on a copy of the project (optionally with another space definition)."""
    work = WORK / "bundle"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(PROJECT_DIR, work, ignore=shutil.ignore_patterns(".databricks", "__pycache__", "notebooks"))
    if space is not None:
        T.save_space(space, work / "space" / "genie_space.yml")
    token = w.config.authenticate()["Authorization"].removeprefix("Bearer ")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DATABRICKS_", "BUNDLE_VAR_"))}
    env.update(DATABRICKS_HOST=HOST, DATABRICKS_TOKEN=token, DATABRICKS_AUTH_TYPE="pat")
    p = subprocess.run([str(cli()), "bundle", *args], cwd=work, env=env, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"databricks bundle {' '.join(args)} failed:\n{p.stderr[-2000:]}")
    return p.stdout


if APPLY:
    bundle("deploy", "-t", "sandbox", space=NEW_SPACE)
    summary = json.loads(bundle("summary", "-t", "sandbox", "-o", "json"))
    sid = summary["resources"]["genie_spaces"][T.SPACE_RESOURCE]["id"]
    print(f"your sandbox space: {HOST}/genie/rooms/{sid}")
else:
    print("[dry run] databricks bundle deploy -t sandbox")

# COMMAND ----------

# MAGIC %md
# MAGIC # Part D · Ship it: from a branch to production
# MAGIC
# MAGIC ```text
# MAGIC  branch --commit--> pull request --checks (genie-ci)--> merge to main
# MAGIC    --> genie-release: dev-snapshot, dev, qa (drift gate, deploy, quality gate, smoke)
# MAGIC    --> waiting for approval (prod) --approve--> prod (drift gate, deploy, smoke) --> tag + GitHub release
# MAGIC ```
# MAGIC
# MAGIC The change: the imported space (part C), or else one of the space's benchmark questions promoted to a sample
# MAGIC question, the smallest change that still goes through every stage. The first release creates the space in dev, qa and prod. The helpers below wait for GitHub and
# MAGIC print each change of state; re-run a cell to keep waiting.

# COMMAND ----------

def base_branch() -> str:
    return "main" if gh_get(f"{R}/branches/main") else repo["default_branch"]


def wait_until(describe, done, timeout_min: float = 90, every_s: int = 30):
    """Call describe() until done(state); print the state whenever it changes. Returns the last state."""
    deadline, last = time.time() + timeout_min * 60, None
    while True:
        state = describe()
        if state != last:
            label = state if isinstance(state, str) else (
                f"run {state['id']} started: {state['html_url']}" if isinstance(state, dict) else "not started yet")
            print(f"{datetime.now(timezone.utc):%H:%M:%S}  {label}")
            last = state
        if done(state) or time.time() > deadline:
            return state
        time.sleep(every_s)


def run_for(workflow: str, sha: str = None, since: str = None):
    """The newest run of a workflow, for a commit (sha) or started after a time (since, ISO)."""
    q = "?per_page=20" + (f"&head_sha={sha}" if sha else "")
    runs = gh("GET", f"{R}/actions/workflows/{workflow}/runs{q}")["workflow_runs"]
    runs = [r for r in runs if not since or r["created_at"] >= since]
    return runs[0] if runs else None


def run_state(run_id: int) -> str:
    run = gh("GET", f"{R}/actions/runs/{run_id}")
    jobs = gh("GET", f"{R}/actions/runs/{run_id}/jobs")["jobs"]
    return f"{run['status']}/{run['conclusion'] or '-'}  " + "  ".join(
        f"{j['name']}={j['conclusion'] or j['status']}" for j in jobs)


def approve(run_id: int, comment: str) -> bool:
    pending = gh("GET", f"{R}/actions/runs/{run_id}/pending_deployments")
    ids = [p["environment"]["id"] for p in pending if p.get("current_user_can_approve")]
    if ids:
        change(f"approve {[p['environment']['name'] for p in pending]}", "POST",
               f"{R}/actions/runs/{run_id}/pending_deployments",
               json={"environment_ids": ids, "state": "approved", "comment": comment})
    return bool(ids)


def watch(run: dict, approve_prod: bool = True) -> str:
    """Follow a release or rollback run to its end, approving prod when it waits (you must be a reviewer)."""
    print(f"run {run['id']}: {run['html_url']}")
    while True:
        state = wait_until(lambda: run_state(run["id"]), lambda s: s.startswith(("completed", "waiting")))
        if state.startswith("waiting") and approve_prod and APPLY and approve(run["id"], "Approved from the notebook"):
            continue
        return state


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def space_at(ref: str) -> dict:
    """The space definition in git at a branch, tag or commit ({} if missing)."""
    f = gh_get(f"{R}/contents/{SPACE_PATH}?ref={ref}")
    return T.space_from_text(base64.b64decode(f["content"]).decode()) if f else {}

# COMMAND ----------

BASE = base_branch()
current = gh("GET", f"{R}/contents/{SPACE_PATH}?ref={BASE}")            # the file on main, and its blob sha
on_main = T.space_from_text(base64.b64decode(current["content"]).decode())
target_space = T.copy_space(NEW_SPACE if IMPORT_ID else on_main)
if not IMPORT_ID:
    # the change: promote a benchmark (or example) question to a sample question users see in the space
    asked = {" ".join(q["question"]) for q in target_space.get("config", {}).get("sample_questions", [])}
    pool = [" ".join(q["question"]) for q in target_space.get("benchmarks", {}).get("questions", [])] + \
           [" ".join(q["question"]) for q in target_space.get("instructions", {}).get("example_question_sqls", [])]
    new_q = next((q for q in pool if q not in asked), None)
    if new_q:
        target_space.setdefault("config", {}).setdefault("sample_questions", []).append({"id": T.new_id(), "question": [new_q]})
CHANGES = T.diff(on_main, target_space)
TITLE_PR = ("Genie: first version of the space (imported)" if IMPORT_ID else
            f"Genie: add the sample question '{new_q}'" if CHANGES else None)
print("\n".join(CHANGES) or "nothing to ship: main already has this content")
if T.validate(target_space)[0]:
    raise RuntimeError("the new definition has errors (see part C); fix them first")

pr = None
if CHANGES:
    BRANCH = f"feature/genie-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    main_sha = gh("GET", f"{R}/git/ref/heads/{BASE}")["object"]["sha"]
    change(f"create branch {BRANCH}", "POST", f"{R}/git/refs", json={"ref": f"refs/heads/{BRANCH}", "sha": main_sha})
    change("commit the change", "PUT", f"{R}/contents/{SPACE_PATH}", json={
        "message": TITLE_PR, "branch": BRANCH, "sha": current["sha"],
        "content": base64.b64encode(T.dumps_space(target_space).encode()).decode()})
    pr = change("open the pull request", "POST", f"{R}/pulls", json={
        "title": TITLE_PR, "head": BRANCH, "base": "main",
        "body": "Opened by the Genie project template notebook.\n\n```diff\n" + "\n".join(CHANGES)[:60000] + "\n```"})
    if pr:
        print(f"pull request #{pr['number']}: {pr['html_url']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### D2 · Checks, merge and release
# MAGIC
# MAGIC `genie-ci` runs the project's tests, validates the definition and writes the change list into the check summary
# MAGIC (what the reviewer reads); with dev connected it also shows `bundle plan`. Merging starts `genie-release`, which
# MAGIC stops at **prod** until an approver approves; with `apply_changes = yes` and you among the approvers the notebook
# MAGIC approves (in real life the approver does it on the run page, after reading the qa results).

# COMMAND ----------

def checks(sha: str) -> str:
    runs = gh("GET", f"{R}/commits/{sha}/check-runs")["check_runs"]
    return "  ".join(sorted(f"{c['name']}={c['conclusion'] or c['status']}" for c in runs)) or "no checks yet"


if pr:
    head = gh("GET", f"{R}/pulls/{pr['number']}")["head"]["sha"]
    state = wait_until(lambda: checks(head), lambda s: s != "no checks yet" and "in_progress" not in s
                       and "queued" not in s, timeout_min=30)
    if "failure" in state:
        raise RuntimeError("a check failed: open the pull request to see why")
    merged = change("merge the pull request (squash)", "PUT", f"{R}/pulls/{pr['number']}/merge",
                    json={"merge_method": "squash"})
    release = wait_until(lambda: run_for("genie-release.yml", sha=merged["sha"]), lambda r: r is not None, 10, 10)
    final = watch(release)
    print("released to prod" if final.startswith("completed/success") else f"the release did not finish: {final}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Part E · Version control: what is live where
# MAGIC
# MAGIC | Version | Where |
# MAGIC |---|---|
# MAGIC | Every change | a commit on `main` that touched `space/genie_space.yml` (reviewed in a pull request) |
# MAGIC | Every prod release | tag `genie-prod-<UTC time>-<commit>` + a GitHub release whose notes list the changes |
# MAGIC | Every dev UI state | tag `genie-dev-snapshot-<time>` (from `genie-dev-sync`) |
# MAGIC | What each environment runs now | tag `genie-deployed-<env>`, moved by every deploy and rollback |
# MAGIC | Every deploy | a GitHub deployment per environment; the live space backed up as a build artifact |
# MAGIC | In Databricks | every quality result carries the deployed commit (`version` in `genie_quality_runs`) |
# MAGIC
# MAGIC "Which version is live?" is answered from the content: each environment's live space is exported, made
# MAGIC environment-neutral, hashed, and compared with the definition of every release tag.

# COMMAND ----------

def tags(prefix: str) -> list[str]:
    return sorted((t["name"] for t in gh("GET", f"{R}/tags?per_page=100") if t["name"].startswith(prefix)), reverse=True)


def live_space(env: str):
    spaces = [s for s in (w.genie.list_spaces().spaces or []) if s.title == TITLE[env]]
    return w.genie.get_space(spaces[-1].space_id, include_serialized_space=True) if spaces else None


releases, snapshots = tags("genie-prod-"), tags("genie-dev-snapshot-")
print(f"{len(releases)} prod release(s), latest {releases[:3]};  {len(snapshots)} dev snapshot(s), latest {snapshots[:3]}")
release_hash = {tag: T.content_hash(space_at(tag)) for tag in releases[:10]}
main_hash = T.content_hash(space_at(base_branch()))
rows = []
for env in ENVIRONMENTS:
    sp = live_space(env)
    if sp is None:
        rows.append({"environment": env, "live version": "(no space yet)"})
        continue
    h = T.content_hash(T.from_target(json.loads(sp.serialized_space), env))
    rows.append({"environment": env, "space id": sp.space_id, "content": h,
                 "live version": next((t for t, th in release_hash.items() if th == h), "not a prod release"),
                 "matches main": h == main_hash})
display(pd.DataFrame(rows))
for r in rows:
    print(f"{r['environment']:>5}: live version {r['live version']}" + (f", matches main: {r['matches main']}" if "matches main" in r else ""))

# COMMAND ----------

# GitHub releases (with notes), the history of the space file, and the deployments per environment
for rel in (gh_get(f"{R}/releases?per_page=5", default=[]) or [])[:5]:
    print(f"== {rel['tag_name']}  {rel.get('published_at') or ''}\n{(rel.get('body') or '').strip()[:1500]}\n")
commits = gh("GET", f"{R}/commits?path={SPACE_PATH}&sha={base_branch()}&per_page=15")
display(pd.DataFrame([{"date": c["commit"]["committer"]["date"], "commit": c["sha"][:7], "author": c["commit"]["author"]["name"],
                       "message": c["commit"]["message"].splitlines()[0]} for c in commits]))
display(pd.DataFrame([{"environment": env, "created": d["created_at"], "commit": d["sha"][:7],
                       "by": (d.get("creator") or {}).get("login")}
                      for env in ENVIRONMENTS for d in gh("GET", f"{R}/deployments?environment={env}&per_page=5")]))

# COMMAND ----------

# Compare any two versions: tags, branches or commits
OLD, NEW = (releases[1], releases[0]) if len(releases) > 1 else ("", base_branch())
print(f"changes {OLD or '(nothing)'} -> {NEW}:")
print("\n".join(T.diff(space_at(OLD) if OLD else {}, space_at(NEW))) or "  (none)")

# COMMAND ----------

# MAGIC %md
# MAGIC # Part F · Edits made in the Genie UI become versions
# MAGIC
# MAGIC People improve the dev space in the Genie UI. `genie-dev-sync` (hourly, and first in every release) exports it,
# MAGIC commits it to the branch `genie/dev-sync`, tags it `genie-dev-snapshot-<time>` and opens or updates a pull request.
# MAGIC Merging that pull request is how a UI edit is promoted; closing it discards the edit (the next release resets dev),
# MAGIC and the snapshot tag keeps it. This part makes an edit through the API, the same as one in the UI, and runs the sync.

# COMMAND ----------

UI_QUESTION = "Edited in the dev Genie UI (template notebook, part F)"
dev = live_space("dev")
if dev and APPLY:
    content = json.loads(dev.serialized_space)
    qs = content.setdefault("config", {}).setdefault("sample_questions", [])
    if not any(UI_QUESTION in q["question"] for q in qs):
        qs.append({"id": T.new_id(), "question": [UI_QUESTION]})
        w.genie.update_space(dev.space_id, serialized_space=json.dumps(T.normalise(content)), etag=dev.etag)
    print(f"edited the dev space: added {UI_QUESTION!r}")
    started = now_iso()
    change("run genie-dev-sync", "POST", f"{R}/actions/workflows/genie-dev-sync.yml/dispatches", json={"ref": "main"})
    sync = wait_until(lambda: run_for("genie-dev-sync.yml", since=started), lambda r: r is not None, 10, 10)
    print(wait_until(lambda: run_state(sync["id"]), lambda s: s.startswith("completed"), 20, 15))
    for p in gh("GET", f"{R}/pulls?head={REPO_NAME.split('/')[0]}:genie/dev-sync&state=open"):
        print(f"sync pull request #{p['number']}: {p['html_url']}")
        for f in gh("GET", f"{R}/pulls/{p['number']}/files"):
            print(f["filename"]); print(f.get("patch", "")[:1500])
else:
    print("skipped (needs apply_changes = yes and a dev space: run part D first)")

# COMMAND ----------

# MAGIC %md
# MAGIC # Part G · Drift protection: an edit in prod blocks the release
# MAGIC
# MAGIC If someone edits qa or prod in the UI, the next deploy would silently overwrite it. The release therefore runs
# MAGIC `bundle plan` first: the bundle knows the etag it deployed, sees the live one, and `scripts/check_drift.py` stops
# MAGIC with exit code 2 and the list of differences (the live space is kept as a build artifact either way). Then choose:
# MAGIC **adopt** the edit (`python scripts/sync_from_workspace.py -t prod`, in a pull request) or **overwrite** it (run
# MAGIC `genie-release` by hand with *allow_drift*). This cell does the edit, shows the block, then overwrites.

# COMMAND ----------

prod = live_space("prod")
if APPLY and prod is not None:
    content = json.loads(prod.serialized_space)
    content.setdefault("config", {}).setdefault("sample_questions", []).append({"id": T.new_id(), "question": ["Edited directly in prod"]})
    w.genie.update_space(prod.space_id, serialized_space=json.dumps(T.normalise(content)), etag=prod.etag)
    started = now_iso()
    change("run genie-release", "POST", f"{R}/actions/workflows/genie-release.yml/dispatches", json={"ref": "main"})
    final = watch(wait_until(lambda: run_for("genie-release.yml", since=started), lambda r: r is not None, 10, 10))
    print("blocked by the drift gate, as intended" if "prod=failure" in final else final)
    started = now_iso()
    change("overwrite the prod edit (allow_drift)", "POST", f"{R}/actions/workflows/genie-release.yml/dispatches",
           json={"ref": "main", "inputs": {"allow_drift": "true"}})
    print(watch(wait_until(lambda: run_for("genie-release.yml", since=started), lambda r: r is not None, 10, 10)))
else:
    print("skipped (needs apply_changes = yes and a prod space)")

# COMMAND ----------

# MAGIC %md
# MAGIC # Part H · Roll back prod
# MAGIC
# MAGIC `genie-rollback` deploys an earlier version to one environment: `to = previous` is the release before the latest,
# MAGIC or any tag or commit. `what = space` restores only the space's content (today's settings); `what = bundle` restores
# MAGIC the whole bundle as it was. A rollback to prod waits for the same approval as a release, and backs up the live
# MAGIC space first. Afterwards fix forward on `main` (for example revert the bad commit), or the next release brings it back.

# COMMAND ----------

releases = tags("genie-prod-")                   # read again: parts D and G added releases
if APPLY and len(releases) >= 2:
    print(f"prod runs {releases[0]}; rolling back to {releases[1]}")
    started = now_iso()
    change("run genie-rollback", "POST", f"{R}/actions/workflows/genie-rollback.yml/dispatches", json={
        "ref": "main", "inputs": {"environment": "prod", "to": "previous", "what": "space",
                                  "reason": "Rollback exercise from the template notebook"}})
    print(watch(wait_until(lambda: run_for("genie-rollback.yml", since=started), lambda r: r is not None, 10, 10)))
    live = T.content_hash(T.from_target(json.loads(live_space("prod").serialized_space), "prod"))
    print(f"prod now runs {releases[1]}: {live == T.content_hash(space_at(releases[1]))}")
else:
    print("skipped: needs apply_changes = yes and two prod releases")

# COMMAND ----------

# MAGIC %md
# MAGIC # Part I · Monitoring and alerting
# MAGIC
# MAGIC | Layer | What is watched | Where | Who is told |
# MAGIC |---|---|---|---|
# MAGIC | Answers | the benchmarks: accuracy, wrong answers with Genie's SQL | quality job (qa gate, nightly in prod) → `genie_quality_runs`, `genie_quality_results` | job failure e-mail (`alert_emails`) |
# MAGIC | Data behind the space | every table readable, not empty, fresh (`max_data_age_hours`) | quality job → `genie_table_health` | job failure e-mail |
# MAGIC | Availability | one question answered with SQL after each prod deploy | smoke test; auto-restore on failure | the release fails (GitHub notification) |
# MAGIC | Usage | conversations, active users, actions | Genie API; audit log `system.access.audit` | dashboards / SQL alerts |
# MAGIC | Deployments | every release, sync and rollback | GitHub Actions runs, environments, releases | GitHub notifications |
# MAGIC
# MAGIC All tables are in `<catalog>.<monitoring_schema>` of each environment; every row has the `target` and the deployed
# MAGIC git commit (`version`), so a drop in accuracy can be traced to the release that caused it.
# MAGIC
# MAGIC Everything here is deployed by the bundle, so every project has the same monitoring:
# MAGIC
# MAGIC | Deployed | What | Where to set it |
# MAGIC |---|---|---|
# MAGIC | Job `<title> · quality` | health + benchmarks; nightly in prod; e-mails on failure or when it runs over an hour | `monitor_cron`, `alert_emails`, `max_data_age_hours` |
# MAGIC | Job `<title> · usage` | every question, its status and feedback → `genie_usage_messages`; nightly in prod | `usage_lookback_hours` |
# MAGIC | Dashboard `<title> · monitoring` | pages *Quality*, *Data health*, *Usage* | — |
# MAGIC | SQL alert *accuracy below the gate* | the latest quality run failed | `alert_subscribers` |
# MAGIC | SQL alert *monitoring stale* | no quality run for 26 hours | `alert_subscribers` |
# MAGIC | SQL alert *failing answers* | more than `alert_max_failed_share` of the last day's questions failed or got a thumbs down | `alert_max_failed_share` |
# MAGIC
# MAGIC The alerts run where `alerts_pause_status` is `UNPAUSED` (prod). Change any of it in a pull request; the next
# MAGIC release applies it.

# COMMAND ----------

# What the bundle deployed for monitoring, per environment: the dashboard and the SQL alerts
dashboards = {d.display_name: d for d in w.lakeview.list()}
alerts = list(w.alerts_v2.list_alerts())
rows = []
for env in ENVIRONMENTS:
    title, suffix = CFG[env]["space_title"], CFG[env].get("title_suffix") or ""
    d = dashboards.get(f"{title} · monitoring{suffix}")
    rows.append({"environment": env, "item": "dashboard", "name": f"{title} · monitoring{suffix}",
                 "state": "deployed" if d else "not deployed yet",
                 "link": f"{HOST}/dashboardsv3/{d.dashboard_id}" if d else ""})
    for a in alerts:
        mine = a.display_name.startswith(title + " · ") and (a.display_name.endswith(suffix) if suffix
                                                             else not a.display_name.endswith("]"))
        if mine:
            pause = a.schedule.pause_status if a.schedule else None
            rows.append({"environment": env, "item": "SQL alert", "name": a.display_name,
                         "state": str(getattr(pause, "value", pause) or ""), "link": f"{HOST}/sql/alerts-v2/{a.id}"})
display(pd.DataFrame(rows))
for r in rows:
    print(f"{r['environment']:>5}  {r['item']:<9} {r['name']}: {r['state']}")

# COMMAND ----------

def monitoring(env: str, table: str, extra: str = "") -> pd.DataFrame | None:
    c = CFG[env]
    try:
        return spark.sql(f"SELECT * FROM `{c['catalog']}`.`{c['monitoring_schema']}`.`{table}` {extra}").toPandas()
    except Exception as e:                       # noqa: BLE001
        print(f"{env}: no {table} yet ({str(e).splitlines()[0][:80]}); the quality job creates it on its first run")
        return None


for env in ENVIRONMENTS:
    df = monitoring(env, "genie_quality_runs", "ORDER BY checked_at DESC LIMIT 20")
    if df is not None and len(df):
        print(f"{env}: quality runs"); display(df)

# COMMAND ----------

# The wrong answers of the latest gate in each environment, and the latest table health
for env in ENVIRONMENTS:
    bad = monitoring(env, "genie_quality_results", "QUALIFY dense_rank() OVER (ORDER BY checked_at DESC) = 1 "
                                                   "AND assessment = 'BAD'")
    if bad is not None and len(bad):
        print(f"{env}: wrong answers in the latest run"); display(bad[["question", "reason", "genie_sql", "expected_sql"]])
    health = monitoring(env, "genie_table_health", "QUALIFY dense_rank() OVER (ORDER BY checked_at DESC) = 1")
    if health is not None and len(health):
        print(f"{env}: table health"); display(health)

# COMMAND ----------

# Usage: questions, failures and thumbs down (from the usage job), and the questions to review
for env in ENVIRONMENTS:
    used = monitoring(env, "genie_usage_messages", "WHERE created_at >= current_date() - INTERVAL 30 DAYS")
    if used is not None and len(used):
        print(f"{env}: {len(used)} question(s) in 30 days, {used['user_id'].nunique()} user(s), "
              f"{(used['status'] != 'COMPLETED').mean():.0%} failed, {(used['feedback_rating'] == 'NEGATIVE').sum()} thumbs down")
        display(used[(used["status"] != "COMPLETED") | (used["feedback_rating"] == "NEGATIVE")]
                [["created_at", "question", "status", "error", "feedback_rating"]])

# Benchmark trend (Genie's evaluation history) and live conversation counts per environment
trend, usage = [], []
for env in ENVIRONMENTS:
    sp = live_space(env)
    if not sp:
        continue
    for ev in (w.genie.genie_list_eval_runs(sp.space_id).eval_runs or [])[:10]:
        graded = (ev.num_questions or 0) - (ev.num_needs_review or 0)
        trend.append({"environment": env, "run": ev.eval_run_id, "created": ev.created_timestamp, "correct": ev.num_correct,
                      "graded": graded, "accuracy": round(ev.num_correct / graded, 2) if graded else None})
    try:
        convs = w.genie.list_conversations(sp.space_id, include_all=True, page_size=100).conversations or []
        usage.append({"environment": env, "conversations (latest 100)": len(convs)})
    except Exception as e:                       # noqa: BLE001
        usage.append({"environment": env, "conversations (latest 100)": f"n/a ({str(e).splitlines()[0][:60]})"})
display(pd.DataFrame(trend)); display(pd.DataFrame(usage))

# COMMAND ----------

# Account-wide, from system tables (an account admin enables them): Genie activity and failed job runs
for title, query in {
    "Genie activity, last 30 days": """
        SELECT event_date, action_name, COUNT(*) AS events, COUNT(DISTINCT user_identity.email) AS users
        FROM system.access.audit
        WHERE service_name = 'aibiGenie' AND event_date >= current_date() - INTERVAL 30 DAYS
        GROUP BY ALL ORDER BY event_date DESC, events DESC""",
    "failed job runs, last 7 days": """
        SELECT period_start_time, job_id, run_id, result_state, termination_code
        FROM system.lakeflow.job_run_timeline
        WHERE period_start_time >= current_date() - INTERVAL 7 DAYS AND result_state NOT IN ('SUCCEEDED')
        ORDER BY period_start_time DESC LIMIT 50""",
}.items():
    try:
        print(title); display(spark.sql(query))
    except Exception as e:                       # noqa: BLE001
        print(f"  not available ({str(e).splitlines()[0][:100]})")

# COMMAND ----------

# The quality job's runs, and the workflow runs on GitHub
runs = []
for job in w.jobs.list(limit=100):
    name = job.settings.name if job.settings else ""
    if name.startswith(CFG["prod"]["space_title"]) and ("quality" in name or "usage" in name):
        for r in w.jobs.list_runs(job_id=job.job_id, limit=5):
            s = r.state
            runs.append({"job": name, "started": pd.to_datetime(r.start_time, unit="ms"),
                         "minutes": round((r.run_duration or 0) / 60000, 1),
                         "result": (s.result_state.value if s and s.result_state else
                                    s.life_cycle_state.value if s and s.life_cycle_state else "?"), "link": r.run_page_url})
display(pd.DataFrame(runs))
recent = [{"workflow": r["name"], "event": r["event"], "started": r["created_at"], "result": r["conclusion"] or r["status"],
           "commit": r["head_sha"][:7], "link": r["html_url"]}
          for wf in ["genie-release.yml", "genie-rollback.yml", "genie-dev-sync.yml", "genie-ci.yml"]
          for r in gh("GET", f"{R}/actions/workflows/{wf}/runs?per_page=5").get("workflow_runs", [])]
display(pd.DataFrame(recent))

# COMMAND ----------

# MAGIC %md
# MAGIC # Part J · Production readiness review, runbook, next project
# MAGIC
# MAGIC ### J1 · Production readiness review
# MAGIC
# MAGIC `scripts/readiness.py` checks what a production Genie service needs. **MUST** checks block a release (genie-ci
# MAGIC fails); **SHOULD** checks are the open items before go-live (owners named, alerts going to someone, enough
# MAGIC benchmarks, freshness checked...). Every pull request shows the same review in its check summary.

# COMMAND ----------

import readiness
results = readiness.checks()
display(pd.DataFrame([{"level": lvl, "check": name, "status": "pass" if ok else "FAIL" if lvl == "MUST" else "to do",
                       "to do": "" if ok else advice} for lvl, name, ok, advice in results]))
must = [n for lvl, n, ok, _ in results if lvl == "MUST" and not ok]
todo = [n for lvl, n, ok, _ in results if lvl == "SHOULD" and not ok]
print(f"{'NOT READY' if must else 'ready' if not todo else 'ready, with open items'}: "
      f"{len(must)} MUST failed, {len(todo)} SHOULD open")

# COMMAND ----------

# MAGIC %md
# MAGIC ### J2 · Runbook
# MAGIC
# MAGIC The full operating model (roles, the approver's checklist, versions, rollback, alerts, regular care, go-live and
# MAGIC retirement) is in **`docs/OPERATIONS.md`**. The essentials:
# MAGIC
# MAGIC | Signal | Meaning | Do this |
# MAGIC |---|---|---|
# MAGIC | `genie-ci` red on a pull request | a test failed, the definition is invalid or not canonical | the check names the item; `python scripts/validate_space.py --fix` for the form |
# MAGIC | Release stops in qa: **drift** | someone edited the qa space | adopt (`sync_from_workspace.py -t qa` into a pull request) or overwrite (*allow_drift*) |
# MAGIC | Release stops in qa: **quality gate** | too few benchmarks answered correctly, or a table is unhealthy | the job's log lists each wrong answer with Genie's SQL; add context (instructions, example SQL, column descriptions) or fix an ambiguous benchmark |
# MAGIC | Release stops in prod: **drift** | someone edited the prod space | as for qa |
# MAGIC | Prod **smoke test** failed | Genie does not answer in prod | the release already restored the previous release's space; investigate in qa |
# MAGIC | Nightly quality job failed (e-mail) | answers degraded or data stale on live data | `genie_quality_runs.reasons`; compare `version` with the last good run; roll back if a release caused it |
# MAGIC | SQL alert *monitoring stale* | the nightly job did not run | Jobs UI → `<title> · quality`; fix (permissions, warehouse) and rerun |
# MAGIC | SQL alert *failing answers* | users fail more than `alert_max_failed_share` | dashboard *Usage* → questions to review → new benchmarks and example SQL |
# MAGIC | Wrong content released | a bad change reached prod | `genie-rollback` (`what = space`, `to = previous`), then revert on `main` |
# MAGIC | Plan wants to **recreate** the space | a change that Genie cannot apply in place | users would lose conversations: avoid, or release with *allow_destroy* on purpose |
# MAGIC
# MAGIC **Regular care**
# MAGIC * Weekly: review the open `genie/dev-sync` pull request; merge or close it.
# MAGIC * Monthly: rotate the service principals' OAuth secrets (re-run B2 and B3) or move to OIDC; review the accuracy trend
# MAGIC   and raise `gate_min_accuracy` when the space is consistently better.
# MAGIC * Each release: read the release notes (GitHub release) and the qa quality results before approving prod.
# MAGIC
# MAGIC ### J3 · Checklist for the next Genie project
# MAGIC
# MAGIC 1. New repository from the template; edit `genie.config.yml` (`# <- EDIT` lines).
# MAGIC 2. Prototype the space in the dev Genie UI; set **import_space_id**; run this notebook with `apply_changes = no`, then `yes`.
# MAGIC 3. Add benchmarks (at least `gate_min_graded`) with trusted SQL answers: they are the gate.
# MAGIC 4. Set the owners, `alert_emails` and `alert_subscribers` for prod, and `max_data_age_hours` to your data's load
# MAGIC    frequency; run J1 until no SHOULD item is open.
# MAGIC 5. From then on: change by pull request or in the dev UI, release by merging, approve prod (read the qa quality
# MAGIC    report first), and watch the dashboard. Turn failed and thumbs-down questions into benchmarks.
