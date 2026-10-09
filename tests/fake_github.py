"""A minimal, in-memory stand-in for the GitHub REST API, for running the CI/CD notebook offline.

It implements the endpoints the notebook uses (repository, branches and refs, file contents, pull requests and
their checks, workflow runs, deployment approvals, environments with variables and encrypted secrets, branch
protection, tag rulesets, tags, releases, commits, deployments) and simulates the repository's workflows:

  * opening a pull request adds the checks run-demo and checks, both successful;
  * merging to main, or dispatching genie-release, runs the release: dev-snapshot, dev and qa at once, then prod
    waits for an approval (the environment prod has reviewers); approving deploys prod and tags genie-prod-*;
  * genie-dev-sync exports the dev space and opens a pull request when it differs from main;
  * genie-rollback deploys an earlier tag to one environment (prod waits for an approval).

"Deploying" calls the hook on_deploy(env, space_yaml_text, allow_drift) -> bool, through which a test updates its
stand-in Databricks workspace (and reports drift). on_dev_sync() -> new space file text or None does the export.
It is a test double, not an emulator of GitHub.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SPACE_PATH = "genie_bundle/resources/freshcart_assistant.space.yml"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()


class FakeGitHub:
    def __init__(self, owner: str, repo: str, files: dict[str, str], token: str = "test-token", branch: str = "work",
                 space_path: str = SPACE_PATH, check_names: tuple = ("run-demo", "checks")):
        from nacl import public
        self.owner, self.repo, self.token = owner, repo, token
        self.space_path, self.check_names = space_path, check_names
        self.releases: list[dict] = []
        self.rulesets: list[dict] = []
        self.login, self.user_id = owner, 4242
        first = _sha("initial", time.time())
        self.commits = {first: {"files": dict(files), "message": "initial", "parent": None, "date": _now()}}
        self.branches = {branch: first}
        self.default_branch = branch
        self.tags: dict[str, str] = {}
        self.pulls: dict[int, dict] = {}
        self.checks: dict[str, list] = {}
        self.runs: dict[int, dict] = {}
        self.environments: dict[str, dict] = {}
        self.env_keys = {}
        self.variables: dict[str, dict] = {}
        self.secrets: dict[str, dict] = {}
        self.deployments: list[dict] = []
        self.settings: dict[str, dict] = {}
        self.on_deploy = lambda env, text, allow_drift: True
        self.on_dev_sync = lambda: None
        # merge_texts(base, main, live) -> text: how the sync applies UI edits onto main (sync_from_workspace.py does
        # it item by item). None: the live export replaces the file (the behaviour of genie_bundle's sync).
        self.merge_texts = None
        self._public = public
        self.lock = threading.Lock()
        handler = type("H", (_Handler,), {"gh": self})
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self._server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.url = f"http://127.0.0.1:{port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()

    # ------------------------------------------------------------------ git objects
    def resolve(self, ref: str) -> str | None:
        return self.branches.get(ref) or self.tags.get(ref) or (ref if ref in self.commits else None)

    def commit(self, branch: str, files: dict, message: str) -> str:
        parent = self.branches[branch]
        sha = _sha(parent, message, time.time(), len(self.commits))
        self.commits[sha] = {"files": {**self.commits[parent]["files"], **files}, "message": message,
                             "parent": parent, "date": _now()}
        self.branches[branch] = sha
        return sha

    def file_at(self, ref: str, path: str | None = None) -> str:
        return self.commits[self.resolve(ref)]["files"][path or self.space_path]

    # ------------------------------------------------------------------ workflows
    def _new_run(self, workflow: str, name: str, sha: str, event: str, jobs: list[str], inputs: dict) -> dict:
        rid = 7000 + len(self.runs) + 1
        run = {"id": rid, "name": name, "path": f".github/workflows/{workflow}", "workflow": workflow, "event": event,
               "status": "in_progress", "conclusion": None, "head_sha": sha, "head_branch": "main",
               "created_at": _now(), "html_url": f"https://github.example/{self.owner}/{self.repo}/actions/runs/{rid}",
               "jobs": [{"name": j, "status": "queued", "conclusion": None} for j in jobs], "inputs": inputs,
               "waiting_env": None}
        self.runs[rid] = run
        return run

    def _job(self, run, name, ok: bool):
        job = next(j for j in run["jobs"] if j["name"] == name)
        job.update(status="completed", conclusion="success" if ok else "failure")
        return ok

    def _finish(self, run, ok: bool):
        for j in run["jobs"]:
            if j["status"] != "completed":
                j.update(status="completed", conclusion="skipped")
        run.update(status="completed", conclusion="success" if ok else "failure", waiting_env=None)

    def _deployed(self, env: str, sha: str) -> None:
        self.tags[f"genie-deployed-{env}"] = sha                    # what the release workflow records

    def start_release(self, sha: str, event: str, inputs: dict | None = None):
        inputs = inputs or {}
        run = self._new_run("genie-release.yml", "genie-release", sha, event, ["dev-snapshot", "dev", "qa", "prod"], inputs)
        allow = str(inputs.get("allow_drift", "false")).lower() == "true"
        text = self.file_at(sha)
        self._job(run, "dev-snapshot", True)
        if not self._job(run, "dev", self.on_deploy("dev", text, True)):
            return self._finish(run, False)
        self._deployed("dev", sha)
        if not self._job(run, "qa", self.on_deploy("qa", text, allow)):
            return self._finish(run, False)
        self._deployed("qa", sha)
        self._wait(run, "prod")

    def start_rollback(self, inputs: dict):
        env, to = inputs["environment"], inputs.get("to", "previous")
        releases = sorted((t for t in self.tags if t.startswith("genie-prod-")), reverse=True)
        ref = releases[1] if to == "previous" and len(releases) > 1 else to
        run = self._new_run("genie-rollback.yml", "genie-rollback", self.resolve(ref) or "", "workflow_dispatch",
                            ["rollback"], {**inputs, "ref": ref})
        if env == "prod":
            self._wait(run, "prod")
        else:
            ok = self._job(run, "rollback", self.on_deploy(env, self.file_at(ref), True))
            if ok:
                self._deployed(env, self.resolve(ref))
            self._finish(run, ok)

    def dev_sync(self):
        run = self._new_run("genie-dev-sync.yml", "genie-dev-sync", self.branches["main"], "workflow_dispatch",
                            ["sync"], {})
        text = self.on_dev_sync()
        base_sha = self.tags.get("genie-deployed-dev")
        if text is not None and self.merge_texts and base_sha:
            base = self.file_at(base_sha)
            text = None if text == base else self.merge_texts(base, self.file_at("main"), text)   # only UI edits
        if text is not None and text != self.file_at("main"):
            self.branches["genie/dev-sync"] = self.branches["main"]
            sha = self.commit("genie/dev-sync", {self.space_path: text}, "Genie: changes made in the dev space")
            self.tags[f"genie-dev-snapshot-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"] = sha
            if not any(p["head"]["ref"] == "genie/dev-sync" and p["state"] == "open" for p in self.pulls.values()):
                self._open_pr("Genie: sync changes made in dev", "genie/dev-sync", "main")
        self._finish(run, self._job(run, "sync", True))

    def _wait(self, run, env):
        run.update(status="waiting", waiting_env=env)
        next(j for j in run["jobs"] if j["name"] in ("prod", "rollback"))["status"] = "waiting"

    def approve(self, run):
        env = run["waiting_env"]
        run.update(status="in_progress", waiting_env=None)
        if run["workflow"] == "genie-release.yml":
            allow = str(run["inputs"].get("allow_drift", "false")).lower() == "true"
            ok = self._job(run, "prod", self.on_deploy("prod", self.file_at(run["head_sha"]), allow))
            if ok:
                self._deployed("prod", run["head_sha"])
                n = sum(1 for t in self.tags if t.startswith("genie-prod-"))
                tag = f"genie-prod-{datetime.now(timezone.utc):%Y%m%d.%H%M%S}.{n:02d}-{run['head_sha'][:7]}"
                self.tags[tag] = run["head_sha"]
                self.releases.insert(0, {"tag_name": tag, "published_at": _now(), "body": f"release of {run['head_sha'][:7]}"})
                for e in ("dev", "qa", "prod"):
                    self.deployments.append({"environment": e, "sha": run["head_sha"], "created_at": _now(),
                                             "creator": {"login": "github-actions[bot]"}})
        else:
            ok = self._job(run, "rollback", self.on_deploy(env, self.file_at(run["inputs"]["ref"]), True))
            if ok:
                self._deployed(env, self.resolve(run["inputs"]["ref"]))
        self._finish(run, ok)

    def _open_pr(self, title, head, base) -> dict:
        n = len(self.pulls) + 1
        sha = self.branches[head]
        pr = {"number": n, "title": title, "state": "open", "merged": False, "base": {"ref": base},
              "head": {"ref": head, "sha": sha}, "html_url": f"https://github.example/{self.owner}/{self.repo}/pull/{n}"}
        self.pulls[n] = pr
        self.checks[sha] = [{"name": c, "status": "completed", "conclusion": "success"} for c in self.check_names]
        return pr


def _view_run(run: dict) -> dict:
    return {k: v for k, v in run.items() if k not in ("jobs", "inputs", "waiting_env", "workflow")}


class _Handler(BaseHTTPRequestHandler):
    gh: FakeGitHub = None

    def log_message(self, *a):
        pass

    def _send(self, code, body=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        try:
            self._route(method)
        except Exception as e:                                       # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._send(500, {"message": f"fake GitHub error: {e}"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")

    def _route(self, method):
        g = self.gh
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else {}
        if self.headers.get("Authorization") != f"Bearer {g.token}":
            return self._send(401, {"message": "Bad credentials"})
        p = u.path
        with g.lock:
            if p == "/user":
                return self._send(200, {"login": g.login, "id": g.user_id})
            if p.startswith("/users/"):
                return self._send(200, {"login": p.split("/")[2], "id": g.user_id})
            prefix = f"/repos/{g.owner}/{g.repo}"
            if not p.startswith(prefix):
                return self._send(404, {"message": "Not Found"})
            p = p[len(prefix):] or "/"
            parts = p.strip("/").split("/") if p != "/" else []

            if not parts:
                if method == "PATCH":
                    g.default_branch = body.get("default_branch", g.default_branch)
                return self._send(200, {"full_name": f"{g.owner}/{g.repo}", "default_branch": g.default_branch,
                                        "visibility": "public", "permissions": {"admin": True}})
            # ---- refs and branches
            if p.startswith("/git/ref/heads/"):
                sha = g.branches.get(p[len("/git/ref/heads/"):])
                return self._send(200, {"object": {"sha": sha}}) if sha else self._send(404, {"message": "Not Found"})
            if p == "/git/refs" and method == "POST":
                g.branches[body["ref"].removeprefix("refs/heads/")] = body["sha"]
                return self._send(201, {"ref": body["ref"], "object": {"sha": body["sha"]}})
            if parts[0] == "branches" and len(parts) == 2:
                sha = g.branches.get(parts[1])
                return self._send(200, {"name": parts[1], "commit": {"sha": sha}}) if sha else \
                    self._send(404, {"message": "Branch not found"})
            if parts[0] == "branches" and parts[-1] == "protection":
                g.settings["protection"] = body
                return self._send(200, body)
            if p == "/actions/permissions/workflow":
                g.settings["workflow_permissions"] = body
                return self._send(204)
            # ---- contents
            if parts[0] == "contents":
                path = "/".join(parts[1:])
                if method == "GET":
                    sha = g.resolve(q.get("ref", g.default_branch))
                    text = g.commits[sha]["files"].get(path) if sha else None
                    if text is None:
                        return self._send(404, {"message": "Not Found"})
                    return self._send(200, {"path": path, "sha": _sha(text), "encoding": "base64",
                                            "content": base64.b64encode(text.encode()).decode()})
                branch = body["branch"]
                current = g.commits[g.branches[branch]]["files"].get(path)
                if current is not None and body.get("sha") != _sha(current):
                    return self._send(409, {"message": "sha does not match"})
                new = g.commit(branch, {path: base64.b64decode(body["content"]).decode()}, body["message"])
                return self._send(200, {"commit": {"sha": new}})
            # ---- pull requests
            if parts[0] == "pulls":
                if len(parts) == 1 and method == "POST":
                    return self._send(201, g._open_pr(body["title"], body["head"], body["base"]))
                if len(parts) == 1:
                    head = q.get("head", "").split(":")[-1]
                    return self._send(200, [pr for pr in g.pulls.values()
                                            if (not head or pr["head"]["ref"] == head)
                                            and (q.get("state", "open") in ("all", pr["state"]))])
                pr = g.pulls[int(parts[1])]
                if len(parts) == 2:
                    return self._send(200, pr)
                if parts[2] == "files":
                    old = g.file_at(pr["base"]["ref"]).splitlines()
                    new = g.file_at(pr["head"]["sha"]).splitlines()
                    patch = "\n".join(difflib.unified_diff(old, new, lineterm="", n=1))
                    return self._send(200, [{"filename": g.space_path, "patch": patch}] if patch else [])
                if parts[2] == "merge":
                    head_files = g.commits[pr["head"]["sha"]]["files"]
                    sha = g.commit(pr["base"]["ref"], head_files, f"{pr['title']} (#{pr['number']})")
                    pr.update(state="closed", merged=True)
                    if pr["base"]["ref"] == "main":
                        g.start_release(sha, "push")
                    return self._send(200, {"sha": sha, "merged": True})
            if parts[0] == "commits" and len(parts) == 3 and parts[2] == "check-runs":
                runs = g.checks.get(parts[1], [])
                return self._send(200, {"total_count": len(runs), "check_runs": runs})
            if parts[0] == "commits" and len(parts) == 1:
                sha, out = g.resolve(q.get("sha", g.default_branch)), []
                while sha:
                    c = g.commits[sha]
                    parent = c["parent"]
                    if parent is None or c["files"].get(q.get("path")) != g.commits[parent]["files"].get(q.get("path")):
                        out.append({"sha": sha, "commit": {"message": c["message"], "author": {"name": "dana"},
                                                           "committer": {"date": c["date"]}}})
                    sha = parent
                return self._send(200, out[:int(q.get("per_page", 30))])
            if parts[0] == "releases":
                return self._send(200, g.releases[:int(q.get("per_page", 30))])
            if parts[0] == "rulesets":
                if method == "POST":
                    g.rulesets.append(body)
                    return self._send(201, body)
                return self._send(200, g.rulesets)
            if parts[0] == "tags":
                return self._send(200, [{"name": t, "commit": {"sha": s}} for t, s in sorted(g.tags.items(), reverse=True)])
            if parts[0] == "deployments":
                return self._send(200, [d for d in reversed(g.deployments) if d["environment"] == q.get("environment")])
            # ---- actions
            if parts[:2] == ["actions", "workflows"] and parts[-1] == "dispatches":
                wf, inputs = parts[2], body.get("inputs") or {}
                if wf == "genie-release.yml":
                    g.start_release(g.branches["main"], "workflow_dispatch", inputs)
                elif wf == "genie-rollback.yml":
                    g.start_rollback(inputs)
                elif wf == "genie-dev-sync.yml":
                    g.dev_sync()
                return self._send(204)
            if parts[:2] == ["actions", "workflows"] and parts[-1] == "runs":
                wf = parts[2]
                runs = [_view_run(r) for r in sorted(g.runs.values(), key=lambda r: -r["id"])
                        if r["workflow"] == wf and (not q.get("head_sha") or r["head_sha"] == q["head_sha"])]
                return self._send(200, {"total_count": len(runs), "workflow_runs": runs[:int(q.get("per_page", 30))]})
            if parts[:2] == ["actions", "runs"]:
                run = g.runs[int(parts[2])]
                if len(parts) == 3:
                    return self._send(200, _view_run(run))
                if parts[3] == "jobs":
                    return self._send(200, {"jobs": run["jobs"]})
                if parts[3] == "pending_deployments":
                    if method == "GET":
                        env = run["waiting_env"]
                        return self._send(200, [{"environment": {"id": 900 + ["dev", "qa", "prod"].index(env), "name": env},
                                                 "current_user_can_approve": True}] if env else [])
                    if run["waiting_env"] and body.get("state") == "approved":
                        g.approve(run)
                    return self._send(200, [])
            # ---- environments, variables, secrets
            if parts[0] == "environments":
                env = parts[1]
                if len(parts) == 2 and method == "GET":         # GitHub's shape: protection rules
                    if env not in g.environments:
                        return self._send(404, {"message": "Not Found"})
                    e = g.environments[env]
                    rules = [{"type": "required_reviewers", "reviewers": [
                        {"type": r["type"], "reviewer": {"login": g.login, "id": r["id"]}} for r in e["reviewers"]]}] \
                        if e.get("reviewers") else []
                    return self._send(200, {"name": env, "protection_rules": rules,
                                            "deployment_branch_policy": e.get("deployment_branch_policy")})
                if len(parts) == 2:
                    g.environments[env] = body
                    g.env_keys.setdefault(env, g._public.PrivateKey.generate())
                    return self._send(200, {"name": env, **body})
                if parts[2] == "deployment-branch-policies":
                    pol = g.environments.setdefault(env, {}).setdefault("_branch_policies", [])
                    if method == "POST":
                        pol.append(body)
                        return self._send(200, body)
                    return self._send(200, {"total_count": len(pol), "branch_policies": pol})
                if parts[2] == "variables":
                    vs = g.variables.setdefault(env, {})
                    if len(parts) == 4 and method == "GET":
                        return self._send(200, {"name": parts[3], "value": vs[parts[3]]}) if parts[3] in vs else \
                            self._send(404, {"message": "Not Found"})
                    vs[body["name"]] = body["value"]
                    return self._send(201 if method == "POST" else 204)
                if parts[2] == "secrets" and parts[3] == "public-key":
                    from nacl import encoding
                    key = g.env_keys.setdefault(env, g._public.PrivateKey.generate())
                    return self._send(200, {"key_id": f"key-{env}",
                                            "key": key.public_key.encode(encoding.Base64Encoder()).decode()})
                if parts[2] == "secrets" and method == "PUT":
                    box = g._public.SealedBox(g.env_keys[env])
                    g.secrets.setdefault(env, {})[parts[3]] = box.decrypt(base64.b64decode(body["encrypted_value"])).decode()
                    return self._send(201)
            return self._send(404, {"message": f"fake GitHub: {method} {p} not implemented"})
