# Databricks notebook source
# MAGIC %md
# MAGIC # FreshCart: from dev to production with Declarative Automation Bundles and GitHub
# MAGIC
# MAGIC The first notebook (`FreshCart_End_to_End_on_Databricks`) built the dev environment **by hand**: you ran
# MAGIC `databricks bundle deploy` yourself. That is how you develop. This notebook turns it into how you **operate**: from
# MAGIC now on nothing reaches qa or prod except through git and GitHub Actions, deployed by service principals, after
# MAGIC checks, gates and an approval, with a version for every change, a way back for every version, and monitoring that
# MAGIC tells you when something breaks.
# MAGIC
# MAGIC | Part | What you do | Changes something? |
# MAGIC |---|---|---|
# MAGIC | A | Understand the architecture | no |
# MAGIC | B | One-time setup: Databricks identities and the GitHub repository settings, through their APIs | yes (only with `apply_changes = yes`) |
# MAGIC | C | Hand dev over to CI/CD | yes |
# MAGIC | D | Ship a change: branch, pull request, checks, merge, release dev → qa → approval → prod | yes |
# MAGIC | E | Version history: what is live where, and since when | no |
# MAGIC | F | Edits in the Genie UI become versions in git | yes |
# MAGIC | G | Drift protection: an edit in prod blocks the next release | yes |
# MAGIC | H | Roll back prod to the previous release | yes |
# MAGIC | I | Monitoring and alerting | no |
# MAGIC | J | Runbook: what to do when something fails | no |
# MAGIC
# MAGIC **Safe by default.** Every cell that would change GitHub or Databricks first prints exactly what it would do.
# MAGIC It only does it when the widget **apply_changes** is `yes`. Read through once with `no`, then switch.
# MAGIC
# MAGIC **Before you start**
# MAGIC * The first notebook ran in dev (data and Genie space exist in `freshcart_dev`).
# MAGIC * The repository is on GitHub and you are an admin of it (your own copy or fork of this repository).
# MAGIC * A GitHub **fine-grained personal access token** for that repository (GitHub → Settings → Developer settings →
# MAGIC   Fine-grained tokens), with repository permissions *Administration, Contents, Pull requests, Actions, Secrets,
# MAGIC   Variables, Environments*: read and write; *Deployments, Checks, Commit statuses*: read.
# MAGIC * You run this notebook from the Git folder of the repository, on Serverless compute, as a workspace admin.

# COMMAND ----------

# MAGIC %md
# MAGIC # Part A · Architecture
# MAGIC
# MAGIC ## A1 · The whole system on one page
# MAGIC
# MAGIC ```text
# MAGIC  People                      GitHub (the source of truth)                         Databricks
# MAGIC  ------                      ----------------------------                         ----------
# MAGIC                     +--------------------------------------------------+
# MAGIC  developer  ------> | feature branch --> pull request                  |
# MAGIC  (Git folder,       |                      | checks: demo (all tests),  |
# MAGIC   laptop)           |                      |  genie-ci (space, bundles) |
# MAGIC                     |                      | review                     |
# MAGIC                     |                      v                            |
# MAGIC                     | main --push--> genie-release                      |      one service principal
# MAGIC                     |                 1 dev-snapshot (UI edits -> PR)   |      per environment
# MAGIC                     |                 2 dev   --------------------------|-->  freshcart_dev   [DEV]
# MAGIC                     |                 3 qa    (refresh, drift, gate)  --|-->  freshcart_qa    [QA]
# MAGIC  approver  -------> |                   [approval: environment prod]    |
# MAGIC                     |                 4 prod  (refresh, drift, smoke) --|-->  freshcart       (prod)
# MAGIC                     |                   tag genie-prod-<time>-<sha>     |
# MAGIC                     |                                                   |
# MAGIC  Genie UI edit ---> | genie-dev-sync (hourly): dev space -> commit,     |
# MAGIC  (dev)              |   tag genie-dev-snapshot-<time>, pull request     |
# MAGIC                     | genie-rollback (by hand): any tag/commit -> env   |
# MAGIC                     +--------------------------------------------------+
# MAGIC                           sign-in: GitHub OIDC (federation policy) or the service principal's OAuth secret
# MAGIC
# MAGIC  In each environment, two bundles, data first:
# MAGIC    databricks/   (freshcart-data)   pipeline bronze+silver, job freshcart_refresh: setup > pipeline > gold >
# MAGIC                                     security + semantic > monitor (health checks, history, failure e-mails)
# MAGIC    genie_bundle/ (freshcart-genie)  Genie space (content = one YAML file) + job genie_quality_gate
# MAGIC                                     (benchmarks in qa, smoke test after prod deploys, nightly in prod)
# MAGIC ```
# MAGIC
# MAGIC ## A2 · Principles
# MAGIC
# MAGIC | Principle | How it is implemented |
# MAGIC |---|---|
# MAGIC | **Everything is code** | Tables, pipeline, jobs, grants, metric views, the Genie space and its benchmarks are files in git. The workspace is an output. |
# MAGIC | **Build once, deploy many** | Every environment gets the *same commit*. Targets differ only in variables: catalog, names, permissions, schedules. |
# MAGIC | **Promotion by pipeline, not by people** | Only GitHub Actions deploys to qa and prod, as a service principal. People merge pull requests and approve prod. |
# MAGIC | **Gates before prod** | Tests on every pull request; in qa the data refresh with health checks, the Genie benchmark gate and a smoke test; then a human approval. |
# MAGIC | **Nothing is lost** | Edits made in the dev Genie UI are exported into git (tagged snapshots). Edits in qa/prod block the release instead of being overwritten (drift gate). A backup of the live space is kept before every deploy. |
# MAGIC | **Every version can come back** | Each prod release is a git tag. `genie-rollback` deploys any tag or commit to any environment. |
# MAGIC | **You hear about problems first** | Health checks after every refresh, nightly Genie regression run, job failure e-mails, GitHub run notifications, system tables. |
# MAGIC
# MAGIC ## A3 · Environments
# MAGIC
# MAGIC | | dev | qa | prod |
# MAGIC |---|---|---|---|
# MAGIC | Catalog | `freshcart_dev` | `freshcart_qa` | `freshcart` |
# MAGIC | Bundle mode | production (shared) | production | production |
# MAGIC | Deployed by | `freshcart-deployer-dev` | `freshcart-deployer-qa` | `freshcart-deployer-prod` |
# MAGIC | When | every merge to `main` | after dev | after qa passed **and** a reviewer approved |
# MAGIC | Gates | — | refresh + health checks, benchmark gate, smoke test | drift check, refresh + health checks, smoke test (auto-restore on failure) |
# MAGIC | Schedules | — | — | refresh 05:00 UTC, Genie regression 06:30 UTC |
# MAGIC | Developers can | edit (Genie UI, jobs) | view | view |
# MAGIC
# MAGIC One workspace with three catalogs is the simplest setup. For separate workspaces, set `workspace.host` per target
# MAGIC in both `databricks.yml` files and `DATABRICKS_HOST` per GitHub environment; nothing else changes.
# MAGIC
# MAGIC ## A4 · Identities and access
# MAGIC
# MAGIC | Who | What | How |
# MAGIC |---|---|---|
# MAGIC | Developers (`freshcart-genie-developers`) | change code through pull requests; edit the dev Genie space | GitHub write access; `CAN_MANAGE` in dev only |
# MAGIC | Approvers | approve prod deployments | required reviewers on the GitHub environment `prod` |
# MAGIC | GitHub Actions | deploy | one **service principal** per environment, in `freshcart-genie-deployers`; signs in with OIDC (no secret) or its OAuth secret stored as a GitHub environment secret |
# MAGIC | Business users (`freshcart-business-users`) | ask Genie | `CAN_RUN` on the space; `SELECT` on gold and semantic; row filters and masks apply |
# MAGIC
# MAGIC ## A5 · Version control, rollback and monitoring at a glance
# MAGIC
# MAGIC | Question | Answer |
# MAGIC |---|---|
# MAGIC | Where is version N of the Genie space? | `git show genie-prod-<...>:genie_bundle/resources/freshcart_assistant.space.yml` |
# MAGIC | What is running in prod now? | Part E compares the live space with every release tag |
# MAGIC | Who changed the dev space in the UI, and what? | the `genie-dev-sync` pull requests and `genie-dev-snapshot-*` tags |
# MAGIC | Roll back the Genie space only | `genie-rollback`, what = `genie-space` |
# MAGIC | Roll back the pipeline and SQL | `genie-rollback`, what = `data-bundle` (rebuilds the data) |
# MAGIC | Is the data fresh and sane? | `<catalog>.monitoring.health_checks`, written after every refresh |
# MAGIC | Is Genie still answering correctly? | the nightly `genie_quality_gate` in prod, and the evaluation history (Part I) |

# COMMAND ----------

# MAGIC %md
# MAGIC # Part B · One-time setup
# MAGIC
# MAGIC ## B1 · Parameters and connections
# MAGIC
# MAGIC | Widget | Meaning |
# MAGIC |---|---|
# MAGIC | github_repo | `owner/name` of the repository on GitHub |
# MAGIC | github_token_secret | where the token is kept: `<secret scope>/<key>` in Databricks secrets |
# MAGIC | github_token | paste the token **once**: it is stored in the secret above; then clear this widget |
# MAGIC | auth_mode | how GitHub Actions signs in to Databricks: `secret` (an OAuth secret per service principal, fully automated here) or `oidc` (no secret at all; an account admin adds a federation policy, see B3) |
# MAGIC | reviewers | GitHub logins who approve prod deployments, comma separated (blank = you) |
# MAGIC | apply_changes | `no` prints what would change; `yes` changes it |

# COMMAND ----------

# MAGIC %pip install --quiet "databricks-sdk>=0.145.0" pyyaml pynacl

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("github_repo", "subrata-samanta/ai-ready-retail-data-genie-demo", "1 GitHub repository")
dbutils.widgets.text("github_token_secret", "freshcart/github_token", "2 token secret (scope/key)")
dbutils.widgets.text("github_token", "", "3 paste token once (then clear)")
dbutils.widgets.dropdown("auth_mode", "secret", ["secret", "oidc"], "4 GitHub sign-in to Databricks")
dbutils.widgets.text("reviewers", "", "5 prod approvers (GitHub logins)")
dbutils.widgets.dropdown("apply_changes", "no", ["no", "yes"], "6 apply changes")

REPO_NAME = dbutils.widgets.get("github_repo").strip()
AUTH_MODE = dbutils.widgets.get("auth_mode")
APPLY = dbutils.widgets.get("apply_changes") == "yes"
print(f"repository {REPO_NAME}   sign-in {AUTH_MODE}   apply_changes={APPLY}")

# COMMAND ----------

import base64, json, os, platform, shutil, subprocess, sys, tempfile, time, urllib.request, uuid, zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml
from databricks.sdk import WorkspaceClient
from databricks.sdk.service import iam, iamv2
from databricks.sdk.service import sql as dbsql

GITHUB_API = "https://api.github.com"
WORK = Path("/tmp/freshcart")
VOLUMES = Path("/Volumes")
ENVIRONMENTS = ["dev", "qa", "prod"]


def find_repo() -> Path:
    """The repository root: the Git folder that contains this notebook (in notebooks/)."""
    for c in [Path.cwd().parent, Path.cwd()]:
        if (c / "genie_bundle" / "databricks.yml").exists():
            return c
    raise RuntimeError("Open this notebook from the repository's Git folder.")


REPO = find_repo()
sys.path.insert(0, str(REPO / "genie_bundle" / "scripts"))
sys.path.insert(0, str(REPO / "databricks" / "src"))
import run_sql                                   # the SQL runner of the data bundle
import space_tools as T                          # read, compare and hash Genie space definitions

CATALOG = {env: cfg["variables"]["catalog"]      # dev -> freshcart_dev, qa -> freshcart_qa, prod -> freshcart
           for env, cfg in yaml.safe_load((REPO / "databricks" / "databricks.yml").read_text())["targets"].items()}
w = WorkspaceClient()
me = w.current_user.me()
HOST = w.config.host.rstrip("/")
print(f"Databricks {HOST} as {me.user_name};  catalogs {CATALOG}")

# COMMAND ----------

# MAGIC %md
# MAGIC The token is read from Databricks secrets, never from the notebook. The first time, paste it into the widget
# MAGIC **github_token**: this cell stores it in the secret scope, and you clear the widget.

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
    """GET that returns `default` instead of failing when the object does not exist (404)."""
    r = requests.get(GITHUB_API + path, headers=HEADERS, timeout=60)
    if r.status_code == 404:
        return default
    if r.status_code != 200:
        raise RuntimeError(f"GitHub GET {path} -> {r.status_code}: {r.text[:400]}")
    return r.json()


def change(what: str, method: str, path: str, **kw):
    """A change to GitHub: done when apply_changes = yes, otherwise only printed."""
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
print(f"GitHub {REPO_NAME} as {user['login']} (admin); default branch {repo['default_branch']}; "
      f"{repo['visibility']}; prod approvers {REVIEWERS}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## B2 · Databricks: one service principal per environment
# MAGIC
# MAGIC A **service principal** is an identity for automation. Each environment gets its own, so a compromised qa
# MAGIC pipeline can never touch prod. For each environment this cell:
# MAGIC
# MAGIC 1. creates `freshcart-deployer-<env>` (or finds it),
# MAGIC 2. adds it to `freshcart-genie-deployers` (managers of everything the bundles deploy) and to `fc_all_regions`
# MAGIC    (the quality gate must see every row),
# MAGIC 3. makes sure the environment's catalog exists, with the landing volume and the raw files (CI never creates
# MAGIC    catalogs; in a real company the source systems deliver the files),
# MAGIC 4. grants it everything inside that catalog (`ALL PRIVILEGES`, and `MANAGE` so it can replace objects you created
# MAGIC    by hand), and `CAN_USE` on the SQL warehouse,
# MAGIC 5. with auth_mode `secret`: creates an OAuth secret for it, kept in memory only and written to GitHub in B3.

# COMMAND ----------

PATCH = [iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP]


def group_id(name: str) -> str:
    found = list(w.groups.list(filter=f'displayName eq "{name}"', attributes="id,displayName"))
    if not found:
        raise RuntimeError(f"group {name} is missing: run step 4 of the first notebook")
    return found[0].id


def add_member(group: str, principal_id: str) -> None:
    """Add a principal to a group: through the account (account groups) or the workspace (workspace groups)."""
    gid = group_id(group)
    if principal_id in {m.value for m in (w.groups.get(gid).members or [])}:
        return
    try:
        w.workspace_iam_v2.create_direct_group_member_proxy(int(gid), iamv2.DirectGroupMember(principal_id=int(principal_id)))
    except Exception:                            # noqa: BLE001
        w.groups.patch(gid, schemas=PATCH, operations=[
            iam.Patch(op=iam.PatchOp.ADD, path="members", value=[{"value": principal_id}])])


def warehouse_id() -> str:
    whs = list(w.warehouses.list())
    serverless = [x for x in whs if x.enable_serverless_compute] or whs
    return sorted(serverless, key=lambda x: x.name != "Serverless Starter Warehouse")[0].id


def prepare_catalog(env: str) -> None:
    """Catalog, landing volume and raw files for env (what an admin and the source systems provide)."""
    cat = CATALOG[env]
    spark.sql(f"CREATE CATALOG IF NOT EXISTS `{cat}`")
    run_sql.run_files(spark, [REPO / "databricks" / "00_setup.sql"], cat, echo=lambda *_: None)
    raw, dst = REPO / "data" / "raw", VOLUMES / cat / "landing" / "raw"
    copied = 0
    for src in (p for p in raw.rglob("*") if p.is_file() and p.name != "README.md"):
        target = dst / src.relative_to(raw)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            copied += 1
    print(f"  {cat}: catalog, landing volume, {copied} new raw file(s)")


WAREHOUSE_ID = warehouse_id()
DEPLOYERS, SECRETS = {}, {}                      # env -> service principal; env -> OAuth secret (memory only)
for env in ENVIRONMENTS:
    name = f"freshcart-deployer-{env}"
    sp = next(iter(w.service_principals.list(filter=f'displayName eq "{name}"')), None)
    if sp is None and APPLY:
        sp = w.service_principals.create(display_name=name)
    if sp is None:
        print(f"[dry run] {env}: create {name}, add to groups, prepare {CATALOG[env]}, grant, warehouse CAN_USE"
              + (", OAuth secret" if AUTH_MODE == "secret" else ""))
        continue
    DEPLOYERS[env] = sp
    if not APPLY:
        print(f"[dry run] {env}: {name} exists ({sp.application_id}); would add groups, grants and secret")
        continue
    for g in ("freshcart-genie-deployers", "fc_all_regions"):
        add_member(g, sp.id)
    prepare_catalog(env)
    spark.sql(f"GRANT ALL PRIVILEGES ON CATALOG `{CATALOG[env]}` TO `{sp.application_id}`")
    try:
        spark.sql(f"GRANT MANAGE ON CATALOG `{CATALOG[env]}` TO `{sp.application_id}`")
    except Exception as e:                       # noqa: BLE001
        print(f"  note: MANAGE could not be granted ({str(e).splitlines()[0][:100]}); objects you created by hand "
              f"stay yours, so drop them or transfer their ownership if CI cannot replace them")
    w.warehouses.update_permissions(WAREHOUSE_ID, access_control_list=[dbsql.WarehouseAccessControlRequest(
        service_principal_name=sp.application_id, permission_level=dbsql.WarehousePermissionLevel.CAN_USE)])
    if AUTH_MODE == "secret":
        SECRETS[env] = w.service_principal_secrets_proxy.create(service_principal_id=sp.id).secret
    print(f"{env}: {name} ({sp.application_id}) ready" + (" + OAuth secret" if env in SECRETS else ""))

display(pd.DataFrame([{"environment": e, "service principal": s.display_name, "application id": s.application_id,
                       "catalog": CATALOG[e]} for e, s in DEPLOYERS.items()]))

# COMMAND ----------

# MAGIC %md
# MAGIC ## B3 · GitHub: branch, environments, approvals, protection
# MAGIC
# MAGIC This cell makes the repository match the architecture:
# MAGIC
# MAGIC | Setting | Value | Why |
# MAGIC |---|---|---|
# MAGIC | Branch `main` | created from the current default branch, made the default | releases run from `main` only |
# MAGIC | Environments `dev`, `qa`, `prod` | variables `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`; secret `DATABRICKS_CLIENT_SECRET` (auth_mode `secret`) | each job of the release signs in as its environment's service principal |
# MAGIC | `prod` | required reviewers; deployments only from `main` | a human approves every prod change |
# MAGIC | `qa` | deployments only from `main` | qa always tests what will reach prod |
# MAGIC | Branch protection on `main` | pull request required, check `run-demo` must pass, no force pushes | nothing reaches `main` untested |
# MAGIC | Workflow permissions | Actions may create pull requests | `genie-dev-sync` opens the sync pull requests |
# MAGIC
# MAGIC Secrets are encrypted in this notebook with the repository environment's public key before they are sent
# MAGIC (GitHub's required libsodium "sealed box"); nobody can read them back, not even admins.
# MAGIC
# MAGIC With auth_mode **`oidc`** no secret is stored. Instead an account admin adds, for each service principal, a
# MAGIC federation policy that trusts GitHub's tokens for this repository and environment. The commands are printed below;
# MAGIC run them where the Databricks CLI is logged in to the **account** (`databricks auth login --host
# MAGIC https://accounts.cloud.databricks.com --account-id <id>`), or use the account console (*User management* →
# MAGIC *Service principals* → the principal → *Credentials & secrets* → *Federation policies*).

# COMMAND ----------

from nacl import encoding, public


def sealed(public_key_b64: str, value: str) -> str:
    box = public.SealedBox(public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder()))
    return base64.b64encode(box.encrypt(value.encode())).decode()


# main: create it from the current default branch and make it the default
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
              else "<application id of freshcart-deployer-" + env + ">"}
    for name, value in values.items():
        exists = APPLY and gh_get(f"{R}/environments/{env}/variables/{name}") is not None
        if exists:
            change(f"{env}: variable {name}", "PATCH", f"{R}/environments/{env}/variables/{name}",
                   json={"name": name, "value": value})
        else:
            change(f"{env}: variable {name}", "POST", f"{R}/environments/{env}/variables",
                   json={"name": name, "value": value})
    if AUTH_MODE == "secret" and env in SECRETS:
        key = gh("GET", f"{R}/environments/{env}/secrets/public-key")
        change(f"{env}: secret DATABRICKS_CLIENT_SECRET", "PUT", f"{R}/environments/{env}/secrets/DATABRICKS_CLIENT_SECRET",
               json={"encrypted_value": sealed(key["key"], SECRETS[env]), "key_id": key["key_id"]})

change("protect main", "PUT", f"{R}/branches/main/protection", json={
    "required_status_checks": {"strict": False, "contexts": ["run-demo"]},
    "enforce_admins": False,
    "required_pull_request_reviews": {"required_approving_review_count": 0},
    "restrictions": None, "allow_force_pushes": False, "allow_deletions": False})
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
# MAGIC **Switch the Git folder to `main`.** In the Git folder, open the Git dialog (the branch name at the top) and check
# MAGIC out `main`. From now on you make changes on a feature branch and merge them through a pull request.

# COMMAND ----------

# MAGIC %md
# MAGIC # Part C · Hand dev over to CI/CD
# MAGIC
# MAGIC In the first notebook you deployed dev **as yourself**. A bundle remembers its deployment per identity, so the
# MAGIC first release (as `freshcart-deployer-dev`) would create a second pipeline next to yours, and two pipelines cannot
# MAGIC own the same tables. So you remove your own deployments once; the release recreates them as the service principal.
# MAGIC
# MAGIC What happens to the data: the raw files in the volume and the gold tables stay. The pipeline's bronze and silver
# MAGIC tables are removed with your pipeline and rebuilt by the first release's refresh (a few minutes).

# COMMAND ----------

CLI_VERSION = "1.18.0"


def cli() -> Path:
    """The Databricks CLI on this compute (downloaded once, as in the first notebook)."""
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


def destroy_my_deployment(folder: str, env: str) -> None:
    """databricks bundle destroy, as you, for a bundle you deployed by hand."""
    work = WORK / "handover" / folder
    shutil.copytree(REPO / folder, work, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".databricks"))
    auth = w.config.authenticate()["Authorization"].removeprefix("Bearer ")
    env_vars = {k: v for k, v in os.environ.items() if not k.startswith(("DATABRICKS_", "BUNDLE_VAR_"))}
    env_vars.update(DATABRICKS_HOST=HOST, DATABRICKS_TOKEN=auth, DATABRICKS_AUTH_TYPE="pat",
                    BUNDLE_VAR_warehouse_id=WAREHOUSE_ID)
    p = subprocess.run([str(cli()), "bundle", "destroy", "-t", env, "--auto-approve"], cwd=work, env=env_vars,
                       capture_output=True, text=True)
    print(f"  {folder} {env}: " + ((p.stdout + p.stderr).strip().splitlines() or ["(no output)"])[-1])


for env in ENVIRONMENTS:
    if not APPLY:
        print(f"[dry run] {env}: bundle destroy of your own genie_bundle and databricks deployments")
        continue
    destroy_my_deployment("genie_bundle", env)       # Genie first: it reads the data
    destroy_my_deployment("databricks", env)

# COMMAND ----------

# MAGIC %md
# MAGIC # Part D · Ship a change from a branch to production
# MAGIC
# MAGIC The whole path of one change, for real:
# MAGIC
# MAGIC ```text
# MAGIC  branch --commit--> pull request --checks (demo, genie-ci)--> merge to main
# MAGIC    --> genie-release: dev-snapshot, dev, qa (refresh, health, drift, gate, smoke)
# MAGIC    --> waiting for approval (prod) --approve--> prod (refresh, health, drift, deploy, smoke) --> tag
# MAGIC ```
# MAGIC
# MAGIC The change: a new sample question in the Genie space, the smallest change that still goes through every stage.
# MAGIC The helpers below wait for GitHub and print each status change; a cell can be re-run to keep waiting.

# COMMAND ----------

SPACE_PATH = "genie_bundle/resources/freshcart_assistant.space.yml"


def base_branch() -> str:
    """main once B3 created it; before that (a dry run) the repository's default branch."""
    return "main" if gh_get(f"{R}/branches/main") else repo["default_branch"]


def wait_until(describe, done, timeout_min: float = 90, every_s: int = 30):
    """Call describe() until done(state) is true; print the state whenever it changes. Returns the last state."""
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
    """The newest run of a workflow file, for a commit (sha) or started after a time (since, ISO)."""
    q = f"?per_page=20" + (f"&head_sha={sha}" if sha else "")
    runs = gh("GET", f"{R}/actions/workflows/{workflow}/runs{q}")["workflow_runs"]
    runs = [r for r in runs if not since or r["created_at"] >= since]
    return runs[0] if runs else None


def run_state(run_id: int) -> str:
    run = gh("GET", f"{R}/actions/runs/{run_id}")
    jobs = gh("GET", f"{R}/actions/runs/{run_id}/jobs")["jobs"]
    parts = [f"{j['name']}={j['conclusion'] or j['status']}" for j in jobs]
    return f"{run['status']}/{run['conclusion'] or '-'}  " + "  ".join(parts)


def approve(run_id: int, comment: str) -> bool:
    """Approve the run's waiting deployments (prod) if you are allowed to; True if something was approved."""
    pending = gh("GET", f"{R}/actions/runs/{run_id}/pending_deployments")
    ids = [p["environment"]["id"] for p in pending if p.get("current_user_can_approve")]
    if ids:
        change(f"approve {[p['environment']['name'] for p in pending]}", "POST",
               f"{R}/actions/runs/{run_id}/pending_deployments",
               json={"environment_ids": ids, "state": "approved", "comment": comment})
    return bool(ids)


def watch_release(run: dict, approve_prod: bool = True) -> str:
    """Follow a genie-release (or genie-rollback) run to the end, approving prod when it waits."""
    print(f"run {run['id']}: {run['html_url']}")
    while True:
        state = wait_until(lambda: run_state(run["id"]), lambda s: s.startswith(("completed", "waiting")))
        if state.startswith("waiting") and approve_prod and APPLY and approve(run["id"], "Approved from the notebook"):
            continue
        return state

# COMMAND ----------

# MAGIC %md
# MAGIC ### D1 · A branch, a commit and a pull request
# MAGIC
# MAGIC The file is changed with the repository's own tools (`space_tools`), so it stays in canonical form and the pull
# MAGIC request shows a one-line diff.

# COMMAND ----------

QUESTION = "Which five stores sold the most last week?"
BRANCH = f"feature/sample-question-{datetime.now(timezone.utc):%Y%m%d%H%M}"

BASE = base_branch()
main_sha = gh("GET", f"{R}/git/ref/heads/{BASE}")["object"]["sha"]
current = gh("GET", f"{R}/contents/{SPACE_PATH}?ref={BASE}")     # the file on main, and its blob sha
with tempfile.TemporaryDirectory() as tmp:
    before, after = Path(tmp) / "before.yml", Path(tmp) / "after.yml"
    before.write_bytes(base64.b64decode(current["content"]))
    space = T.load_space(before)
    questions = space.setdefault("config", {}).setdefault("sample_questions", [])
    if not any(QUESTION in q["question"] for q in questions):
        questions.append({"id": uuid.uuid4().hex, "question": [QUESTION]})
    T.save_space(space, after)                   # canonical form: the pull request shows only this change
    new_content = after.read_text()
    print("\n".join(T.diff(T.load_space(before), T.load_space(after))) or "(the question is already there)")

change(f"create branch {BRANCH}", "POST", f"{R}/git/refs", json={"ref": f"refs/heads/{BRANCH}", "sha": main_sha})
change("commit the change", "PUT", f"{R}/contents/{SPACE_PATH}", json={
    "message": f"Genie: add the sample question '{QUESTION}'", "branch": BRANCH, "sha": current["sha"],
    "content": base64.b64encode(new_content.encode()).decode()})
pr = change("open the pull request", "POST", f"{R}/pulls", json={
    "title": f"Genie: add the sample question '{QUESTION}'", "head": BRANCH, "base": "main",
    "body": "A new sample question for the FreshCart Sales & Stock Assistant. Opened by the CI/CD notebook."})
if pr:
    print(f"pull request #{pr['number']}: {pr['html_url']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### D2 · The checks on the pull request
# MAGIC
# MAGIC `demo` runs the whole local pipeline and every test (including the bundles with the real Databricks CLI against a
# MAGIC stand-in workspace). `genie-ci` validates the space, runs every SQL of it, validates both bundles for every target,
# MAGIC and writes the list of changes in the space into the check's summary: that is what the reviewer reads.

# COMMAND ----------

def checks(sha: str) -> str:
    runs = gh("GET", f"{R}/commits/{sha}/check-runs")["check_runs"]
    return "  ".join(sorted(f"{c['name']}={c['conclusion'] or c['status']}" for c in runs)) or "no checks yet"


if pr:
    head = gh("GET", f"{R}/pulls/{pr['number']}")["head"]["sha"]
    state = wait_until(lambda: checks(head), lambda s: s != "no checks yet" and "in_progress" not in s
                       and "queued" not in s, timeout_min=30)
    print("all checks passed" if "failure" not in state else "a check failed: open the pull request to see why")

# COMMAND ----------

# MAGIC %md
# MAGIC ### D3 · Merge, then follow the release to production
# MAGIC
# MAGIC Merging starts `genie-release` for the merge commit. It stops at **prod** until an approver approves; with
# MAGIC `apply_changes = yes` and you among the approvers, the notebook approves for you (in real life the approver does
# MAGIC it on the run page, after reading the qa results). The full run takes roughly 30 to 60 minutes, mostly the three
# MAGIC data refreshes.

# COMMAND ----------

if pr:
    merged = change("merge the pull request (squash)", "PUT", f"{R}/pulls/{pr['number']}/merge",
                    json={"merge_method": "squash"})
    MERGE_SHA = merged["sha"]
    release = wait_until(lambda: run_for("genie-release.yml", sha=MERGE_SHA), lambda r: r is not None, timeout_min=10,
                         every_s=10)
    final = watch_release(release)
    print("released to prod" if final.startswith("completed/success") else f"the release did not finish: {final}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Part E · Version history: what is live where
# MAGIC
# MAGIC Every prod release is a tag `genie-prod-<UTC time>-<commit>`; every snapshot of the dev Genie UI is a tag
# MAGIC `genie-dev-snapshot-<time>`. GitHub also records each deployment per environment. This part answers the two
# MAGIC questions every operator asks: *which version is running in prod?* and *what changed between versions?*
# MAGIC
# MAGIC "Which version is live" is answered from the content, not from bookkeeping: the live Genie space of each
# MAGIC environment is exported, brought to environment-neutral form, hashed, and compared with the space file of every
# MAGIC release tag.

# COMMAND ----------

tags = gh("GET", f"{R}/tags?per_page=100")
releases = sorted((t["name"] for t in tags if t["name"].startswith("genie-prod-")), reverse=True)
snapshots = sorted((t["name"] for t in tags if t["name"].startswith("genie-dev-snapshot-")), reverse=True)
print(f"{len(releases)} prod release(s), latest {releases[:3]};  {len(snapshots)} dev snapshot(s), latest {snapshots[:3]}")


def hash_at(ref: str) -> str:
    content = gh("GET", f"{R}/contents/{SPACE_PATH}?ref={ref}")["content"]
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "space.yml"
        p.write_bytes(base64.b64decode(content))
        return T.content_hash(T.load_space(p))


def live_space(env: str):
    """The Genie space of env, found by its title (prod has no suffix)."""
    title = "FreshCart Sales & Stock Assistant" + {"dev": " [DEV]", "qa": " [QA]", "prod": ""}[env]
    spaces = [s for s in (w.genie.list_spaces().spaces or []) if s.title == title]
    return w.genie.get_space(spaces[-1].space_id, include_serialized_space=True) if spaces else None


release_hash = {tag: hash_at(tag) for tag in releases[:10]}
main_hash = hash_at(base_branch())
rows = []
for env in ENVIRONMENTS:
    sp = live_space(env)
    if sp is None:
        rows.append({"environment": env, "live version": "(no space)"})
        continue
    h = T.content_hash(T.neutralise(json.loads(sp.serialized_space), CATALOG[env]))
    match = next((tag for tag, th in release_hash.items() if th == h), None)
    rows.append({"environment": env, "content hash": h, "live version": match or "not a prod release",
                 "matches main": h == main_hash})
display(pd.DataFrame(rows))
for r in rows:
    print(f"{r['environment']:>5}: live version {r['live version']}" +
          ("" if "matches main" not in r else f", matches main: {r['matches main']}"))

# COMMAND ----------

# MAGIC %md
# MAGIC The history of the space file, and the deployments GitHub recorded (who, when, which commit, per environment):

# COMMAND ----------

commits = gh("GET", f"{R}/commits?path={SPACE_PATH}&sha={base_branch()}&per_page=15")
display(pd.DataFrame([{"date": c["commit"]["committer"]["date"], "commit": c["sha"][:7],
                       "author": c["commit"]["author"]["name"], "message": c["commit"]["message"].splitlines()[0]}
                      for c in commits]))
deployments = []
for env in ENVIRONMENTS:
    for d in gh("GET", f"{R}/deployments?environment={env}&per_page=5"):
        deployments.append({"environment": env, "created": d["created_at"], "commit": d["sha"][:7],
                            "by": (d.get("creator") or {}).get("login")})
display(pd.DataFrame(deployments))

# COMMAND ----------

# MAGIC %md
# MAGIC # Part F · Edits made in the Genie UI become versions
# MAGIC
# MAGIC People improve the dev space in the Genie UI. `genie-dev-sync` (hourly, and at the start of every release)
# MAGIC exports it, commits it on the branch `genie/dev-sync`, tags the commit `genie-dev-snapshot-<time>` and opens or
# MAGIC updates a pull request. Reviewing and merging that pull request is how a UI edit is promoted. This part makes an
# MAGIC edit through the API (the same as an edit in the UI), runs the sync now and shows the pull request.

# COMMAND ----------

UI_QUESTION = "What were online sales by region last week?"
dev = live_space("dev")
if dev and APPLY:
    content = json.loads(dev.serialized_space)
    questions = content.setdefault("config", {}).setdefault("sample_questions", [])
    if not any(UI_QUESTION in q["question"] for q in questions):
        questions.append({"id": uuid.uuid4().hex, "question": [UI_QUESTION]})
        questions.sort(key=lambda q: q["id"])
        w.genie.update_space(dev.space_id, serialized_space=json.dumps(content), etag=dev.etag)
    print(f"edited the dev space: added {UI_QUESTION!r}")
started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
change("run genie-dev-sync now", "POST", f"{R}/actions/workflows/genie-dev-sync.yml/dispatches", json={"ref": "main"})
if APPLY:
    sync = wait_until(lambda: run_for("genie-dev-sync.yml", since=started), lambda r: r is not None, 10, 10)
    print(wait_until(lambda: run_state(sync["id"]), lambda s: s.startswith("completed"), 20, 15))
    owner = REPO_NAME.split("/")[0]
    prs = gh("GET", f"{R}/pulls?head={owner}:genie/dev-sync&state=open")
    for p in prs:
        print(f"sync pull request #{p['number']}: {p['html_url']}")
        for f in gh("GET", f"{R}/pulls/{p['number']}/files"):
            print(f["filename"]); print(f.get("patch", "")[:1500])

# COMMAND ----------

# MAGIC %md
# MAGIC Merge that pull request like any other (Part D) to release the edit, or close it to discard the edit: the next
# MAGIC release then puts the dev space back to what is in git, and the snapshot tag keeps the discarded version.

# COMMAND ----------

# MAGIC %md
# MAGIC # Part G · Drift protection: an edit in prod blocks the release
# MAGIC
# MAGIC Nobody should edit qa or prod directly, but if someone does, the next `bundle deploy` would silently overwrite
# MAGIC it. The release therefore runs `bundle plan` first: the bundle knows the version (etag) it deployed, sees the
# MAGIC live one, and `scripts/check_drift.py` stops the release with exit code 2 and the list of differences. The live
# MAGIC space is saved as a build artifact either way.
# MAGIC
# MAGIC This cell edits the prod space, starts a release and shows it stop in prod. Then choose: adopt the edit (export
# MAGIC it with `scripts/sync_from_workspace.py -t prod` into a pull request) or overwrite it (run `genie-release` by hand
# MAGIC with **allow_drift**; the backup artifact keeps the edit).

# COMMAND ----------

prod = live_space("prod")
RUN_DRIFT_DEMO = APPLY and prod is not None
if RUN_DRIFT_DEMO:
    content = json.loads(prod.serialized_space)
    content["config"]["sample_questions"].append({"id": uuid.uuid4().hex, "question": ["Edited directly in prod"]})
    content["config"]["sample_questions"].sort(key=lambda q: q["id"])
    w.genie.update_space(prod.space_id, serialized_space=json.dumps(content), etag=prod.etag)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    change("run genie-release", "POST", f"{R}/actions/workflows/genie-release.yml/dispatches",
           json={"ref": "main", "inputs": {"refresh_data": "false"}})
    run = wait_until(lambda: run_for("genie-release.yml", since=started), lambda r: r is not None, 10, 10)
    final = watch_release(run)
    print("blocked by the drift gate, as intended" if "prod=failure" in final else final)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    change("overwrite the prod edit (allow_drift)", "POST", f"{R}/actions/workflows/genie-release.yml/dispatches",
           json={"ref": "main", "inputs": {"refresh_data": "false", "allow_drift": "true"}})
    run = wait_until(lambda: run_for("genie-release.yml", since=started), lambda r: r is not None, 10, 10)
    print(watch_release(run))
else:
    print("skipped (needs apply_changes = yes and a prod space)")

# COMMAND ----------

# MAGIC %md
# MAGIC # Part H · Roll back prod
# MAGIC
# MAGIC `genie-rollback` deploys an earlier version to one environment: `to = previous` is the release before the latest
# MAGIC tag, or any tag or commit. `what` chooses the scope: only the Genie space, the whole Genie bundle, the data bundle
# MAGIC (pipeline, jobs and SQL, then a rebuild) or everything. A rollback to prod waits for the same approval as a release.
# MAGIC Afterwards, fix forward on `main` (for example revert the bad commit), or the next release brings it back.

# COMMAND ----------

releases = sorted((t["name"] for t in gh("GET", f"{R}/tags?per_page=100") if t["name"].startswith("genie-prod-")),
                  reverse=True)                  # read again: Parts D and G may have added releases
if APPLY and len(releases) >= 2:
    print(f"prod runs {releases[0]}; rolling back to {releases[1]}")
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    change("run genie-rollback", "POST", f"{R}/actions/workflows/genie-rollback.yml/dispatches", json={
        "ref": "main", "inputs": {"environment": "prod", "to": "previous", "what": "genie-space",
                                  "reason": "Rollback exercise from the CI/CD notebook"}})
    run = wait_until(lambda: run_for("genie-rollback.yml", since=started), lambda r: r is not None, 10, 10)
    print(watch_release(run))
    live = T.content_hash(T.neutralise(json.loads(live_space("prod").serialized_space), CATALOG["prod"]))
    print(f"prod now runs {releases[1]}: {live == hash_at(releases[1])}")
else:
    print("skipped: needs apply_changes = yes and two prod releases (run Part D twice)")

# COMMAND ----------

# MAGIC %md
# MAGIC # Part I · Monitoring and alerting
# MAGIC
# MAGIC | Layer | What is watched | Where | Who is told |
# MAGIC |---|---|---|---|
# MAGIC | Data health | freshness of sales and stock, volume last week, rejected POS rows, unknown products and stores | task `monitor` after every refresh → `<catalog>.monitoring.health_checks` | job failure e-mail (`alert_emails`) |
# MAGIC | Data quality in the pipeline | every expectation, rows passed and dropped | the pipeline's event log | pipeline UI, this notebook |
# MAGIC | Jobs | every refresh and gate run: status, duration | Jobs API, `system.lakeflow.job_run_timeline` | job failure e-mail |
# MAGIC | Genie answers | the benchmarks, nightly in prod | evaluation runs of the space | gate job failure e-mail |
# MAGIC | Deployments | every release and rollback | GitHub Actions runs and environments | GitHub notifications |
# MAGIC | Cost | DBUs of the jobs and pipelines | `system.billing.usage` | budgets (account console) |
# MAGIC
# MAGIC **Turn on e-mails:** set `alert_emails` in each target of `databricks/databricks.yml` and
# MAGIC `genie_bundle/databricks.yml`, for example `alert_emails: [data-team@freshcart.example]`, in a pull request. The
# MAGIC next release applies it.
# MAGIC
# MAGIC ### I1 · Data health history

# COMMAND ----------

health = []
for env in ENVIRONMENTS:
    try:
        df = spark.sql(f"SELECT '{env}' AS environment, checked_at, check_name, observed, threshold, rule, passed "
                       f"FROM `{CATALOG[env]}`.monitoring.health_checks "
                       f"QUALIFY dense_rank() OVER (ORDER BY checked_at DESC) <= 1").toPandas()
        health.append(df)
    except Exception as e:                       # noqa: BLE001
        print(f"{env}: no health checks yet ({str(e).splitlines()[0][:80]})")
if health:
    display(pd.concat(health))

# COMMAND ----------

# MAGIC %md
# MAGIC ### I2 · Pipeline data quality (expectations)
# MAGIC
# MAGIC The pipeline records every expectation in its event log: how many rows passed and how many were dropped.

# COMMAND ----------

for env in ENVIRONMENTS:
    try:
        display(spark.sql(f'''
            SELECT '{env}' AS environment, timestamp, e.dataset, e.name AS expectation, e.passed_records, e.failed_records
            FROM event_log(TABLE(`{CATALOG[env]}`.silver.pos_sales_line))
            LATERAL VIEW explode(from_json(details:flow_progress.data_quality.expectations,
                'array<struct<name:string,dataset:string,passed_records:bigint,failed_records:bigint>>')) x AS e
            WHERE event_type = 'flow_progress' ORDER BY timestamp DESC LIMIT 20'''))
    except Exception as e:                       # noqa: BLE001
        print(f"{env}: no event log yet ({str(e).splitlines()[0][:80]})")

# COMMAND ----------

# MAGIC %md
# MAGIC ### I3 · Job runs and the Genie quality trend

# COMMAND ----------

runs = []
for job in w.jobs.list(name=None, limit=100):
    name = job.settings.name if job.settings else ""
    if name.startswith(("FreshCart data refresh", "FreshCart Genie quality gate")):
        for r in w.jobs.list_runs(job_id=job.job_id, limit=5):
            state = r.state
            runs.append({"job": name, "started": pd.to_datetime(r.start_time, unit="ms"),
                         "minutes": round((r.run_duration or 0) / 60000, 1),
                         "result": (state.result_state.value if state and state.result_state else
                                    state.life_cycle_state.value if state and state.life_cycle_state else "?"),
                         "link": r.run_page_url})
display(pd.DataFrame(runs).sort_values("started", ascending=False) if runs else pd.DataFrame())

trend = []
for env in ENVIRONMENTS:
    sp = live_space(env)
    if sp:
        for ev in (w.genie.genie_list_eval_runs(sp.space_id).eval_runs or [])[:10]:
            graded = (ev.num_questions or 0) - (ev.num_needs_review or 0)
            trend.append({"environment": env, "run": ev.eval_run_id, "created": ev.created_timestamp,
                          "correct": ev.num_correct, "graded": graded,
                          "accuracy": round(ev.num_correct / graded, 2) if graded else None})
display(pd.DataFrame(trend))

# COMMAND ----------

# MAGIC %md
# MAGIC ### I4 · Account-wide: system tables
# MAGIC
# MAGIC System tables are enabled by an account admin (they are in the `system` catalog). These queries show failed job
# MAGIC runs of the last 7 days and the DBUs used by the FreshCart jobs and pipelines per day.

# COMMAND ----------

for title, query in {
    "failed runs, last 7 days": '''
        SELECT period_start_time, job_id, run_id, result_state, termination_code
        FROM system.lakeflow.job_run_timeline
        WHERE period_start_time >= current_date() - INTERVAL 7 DAYS AND result_state NOT IN ('SUCCEEDED')
        ORDER BY period_start_time DESC LIMIT 50''',
    "DBUs per day (jobs and pipelines)": '''
        SELECT usage_date, billing_origin_product, SUM(usage_quantity) AS dbus
        FROM system.billing.usage
        WHERE usage_date >= current_date() - INTERVAL 30 DAYS
          AND (usage_metadata.job_id IS NOT NULL OR usage_metadata.dlt_pipeline_id IS NOT NULL)
        GROUP BY ALL ORDER BY usage_date DESC''',
}.items():
    try:
        print(title); display(spark.sql(query))
    except Exception as e:                       # noqa: BLE001
        print(f"  not available ({str(e).splitlines()[0][:100]}): an account admin enables system schemas")

# COMMAND ----------

# MAGIC %md
# MAGIC ### I5 · Deployments on GitHub

# COMMAND ----------

recent = []
for wf in ["genie-release.yml", "genie-rollback.yml", "genie-dev-sync.yml", "genie-ci.yml", "ci.yml"]:
    for r in gh("GET", f"{R}/actions/workflows/{wf}/runs?per_page=5").get("workflow_runs", []):
        recent.append({"workflow": r["name"], "event": r["event"], "started": r["created_at"],
                       "result": r["conclusion"] or r["status"], "commit": r["head_sha"][:7], "link": r["html_url"]})
display(pd.DataFrame(recent).sort_values("started", ascending=False) if recent else pd.DataFrame())

# COMMAND ----------

# MAGIC %md
# MAGIC # Part J · Runbook
# MAGIC
# MAGIC | Signal | Meaning | Do this |
# MAGIC |---|---|---|
# MAGIC | Pull request check `demo` red | a test or the docs check failed | open the check, fix on the branch; nothing was deployed |
# MAGIC | `genie-ci` red | the space is invalid, one of its SQL fails, or a bundle does not validate | the check's log names the item |
# MAGIC | Release stops in qa: **refresh** | pipeline, SQL or a health check failed in qa | job link in the log; qa is not prod, fix forward with a pull request |
# MAGIC | Release stops in qa: **gate** | Genie answers too few benchmarks correctly | the log lists each wrong answer with Genie's SQL; improve the layer that lacks context (doc 7), or the benchmark if it is ambiguous |
# MAGIC | Release stops in prod: **drift** | someone edited the prod space | adopt (`sync_from_workspace.py -t prod` into a pull request) or overwrite (run `genie-release` with allow_drift) |
# MAGIC | Prod **smoke** failed | Genie does not answer in prod | the release already restored the previous space; then run `genie-rollback` for anything else |
# MAGIC | Nightly refresh failed (e-mail) | data late or wrong | job run page; `monitoring.health_checks` shows which check; rerun the job when the source is fixed |
# MAGIC | Nightly Genie gate failed (e-mail) | answers degraded on live data | compare with the last good evaluation run (Part I3); roll back the space if a release caused it |
# MAGIC | Wrong data released | a bad pipeline or SQL change reached prod | `genie-rollback` what = `data-bundle` to the previous tag (rebuilds), then revert on `main` |
# MAGIC
# MAGIC **Regular care**
# MAGIC * Weekly: read the open `genie/dev-sync` pull request; merge or close it.
# MAGIC * Monthly: rotate the service principals' OAuth secrets (re-run B2 and B3), or move to OIDC.
# MAGIC * Before raising the Genie gate (`gate_min_accuracy`): look at the evaluation trend (I3).
