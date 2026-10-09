# Databricks notebook source
# MAGIC %md
# MAGIC # The Genie CI/CD process, from scratch
# MAGIC
# MAGIC This guide explains, step by step, how a Genie space is developed, versioned and promoted **dev → qa → prod** with
# MAGIC git, GitHub and Declarative Automation Bundles (DAB). It is the companion of the setup notebook
# MAGIC `Genie_Project_Template`: that one *sets everything up*; this one *explains how it works* and lets you see it on your
# MAGIC own project.
# MAGIC
# MAGIC | Chapter | You will understand |
# MAGIC |---|---|
# MAGIC | 1 | The big picture: why CI/CD for Genie, the words used, the architecture, the environments |
# MAGIC | 2 | Where the Genie space lives as code, and how one file serves dev, qa and prod |
# MAGIC | 3 | **Version history**: the four layers that record every version, and how to answer "what is in prod?" |
# MAGIC | 4 | **Raising a pull request**: the three ways to change the space, including **syncing the dev Genie UI with GitHub** |
# MAGIC | 5 | **Environment promotion**: what happens, step by step, from merge to dev, from **dev to qa**, and from **qa to prod** |
# MAGIC | 6 | Rollback: putting an earlier version back |
# MAGIC | 7 | Who may do what: identities, approvals, protection |
# MAGIC | 8 | A complete worked example, Monday to Thursday |
# MAGIC | 9 | Troubleshooting, FAQ and a cheat sheet |
# MAGIC
# MAGIC **Read-only.** Every code cell only *reads*: your project files, and (if you give it your repository) GitHub and the
# MAGIC workspace. Nothing is created, changed or deployed. Cells that need GitHub or the workspace say "skipped" when they
# MAGIC are not configured, so you can read the whole guide without any setup.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Setup (optional, read-only)
# MAGIC
# MAGIC | Widget | Meaning |
# MAGIC |---|---|
# MAGIC | github_repo | `owner/name` of your project's repository; leave blank to read only the local files |
# MAGIC | github_token_secret | `<scope>/<key>` of the GitHub token in Databricks secrets (the setup notebook stores it there); read access is enough |
# MAGIC | project_folder | the project's folder inside the repository; blank when it is the repository root |

# COMMAND ----------

# MAGIC %pip install --quiet "databricks-sdk>=0.145.0" pyyaml

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("github_repo", "", "1 GitHub repository (owner/name, blank = local only)")
dbutils.widgets.text("github_token_secret", "genie/github_token", "2 token secret (scope/key)")
dbutils.widgets.text("project_folder", "", "3 project folder in the repository")

import base64, json, sys
from pathlib import Path

import pandas as pd
import requests

GITHUB_API = "https://api.github.com"
ENVIRONMENTS = ["dev", "qa", "prod"]


def find_project() -> Path:
    for c in [Path.cwd().parent, Path.cwd()]:
        if (c / "genie.config.yml").exists():
            return c
    raise RuntimeError("Open this notebook from the project's Git folder (genie.config.yml not found).")


PROJECT_DIR = find_project()
sys.path.insert(0, str(PROJECT_DIR / "scripts"))
import genie_tools as T                          # the project's own config and space library

CFG = {env: T.settings(env) for env in ENVIRONMENTS}
SPACE = T.load_space()
REPO_NAME = dbutils.widgets.get("github_repo").strip()
FOLDER = dbutils.widgets.get("project_folder").strip().strip("/")
SPACE_PATH = f"{FOLDER}/space/genie_space.yml" if FOLDER else "space/genie_space.yml"

HEADERS = None
if REPO_NAME:
    scope, key = dbutils.widgets.get("github_token_secret").split("/", 1)
    HEADERS = {"Authorization": f"Bearer {dbutils.secrets.get(scope, key)}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}


def gh_get(path: str, default=None):
    """Read from GitHub (GET only: this guide never changes anything)."""
    r = requests.get(f"{GITHUB_API}/repos/{REPO_NAME}{path}", headers=HEADERS, timeout=60)
    if r.status_code == 404:
        return default
    r.raise_for_status()
    return r.json()


def space_at(ref: str) -> dict:
    f = gh_get(f"/contents/{SPACE_PATH}?ref={ref}")
    return T.space_from_text(base64.b64decode(f["content"]).decode()) if f else {}


try:
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    w.current_user.me()
except Exception as e:                           # noqa: BLE001
    w = None
    print(f"(no workspace connection: {str(e).splitlines()[0][:80]})")

print(f"project {T.project_name()} in {PROJECT_DIR}")
print(f"GitHub: {REPO_NAME or 'not configured (local files only)'};  workspace: {'connected' if w else 'not connected'}")

# COMMAND ----------

# MAGIC %md
# MAGIC # 1 · The big picture
# MAGIC
# MAGIC ## 1.1 Why CI/CD for a Genie space?
# MAGIC
# MAGIC A Genie space is configuration: tables, instructions, example SQL, filters and benchmarks. Without CI/CD it is edited
# MAGIC by hand in each workspace, and that causes the same problems every time:
# MAGIC
# MAGIC | Without CI/CD | With this process |
# MAGIC |---|---|
# MAGIC | Nobody knows what changed, when, or why | every change is a reviewed commit with a description |
# MAGIC | qa and prod are copied by hand, and drift apart | the **same file** is deployed to every environment by a pipeline |
# MAGIC | A bad edit in prod cannot be undone | every prod release is a tagged version; rollback is one workflow |
# MAGIC | Quality is a feeling | benchmarks must pass in qa before prod; prod is re-checked every night |
# MAGIC | Edits in the UI are lost or overwrite each other | UI edits in dev become pull requests; edits in qa/prod block the release |
# MAGIC
# MAGIC ## 1.2 The words used in this guide
# MAGIC
# MAGIC | Word | Meaning here |
# MAGIC |---|---|
# MAGIC | **git repository** | the folder of files on GitHub that *is* the project: config, the space file, the pipeline |
# MAGIC | **branch** | a line of work. `main` is what is released; you work on a *feature branch*, e.g. `feature/new-question` |
# MAGIC | **commit** | one saved change of files, with an author, a time and a message; identified by a hash such as `1a2b3c4` |
# MAGIC | **pull request (PR)** | a request to merge a branch into `main`; where checks run and people review |
# MAGIC | **merge** | accepting a PR: its commits become part of `main` |
# MAGIC | **tag** | a permanent name for one commit, e.g. `genie-prod-20261001.0915-1a2b3c4` |
# MAGIC | **GitHub Actions / workflow / job** | automation in GitHub: a *workflow* (a YAML file in `.github/workflows/`) runs *jobs* on events such as a merge |
# MAGIC | **GitHub environment** | `dev`, `qa`, `prod` in the repository settings: holds each environment's sign-in and its protection (prod: approvers) |
# MAGIC | **Declarative Automation Bundle (DAB)** | the project's Databricks resources described as YAML (`databricks.yml`, `resources/`); `databricks bundle deploy -t <target>` creates or updates them |
# MAGIC | **target** | one environment of the bundle: `sandbox`, `dev`, `qa`, `prod` |
# MAGIC | **service principal (SP)** | a non-human Databricks identity; each environment has its own, and only it deploys there |
# MAGIC | **drift** | the live space was edited outside the bundle (in the UI) since the last deploy |
# MAGIC | **etag** | a version counter Databricks keeps for the space; it changes with every edit, which is how drift is detected |
# MAGIC | **gate** | a check that must pass before the next step (tests on a PR, benchmarks in qa, approval before prod) |
# MAGIC
# MAGIC ## 1.3 The architecture
# MAGIC
# MAGIC ```text
# MAGIC             YOU                                   GITHUB                                      DATABRICKS
# MAGIC  ┌────────────────────────┐        ┌──────────────────────────────────────┐        ┌───────────────────────────┐
# MAGIC  │ edit in the dev Genie  │──sync─▶│ branch genie/dev-sync ─┐              │        │                           │
# MAGIC  │ UI, or edit the file   │        │ feature branch ────────┼─▶ PULL      │        │                           │
# MAGIC  │ in a branch            │──push─▶│                        │   REQUEST    │        │                           │
# MAGIC  └────────────────────────┘        │      genie-ci checks ──┘   + review   │        │                           │
# MAGIC                                    │                              │ merge │        │                           │
# MAGIC                                    │                              ▼       │        │                           │
# MAGIC                                    │  main ──▶ genie-release workflow     │        │                           │
# MAGIC                                    │            ├─ dev   ───────────────────deploy─▶│ [DEV] space  (dev catalog)│
# MAGIC                                    │            ├─ qa    drift · gate ──────deploy─▶│ [QA]  space  (qa catalog) │
# MAGIC  ┌────────────────────────┐        │            │        benchmarks · smoke│        │                           │
# MAGIC  │ APPROVER reads the qa  │─approve▶│           ├─ prod  drift · smoke ────deploy─▶│ prod space  (prod catalog)│
# MAGIC  │ report, approves prod  │        │            └─ tag genie-prod-* + release notes │ nightly quality + usage,  │
# MAGIC  └────────────────────────┘        │  genie-rollback: any version → any env ─────▶│ dashboard, alerts         │
# MAGIC                                    └──────────────────────────────────────┘        └───────────────────────────┘
# MAGIC ```
# MAGIC
# MAGIC Three rules make it work:
# MAGIC 1. **Git is the source of truth.** The workspace is an output of git, never the other way round (except dev UI edits,
# MAGIC    which come back to git through a pull request).
# MAGIC 2. **Build once, deploy many.** Every environment receives the *same commit*; only settings (catalog, title, permissions,
# MAGIC    schedules) differ, and they come from `genie.config.yml`.
# MAGIC 3. **Only the pipeline deploys** dev, qa and prod, each as its own service principal. People merge and approve.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1.4 The environments
# MAGIC
# MAGIC | | sandbox | dev | qa | prod |
# MAGIC |---|---|---|---|---|
# MAGIC | Purpose | your private try-out copy | the team's shared working space | the test of the release candidate | what business users use |
# MAGIC | Deployed by | you, by hand | GitHub Actions, every merge to `main` | GitHub Actions, after dev | GitHub Actions, after qa **and** an approval |
# MAGIC | Editing in the UI | yes (yours) | yes: synced to git as a pull request | no: blocks the next release | no: blocks the next release |
# MAGIC | Gates | — | — | drift, health checks, benchmarks, smoke test | drift, smoke test (auto-restore on failure) |
# MAGIC | Monitoring | — | — | — | nightly quality and usage jobs, dashboard, SQL alerts |
# MAGIC
# MAGIC This project's actual values, read from `genie.config.yml`:

# COMMAND ----------

rows = []
for env in ["sandbox"] + ENVIRONMENTS:
    c = T.settings(env)
    rows.append({"environment": env, "space title": T.space_title(env) if env != "sandbox" else f"[dev <you>] {c['space_title']}",
                 "data": f"{c['catalog']}.{c['schema']}", "warehouse": c["warehouse_name"],
                 "gate": f">= {float(c['gate_min_accuracy']):.0%}" if env == "qa" else "",
                 "alerts": c.get("alerts_pause_status", "")})
display(pd.DataFrame(rows))
for r in rows:
    print(f"{r['environment']:>8}: {r['space title']:<45} data {r['data']}")

# COMMAND ----------

# MAGIC %md
# MAGIC # 2 · The Genie space as code
# MAGIC
# MAGIC ## 2.1 The files
# MAGIC
# MAGIC | File | What it holds | Who changes it |
# MAGIC |---|---|---|
# MAGIC | `space/genie_space.yml` | **the space's content**: data sources (tables, metric views), text instructions, example SQL, SQL filters / expressions / measures, trusted functions, sample questions, **benchmarks** | developers (PR) or the dev UI (sync) |
# MAGIC | `genie.config.yml` | settings per environment: catalog, schema, warehouse, title, groups, gate thresholds, monitoring, alerts | developers (PR), reviewed by the platform team |
# MAGIC | `resources/genie_space.yml` | the space as a bundle resource: title, description, warehouse, `CAN_RUN` for users | rarely |
# MAGIC | `resources/genie_quality.job.yml` | the quality job (gate, smoke test, nightly monitor) | rarely |
# MAGIC | `databricks.yml` | the targets sandbox/dev/qa/prod, their permissions and schedules | rarely |
# MAGIC | `.github/workflows/*.yml` | the pipeline: `genie-ci`, `genie-release`, `genie-dev-sync`, `genie-rollback` | rarely |
# MAGIC
# MAGIC ## 2.2 One file, every environment
# MAGIC
# MAGIC The space file never names a real catalog. Tables are written `${var.catalog}.${var.schema}.<table>`. When the bundle
# MAGIC deploys a target, it fills in that target's values from `genie.config.yml`. When the dev space is exported back to git
# MAGIC (the sync), the real names are turned back into the variables. So the **same file** deploys everywhere, and a diff
# MAGIC between two versions only ever shows real content changes, never catalog names.

# COMMAND ----------

ident = sorted(T.data_sources(SPACE))[0]
example = next(iter(T.sql_statements(SPACE)), ("", ""))
print(f"in git            {ident}")
for env in ENVIRONMENTS:
    print(f"deployed to {env:<5} {sorted(T.data_sources(T.for_target(SPACE, env)))[0]}")
print("\nThe first SQL of the space, as written in git:\n" + example[1])
print("\n... and as Genie receives it in prod:\n" + T.for_target({"x": example[1]}, "prod")["x"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2.3 What the space contains
# MAGIC
# MAGIC The file has the same sections as the Genie UI's configuration. Below is this project's summary. The **content hash**
# MAGIC is a short fingerprint of the content: two environments with the same hash run exactly the same version. Chapter 3 uses
# MAGIC it to tell which version is live where.

# COMMAND ----------

print(f"content hash {T.content_hash(SPACE)}")
display(pd.DataFrame([{"part": k, "items": v} for k, v in T.summary(SPACE).items()]))
for k, v in T.summary(SPACE).items():
    print(f"  {k:<18} {v}")

# COMMAND ----------

# MAGIC %md
# MAGIC # 3 · How the Genie version history is managed
# MAGIC
# MAGIC ## 3.1 Four layers of history
# MAGIC
# MAGIC Every version of the space is recorded, in four complementary places:
# MAGIC
# MAGIC | Layer | What is recorded | Created by | Where you see it | Kept |
# MAGIC |---|---|---|---|---|
# MAGIC | **1 · Commits on `main`** | every reviewed change: who, when, why (the PR) and exactly what (the diff) | merging a pull request | GitHub → *Commits*, or *History* of `space/genie_space.yml` | forever |
# MAGIC | **2 · Dev snapshots** | the state of the dev space each time someone edited it in the UI | `genie-dev-sync` (hourly, and first in every release) | tags `genie-dev-snapshot-<UTC time>`; branch `genie/dev-sync` | forever |
# MAGIC | **3 · Prod releases** | every version that reached prod, with release notes listing the changes since the previous release | `genie-release`, after prod deployed and passed its smoke test | tags `genie-prod-<UTC>-<commit>`; GitHub → *Releases* | forever (protected from deletion) |
# MAGIC | **4 · Deploy backups and results** | the exact live space just before each deploy; the commit behind every quality result | `check_drift.py` in every release and rollback; the quality job | workflow run → *Artifacts*; column `version` in the monitoring tables | 90 days (qa), 400 days (prod); tables forever |
# MAGIC
# MAGIC Next to the history, one **pointer per environment** says what it runs *right now*: the tags `genie-deployed-dev`,
# MAGIC `genie-deployed-qa` and `genie-deployed-prod`. The release (and the rollback) moves each one to the deployed commit
# MAGIC right after that environment is deployed. The dev sync uses `genie-deployed-dev` to tell a real UI edit from a change
# MAGIC that is merged but not yet deployed (4.1).
# MAGIC
# MAGIC ```text
# MAGIC  time ─────────────────────────────────────────────────────────────────────────────────────────────▶
# MAGIC
# MAGIC  dev UI edit        ●──snapshot tag genie-dev-snapshot-20261006T0917Z──▶ PR "Genie: sync changes made in dev"
# MAGIC                                                                             │ merge
# MAGIC  main      ──●───────────●───────────────────────────────────────────────────●───────────●──────────
# MAGIC              │PR #12     │PR #13                                             │PR #15     │PR #16
# MAGIC              ▼           ▼                                                   ▼           ▼
# MAGIC  releases  genie-prod-20261001.0915-1a2b3c4               genie-prod-20261007.1402-9f8e7d6
# MAGIC            (notes: +2 benchmarks)                          (notes: +1 sample question, ~1 instruction)
# MAGIC ```
# MAGIC
# MAGIC ## 3.2 Questions you can answer
# MAGIC
# MAGIC | Question | How |
# MAGIC |---|---|
# MAGIC | What is in prod right now? | the tag `genie-deployed-prod` (and the latest `genie-prod-*` release); section 3.5 proves it by comparing content hashes with the live space |
# MAGIC | What changed between two releases? | the GitHub release notes; or section 3.4 for any two tags, branches or commits |
# MAGIC | What is merged but not yet in prod? | section 3.4 compares `main` with the latest release |
# MAGIC | What did the dev space look like last Tuesday? | the `genie-dev-snapshot-*` tag of that day |
# MAGIC | Who changed this instruction, and why? | GitHub → the file → *Blame* or *History* → the pull request |
# MAGIC | Which version produced this bad quality result? | the `version` column of `genie_quality_runs` is the deployed commit |
# MAGIC | What exactly was overwritten when someone used *allow_drift*? | the `genie-<env>-pre-deploy-<run>` artifact of that run |
# MAGIC
# MAGIC ## 3.3 How a change list is produced
# MAGIC
# MAGIC Reviewers never read raw YAML. The scripts compare two versions item by item and print a change list. This is what
# MAGIC `genie-ci` writes into every pull request, and what the release notes contain. Here it is on an imaginary change to
# MAGIC your space: one new sample question, and one edited instruction.

# COMMAND ----------

after = T.copy_space(SPACE)
after.setdefault("config", {}).setdefault("sample_questions", []).append(
    {"id": T.new_id("guide"), "question": ["A question added in this guide"]})
instructions = after.get("instructions", {}).get("text_instructions", [])
if instructions:
    instructions[0]["content"] = list(instructions[0]["content"]) + ["\n", "Always say which period the figures cover."]
for line in T.diff(SPACE, after):
    print(line)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3.4 Your real history (GitHub)
# MAGIC
# MAGIC The prod releases and dev snapshots of your repository, the latest release notes, the commits that changed the space,
# MAGIC and what is merged on `main` but not yet released to prod.

# COMMAND ----------

if not REPO_NAME:
    print("skipped: set the widget github_repo to see your repository's history")
else:
    tags = [t["name"] for t in gh_get("/tags?per_page=100", [])]
    releases = sorted((t for t in tags if t.startswith("genie-prod-")), reverse=True)
    snapshots = sorted((t for t in tags if t.startswith("genie-dev-snapshot-")), reverse=True)
    print(f"{len(releases)} prod release(s): {releases[:5]}")
    print(f"{len(snapshots)} dev snapshot(s): {snapshots[:5]}")
    tag_sha = {t["name"]: t["commit"]["sha"] for t in gh_get("/tags?per_page=100", [])}
    for env in ENVIRONMENTS:
        sha = tag_sha.get(f"genie-deployed-{env}")
        print(f"{env:>5} runs " + (f"commit {sha[:7]} (tag genie-deployed-{env})" if sha else "nothing yet (no genie-deployed tag)"))
    for rel in (gh_get("/releases?per_page=3", []) or [])[:3]:
        print(f"\n== release {rel['tag_name']} ({rel.get('published_at') or ''})\n{(rel.get('body') or '').strip()[:1200]}")
    repo = gh_get("", {})
    main = "main" if gh_get("/branches/main") else repo.get("default_branch", "main")
    commits = gh_get(f"/commits?path={SPACE_PATH}&sha={main}&per_page=10", [])
    display(pd.DataFrame([{"date": c["commit"]["committer"]["date"], "commit": c["sha"][:7],
                           "author": c["commit"]["author"]["name"], "message": c["commit"]["message"].splitlines()[0]}
                          for c in commits]))
    print(f"\n{len(commits)} recent commit(s) changed {SPACE_PATH}")
    pending = T.diff(space_at(releases[0]), space_at(main)) if releases else T.diff({}, space_at(main))
    print(f"\nmerged on {main} but not yet in prod ({releases[0] if releases else 'no release yet'} -> {main}):")
    print("\n".join("  " + p for p in pending) or "  nothing: prod runs what is on main")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3.5 Which version is live in each environment?
# MAGIC
# MAGIC Answered from the content itself, not from bookkeeping: each environment's live space is read, made
# MAGIC environment-neutral, hashed, and compared with the space file of every prod release.

# COMMAND ----------

if not (REPO_NAME and w):
    print("skipped: needs github_repo and a workspace connection")
else:
    release_hash = {tag: T.content_hash(space_at(tag)) for tag in releases[:10]}
    spaces = {s.title: s for s in (w.genie.list_spaces().spaces or [])}
    for env in ENVIRONMENTS:
        s = spaces.get(T.space_title(env))
        if not s:
            print(f"{env:>5}: no space yet")
            continue
        live = w.genie.get_space(s.space_id, include_serialized_space=True)
        h = T.content_hash(T.from_target(json.loads(live.serialized_space), env))
        tag = next((t for t, th in release_hash.items() if th == h), None)
        print(f"{env:>5}: content {h} = " + (tag or "not a prod release (dev runs main, or an edit was made in the UI)"))

# COMMAND ----------

# MAGIC %md
# MAGIC # 4 · Making a change: raising a pull request
# MAGIC
# MAGIC Every change reaches `main` through a **pull request**. There are three ways to produce one; pick the one that suits
# MAGIC the change.
# MAGIC
# MAGIC | Way | Best for | You need |
# MAGIC |---|---|---|
# MAGIC | **A · Edit the dev space in the Genie UI** | instructions, sample questions, example SQL: trying wording with real answers | `CAN_MANAGE` on the dev space (developers group) |
# MAGIC | **B · Edit the file in a branch** | larger or structural changes, benchmarks, settings in `genie.config.yml` | write access to the repository |
# MAGIC | **C · Try in your sandbox, then sync** | experiments you do not want the team to see yet | a sandbox deployment (`make deploy`) |
# MAGIC
# MAGIC ## 4.1 Way A · Sync the dev Genie UI with GitHub (automatic)
# MAGIC
# MAGIC You change the **[DEV]** space in the Genie UI like any Genie space. You don't touch git at all. The workflow
# MAGIC **`genie-dev-sync`** brings your edit into GitHub:
# MAGIC
# MAGIC ```text
# MAGIC  you edit the [DEV] space ──▶ genie-dev-sync (every hour at :17, or on demand, or first step of every release)
# MAGIC                                 1. bundle summary + bundle generate genie-space: export the live dev space
# MAGIC                                 2. dev catalog/schema -> ${var.catalog}/${var.schema}, canonical order
# MAGIC                                 3. compare it with what was last DEPLOYED to dev (tag genie-deployed-dev):
# MAGIC                                      no difference?  stop: "no edits in the dev Genie UI since the last deploy"
# MAGIC                                 4. apply only those UI edits, item by item, onto space/genie_space.yml as it is on main
# MAGIC                                    (a change merged after that deploy is kept, never undone; an item changed on both
# MAGIC                                    sides keeps the UI's version and is reported)
# MAGIC                                 5. commit on branch genie/dev-sync
# MAGIC                                    tag genie-dev-snapshot-<UTC time>                 (version history, layer 2)
# MAGIC                                    open / update PR "Genie: sync changes made in dev"
# MAGIC                                    start genie-ci on it                               (the required check)
# MAGIC ```
# MAGIC
# MAGIC Why step 3 compares with the *deployed* version and not with `main`: when a pull request is merged, `main` is ahead
# MAGIC of dev until the release has deployed dev. If the sync compared the live dev space with `main`, it would see the
# MAGIC just-merged change as "missing in dev" and open a pull request that undoes it. Comparing with `genie-deployed-dev`
# MAGIC isolates what really happened in the UI.
# MAGIC
# MAGIC **Run the sync now instead of waiting for the hour:** GitHub → *Actions* → **genie-dev-sync** → *Run workflow*
# MAGIC (branch `main`) → *Run workflow*. After a minute the pull request appears, or is updated, under *Pull requests*.
# MAGIC
# MAGIC **Then:** open the PR, read the change list in the *genie-ci* check summary (*Details* → *Summary*), and either:
# MAGIC * **merge it**: the edit is released to qa and prod like any change (chapter 5), or
# MAGIC * **close it**: the edit is discarded. The next release resets the dev space to `main`, and the snapshot tag keeps
# MAGIC   the discarded version in case you want it back.
# MAGIC
# MAGIC **Good to know**
# MAGIC * There is only one sync PR at a time. Further UI edits are added to it until it is merged or closed.
# MAGIC * Every hourly run that finds a change creates a new snapshot tag, so even edits you discard stay in the history.
# MAGIC * The release always runs a sync first, so a merge to `main` never silently overwrites a UI edit that was not yet
# MAGIC   captured.
# MAGIC * Edit only **dev** in the UI. Edits in qa or prod make the next release stop (chapter 5.5).

# COMMAND ----------

if not REPO_NAME:
    print("skipped: set github_repo to see the current sync pull request")
else:
    owner = REPO_NAME.split("/")[0]
    sync = gh_get(f"/pulls?head={owner}:genie/dev-sync&state=open", [])
    if not sync:
        print("no open sync pull request: the dev space matches main (or nobody edited it since the last merge)")
    for p in sync:
        print(f"sync pull request #{p['number']}: {p['html_url']}")
        for f in gh_get(f"/pulls/{p['number']}/files", []):
            print(f"  {f['filename']}\n" + "\n".join("    " + l for l in (f.get("patch") or "").splitlines()[:40]))
        try:
            new = T.space_from_text(base64.b64decode(gh_get(f"/contents/{SPACE_PATH}?ref=genie/dev-sync")["content"]).decode())
            print("\n  change list:\n" + "\n".join("    " + c for c in T.diff(space_at(p["base"]["ref"]), new)))
        except Exception as e:                   # noqa: BLE001
            print(f"  (change list not available: {e})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4.2 Way B · Edit the file in a branch and open a pull request
# MAGIC
# MAGIC **In Databricks (Git folder), step by step**
# MAGIC 1. Open your project's Git folder → click the **branch name** at the top → **Create branch** → name it, e.g.
# MAGIC    `feature/add-margin-benchmark` (always branch from `main`).
# MAGIC 2. Edit `space/genie_space.yml` (or `genie.config.yml`). Keep the format: in a notebook cell of the project's
# MAGIC    `notebooks/` folder run `!python ../scripts/validate_space.py --fix`, which rewrites it in canonical form (and
# MAGIC    `!python ../scripts/validate_space.py --diff-against main` shows your change list).
# MAGIC 3. Git dialog → review the changed files → write a commit message ("Add a benchmark for margin by region") →
# MAGIC    **Commit & Push**.
# MAGIC 4. On GitHub, a banner offers **Compare & pull request** → fill in the template (what changes, why, the checklist) →
# MAGIC    **Create pull request**.
# MAGIC
# MAGIC **On a laptop**
# MAGIC ```bash
# MAGIC git checkout main && git pull
# MAGIC git checkout -b feature/add-margin-benchmark
# MAGIC # edit space/genie_space.yml
# MAGIC make fix          # canonical form
# MAGIC make check        # everything genie-ci checks without a workspace
# MAGIC make diff         # the change list versus main
# MAGIC git commit -am "Add a benchmark for margin by region"
# MAGIC git push -u origin feature/add-margin-benchmark      # then open the PR on GitHub
# MAGIC ```
# MAGIC
# MAGIC **Rules for editing the space file**
# MAGIC * Write tables as `${var.catalog}.${var.schema}.<table>` (or `${var.catalog}.<other schema>.<table>`), never a real
# MAGIC   catalog name. The check fails otherwise.
# MAGIC * Every new item needs a unique 32-character hex `id`: `python -c "import uuid; print(uuid.uuid4().hex)"`.
# MAGIC * A new kind of question deserves a **benchmark** (a question with its trusted SQL answer): benchmarks are the gate.
# MAGIC
# MAGIC ## 4.3 Way C · Try it in your sandbox first
# MAGIC
# MAGIC ```bash
# MAGIC make deploy                 # = databricks bundle deploy -t sandbox: your own "[dev <you>] ..." space, on dev data
# MAGIC # edit your sandbox space in the Genie UI, ask questions, iterate
# MAGIC make sync T=sandbox         # write your sandbox's content into space/genie_space.yml
# MAGIC make gate T=sandbox         # optional: run the benchmarks on your copy
# MAGIC git checkout -b feature/... && git commit -am "..." && git push   # then open the PR
# MAGIC make destroy                # remove your sandbox when done
# MAGIC ```
# MAGIC
# MAGIC ## 4.4 What happens on the pull request: `genie-ci`
# MAGIC
# MAGIC | Job · step | What it checks | If it fails |
# MAGIC |---|---|---|
# MAGIC | checks · project tests | config resolves for every environment, every variable defined, drift and quality logic | the test names the problem |
# MAGIC | checks · validate the space | valid structure, unique ids, no hard-coded catalog, canonical form | `make fix`, or follow the message |
# MAGIC | checks · notebooks up to date | `.ipynb` files match their sources | `python scripts/build_notebooks.py` |
# MAGIC | checks · readiness review | MUST checks: enough benchmarks for the gate, a smoke question, distinct environments | add what is missing |
# MAGIC | checks · change list | (always passes) writes what changes in the space into the summary | — |
# MAGIC | bundle · validate + plan (dev) | the bundle resolves; what would change in dev | read the error |
# MAGIC | bundle · SQL on dev data | every example and benchmark SQL runs on the dev tables | fix the SQL or the data |
# MAGIC
# MAGIC `checks` is a **required status check**: GitHub does not allow merging until it is green.
# MAGIC
# MAGIC ## 4.5 Review and merge
# MAGIC
# MAGIC The reviewer reads the **change list** (not the YAML), the readiness review and the SQL check. Then they ask: is the
# MAGIC wording clear for business users? Is there a benchmark for the new behaviour? With CODEOWNERS set up, the right team
# MAGIC is asked for review automatically. Then **Merge** (squash is recommended: one commit per change). **Merging starts the
# MAGIC release** (chapter 5).

# COMMAND ----------

if not REPO_NAME:
    print("skipped: set github_repo to see the open pull requests and their checks")
else:
    prs = gh_get("/pulls?state=open&per_page=20", [])
    rows = []
    for p in prs:
        runs = gh_get(f"/commits/{p['head']['sha']}/check-runs", {}).get("check_runs", [])
        rows.append({"#": p["number"], "title": p["title"], "branch": p["head"]["ref"],
                     "checks": ", ".join(f"{c['name']}={c['conclusion'] or c['status']}" for c in runs) or "none yet"})
    display(pd.DataFrame(rows))
    for r in rows:
        print(f"#{r['#']} {r['title']}  [{r['branch']}]  {r['checks']}")
    print(f"{len(rows)} open pull request(s)")

# COMMAND ----------

# MAGIC %md
# MAGIC # 5 · Environment promotion: dev → qa → prod
# MAGIC
# MAGIC ## 5.1 What starts a release
# MAGIC
# MAGIC The workflow **`genie-release`** starts when:
# MAGIC * a pull request is **merged to `main`** and it changed the space, the config, the bundle or the job code
# MAGIC   (`space/**`, `genie.config.yml`, `databricks.yml`, `resources/**`, `src/**`); or
# MAGIC * someone starts it by hand: *Actions* → **genie-release** → *Run workflow*. This is how you re-run a release, or
# MAGIC   release with *allow_drift* or *allow_destroy*.
# MAGIC
# MAGIC Only one deployment runs at a time (releases and rollbacks share a queue).
# MAGIC
# MAGIC ## 5.2 The whole release, step by step
# MAGIC
# MAGIC ```text
# MAGIC  merge to main (commit 9f8e7d6)
# MAGIC    │
# MAGIC    ├─ 1. dev-snapshot   genie-dev-sync: capture any UI edits in dev first (they become a PR; nothing is lost)
# MAGIC    │
# MAGIC    ├─ 2. dev            sign in as <project>-deployer-dev
# MAGIC    │                    bundle validate -t dev  ·  bundle deploy -t dev            → [DEV] space = 9f8e7d6
# MAGIC    │                    tag genie-deployed-dev → 9f8e7d6
# MAGIC    │
# MAGIC    ├─ 3. qa             sign in as <project>-deployer-qa
# MAGIC    │                    bundle plan -t qa  → check_drift.py:
# MAGIC    │                        · edited in the qa UI since the last deploy?           → STOP (exit 2)
# MAGIC    │                        · would the space be deleted or recreated?             → STOP (exit 3)
# MAGIC    │                        · back up the live qa space (artifact)
# MAGIC    │                    bundle deploy -t qa                                       → [QA] space = 9f8e7d6
# MAGIC    │                    tag genie-deployed-qa → 9f8e7d6
# MAGIC    │                    bundle run genie_quality -t qa      (health checks + benchmarks ≥ gate)  → STOP if below
# MAGIC    │                    quality report → run summary        (for the approver)
# MAGIC    │                    bundle run genie_quality -t qa --params mode=smoke     (one question, answered with SQL)
# MAGIC    │
# MAGIC    ├─ ⏸  waiting for approval: environment prod has required reviewers
# MAGIC    │
# MAGIC    └─ 4. prod           sign in as <project>-deployer-prod
# MAGIC                         bundle plan -t prod → check_drift.py (drift / destroy gate, backup of the live prod space)
# MAGIC                         bundle deploy -t prod                                     → prod space = 9f8e7d6
# MAGIC                         smoke test → if it fails: redeploy the previous release's space, and stop
# MAGIC                         tag genie-deployed-prod → 9f8e7d6
# MAGIC                         tag genie-prod-<UTC>-9f8e7d6 + GitHub release with notes (version history, layer 3)
# MAGIC ```
# MAGIC
# MAGIC ## 5.3 dev → qa: what is checked
# MAGIC
# MAGIC | Step | Purpose | Configured by |
# MAGIC |---|---|---|
# MAGIC | Deploy dev | the team's shared space follows `main` | — |
# MAGIC | Drift gate (qa) | never silently overwrite an edit made in the qa UI | — |
# MAGIC | Backup (qa) | the exact qa space before the deploy, as an artifact (90 days) | — |
# MAGIC | Health checks | every table of the space is readable and not empty (and fresh, if configured) | `max_data_age_hours` |
# MAGIC | Benchmark gate | Genie answers the benchmarks; accuracy on graded benchmarks ≥ the threshold, enough of them graded | `gate_min_accuracy`, `gate_min_graded`, `gate_max_bad` |
# MAGIC | Smoke test | one question answered with SQL: the space works end to end | `smoke_question` |
# MAGIC | Quality report | accuracy and every wrong answer, with Genie's SQL, in the run summary | — |
# MAGIC
# MAGIC **qa passes only if every step passes.** Otherwise the run stops, prod is never reached, and the failure is in the run
# MAGIC log. Fix it with a new pull request, which starts a new release.
# MAGIC
# MAGIC ## 5.4 qa → prod: the approval and what follows
# MAGIC
# MAGIC 1. The run waits at **prod**. Each required reviewer gets a GitHub notification.
# MAGIC 2. The approver opens the run → reads the **quality report** of qa in the summary (accuracy, wrong answers) and the
# MAGIC    change list (release notes of the PRs) → **Review deployments** → tick *prod* → **Approve and deploy** (or
# MAGIC    *Reject*).
# MAGIC 3. prod: drift gate and backup (400 days) → deploy → smoke test. If the smoke test fails, the previous release's space
# MAGIC    is redeployed automatically and the run fails: prod is never left broken.
# MAGIC 4. Success: the commit is tagged `genie-prod-<UTC time>-<commit>`, and a **GitHub release** is published whose notes
# MAGIC    list every change to the space since the previous release.
# MAGIC
# MAGIC What prod receives is **exactly the commit that passed qa**. Approvals are not given on code: they are given on a
# MAGIC tested result.
# MAGIC
# MAGIC ## 5.5 What can stop a release, and what to do
# MAGIC
# MAGIC | Stopped at | Message | Meaning | Do this |
# MAGIC |---|---|---|---|
# MAGIC | qa or prod · drift gate | `DRIFT: edited outside the bundle` (exit 2) | someone edited that space in the UI | **adopt** it: `python scripts/sync_from_workspace.py -t <env>`, commit, PR. Or **overwrite** it: run genie-release with *allow_drift* (the edit stays in the backup artifact) |
# MAGIC | qa or prod · drift gate | `Refusing: the plan would recreate` (exit 3) | the change can't be applied in place; users would lose conversations | avoid it; if intended, run with *allow_destroy* (prod also needs `prevent_destroy` removed in a PR) |
# MAGIC | qa · quality gate | `GATE FAILED: accuracy 55% is below 60%` | Genie answers too few benchmarks correctly | read the wrong answers in the report; improve instructions, example SQL or column descriptions, or fix an ambiguous benchmark; new PR |
# MAGIC | qa · quality gate | `health check(s) failed` | a table is missing, empty or stale | check the data pipeline; re-run the release when fixed |
# MAGIC | prod · approval | rejected | the approver said no | fix and merge again; nothing reached prod |
# MAGIC | prod · smoke test | `smoke test failed on prod; restoring` | Genie did not answer in prod | the previous release was restored; investigate in qa |
# MAGIC
# MAGIC ## 5.6 Your promotion setup and recent releases

# COMMAND ----------

if not REPO_NAME:
    print("skipped: set github_repo to see the environments, approvers and recent releases")
else:
    for env in ENVIRONMENTS:
        e = gh_get(f"/environments/{env}")
        if e is None:
            print(f"{env:>5}: environment not set up (run part B of the setup notebook)")
            continue
        reviewers = [r["reviewer"].get("login") or r["reviewer"].get("name")
                     for rule in e.get("protection_rules", []) if rule.get("type") == "required_reviewers"
                     for r in rule.get("reviewers", [])]
        policy = e.get("deployment_branch_policy") or {}
        print(f"{env:>5}: approvers {reviewers or 'none'}; "
              + ("deploys from selected branches (main)" if policy.get("custom_branch_policies") else "deploys from any branch"))
    runs = gh_get("/actions/workflows/genie-release.yml/runs?per_page=5", {}).get("workflow_runs", [])
    for r in runs:
        jobs = gh_get(f"/actions/runs/{r['id']}/jobs", {}).get("jobs", [])
        print(f"\nrelease run {r['id']} ({r['event']}, commit {r['head_sha'][:7]}): {r['conclusion'] or r['status']}")
        print("   " + "  ->  ".join(f"{j['name']}: {j['conclusion'] or j['status']}" for j in jobs))
    if not runs:
        print("\nno release has run yet")
    for env in ENVIRONMENTS:
        d = gh_get(f"/deployments?environment={env}&per_page=1", [])
        print(f"last deployment to {env:>5}: " + (f"commit {d[0]['sha'][:7]} at {d[0]['created_at']}" if d else "none"))

# COMMAND ----------

# MAGIC %md
# MAGIC # 6 · Rollback
# MAGIC
# MAGIC When a release turns out to be wrong in prod (users complain, or the nightly quality alert fires):
# MAGIC
# MAGIC 1. GitHub → *Actions* → **genie-rollback** → *Run workflow* →
# MAGIC    * **environment**: `prod`
# MAGIC    * **to**: `previous` (the release before the latest), or any tag or commit (e.g. `genie-prod-20261001.0915-1a2b3c4`)
# MAGIC    * **what**: `space` (only the space's content, with today's settings), or `bundle` (everything as it was:
# MAGIC      settings, jobs, dashboard, alerts)
# MAGIC    * **reason**: why (kept in the log)
# MAGIC 2. The rollback waits for the same **approval** as a release. Then it backs up the live space, deploys the restored
# MAGIC    version, moves `genie-deployed-prod` to it, and runs the smoke test.
# MAGIC 3. **Fix forward**: revert the bad change on `main` with a pull request (GitHub → the merged PR → *Revert*).
# MAGIC    Otherwise the next release brings it back.
# MAGIC
# MAGIC ```text
# MAGIC  genie-prod-…0915-1a2b3c4 (good) ── genie-prod-…1402-9f8e7d6 (bad, live) ── rollback to previous ──▶ prod = 1a2b3c4
# MAGIC                                                                       └── PR "Revert …" ──▶ main no longer has it
# MAGIC ```
# MAGIC
# MAGIC A rollback of **dev** or **qa** works the same, without an approval (only prod has required reviewers). Any `genie-dev-snapshot-*` tag can be restored
# MAGIC too, for example to get back a UI edit whose sync PR was closed.
# MAGIC
# MAGIC # 7 · Who may do what
# MAGIC
# MAGIC | Who | Can | Enforced by |
# MAGIC |---|---|---|
# MAGIC | Developers | push branches, open PRs, edit the dev space in the UI | GitHub write access; `CAN_MANAGE` on dev only |
# MAGIC | Reviewers / code owners | review and approve PRs | branch protection on `main`: a PR is required, `checks` must pass, no force pushes. The setup notebook requires **0** approving reviews; raise it (GitHub → *Settings* → *Branches*) and turn on *Require review from Code Owners* if your process needs four eyes on every change |
# MAGIC | Approvers | approve prod deployments and rollbacks | required reviewers on the GitHub environment `prod` |
# MAGIC | GitHub Actions | deploy dev, qa, prod | one service principal per environment; qa and prod deploy from `main` only |
# MAGIC | Business users | ask questions | `CAN_RUN` on the space; their own data permissions (row filters apply) |
# MAGIC | Nobody | delete a release tag, or delete the prod space by accident | tag ruleset `genie-prod-*`; `lifecycle.prevent_destroy` on prod |
# MAGIC
# MAGIC Sign-in from GitHub to Databricks is **GitHub OIDC** (a federation policy: no secret stored at all), or the service
# MAGIC principal's **OAuth secret**, stored encrypted as a GitHub environment secret. Either way each job can only reach its
# MAGIC own environment.

# COMMAND ----------

# MAGIC %md
# MAGIC # 8 · A complete worked example
# MAGIC
# MAGIC **Monday 09:00 · a developer improves the dev space.** Priya adds the sample question *"Top 5 products by margin last
# MAGIC month"* in the **[DEV]** Genie UI and clarifies one instruction.
# MAGIC
# MAGIC **Monday 09:17 · the sync.** `genie-dev-sync` exports dev, finds two changes, commits them to `genie/dev-sync`, tags
# MAGIC `genie-dev-snapshot-20261005T0917Z` and opens PR #21 *"Genie: sync changes made in dev"*. `genie-ci` runs on it: green;
# MAGIC the summary shows `+ added sample question: Top 5 products by margin last month` and `~ changed text instruction: …`.
# MAGIC
# MAGIC **Monday 11:00 · a second change, in a branch.** Marco adds two benchmarks for margin questions in branch
# MAGIC `feature/margin-benchmarks` and opens PR #22. The *SQL on dev data* step fails: a column is misspelt. He fixes it,
# MAGIC pushes again, and the check turns green.
# MAGIC
# MAGIC **Monday 15:00 · review and merge.** Both PRs are reviewed and merged. Merging #22 starts `genie-release` for commit
# MAGIC `9f8e7d6`. (The release's dev-snapshot finds nothing new: PR #21 is already merged.)
# MAGIC
# MAGIC **Monday 15:05 · dev and qa.** dev is deployed. In qa: no drift; backup saved; deploy; health checks pass; benchmarks
# MAGIC 18/21 graded correct = 86% ≥ 60%; smoke test answers with SQL. The run summary shows the quality report. The run now
# MAGIC **waits for approval**.
# MAGIC
# MAGIC **Tuesday 09:30 · approval.** Ana (approver) reads the qa report and the changes, clicks **Approve and deploy**. prod
# MAGIC is deployed and smoke-tested; the release is tagged `genie-prod-20261006.0931-9f8e7d6`; release notes list the
# MAGIC sample question, the instruction and the two benchmarks.
# MAGIC
# MAGIC **Wednesday 06:00 · nightly monitoring.** The quality job in prod: benchmarks 86%, all tables healthy and fresh. The
# MAGIC usage job records yesterday's 140 questions, 3 failed, 2 thumbs down: they appear on the dashboard under *Questions to
# MAGIC review*.
# MAGIC
# MAGIC **Wednesday 14:00 · someone edits prod in the UI** (they should not). Nothing happens yet.
# MAGIC
# MAGIC **Thursday 10:00 · the next release stops.** A new merge reaches prod's drift gate: `DRIFT: edited outside the bundle`,
# MAGIC with the list of differences. The team decides the edit was a mistake and re-runs `genie-release` with *allow_drift*:
# MAGIC the edit is overwritten, and kept in the backup artifact.
# MAGIC
# MAGIC | After Thursday | Version |
# MAGIC |---|---|
# MAGIC | main | the latest merge |
# MAGIC | dev | main |
# MAGIC | qa | main |
# MAGIC | prod | main, tagged `genie-prod-20261008.1012-…` |
# MAGIC | history | 2 commits for PR #21 and #22, 1 dev snapshot, 2 prod releases with notes, 3 backups |

# COMMAND ----------

# MAGIC %md
# MAGIC # 9 · Troubleshooting, FAQ, cheat sheet
# MAGIC
# MAGIC ## FAQ
# MAGIC
# MAGIC **Can I edit qa or prod in the Genie UI?** No. The next release stops at the drift gate. Change dev (UI or file) and
# MAGIC release.
# MAGIC
# MAGIC **I edited dev but no pull request appeared.** The sync runs hourly at :17. Run *genie-dev-sync* by hand to sync now.
# MAGIC If its log says "dev matches main", your edit was already captured, or it was made in a different space (check the
# MAGIC title ends with `[DEV]`).
# MAGIC
# MAGIC **I closed the sync PR by mistake.** The edit is still in its snapshot tag: restore it with *genie-rollback*
# MAGIC (environment `dev`, to = the snapshot tag), and the next sync opens a new PR.
# MAGIC
# MAGIC **Why does my change not reach prod?** Check, in order: did the PR change a path that starts a release (5.1)? Did qa
# MAGIC pass (the run log)? Is the run waiting for approval?
# MAGIC
# MAGIC **How do I see what will go to prod with the next release?** Section 3.4: *merged on main but not yet in prod*.
# MAGIC
# MAGIC **Can I promote only to qa?** Yes: the release stops at the approval; simply don't approve (or reject). qa keeps the
# MAGIC candidate.
# MAGIC
# MAGIC **Can a hotfix skip qa?** No. Make a small PR; the gate takes minutes. If prod is broken now, roll back first (6).
# MAGIC
# MAGIC **Two people edit dev at the same time?** The sync captures the latest state of the dev space; both edits are in it,
# MAGIC and in the PR's change list.
# MAGIC
# MAGIC ## Cheat sheet
# MAGIC
# MAGIC | I want to… | Do this |
# MAGIC |---|---|
# MAGIC | change the space quickly, with real answers | edit the **[DEV]** space in the Genie UI → wait for / run *genie-dev-sync* → review and merge the sync PR |
# MAGIC | change the space in a branch | branch → edit `space/genie_space.yml` → `make fix` → commit & push → PR |
# MAGIC | try something privately | `make deploy` (sandbox) → edit → `make sync T=sandbox` → PR |
# MAGIC | see what a branch changes | `make diff`, or the *genie-ci* summary on the PR |
# MAGIC | promote to qa and prod | merge the PR to `main` → watch *genie-release* → approve prod |
# MAGIC | re-run a release | *Actions* → *genie-release* → *Run workflow* |
# MAGIC | overwrite a UI edit in qa/prod | *genie-release* → *Run workflow* with **allow_drift** |
# MAGIC | keep a UI edit made in qa/prod | `python scripts/sync_from_workspace.py -t <env>` → commit → PR |
# MAGIC | undo a release | *genie-rollback* (environment `prod`, to `previous`) → approve → revert the PR on `main` |
# MAGIC | see every version | *Releases* (prod), tags `genie-dev-snapshot-*` (dev), file history (every change) |
# MAGIC | know what is live | section 3.5 of this guide, or part E of the setup notebook |
# MAGIC
# MAGIC **Further reading in the project:** `README.md` (the template), `docs/OPERATIONS.md` (roles, approver checklist, runbook,
# MAGIC go-live checklist), `notebooks/Genie_Project_Template` (setup and a live walk-through).
