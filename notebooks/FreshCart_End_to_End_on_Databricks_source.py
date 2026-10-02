# Databricks notebook source
# MAGIC %md
# MAGIC # FreshCart on Databricks, end to end
# MAGIC
# MAGIC This notebook takes an **empty Databricks workspace** to a governed **Genie space** that business users can ask
# MAGIC questions in, and promotes it from **dev to qa to prod**. Everything that gets deployed is defined in this
# MAGIC repository as a **Declarative Automation Bundle (DAB)** and deployed with `databricks bundle deploy`.
# MAGIC
# MAGIC | Step | What happens | Defined in |
# MAGIC |---|---|---|
# MAGIC | 1 | Choose the environment and options | the widgets at the top |
# MAGIC | 2 | Find the repository files, check the workspace, pick a SQL warehouse | |
# MAGIC | 3 | Install the Databricks CLI, the tool that deploys bundles | |
# MAGIC | 4 | Create the groups that decide who sees what | `databricks/governance/` |
# MAGIC | 5 | Create the catalog, the schemas and the landing volume | `databricks/00_setup.sql` |
# MAGIC | 6 | Upload the raw source files | `data/raw/` |
# MAGIC | 7 | Deploy the **data bundle**: the pipeline and the refresh job | `databricks/databricks.yml` |
# MAGIC | 8 | Run the refresh job: bronze, silver, gold, security, semantic layer | `databricks/resources/freshcart_refresh.yml` |
# MAGIC | 9 | Look at every layer | |
# MAGIC | 10 | Prove the Databricks numbers equal the tested local reference | `freshcart/` |
# MAGIC | 11 | Deploy the **Genie bundle**: the space and its quality-gate job | `genie_bundle/` |
# MAGIC | 12 | Ask Genie questions from this notebook | |
# MAGIC | 13 | Run the quality gate: Genie's answers versus the benchmarks | `genie_bundle/src/genie_quality_gate.py` |
# MAGIC | 14 | Version history: catch an edit made in the Genie UI, keep it or roll it back | `genie_bundle/scripts/` |
# MAGIC | 15 | Promote to qa and prod | the same bundles, other targets |
# MAGIC | 16 | Hand over to CI/CD (GitHub Actions with service principals) | `.github/workflows/` |
# MAGIC | 17 | Clean up | |
# MAGIC
# MAGIC ## Before you start (once, about 5 minutes)
# MAGIC
# MAGIC 1. **A workspace with Unity Catalog and serverless compute.** If you only have a new account, create the first
# MAGIC    workspace: signing up for a free trial or for Databricks Free Edition creates one, and in the account console it
# MAGIC    is *Workspaces* → *Create workspace*. New workspaces have Unity Catalog and serverless compute. You need to be a
# MAGIC    **workspace admin** (the person who created the workspace is one), because the notebook creates groups and
# MAGIC    catalogs.
# MAGIC 2. **Bring this repository into the workspace as a Git folder.** In the left sidebar: *Workspace* → your user
# MAGIC    folder → *Create* → *Git folder*. Paste `https://github.com/subrata-samanta/ai-ready-retail-data-genie-demo`,
# MAGIC    provider *GitHub*, and pick the branch. If the repository is private, first connect GitHub under
# MAGIC    *Settings* → *Linked accounts*.
# MAGIC 3. **Open this notebook from the Git folder** (`notebooks/FreshCart_End_to_End_on_Databricks`) and connect it to
# MAGIC    **Serverless** compute (top right).
# MAGIC 4. **Run the cells in order.** *Run all* works too. The first run takes about 20 to 40 minutes, mostly the pipeline
# MAGIC    and Genie's benchmark evaluation. Every step can be re-run.
# MAGIC
# MAGIC ## Three ideas you need
# MAGIC
# MAGIC * **Unity Catalog** names every table `catalog.schema.table`. One catalog per environment (`freshcart_dev`,
# MAGIC   `freshcart_qa`, `freshcart`), and one schema per layer (`bronze`, `silver`, `gold`, `semantic`).
# MAGIC * **A bundle** is a folder with a `databricks.yml`: YAML that describes Databricks resources (pipelines, jobs,
# MAGIC   Genie spaces) and the files they use. `databricks bundle deploy -t dev` makes the workspace match the YAML for the
# MAGIC   target `dev`. The same YAML deploys `qa` and `prod`; only the target's variables differ (the catalog, for example).
# MAGIC * **Two bundles**, deployed in this order: the **data bundle** (`databricks/`) builds the tables, then the **Genie
# MAGIC   bundle** (`genie_bundle/`) creates the Genie space that reads them.
# MAGIC
# MAGIC ## How the code is organised
# MAGIC
# MAGIC Each step first **defines** a small function for its job, then **calls** it for the environment chosen in step 1.
# MAGIC Step 15 calls the very same functions for qa and prod, which is what "promotion" means: same code, next target.
# MAGIC
# MAGIC | Function | Step | Does |
# MAGIC |---|---|---|
# MAGIC | `catalog_for(env)` | 2 | the catalog of an environment, read from `databricks/databricks.yml` |
# MAGIC | `bundle(name, env, ...)` | 3 | runs `databricks bundle ... -t env` for the data (`"data"`) or Genie (`"genie"`) bundle |
# MAGIC | `prepare_catalog(env)` | 5 | creates the catalog, schemas, volume and governance functions |
# MAGIC | `upload_raw(env)` | 6 | copies `data/raw/` into the environment's landing volume |
# MAGIC | `deploy_data(env)` / `run_refresh(env)` | 7, 8 | deploys the data bundle / runs its refresh job |
# MAGIC | `deploy_genie(env)` | 11 | deploys the Genie bundle and returns the space id |
# MAGIC | `ask(question)` | 12 | asks Genie through the API and shows its SQL and result |
# MAGIC | `run_gate(env, mode)` | 13 | runs the quality-gate job (`gate` or `smoke`) |
# MAGIC
# MAGIC The notebook never deploys anything itself: every resource is created by `databricks bundle deploy`, exactly as the
# MAGIC GitHub workflows do. The Python around it only prepares inputs (catalog, files, groups) and shows results.
# MAGIC
# MAGIC This notebook exists in two formats with the same cells: `FreshCart_End_to_End_on_Databricks.ipynb` (open this one,
# MAGIC on GitHub or in Databricks) and `FreshCart_End_to_End_on_Databricks_source.py` (Databricks source format, which
# MAGIC gives clean diffs in pull requests; the `.ipynb` is generated from it by `python -m freshcart.databricks_notebook`).

# COMMAND ----------

# MAGIC %md
# MAGIC ### Install the libraries this notebook uses
# MAGIC The Databricks SDK talks to the workspace from Python (version 0.145 or later has the Genie APIs used here), and
# MAGIC PyYAML reads the bundle files. Python restarts afterwards, which is normal.

# COMMAND ----------

# MAGIC %pip install --quiet "databricks-sdk>=0.145.0" pyyaml

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 · Choose the environment and options
# MAGIC
# MAGIC The widgets at the top of the notebook control the run. Leave the defaults for a first run.
# MAGIC
# MAGIC | Widget | Default | Meaning |
# MAGIC |---|---|---|
# MAGIC | environment | `dev` | The target to build. Steps 4 to 14 work on this one. |
# MAGIC | catalog | *(blank)* | Blank uses the target's catalog from `databricks/databricks.yml` (`freshcart_dev` for dev). Set it only if your workspace cannot create catalogs (step 5 tells you), for example to `workspace`. |
# MAGIC | warehouse | *(blank)* | The SQL warehouse Genie runs its queries on. Blank picks a serverless one, or creates a small one. |
# MAGIC | as_of_date | `2026-09-27` | The day the data treats as *today*. The demo files end on 2026-09-26, so with this date "last week" means FY2026 week 34 and the answers match the documentation. Use `today` for live daily files. |
# MAGIC | promote | `no` | `yes` also builds qa and prod in step 15. |
# MAGIC | cleanup | `no` | `yes` deletes everything this notebook created, in step 17. |

# COMMAND ----------

dbutils.widgets.dropdown("environment", "dev", ["dev", "qa", "prod"], "1 environment")
dbutils.widgets.text("catalog", "", "2 catalog (blank = default)")
dbutils.widgets.text("warehouse", "", "3 SQL warehouse (blank = pick)")
dbutils.widgets.text("as_of_date", "2026-09-27", "4 as-of date")
dbutils.widgets.dropdown("promote", "no", ["no", "yes"], "5 promote to qa and prod")
dbutils.widgets.dropdown("cleanup", "no", ["no", "yes"], "6 clean up at the end")

# Read the widget values once; every later cell uses these Python variables.
ENV = dbutils.widgets.get("environment")
CATALOG_OVERRIDE = dbutils.widgets.get("catalog").strip()
WAREHOUSE_NAME = dbutils.widgets.get("warehouse").strip()
AS_OF_DATE = dbutils.widgets.get("as_of_date").strip() or "today"
PROMOTE = dbutils.widgets.get("promote") == "yes"
CLEANUP = dbutils.widgets.get("cleanup") == "yes"
print(f"environment={ENV}  catalog={CATALOG_OVERRIDE or '(default)'}  warehouse={WAREHOUSE_NAME or '(pick)'}  "
      f"as_of_date={AS_OF_DATE}  promote={PROMOTE}  cleanup={CLEANUP}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 · Find the repository, check the workspace, pick a SQL warehouse
# MAGIC
# MAGIC * The repository's files are next to this notebook in the Git folder. Everything below reads them from there, so
# MAGIC   what you run is exactly what is in git.
# MAGIC * The catalog for each environment comes from the data bundle's `databricks.yml`, the same file that deploys it.
# MAGIC * Genie runs SQL on a **SQL warehouse**. A serverless warehouse starts in seconds and stops itself when idle.

# COMMAND ----------

import difflib, json, os, platform, re, shutil, subprocess, sys, time, urllib.request, uuid, zipfile
from pathlib import Path

import pandas as pd
import yaml
from databricks.sdk import WorkspaceClient
from databricks.sdk.service import iam
from databricks.sdk.service import sql as dbsql

WORK = Path("/tmp/freshcart")                    # scratch space on this compute: CLI binary, bundle working copies
VOLUMES = Path("/Volumes")                       # Unity Catalog volumes, mounted as files


def find_repo() -> Path:
    """The repository root: the Git folder that contains this notebook (in notebooks/)."""
    candidates = [Path.cwd().parent, Path.cwd()]          # a notebook's working directory is its own folder
    try:
        nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
        candidates += [Path("/Workspace" + nb).parent.parent, Path(nb).parent.parent]
    except Exception:                            # noqa: BLE001  (not available in every context)
        pass
    for c in candidates:
        if (c / "genie_bundle" / "databricks.yml").exists() and (c / "databricks" / "databricks.yml").exists():
            return c
    raise RuntimeError("The repository files are not next to this notebook. Open the notebook from the Git folder "
                       "(see 'Before you start').")


REPO = find_repo()
# The environments and their catalogs come from the data bundle itself, so this notebook can never disagree with it:
#   targets: {dev: {variables: {catalog: freshcart_dev}}, qa: {...: freshcart_qa}, prod: {...: freshcart}}
DATA_TARGETS = yaml.safe_load((REPO / "databricks" / "databricks.yml").read_text())["targets"]
ENVIRONMENTS = list(DATA_TARGETS)                                       # dev, qa, prod
DEFAULT_CATALOG = {t: cfg["variables"]["catalog"] for t, cfg in DATA_TARGETS.items()}


def catalog_for(env: str) -> str:
    """The catalog of an environment; the 'catalog' widget overrides it for the chosen environment only."""
    return CATALOG_OVERRIDE if (CATALOG_OVERRIDE and env == ENV) else DEFAULT_CATALOG[env]


CATALOG = catalog_for(ENV)

w = WorkspaceClient()                            # authenticated as you, automatically, inside a notebook
me = w.current_user.me()
HOST = w.config.host.rstrip("/")
try:
    metastore = w.metastores.current()
    print(f"Unity Catalog metastore: {metastore.metastore_id}")
except Exception as e:                           # noqa: BLE001
    raise RuntimeError("This workspace has no Unity Catalog metastore. New workspaces get one automatically; "
                       "for an older workspace an account admin assigns one in the account console.") from e


def pick_warehouse():
    """The warehouse named in the widget, else a serverless one (the starter warehouse first), else a new one."""
    warehouses = list(w.warehouses.list())
    if WAREHOUSE_NAME:
        named = [x for x in warehouses if x.name == WAREHOUSE_NAME]
        if not named:
            raise RuntimeError(f"No SQL warehouse named {WAREHOUSE_NAME!r}: {[x.name for x in warehouses]}")
        return named[0]
    serverless = [x for x in warehouses if x.enable_serverless_compute]
    if serverless:
        return sorted(serverless, key=lambda x: x.name != "Serverless Starter Warehouse")[0]
    if warehouses:
        return warehouses[0]
    print("No SQL warehouse yet: creating a small serverless one (stops after 10 idle minutes).")
    return w.warehouses.create_and_wait(
        name="FreshCart Serverless", cluster_size="2X-Small", max_num_clusters=1, auto_stop_mins=10,
        enable_serverless_compute=True,
        warehouse_type=dbsql.CreateWarehouseRequestWarehouseType.PRO)


wh = pick_warehouse()
WAREHOUSE_ID = wh.id
print(f"repository : {REPO}")
print(f"workspace  : {HOST}   you: {me.user_name}")
print(f"warehouse  : {wh.name} ({WAREHOUSE_ID})")
print(f"catalogs   : {DEFAULT_CATALOG}   this run: {ENV} -> {CATALOG}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 · Install the Databricks CLI
# MAGIC
# MAGIC Bundles are deployed with the **Databricks CLI** (`databricks bundle ...`): the same command you would run on a
# MAGIC laptop or in GitHub Actions. This cell downloads the version this repository is tested with into scratch space on
# MAGIC the notebook's compute and signs it in as you, with the notebook's own short-lived token. Nothing is stored.
# MAGIC
# MAGIC Each bundle is copied to a working folder first (`/tmp/freshcart/bundles/...`), because the CLI keeps a small
# MAGIC local cache next to `databricks.yml` and the Git folder should stay clean.
# MAGIC
# MAGIC The catalog, warehouse and as-of date reach the CLI as `BUNDLE_VAR_catalog`, `BUNDLE_VAR_warehouse_id` and
# MAGIC `BUNDLE_VAR_as_of_date`. Environment variables named `BUNDLE_VAR_<name>` set a bundle variable, so every command
# MAGIC (including the helper scripts in step 14) uses the same values.

# COMMAND ----------

CLI_VERSION = "1.18.0"
BUNDLES = {"data": "databricks", "genie": "genie_bundle"}      # bundle name -> folder in the repository


def install_cli() -> Path:
    """Download the CLI release for this machine's processor (once per compute session) and return its path."""
    exe = WORK / "cli" / "databricks"
    if exe.exists():                             # already installed earlier in this session
        return exe
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine().lower()]
    url = (f"https://github.com/databricks/cli/releases/download/v{CLI_VERSION}/"
           f"databricks_cli_{CLI_VERSION}_linux_{arch}.zip")
    exe.parent.mkdir(parents=True, exist_ok=True)
    archive = exe.parent / "cli.zip"
    print(f"downloading {url}")
    urllib.request.urlretrieve(url, archive)
    with zipfile.ZipFile(archive) as z:
        z.extract("databricks", exe.parent)
    exe.chmod(0o755)
    return exe


def token() -> str:
    """A short-lived token for you, taken from the notebook's own sign-in (nothing is created or stored)."""
    auth = w.config.authenticate().get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):]
    return dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()


def cli_env(env: str) -> dict:
    """Environment variables for a CLI call: who to sign in as, and the bundle variables for this environment."""
    # Start clean: the notebook's own DATABRICKS_* variables describe the notebook, not the CLI's sign-in.
    e = {k: v for k, v in os.environ.items() if not k.startswith(("DATABRICKS_", "BUNDLE_VAR_"))}
    e.update({
        "DATABRICKS_HOST": HOST, "DATABRICKS_TOKEN": token(), "DATABRICKS_AUTH_TYPE": "pat",
        "DATABRICKS_CLI": str(CLI),                            # used by the helper scripts in step 14
        "BUNDLE_VAR_catalog": catalog_for(env), "BUNDLE_VAR_warehouse_id": WAREHOUSE_ID,
        "BUNDLE_VAR_as_of_date": AS_OF_DATE,
    })
    return e


def run(cmd, cwd, env: str, check: bool = True, echo: bool = True) -> str:
    """Run a command and stream its output into the notebook."""
    p = subprocess.Popen([str(c) for c in cmd], cwd=cwd, env=cli_env(env), text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lines = []
    for line in p.stdout:
        lines.append(line)
        if echo:
            print(line, end="")
    p.wait()
    if check and p.returncode != 0:
        raise RuntimeError(f"`{' '.join(str(c) for c in cmd[:5])} ...` failed with exit code {p.returncode}")
    return "".join(lines)


def working_copy(name: str, refresh: bool = False) -> Path:
    """A copy of the bundle folder from the repository (refresh=True: take the repository's version again)."""
    dst = WORK / "bundles" / BUNDLES[name]
    if refresh or not dst.exists():
        shutil.copytree(REPO / BUNDLES[name], dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".databricks", "__pycache__"))
    return dst


def bundle(name: str, env: str, *args, check: bool = True, echo: bool = True) -> str:
    """databricks bundle <args> -t <env>, in the working copy of bundle `name` ("data" or "genie")."""
    return run([CLI, "bundle", *args, "-t", env], cwd=working_copy(name), env=env, check=check, echo=echo)


def bundle_json(name: str, env: str, *args) -> dict:
    p = subprocess.run([str(CLI), "bundle", *args, "-t", env, "-o", "json"], cwd=working_copy(name),
                       env=cli_env(env), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr)
    return json.loads(p.stdout[p.stdout.find("{"):])


CLI = install_cli()
run([CLI, "--version"], cwd=WORK, env=ENV)          # proves the binary runs here
# proves the sign-in works (the output is hidden; it is your user record)
run([CLI, "current-user", "me", "-o", "json"], cwd=WORK, env=ENV, echo=False)
print(f"signed in to {HOST} as {me.user_name}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 · Groups: who may see what
# MAGIC
# MAGIC Security is enforced **in the data**, never in a Genie instruction, so it applies to every tool and every user:
# MAGIC
# MAGIC | Group | Purpose | You join it? |
# MAGIC |---|---|---|
# MAGIC | `fc_all_regions` | Sees every region's rows in the sales and stock facts. Without it, the **row filter** shows only the rows of your `fc_region_<region>` group(s). | yes, to see all data |
# MAGIC | `fc_crm_admins` | Sees loyalty members' personal data. Everyone else sees `***` (a **column mask**). | no: step 9 shows the mask working |
# MAGIC | `freshcart-business-users` | May ask questions in the Genie space, and read the gold and semantic schemas. | yes |
# MAGIC | `freshcart-genie-developers` | Builds the Genie space (manage in dev, view in qa and prod). | yes |
# MAGIC | `freshcart-genie-deployers` | The CI/CD service principals that deploy the bundles (step 16). | yes, while you deploy by hand |
# MAGIC
# MAGIC The row filter and mask functions (`databricks/governance/00_functions.sql`) accept both account groups (the
# MAGIC recommended kind, managed in the account console) and the workspace groups this cell creates, so the notebook works
# MAGIC without account-admin access. New group memberships can take a minute to apply.

# COMMAND ----------

GROUPS = ["fc_all_regions", "freshcart-business-users", "freshcart-genie-developers", "freshcart-genie-deployers"]
PATCH = [iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP]


def ensure_group(name: str) -> str:
    """The id of the workspace group `name`, created if it does not exist yet."""
    found = list(w.groups.list(filter=f'displayName eq "{name}"', attributes="id,displayName"))
    group = found[0] if found else w.groups.create(display_name=name)
    if not found:
        print(f"created group {name}")
    return group.id


def add_member(group_id: str, principal_id: str) -> None:
    """Add a user or service principal to a group (a SCIM PATCH 'add members'), unless it is already in it."""
    members = {m.value for m in (w.groups.get(group_id).members or [])}
    if principal_id not in members:
        w.groups.patch(group_id, schemas=PATCH, operations=[
            iam.Patch(op=iam.PatchOp.ADD, path="members", value=[{"value": principal_id}])])


GROUP_IDS = {g: ensure_group(g) for g in GROUPS + ["fc_crm_admins"]}
for g in GROUPS:
    add_member(GROUP_IDS[g], me.id)
print(f"{me.user_name} is in: {', '.join(GROUPS)} (and not in fc_crm_admins)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 · Catalog, schemas and the landing volume
# MAGIC
# MAGIC * The **catalog** is created here, once, by you as an admin. Creating catalogs needs a metastore privilege that the
# MAGIC   CI/CD deployers should not have, so the bundle does not do it.
# MAGIC * The **schemas** (`landing`, `bronze`, `silver`, `gold`, `semantic`, `governance`), the **landing volume** (a folder
# MAGIC   for raw files, governed by Unity Catalog) and the **governance functions** come from `databricks/00_setup.sql` and
# MAGIC   `databricks/governance/00_functions.sql`. The refresh job runs the same files again as its first task, so an
# MAGIC   environment built only by CI/CD gets them too.
# MAGIC * The SQL files name the catalog `${catalog}`. `databricks/src/run_sql.py` replaces it with this environment's
# MAGIC   catalog and runs each statement with `spark.sql`. The job runs the same script.
# MAGIC
# MAGIC If `CREATE CATALOG` fails, the workspace has no default storage for new catalogs. Either ask an account admin to
# MAGIC set a storage location, or set the **catalog** widget to an existing catalog (for example `workspace`) and build
# MAGIC only dev.

# COMMAND ----------

sys.path.insert(0, str(REPO / "databricks" / "src"))
import run_sql                                   # the same runner the bundle job uses


def prepare_catalog(env: str) -> None:
    """Catalog (here, as an admin), then schemas, volume and governance functions (the job's own setup files)."""
    cat = catalog_for(env)
    try:
        spark.sql(f"CREATE CATALOG IF NOT EXISTS `{cat}` COMMENT 'FreshCart demo ({env}): AI-ready retail data for Genie'")
    except Exception as e:                       # noqa: BLE001
        raise RuntimeError(f"Could not create catalog {cat}: {e}\nSee the note above this cell.") from e
    run_sql.run_files(spark, [REPO / "databricks" / "00_setup.sql",
                              REPO / "databricks" / "governance" / "00_functions.sql"], cat, AS_OF_DATE)


prepare_catalog(ENV)
spark.sql(f"USE CATALOG `{CATALOG}`")            # the %sql cells below use this catalog
display(spark.sql(f"SHOW SCHEMAS IN `{CATALOG}`"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 · Upload the raw source files
# MAGIC
# MAGIC The source systems deliver files: monthly POS transaction logs, online orders as JSON lines, nightly stock
# MAGIC snapshots, monthly store masters, ERP product and cost extracts, loyalty CRM extracts, promotions and finance
# MAGIC calendars (see `data/raw/README.md`). They go into the landing volume **exactly as delivered**: the pipeline,
# MAGIC not the upload, cleans them. Files that are already there are skipped, so re-running is safe.

# COMMAND ----------

RAW = REPO / "data" / "raw"


def upload_raw(env: str) -> None:
    """Copy data/raw/ into /Volumes/<catalog>/landing/raw/, keeping the folder per source system."""
    dst = VOLUMES / catalog_for(env) / "landing" / "raw"         # a volume is mounted like a normal folder
    copied = skipped = 0
    for src in sorted(p for p in RAW.rglob("*") if p.is_file() and p.name != "README.md"):
        target = dst / src.relative_to(RAW)
        if target.exists() and target.stat().st_size == src.stat().st_size:
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)             # plain copy: volumes do not keep file permissions
        copied += 1
    print(f"{dst}: {copied} file(s) uploaded, {skipped} already there")


upload_raw(ENV)
feeds = sorted({p.relative_to(RAW).parts[0] for p in RAW.rglob("*") if p.is_file() and p.name != "README.md"})
display(pd.DataFrame([{"feed": f, "files": sum(1 for p in (RAW / f).rglob("*") if p.is_file())} for f in feeds]))

# COMMAND ----------

# MAGIC %md
# MAGIC A first look at one raw file: every column is text, store numbers have leading zeros, dates use the source
# MAGIC system's format. These are the problems silver fixes.

# COMMAND ----------

pos_files = sorted((VOLUMES / CATALOG / "landing" / "raw" / "pos").glob("*"))
display(spark.read.option("header", True).csv(str(pos_files[0])).limit(5))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7 · Deploy the data bundle (`databricks/`)
# MAGIC
# MAGIC The data bundle defines two resources:
# MAGIC
# MAGIC * **`freshcart_pipeline`**, a Lakeflow Declarative Pipeline on serverless compute, with
# MAGIC   `pipelines/01_bronze.sql` (15 streaming tables and materialized views that land each feed as delivered) and
# MAGIC   `pipelines/02_silver.sql` (13 cleaned, typed and decoded tables, with data-quality expectations and a quarantine
# MAGIC   table for rejected rows). The bundle passes `catalog` and `as_of_date` to the pipeline as parameters, and the SQL
# MAGIC   uses them as `${catalog}` and `${as_of_date}`.
# MAGIC * **`freshcart_refresh`**, a job that builds everything in order: `setup` → `pipeline` → `gold` → `security`
# MAGIC   and `semantic`.
# MAGIC
# MAGIC `bundle validate` checks the YAML. `bundle deploy` uploads the files and creates or updates the pipeline and the
# MAGIC job, so the workspace matches the YAML. Nothing runs yet.

# COMMAND ----------

print((REPO / "databricks" / "resources" / "freshcart_refresh.yml").read_text())

# COMMAND ----------

def deploy_data(env: str) -> dict:
    """validate + deploy the data bundle to `env`; returns what the bundle created (names, ids, links)."""
    working_copy("data", refresh=True)           # deploy exactly what is in the repository
    bundle("data", env, "validate")
    bundle("data", env, "deploy", "--auto-approve")
    return bundle_json("data", env, "summary")["resources"]


def show_links(resources: dict) -> None:
    """A clickable list of the resources a bundle deployed (from `bundle summary`)."""
    rows = [f'<li>{kind[:-1]} <b>{key}</b>: <a href="{r.get("url", "")}" target="_blank">{r.get("name", key)}</a></li>'
            for kind, items in resources.items() for key, r in items.items() if isinstance(r, dict)]
    displayHTML("<ul>" + "".join(rows) + "</ul>")


data_resources = deploy_data(ENV)
show_links(data_resources)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 8 · Run the refresh job
# MAGIC
# MAGIC `databricks bundle run freshcart_refresh` starts the job and waits. The first run takes roughly 10 to 20 minutes
# MAGIC (serverless compute starts, the pipeline reads every file). Open the job link above to watch the tasks:
# MAGIC
# MAGIC | Task | Runs | Builds |
# MAGIC |---|---|---|
# MAGIC | `setup` | `00_setup.sql`, `governance/00_functions.sql` | schemas, landing volume, row-filter and mask functions |
# MAGIC | `pipeline` | the pipeline (`01_bronze.sql`, `02_silver.sql`) | bronze: one table per feed; silver: typed, deduplicated, decoded tables, SCD2 store history, quarantine |
# MAGIC | `gold` | `gold/00_create_gold_tables.sql`, `gold/01_load_gold.sql` | the star schema (5 dimensions, 2 facts) with comments, keys and tags, loaded with MERGE |
# MAGIC | `security` | `governance/01_security.sql` | row filters on the facts, grants for Genie users |
# MAGIC | `semantic` | `semantic/02_metric_views.sql`, `semantic/03_fn_like_for_like_sales.sql` | the two metric views (governed KPIs) and the trusted like-for-like function |
# MAGIC
# MAGIC Every step can be re-run: streaming tables never load the same file twice, and gold uses MERGE.

# COMMAND ----------

def run_refresh(env: str) -> None:
    """Start the refresh job and wait for it; fails this cell if any task fails."""
    bundle("data", env, "run", "freshcart_refresh")


run_refresh(ENV)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 9 · Look at every layer
# MAGIC
# MAGIC The `%sql` cells use the catalog chosen in step 5 (`USE CATALOG`), so the names are `schema.table`.
# MAGIC
# MAGIC **Every table, by layer.** Bronze keeps the files as delivered, silver is clean and typed, gold is the business
# MAGIC star schema, semantic holds the metric views.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT table_schema AS layer, table_name, table_type, comment
# MAGIC FROM information_schema.tables
# MAGIC WHERE table_schema IN ('bronze', 'silver', 'gold', 'semantic')
# MAGIC ORDER BY array_position(array('bronze', 'silver', 'gold', 'semantic'), table_schema), table_name

# COMMAND ----------

# MAGIC %md
# MAGIC **Row counts in gold.**

# COMMAND ----------

gold_tables = [r.table_name for r in spark.sql("SHOW TABLES IN gold").collect()]
display(pd.DataFrame([{"table": f"gold.{t}", "rows": spark.table(f"gold.{t}").count()} for t in sorted(gold_tables)]))

# COMMAND ----------

# MAGIC %md
# MAGIC **Data quality.** POS rows that break a hard rule are not silently dropped: the pipeline's expectation removes them
# MAGIC from `silver.pos_sales_line` and the quarantine table keeps them with the reason, so they can be fixed at source.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT failure_reason, count(*) AS rows FROM silver.quarantine_pos_sales_line GROUP BY failure_reason ORDER BY rows DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **Relative dates come from the data.** `gold.dim_date` carries flags such as `is_last_completed_fiscal_week`,
# MAGIC computed from the as-of date. With `2026-09-27` "last week" is Sunday 20 to Saturday 26 September 2026.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT fiscal_year, fiscal_week, min(calendar_date) AS first_day, max(calendar_date) AS last_day
# MAGIC FROM gold.dim_date WHERE is_last_completed_fiscal_week GROUP BY fiscal_year, fiscal_week

# COMMAND ----------

# MAGIC %md
# MAGIC **Security, in the data.** You are in `fc_all_regions`, so the row filter shows every region. A user only in
# MAGIC `fc_region_northeast` would see one row here. And you are not in `fc_crm_admins`, so personal data is masked.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT region, count(*) AS sales_lines FROM gold.fct_sales_line GROUP BY region ORDER BY region

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT * FROM silver.loyalty_member_restricted LIMIT 5

# COMMAND ----------

# MAGIC %md
# MAGIC **The semantic layer.** A metric view defines each KPI once (net sales, gross margin %, return rate, ...), with
# MAGIC its joins. Genie and every other tool query it with `MEASURE()`, so the definitions cannot drift apart.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT region, MEASURE(net_sales) AS net_sales, MEASURE(gross_margin_pct) AS gross_margin_pct
# MAGIC FROM semantic.sales_metrics
# MAGIC WHERE is_last_completed_fiscal_week
# MAGIC GROUP BY region ORDER BY net_sales DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **The trusted function** that Genie calls for like-for-like growth (Finance owns the logic):

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT * FROM semantic.fn_like_for_like_sales(2026)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 10 · Check Databricks against the tested local reference
# MAGIC
# MAGIC The repository also builds the same warehouse on SQLite (`freshcart/`, `pipeline/`), and its tests check every
# MAGIC number. This cell builds that local reference on the notebook's compute, runs the **expected SQL of every Genie
# MAGIC benchmark** in both places and compares the answers. They should all match. Any difference points at the exact
# MAGIC question, and so at the layer, to look at.
# MAGIC
# MAGIC (The comparison assumes `as_of_date` is `2026-09-27`, the date the local reference uses.)

# COMMAND ----------

# The local reference runs in a separate Python process, in a copy of the repository, so it cannot touch the
# Git folder. It prints the benchmark answers as JSON after the marker RESULT.
LOCAL_SCRIPT = '''
import json
from freshcart import benchmarks, pipeline
con = pipeline.run(full_refresh=True, verbose=False)
out = [{"question": b["question"], "sql": b["expected_sql"], "rows": rows}
       for b, cols, rows in benchmarks.run_benchmarks(con) if cols is not None]
print("RESULT" + json.dumps(out, default=str))
'''


def local_reference() -> list[dict]:
    local = WORK / "local"
    shutil.rmtree(local, ignore_errors=True)
    shutil.copytree(REPO, local, ignore=shutil.ignore_patterns(
        ".git", "notebooks", "docs", "exports", "warehouse", "__pycache__", ".databricks"))
    p = subprocess.run([sys.executable, "-c", LOCAL_SCRIPT], cwd=local, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-3000:])
    return json.loads(p.stdout[p.stdout.rfind("RESULT") + len("RESULT"):])


def comparable(rows) -> list[tuple]:
    """Rows in a form both engines agree on: numbers rounded to cents, booleans as 0/1, dates as text, sorted."""
    def value(v):
        if v is None:
            return None
        if isinstance(v, bool):
            return float(v)
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return str(v)
    return sorted((tuple(value(v) for v in row) for row in rows), key=repr)


def same(a, b) -> bool:
    """Equal row by row, allowing one cent of difference (SQLite sums floats, Databricks sums decimals)."""
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b):
        for x, y in zip(ra, rb):
            if isinstance(x, float) and isinstance(y, float):
                if abs(x - y) > max(0.011, 1e-6 * abs(y)):
                    return False
            elif x != y:
                return False
    return True


reference = local_reference()
checks = []
for b in reference:
    # the benchmark SQL is written with ${var.catalog} (the bundle variable); fill in this environment's catalog
    on_databricks = [tuple(r) for r in spark.sql(b["sql"].replace("${var.catalog}", CATALOG)).collect()]
    ok = same(comparable(on_databricks), comparable(b["rows"]))
    checks.append({"question": b["question"], "local rows": len(b["rows"]), "Databricks rows": len(on_databricks),
                   "match": "yes" if ok else "NO", "Databricks answer": str(on_databricks[:3])[:160]})
display(pd.DataFrame(checks))
print(f"{sum(c['match'] == 'yes' for c in checks)} of {len(checks)} benchmark answers match the local reference")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 11 · Deploy the Genie bundle (`genie_bundle/`)
# MAGIC
# MAGIC The Genie bundle defines:
# MAGIC
# MAGIC * **`freshcart_assistant`**, the Genie space. Its whole content (the tables and metric views it may use, column
# MAGIC   descriptions and synonyms, the SQL expressions, the trusted function, six example queries, the general
# MAGIC   instructions and 17 benchmark questions with their correct SQL) is the YAML file
# MAGIC   `genie_bundle/resources/freshcart_assistant.space.yml`, written with `${var.catalog}` so the same file serves every
# MAGIC   environment. Business users get `CAN_RUN` on the space.
# MAGIC * **`genie_quality_gate`**, a job that runs the space's benchmarks (step 13) or a one-question smoke test.
# MAGIC
# MAGIC `bundle plan` shows what deploy would change before anything changes.

# COMMAND ----------

def deploy_genie(env: str) -> str:
    """validate, plan and deploy the Genie bundle to `env`; returns the Genie space id."""
    working_copy("genie", refresh=True)
    bundle("genie", env, "validate")
    bundle("genie", env, "plan")
    bundle("genie", env, "deploy", "--auto-approve")
    resources = bundle_json("genie", env, "summary")["resources"]
    show_links(resources)
    return resources["genie_spaces"]["freshcart_assistant"]["id"]


SPACE_ID = deploy_genie(ENV)
displayHTML(f'<p>Open the space: <a href="{HOST}/genie/rooms/{SPACE_ID}" target="_blank">{HOST}/genie/rooms/{SPACE_ID}</a></p>')

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 12 · Ask Genie
# MAGIC
# MAGIC You can now ask questions in the Genie UI (link above). The same works through the API: this cell asks three
# MAGIC questions and shows the SQL Genie wrote and its result. These three are titles of the space's trusted example
# MAGIC queries, so Genie reuses that SQL. Try your own wording too (for example "turnover in the north east last week"):
# MAGIC Genie's wording can differ between runs, the numbers should not.

# COMMAND ----------

def ask(question: str, space_id: str = None) -> None:
    """Start a Genie conversation, wait for the answer, print Genie's text and SQL, show the result table."""
    space_id = space_id or SPACE_ID
    print(f"Q: {question}")
    msg = w.genie.start_conversation_and_wait(space_id, question)
    message_id = msg.message_id or msg.id
    for a in msg.attachments or []:              # an answer has text and/or a query attachment
        if a.text and a.text.content:
            print(f"Genie: {a.text.content}")
        if a.query:
            print(f"SQL:\n{a.query.query}")
            res = w.genie.get_message_attachment_query_result(space_id, msg.conversation_id, message_id, a.attachment_id)
            sr = res.statement_response
            cols = [c.name for c in sr.manifest.schema.columns] if sr and sr.manifest and sr.manifest.schema else None
            display(pd.DataFrame((sr.result.data_array if sr and sr.result else None) or [], columns=cols))
    print()


for q in ["What were net sales and margin by region last week?",
          "How are we trading year to date versus last year?",
          "Which categories rely most on promotions this quarter?"]:
    ask(q)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 13 · The quality gate
# MAGIC
# MAGIC Each benchmark is a question with the SQL that gives the right answer, signed off by the business. The gate job
# MAGIC asks Genie every benchmark, lets Genie's evaluation compare the answers, and **fails** unless at least 95% are
# MAGIC right and none is wrong (`genie_bundle/databricks.yml`: `gate_min_accuracy`, `gate_max_bad`). In the release
# MAGIC pipeline nothing reaches prod without a passed gate in qa. `mode=smoke` asks one question and only checks that
# MAGIC Genie answers with SQL; it runs right after a prod deploy.

# COMMAND ----------

def job_output(name: str, env: str, job_key: str) -> None:
    """Print the log of the latest run of a bundle job (each task's standard output)."""
    job_id = int(bundle_json(name, env, "summary")["resources"]["jobs"][job_key]["id"])
    latest = next(iter(w.jobs.list_runs(job_id=job_id, limit=1)), None)
    for task in (w.jobs.get_run(latest.run_id).tasks or []) if latest else []:
        try:
            print(w.jobs.get_run_output(task.run_id).logs or "")
        except Exception as e:                   # noqa: BLE001
            print(f"(no output for task {task.task_key}: {e})")


def run_gate(env: str, mode: str = "gate") -> bool:
    """Run the quality-gate job: mode 'gate' = all benchmarks, 'smoke' = one question. True if it passed."""
    args = ["run", "genie_quality_gate"] + ([] if mode == "gate" else ["--params", f"mode={mode}"])
    try:
        bundle("genie", env, *args)              # prints the run's progress and the gate's report
        passed = True
    except RuntimeError:
        passed = False
        job_output("genie", env, "genie_quality_gate")   # the full log of the failed run
    print(f"{mode} in {env}: {'PASSED' if passed else 'FAILED'}")
    return passed


GATE_PASSED = run_gate(ENV, "gate")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 14 · Version history: an edit in the Genie UI, then keep it or roll it back
# MAGIC
# MAGIC People improve a Genie space in its UI. Git must stay the record of every version, and a deploy must never
# MAGIC silently undo someone's work. This step walks through the whole loop on the space you just deployed:
# MAGIC
# MAGIC 1. **Someone edits the space in the UI.** Here the cell does it through the API: it adds a sample question.
# MAGIC    You can instead edit the space yourself in the UI and skip to 2.
# MAGIC 2. **Drift is detected.** `bundle plan` knows the version it deployed (its etag) and sees the live one.
# MAGIC    `scripts/check_drift.py` turns that into a clear stop (exit code 2) and saves a backup of the live space.
# MAGIC    The release pipeline runs this before every qa and prod deploy.
# MAGIC 3. **Keep it: the edit goes into git.** `scripts/sync_from_workspace.py` exports the live space with
# MAGIC    `databricks bundle generate genie-space` and writes it back into the YAML in `${var.catalog}` form. In dev, the
# MAGIC    hourly `genie-dev-sync` workflow does this and opens a pull request; each snapshot is tagged, which is the
# MAGIC    version history.
# MAGIC 4. **Or roll it back.** Deploying the version from git (`bundle deploy`) restores it. Any older version works the
# MAGIC    same way: check out that file from a tag or commit, then deploy (the `genie-rollback` workflow).

# COMMAND ----------

QUESTION = "Which store sold the most last week?"

# The whole space is one JSON document (serialized_space) with a version stamp (etag). An edit in the UI changes
# both; update_space with the etag fails if someone else changed the space in between.
live = w.genie.get_space(SPACE_ID, include_serialized_space=True)
content = json.loads(live.serialized_space)
questions = content.setdefault("config", {}).setdefault("sample_questions", [])
if not any(QUESTION in q.get("question", []) for q in questions):
    questions.append({"id": uuid.uuid4().hex, "question": [QUESTION]})
    questions.sort(key=lambda q: q["id"])
    w.genie.update_space(SPACE_ID, serialized_space=json.dumps(content), etag=live.etag)
print(f"1. 'UI edit' made: added the sample question {QUESTION!r}")

# COMMAND ----------

genie_dir = working_copy("genie")
drift_dir = WORK / "drift"                       # outside the bundle folder, so deploys never upload it
drift_dir.mkdir(parents=True, exist_ok=True)
plan_file = drift_dir / "plan.json"
plan_file.write_text(json.dumps(bundle_json("genie", ENV, "plan")))   # what deploy would do, as JSON
# check_drift.py: exit 0 = safe to deploy, 2 = edited outside the bundle, 3 = deploy would delete the space
drift = subprocess.run([sys.executable, "scripts/check_drift.py", str(plan_file), "-t", ENV, "--catalog", CATALOG,
                        "--backup-dir", str(drift_dir / "backup")],
                       cwd=genie_dir, env=cli_env(ENV), capture_output=True, text=True)
print(drift.stdout or drift.stderr)
print(f"2. check_drift.py exit code {drift.returncode} (2 = edited outside the bundle; a deploy now would undo it)")

# COMMAND ----------

# Export the live space into the working copy's YAML, then compare that file with the one in git.
sync = run([sys.executable, "scripts/sync_from_workspace.py", "-t", ENV], cwd=genie_dir, env=ENV)
in_git = (REPO / "genie_bundle" / "resources" / "freshcart_assistant.space.yml").read_text().splitlines()
synced = (genie_dir / "resources" / "freshcart_assistant.space.yml").read_text().splitlines()
print("3. the change, as it would appear in the sync pull request:")
print("\n".join(difflib.unified_diff(in_git, synced, "git", "live space", lineterm="", n=1)))

# COMMAND ----------

# MAGIC %md
# MAGIC To **keep** the edit, copy the synced file into the Git folder and commit it from the Git folder's UI (*Git...*
# MAGIC button): uncomment the line below. To **roll back**, run the next cell: it deploys the version that is in git.

# COMMAND ----------

# shutil.copyfile(genie_dir / "resources" / "freshcart_assistant.space.yml",
#                 REPO / "genie_bundle" / "resources" / "freshcart_assistant.space.yml")

# COMMAND ----------

working_copy("genie", refresh=True)              # the version in git
bundle("genie", ENV, "deploy", "--auto-approve")
restored = json.loads(w.genie.get_space(SPACE_ID, include_serialized_space=True).serialized_space)
still_there = any(QUESTION in q.get("question", []) for q in restored.get("config", {}).get("sample_questions", []))
print(f"4. rolled back to the git version: sample question present = {still_there}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 15 · Promote to qa and prod
# MAGIC
# MAGIC Promotion means deploying **the same bundles, from the same commit**, to the next target. Only the target's
# MAGIC variables differ: the catalog (`freshcart_qa`, `freshcart`), the names' suffix and the permissions (developers
# MAGIC can only view qa and prod). In prod the refresh job is scheduled nightly at 05:00 UTC and the Genie gate runs as a
# MAGIC regression monitor at 06:30 UTC.
# MAGIC
# MAGIC With the **promote** widget set to `yes`, this cell builds each later environment the way steps 5 to 13 built
# MAGIC this one, and stops if the qa gate fails: nothing goes to prod without it. In real life the GitHub workflows do
# MAGIC this (step 16), with an approval before prod.

# COMMAND ----------

if not PROMOTE:
    print("promote = no: skipped. Set the 'promote' widget to yes and run this cell to build qa and prod.")
else:
    for env in ENVIRONMENTS[ENVIRONMENTS.index(ENV) + 1:]:         # the environments after this one, in order
        print(f"\n========== {env} ({catalog_for(env)}) ==========")
        prepare_catalog(env)
        upload_raw(env)
        deploy_data(env)
        run_refresh(env)
        deploy_genie(env)
        if not run_gate(env, "gate" if env == "qa" else "smoke"):
            print(f"{env} did not pass: stopping the promotion here")
            break

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 16 · Hand over to CI/CD
# MAGIC
# MAGIC So far you deployed as yourself. In production, people do not deploy: **GitHub Actions** does, as one
# MAGIC **service principal** (a non-human identity) per environment, after review and tests. The workflows are in
# MAGIC `.github/workflows/`:
# MAGIC
# MAGIC | Workflow | When | What |
# MAGIC |---|---|---|
# MAGIC | `demo` (`ci.yml`) | every push and pull request | the local pipeline, all tests, docs up to date |
# MAGIC | `genie-ci` | pull requests that touch the bundles | space validation, `bundle validate` for every target, the list of changes in the space |
# MAGIC | `genie-release` | merge to `main` | dev, then qa (drift check, deploy, benchmark gate), then **approval**, then prod (deploy, smoke test, automatic restore if it fails, release tag) |
# MAGIC | `genie-rollback` | by hand | restore any release tag or commit to an environment |
# MAGIC | `genie-dev-sync` | hourly | edits made in the dev Genie UI become a tagged commit and a pull request |
# MAGIC
# MAGIC **What to set up once:**
# MAGIC
# MAGIC 1. **Service principals.** The next cell creates `freshcart-deployer-<env>` for each environment, adds each to
# MAGIC    `freshcart-genie-deployers` and `fc_all_regions`, lets it read its catalog and use the SQL warehouse. Set
# MAGIC    `CREATE_SERVICE_PRINCIPALS = True` to run it.
# MAGIC 2. **Federation policies**, so GitHub can sign in as each service principal without any stored secret (an
# MAGIC    account admin does this in the account console, or with the CLI command the cell prints).
# MAGIC 3. **GitHub environments** `dev`, `qa` and `prod` (repository *Settings* → *Environments*), each with the variables
# MAGIC    `DATABRICKS_HOST` and `DATABRICKS_CLIENT_ID` that the cell prints. On `prod`, add required reviewers and allow
# MAGIC    only the `main` branch.
# MAGIC 4. **A `main` branch**, as the repository's default branch, protected so that changes arrive by pull request with
# MAGIC    the `demo` and `genie-ci` checks green.
# MAGIC
# MAGIC Notes for the hand-over:
# MAGIC
# MAGIC * The data in qa and prod that this notebook built stays as it is. The release workflow deploys the Genie bundle
# MAGIC   as the service principal, which creates its own qa and prod spaces; remove the ones you deployed by hand with
# MAGIC   `bundle("genie", "qa", "destroy", "--auto-approve")` (same for prod) once the first release has run.
# MAGIC * CI finds the SQL warehouse by name: `Serverless Starter Warehouse` (`warehouse_id` in `genie_bundle/databricks.yml`).
# MAGIC   If yours has another name, change that lookup in the YAML.

# COMMAND ----------

CREATE_SERVICE_PRINCIPALS = False                # set to True to create the CI/CD identities

if not CREATE_SERVICE_PRINCIPALS:
    print("Skipped. Set CREATE_SERVICE_PRINCIPALS = True and re-run this cell.")
else:
    github_settings = []
    for env in ENVIRONMENTS:
        name = f"freshcart-deployer-{env}"
        sp = next(iter(w.service_principals.list(filter=f'displayName eq "{name}"')), None) \
            or w.service_principals.create(display_name=name)
        for g in ("freshcart-genie-deployers", "fc_all_regions"):
            add_member(GROUP_IDS[g], sp.id)
        cat = catalog_for(env)
        # read-only access for the Genie quality gate; a service principal is named by its application id
        if spark.sql(f"SHOW CATALOGS LIKE '{cat}'").count():
            spark.sql(f"GRANT USE CATALOG, USE SCHEMA, SELECT, EXECUTE ON CATALOG `{cat}` TO `{sp.application_id}`")
        w.warehouses.update_permissions(WAREHOUSE_ID, access_control_list=[dbsql.WarehouseAccessControlRequest(
            service_principal_name=sp.application_id, permission_level=dbsql.WarehousePermissionLevel.CAN_USE)])
        github_settings.append({"GitHub environment": env, "DATABRICKS_HOST": HOST,
                                "DATABRICKS_CLIENT_ID": sp.application_id, "service principal id": sp.id})
        print(f"\n# {env}: let GitHub Actions sign in as {name} (account admin, Databricks CLI):")
        print(f"databricks account service-principal-federation-policy create {sp.id} --json '"
              + json.dumps({"oidc_policy": {
                  "issuer": "https://token.actions.githubusercontent.com",
                  "audiences": ["<your Databricks account id>"],
                  "subject": f"repo:subrata-samanta/ai-ready-retail-data-genie-demo:environment:{env}"}}) + "'")
    display(pd.DataFrame(github_settings))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 17 · Clean up
# MAGIC
# MAGIC With the **cleanup** widget set to `yes`, this cell removes what the notebook deployed: `bundle destroy` deletes
# MAGIC the Genie space, the jobs and the pipeline (the bundles know exactly what they created), then the catalogs and their
# MAGIC data are dropped. The groups stay. Serverless compute stops by itself when idle, so leaving everything costs
# MAGIC nothing until it runs again, except the nightly prod schedule if you promoted.

# COMMAND ----------

if not CLEANUP:
    print("cleanup = no: nothing deleted.")
else:
    built = ENVIRONMENTS[ENVIRONMENTS.index(ENV):] if PROMOTE else [ENV]
    for env in built:
        # Genie first (it reads the data), then the data bundle; destroy deletes only what the bundle created
        bundle("genie", env, "destroy", "--auto-approve", check=False)
        bundle("data", env, "destroy", "--auto-approve", check=False)
        spark.sql(f"DROP CATALOG IF EXISTS `{catalog_for(env)}` CASCADE")
        print(f"{env}: removed")

# COMMAND ----------

# MAGIC %md
# MAGIC ## If something goes wrong
# MAGIC
# MAGIC | Where | Message | What to do |
# MAGIC |---|---|---|
# MAGIC | Step 3 | download fails | The compute has no internet access. Install the CLI on your laptop, or use the workspace's web terminal, and run the same `databricks bundle` commands there. |
# MAGIC | Step 5 | `CREATE CATALOG` fails | No default storage for new catalogs: set the **catalog** widget to an existing catalog (for example `workspace`), or ask an account admin for a storage location. |
# MAGIC | Step 7 or 11 | a group does not exist | Re-run step 4. |
# MAGIC | Step 8 | pipeline update failed | Open the pipeline link from step 7: the failing table and its error are shown in the graph. |
# MAGIC | Step 9 | zero rows in the facts | Group membership takes a minute to apply: wait and re-run the cell. |
# MAGIC | Step 10 | an answer does not match | The row shows which question, so which tables, to compare. A different **as_of_date** changes every relative period. |
# MAGIC | Step 12 | Genie is not available | Genie must be enabled for the workspace (settings, previews or AI features, depending on the account). |
# MAGIC | Step 13 | gate FAILED | The job output lists each benchmark and Genie's verdict. Fix the lowest layer that lacks context (doc 7 in `docs/`). |
# MAGIC | Step 14 | exit code 0 instead of 2 | The edit had not reached the workspace yet: re-run the drift cell. |
# MAGIC
# MAGIC The design behind every table is explained in `docs/` (start with `docs/01-business-and-questions.md`) and in
# MAGIC `notebooks/FreshCart_Data_Transformation_Walkthrough.ipynb`. The CI/CD method is explained in
# MAGIC `notebooks/Genie_CICD_with_Declarative_Automation_Bundles.ipynb`.
