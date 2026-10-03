"""A minimal, in-memory stand-in for a Databricks workspace, for running the real Databricks CLI
(`databricks bundle validate / plan / deploy / generate / summary` and `databricks genie ...`) offline.

It implements just the REST endpoints those commands use for the two bundles in this repo: current
user, workspace files (bundle files, deployment lock and state), Genie spaces (create, get, update with
etag, trash), jobs, pipelines and permissions, plus what the end-to-end notebook uses: groups and service
principals (SCIM), the current metastore, job runs and Genie query results. A job run executes the
quality-gate script for real (against this workspace); every other task is reported as succeeded without
running. It is a test double, not an emulator of Databricks.

    python tests/fake_workspace.py 8765            # then: DATABRICKS_HOST=http://127.0.0.1:8765 DATABRICKS_TOKEN=x

Helpers for tests:
    FakeWorkspace.start() -> FakeWorkspace (runs in a background thread; .host, .stop(), .spaces, .requests)
    POST /__test__/ui-edit/<space_id>   body = full serialized_space string: simulates an edit in the Genie UI
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

USER = {"id": "1001", "userName": "dana@freshcart.example", "displayName": "Dana Developer",
        "groups": [{"display": "users"}, {"display": "admins"}, {"display": "freshcart-genie-deployers"},
                   {"display": "freshcart-genie-developers"}]}


RUNNABLE = {"genie_quality_gate.py", "genie_quality.py", "genie_usage.py"}   # job tasks the stand-in executes


class _State:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/", "/Workspace", "/Workspace/Users", "/Workspace/Shared", "/Users", "/Shared"}
        self.spaces: dict[str, dict] = {}
        self.permissions: dict[str, list] = {}
        self.jobs: dict[int, dict] = {}
        self.pipelines: dict[str, dict] = {}
        self.runs: dict[int, dict] = {}
        self.scim: dict[str, dict[str, dict]] = {"Groups": {}, "ServicePrincipals": {}}
        self.account_groups: dict[str, dict] = {}       # identity API: groups of the account
        self.identity_api = True                        # False: the workspace has no identity API (404)
        self.secret_scopes: dict[str, dict[str, str]] = {}
        self.sp_secrets: dict[str, list[str]] = {}
        self.host = ""
        self.eval_runs: dict[str, dict] = {}
        self.dashboards: dict[str, dict] = {}            # AI/BI (Lakeview) dashboards
        self.alerts: dict[str, dict] = {}                # SQL alerts (v2)
        self.messages: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.evaluator = None            # callable(space: dict, question: str) -> SQL answer or None


def _norm(path: str) -> str:
    path = "/" + unquote(path).strip("/")
    return path if path.startswith("/Workspace") or path in ("/",) else "/Workspace" + path


class _Handler(BaseHTTPRequestHandler):
    state: _State = None

    def log_message(self, *args):
        pass

    # -------------------------------------------------------------------------------- helpers
    def _send(self, code: int, body=None, raw: bytes | None = None):
        data = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Databricks-Org-Id", "1234567890123456")
        self.end_headers()
        self.wfile.write(data)

    def _missing(self, what="not found"):
        self._send(404, {"error_code": "RESOURCE_DOES_NOT_EXIST", "message": what})

    def _raw(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self, raw: bytes) -> dict:
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _mkdirs(self, path: str):
        parts = path.strip("/").split("/")
        for i in range(1, len(parts) + 1):
            self.state.dirs.add("/" + "/".join(parts[:i]))

    def _space_view(self, sp: dict, with_serialized: bool) -> dict:
        out = {k: v for k, v in sp.items() if k != "serialized_space" or with_serialized}
        return out

    # ------------------------------------------------------------------------------- dispatch
    def _handle(self, method: str):
        st = self.state
        u = urlparse(self.path)
        p, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
        raw = self._raw() if method in ("POST", "PATCH", "PUT") else b""
        body = self._json(raw)
        with st.lock:
            st.requests.append({"method": method, "path": p, "query": q, "body": body})

            if p.endswith("/scim/v2/Me"):
                return self._send(200, USER)
            if p.startswith("/telemetry") or p.startswith("/.well-known") or "oidc" in p:
                return self._send(200, {})

            # ---- test hook: an edit made in the Genie UI
            if p.startswith("/__test__/ui-edit/"):
                sid = p.rsplit("/", 1)[-1]
                sp = st.spaces[sid]
                sp["serialized_space"] = raw.decode()
                sp["etag"] = str(int(sp["etag"]) + 1)
                return self._send(200, {"etag": sp["etag"]})

            # ---- Genie spaces
            if p.startswith("/api/2.0/genie/spaces") and method in ("POST", "PATCH") and _v1_fields(body):
                return self._send(400, {"error_code": "INVALID_PARAMETER_VALUE", "message": _v1_fields(body)})
            if p == "/api/2.0/genie/spaces" and method == "POST":
                sid = uuid.uuid4().hex
                sp = {"space_id": sid, "title": body.get("title", ""), "description": body.get("description", ""),
                      "warehouse_id": body.get("warehouse_id"), "parent_path": body.get("parent_path"),
                      "serialized_space": _upgrade(body.get("serialized_space", "{}")), "etag": "1"}
                st.spaces[sid] = sp
                return self._send(200, self._space_view(sp, True))
            if p == "/api/2.0/genie/spaces" and method == "GET":
                return self._send(200, {"spaces": [self._space_view(s, False) for s in st.spaces.values()]})
            if "/eval-runs" in p or "/start-conversation" in p or "/conversations/" in p or p.endswith("/conversations"):
                return self._genie_runtime(method, p, q, body)
            if p.startswith("/api/2.2/jobs/") or p.startswith("/api/2.1/jobs/"):
                return self._jobs(method, p, q, body)
            if p == "/api/2.0/pipelines" or p.startswith("/api/2.0/pipelines/"):
                return self._pipelines(method, p, body)
            if p.startswith("/api/2.0/preview/scim/v2/Groups") or p.startswith("/api/2.0/preview/scim/v2/ServicePrincipals"):
                return self._scim(method, p, q, body)
            if p.startswith("/api/2.0/identity/"):
                return self._identity(method, p, body)
            if p.startswith("/api/2.0/secrets/"):
                return self._secrets(p, body)
            if p.startswith("/api/2.0/accounts/servicePrincipals/") and p.endswith("/credentials/secrets"):
                sp_id = p.split("/")[5]
                secret = "dose" + uuid.uuid4().hex
                st.sp_secrets.setdefault(sp_id, []).append(secret)
                return self._send(200, {"id": uuid.uuid4().hex, "secret": secret, "status": "ACTIVE"})
            if p == "/api/2.1/unity-catalog/current-metastore-assignment":
                return self._send(200, {"metastore_id": "11111111-2222-3333-4444-555555555555",
                                        "workspace_id": 1234567890123456, "default_catalog_name": "main"})
            if p.startswith("/api/2.0/genie/spaces/"):
                sid = p.split("/")[5]
                sp = st.spaces.get(sid)
                if sp is None:
                    return self._missing(f"Genie space {sid} does not exist")
                if method == "GET":
                    return self._send(200, self._space_view(sp, q.get("include_serialized_space") == "true"))
                if method == "PATCH":
                    if body.get("etag") and body["etag"] != sp["etag"]:
                        return self._send(409, {"error_code": "ABORTED", "message": "etag mismatch: the space was modified"})
                    for k in ("title", "description", "warehouse_id", "parent_path"):
                        if k in body:
                            sp[k] = body[k]
                    if "serialized_space" in body:
                        sp["serialized_space"] = _upgrade(body["serialized_space"])
                    sp["etag"] = str(int(sp["etag"]) + 1)
                    return self._send(200, self._space_view(sp, True))
                if method == "DELETE":
                    st.spaces.pop(sid)
                    return self._send(200, {})

            # ---- AI/BI dashboards and SQL alerts (stored; enough for bundle deploy, plan and destroy)
            if p.startswith("/api/2.0/lakeview/dashboards"):
                return self._dashboards(method, p, body, q)
            if p.startswith("/api/2.0/alerts"):
                return self._alerts(method, p, body)

            # ---- permissions (accepted and stored)
            if p.startswith("/api/2.0/permissions/"):
                key = p[len("/api/2.0/permissions/"):]
                if method in ("PUT", "PATCH"):
                    st.permissions[key] = body.get("access_control_list", [])
                acl = [{**{k: v for k, v in e.items() if k != "permission_level"},
                        "all_permissions": [{"permission_level": e.get("permission_level"), "inherited": False}]}
                       for e in st.permissions.get(key, [])]
                return self._send(200, {"object_id": key, "access_control_list": acl})

            # ---- workspace files (bundle sync, deployment lock, deployment state)
            if p.startswith("/api/2.0/workspace-files/import-file/"):
                path = _norm(p[len("/api/2.0/workspace-files/import-file/"):])
                if path in st.files and q.get("overwrite") != "true":
                    return self._send(409, {"error_code": "RESOURCE_ALREADY_EXISTS", "message": "exists"})
                self._mkdirs(path.rsplit("/", 1)[0])
                st.files[path] = raw
                return self._send(200, {})
            if p == "/api/2.0/workspace/get-status":
                path = _norm(q.get("path", "/"))
                if path in st.files:
                    return self._send(200, {"object_type": "FILE", "path": path, "object_id": abs(hash(path)) % 10**9,
                                            "size": len(st.files[path])})
                if path in st.dirs:
                    return self._send(200, {"object_type": "DIRECTORY", "path": path, "object_id": abs(hash(path)) % 10**9})
                return self._missing(f"Path ({path}) doesn't exist.")
            if p == "/api/2.0/workspace/mkdirs":
                self._mkdirs(_norm(body.get("path", "/")))
                return self._send(200, {})
            if p == "/api/2.0/workspace/export":
                path = _norm(q.get("path", "/"))
                if path not in st.files:
                    return self._missing(f"Path ({path}) doesn't exist.")
                return self._send(200, raw=st.files[path])
            if p == "/api/2.0/workspace/delete":
                path = _norm(body.get("path", "/"))
                children = [x for x in list(st.files) + list(st.dirs) if x.startswith(path + "/")]
                if children and not body.get("recursive"):
                    return self._send(400, {"error_code": "DIRECTORY_NOT_EMPTY",
                                            "message": f"Folder ({path}) is not empty"})
                for f in [f for f in st.files if f == path or f.startswith(path + "/")]:
                    st.files.pop(f)
                st.dirs = {d for d in st.dirs if not (d == path or d.startswith(path + "/"))} | {"/", "/Workspace"}
                return self._send(200, {})
            if p == "/api/2.0/workspace/list":
                path = _norm(q.get("path", "/"))
                if path not in st.dirs:
                    return self._missing(f"Path ({path}) doesn't exist.")
                kids = {f for f in st.files if f.rsplit("/", 1)[0] == path}
                subdirs = {d for d in st.dirs if d.rsplit("/", 1)[0] == path and d != path}
                return self._send(200, {"objects": [{"path": f, "object_type": "FILE"} for f in sorted(kids)]
                                        + [{"path": d, "object_type": "DIRECTORY"} for d in sorted(subdirs)]})
            if p.startswith("/api/2.0/sql/warehouses"):
                return self._send(200, {"warehouses": [{"id": "wh-0001", "name": "Serverless Starter Warehouse",
                                                         "enable_serverless_compute": True, "state": "RUNNING"}]})
            return self._send(200, {})

    # ------------------------------------------------------------- dashboards and alerts
    def _dashboards(self, method, p, body, q=None):
        st = self.state
        parts = p[len("/api/2.0/lakeview/dashboards"):].strip("/").split("/")
        did = parts[0] if parts[0] else None
        if did is None and method == "POST":
            did = uuid.uuid4().hex[:32]
            d = {**body, **{k: v for k, v in (q or {}).items() if k.startswith("dataset_")},
                 "dashboard_id": did, "etag": "1", "lifecycle_state": "ACTIVE",
                 "path": f"{body.get('parent_path', '/Workspace')}/{body.get('display_name', did)}.lvdash.json",
                 "create_time": "2026-01-01T00:00:00Z", "update_time": "2026-01-01T00:00:00Z"}
            st.dashboards[did] = d
            return self._send(200, d)
        if did is None:
            return self._send(200, {"dashboards": list(st.dashboards.values())})
        d = st.dashboards.get(did)
        if d is None:
            return self._missing(f"dashboard {did} does not exist")
        if len(parts) > 1 and parts[1] == "published":
            if method == "DELETE":
                d.pop("_published", None)
                return self._send(200, {})
            if method == "POST":
                d["_published"] = {"display_name": d.get("display_name"), "warehouse_id": body.get("warehouse_id"),
                                   "embed_credentials": body.get("embed_credentials", False),
                                   "revision_create_time": "2026-01-01T00:00:00Z"}
            return self._send(200, d.get("_published", {})) if d.get("_published") else self._missing("not published")
        if method == "DELETE":
            d["lifecycle_state"] = "TRASHED"
            st.dashboards.pop(did)
            return self._send(200, {})
        if method == "PATCH":
            d.update({k: v for k, v in body.items() if k != "dashboard_id"}, etag=str(int(d["etag"]) + 1))
            d.update({k: v for k, v in (q or {}).items() if k.startswith("dataset_")})
        return self._send(200, {k: v for k, v in d.items() if not k.startswith("_")})

    def _alerts(self, method, p, body):
        st = self.state
        aid = p[len("/api/2.0/alerts"):].strip("/") or None
        if aid is None and method == "POST":
            aid = uuid.uuid4().hex[:32]
            st.alerts[aid] = {**body, "id": aid, "lifecycle_state": "ACTIVE", "owner_user_name": USER["userName"],
                              "create_time": "2026-01-01T00:00:00Z", "update_time": "2026-01-01T00:00:00Z"}
            return self._send(200, st.alerts[aid])
        if aid is None:
            return self._send(200, {"alerts": list(st.alerts.values())})
        a = st.alerts.get(aid)
        if a is None:
            return self._missing(f"alert {aid} does not exist")
        if method == "DELETE":
            st.alerts.pop(aid)
            return self._send(200, {})
        if method == "PATCH":
            a.update({k: v for k, v in body.items() if k != "id"})
        return self._send(200, a)

    # ------------------------------------------------------------------- jobs (deploy only)
    def _jobs(self, method, p, q, body):
        st = self.state
        op = p.rsplit("/", 1)[-1]
        if op == "run-now":
            return self._run_now(int(body["job_id"]), body.get("job_parameters") or {})
        if p.endswith("/runs/get"):
            run = st.runs.get(int(q.get("run_id", 0)))
            return self._send(200, run["view"]) if run else self._missing("run not found")
        if p.endswith("/runs/get-output"):
            rid = int(q.get("run_id", 0))
            run = next((r for r in st.runs.values() if rid == r["view"]["run_id"]
                        or rid in [t["run_id"] for t in r["view"]["tasks"]]), None)
            if run is None:
                return self._missing("run not found")
            return self._send(200, {"logs": run["logs"], "metadata": run["view"]})
        if p.endswith("/runs/list"):
            job_id = int(q.get("job_id", 0))
            runs = sorted((r["view"] for r in st.runs.values() if r["view"]["job_id"] == job_id),
                          key=lambda v: -v["run_id"])[:int(q.get("limit", 25))]
            return self._send(200, {"runs": runs, "has_more": False})
        if op == "create":
            job_id = 1000 + len(st.jobs) + 1
            st.jobs[job_id] = body
            return self._send(200, {"job_id": job_id})
        if op == "get":
            job_id = int(q.get("job_id", 0))
            if job_id not in st.jobs:
                return self._missing(f"Job {job_id} does not exist.")
            return self._send(200, {"job_id": job_id, "settings": st.jobs[job_id],
                                    "creator_user_name": USER["userName"], "run_as_user_name": USER["userName"]})
        if op == "reset":
            st.jobs[int(body["job_id"])] = body.get("new_settings", {})
            return self._send(200, {})
        if op == "update":
            st.jobs[int(body["job_id"])].update(body.get("new_settings", {}))
            return self._send(200, {})
        if op == "delete":
            st.jobs.pop(int(body["job_id"]), None)
            return self._send(200, {})
        if op == "list":
            return self._send(200, {"jobs": [{"job_id": k, "settings": v} for k, v in st.jobs.items()]})
        return self._send(200, {})

    def _run_now(self, job_id: int, overrides: dict):
        """Start a job run in the background (like Databricks): the python tasks in RUNNABLE are executed against
        this workspace (without Spark); every other task just succeeds."""
        st = self.state
        settings = st.jobs[job_id]
        params = {p["name"]: str(p.get("default", "")) for p in settings.get("parameters", [])}
        params.update({k: str(v) for k, v in overrides.items()})
        run_id = 5000 + len(st.runs) * 10 + 1
        tasks = [{"run_id": run_id + i, "task_key": t.get("task_key"), "state": {"life_cycle_state": "RUNNING"}}
                 for i, t in enumerate(settings.get("tasks", []), start=1)]
        view = {"run_id": run_id, "job_id": job_id, "number_in_job": len(st.runs) + 1,
                "run_page_url": f"{st.host}/jobs/{job_id}/runs/{run_id}", "tasks": tasks,
                "state": {"life_cycle_state": "RUNNING", "state_message": ""}, "status": {"state": "RUNNING"}}
        st.runs[run_id] = {"view": view, "logs": ""}
        threading.Thread(target=self._execute, args=(run_id, settings, params), daemon=True).start()
        return self._send(200, {"run_id": run_id, "number_in_job": view["number_in_job"]})

    def _execute(self, run_id: int, settings: dict, params: dict):
        import os
        import subprocess
        import tempfile
        st = self.state
        logs, ok = [], True
        for task, view in zip(settings.get("tasks", []), st.runs[run_id]["view"]["tasks"]):
            py = task.get("spark_python_task") or {}
            result = "SUCCESS"
            if py.get("python_file", "").rsplit("/", 1)[-1] in RUNNABLE:
                args = list(py.get("parameters", []))
                for k, v in params.items():
                    args = [a.replace("{{job.parameters.%s}}" % k, v) for a in args]
                with st.lock:
                    code = st.files.get(_norm(py["python_file"]), b"")
                with tempfile.NamedTemporaryFile("wb", suffix=".py", delete=False) as fh:
                    fh.write(code)
                env = {**os.environ, "DATABRICKS_HOST": st.host, "DATABRICKS_TOKEN": "fake"}
                env.pop("DATABRICKS_CONFIG_PROFILE", None)
                p = subprocess.run([sys.executable, fh.name, *args], capture_output=True, text=True, env=env)
                os.unlink(fh.name)
                logs.append(p.stdout + p.stderr)
                if p.returncode != 0:
                    result, ok = "FAILED", False
            view["state"] = {"life_cycle_state": "TERMINATED", "result_state": result}
        with st.lock:
            run = st.runs[run_id]
            run["logs"] = "\n".join(logs)
            run["view"]["state"] = {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS" if ok else "FAILED",
                                    "state_message": "" if ok else "a task failed"}
            run["view"]["status"] = {"state": "TERMINATED", "termination_details": {
                "code": "SUCCESS" if ok else "RUN_EXECUTION_ERROR", "type": "SUCCESS" if ok else "CLIENT_ERROR"}}

    # ------------------------------------------------------------------------------- secrets
    def _secrets(self, p, body):
        st = self.state
        if p.endswith("/scopes/list"):
            return self._send(200, {"scopes": [{"name": n} for n in st.secret_scopes]})
        if p.endswith("/scopes/create"):
            st.secret_scopes.setdefault(body["scope"], {})
            return self._send(200, {})
        if p.endswith("/secrets/put"):
            st.secret_scopes.setdefault(body["scope"], {})[body["key"]] = body.get("string_value", "")
            return self._send(200, {})
        return self._send(200, {})

    # --------------------------------------------- identity API (account groups, through the workspace)
    def _identity(self, method, p, body):
        st = self.state
        if not st.identity_api:
            return self._missing("ENDPOINT_NOT_FOUND")
        parts = p.split("/")                                          # ['', 'api', '2.0', 'identity', ...]
        if p == "/api/2.0/identity/groups":
            if method == "POST":
                if any(g["group_name"] == body.get("group_name") for g in st.account_groups.values()):
                    return self._send(409, {"error_code": "RESOURCE_ALREADY_EXISTS", "message": "group exists"})
                gid = str(9000 + len(st.account_groups))
                st.account_groups[gid] = {"group_id": gid, "group_name": body.get("group_name"),
                                          "account_id": "acc-1"}
                return self._send(200, st.account_groups[gid])
            return self._send(200, {"groups": list(st.account_groups.values())})
        if p == "/api/2.0/identity/workspace-assignments" and method == "POST":
            gid = str(body["principal_id"])
            if gid in st.scim["Groups"]:
                return self._send(409, {"error_code": "RESOURCE_ALREADY_EXISTS", "message": "already assigned"})
            g = st.account_groups[gid]
            st.scim["Groups"][gid] = {"id": gid, "displayName": g["group_name"], "members": [],
                                      "meta": {"resourceType": "Group"}}
            return self._send(200, {"principal_id": int(gid), "principal_type": "GROUP"})
        if len(parts) == 7 and parts[4] == "groups" and parts[6] == "direct-members" and method == "POST":
            members = st.scim["Groups"][parts[5]].setdefault("members", [])
            if all(m["value"] != str(body["principal_id"]) for m in members):
                members.append({"value": str(body["principal_id"])})
            return self._send(200, {"principal_id": body["principal_id"], "group_id": int(parts[5])})
        return self._missing("identity endpoint not implemented in the fake")

    # ------------------------------------------------------------- SCIM: groups, service principals
    def _scim(self, method, p, q, body):
        st = self.state
        parts = p.split("/")                                          # ['', 'api', '2.0', 'preview', 'scim', 'v2', kind, id]
        kind = parts[6]
        items = st.scim[kind]
        if len(parts) == 7:
            if method == "POST":
                oid = str(7000 + sum(len(v) for v in st.scim.values()))
                item = {**body, "id": oid}
                if kind == "ServicePrincipals":
                    item.setdefault("applicationId", str(uuid.uuid4()))
                else:                                                 # the workspace SCIM API makes local groups
                    item["meta"] = {"resourceType": "WorkspaceGroup"}
                items[oid] = item
                return self._send(200, item)
            found = list(items.values())
            flt = q.get("filter", "")
            if flt.startswith("displayName eq "):
                name = flt[len("displayName eq "):].strip('"')
                found = [i for i in found if i.get("displayName") == name]
            start = int(q.get("startIndex", 1))
            page = found[start - 1:start - 1 + int(q.get("count", 100))]
            return self._send(200, {"Resources": page, "totalResults": len(found), "startIndex": start,
                                    "itemsPerPage": len(page)})
        oid = parts[7]
        if oid not in items:
            return self._missing(f"{kind} {oid} not found")
        if method == "PATCH":
            for op in body.get("Operations", []):
                if op.get("op", "").lower() == "add" and op.get("path") == "members":
                    have = {m["value"] for m in items[oid].setdefault("members", [])}
                    items[oid]["members"] += [m for m in op.get("value", []) if m["value"] not in have]
            return self._send(200, {})
        if method == "DELETE":
            items.pop(oid)
            return self._send(200, {})
        return self._send(200, items[oid])

    # -------------------------------------------------------------- pipelines (deploy only)
    def _pipelines(self, method, p, body):
        st = self.state
        parts = p.split("/")
        if len(parts) == 4:                                            # /api/2.0/pipelines
            if method == "POST":
                pid = str(uuid.uuid4())
                st.pipelines[pid] = {k: v for k, v in body.items() if k not in ("dry_run", "allow_duplicate_names")}
                return self._send(200, {"pipeline_id": pid})
            return self._send(200, {"statuses": [{"pipeline_id": k, "name": v.get("name")}
                                                 for k, v in st.pipelines.items()]})
        pid = parts[4]
        if pid not in st.pipelines:
            return self._missing(f"The specified pipeline {pid} was not found.")
        if method == "GET":
            spec = {**st.pipelines[pid], "id": pid}
            return self._send(200, {"pipeline_id": pid, "name": spec.get("name"), "spec": spec, "state": "IDLE",
                                    "creator_user_name": USER["userName"], "run_as_user_name": USER["userName"]})
        if method == "PUT":
            st.pipelines[pid] = {k: v for k, v in body.items()
                                 if k not in ("pipeline_id", "id", "expected_last_modified", "allow_duplicate_names")}
            return self._send(200, {})
        if method == "DELETE":
            st.pipelines.pop(pid)
            return self._send(200, {})
        return self._send(200, {})

    # --------------------------------------------- Genie benchmarks and conversations (test double)
    def _answer(self, space: dict, question: str):
        return self.state.evaluator(space, question) if self.state.evaluator else None

    def _genie_runtime(self, method, p, q, body):
        st = self.state
        parts = p.split("/")
        sid = parts[5]
        if sid not in st.spaces:
            return self._missing(f"Genie space {sid} does not exist")
        space = json.loads(st.spaces[sid]["serialized_space"] or "{}")
        if p.endswith("/eval-runs") and method == "GET":
            return self._send(200, {"eval_runs": [self._run_view(rid, "DONE") for rid, r in st.eval_runs.items()
                                                  if r["space_id"] == sid]})
        if p.endswith("/eval-runs") and method == "POST":
            results = []
            for b in (space.get("benchmarks") or {}).get("questions", []):
                question = "".join(b.get("question", []))
                expected = "".join(b["answer"][0]["content"]) if b.get("answer") else None
                verdict = self.state.evaluator.grade(space, question, expected) if hasattr(self.state.evaluator, "grade") \
                    else ("NEEDS_REVIEW", "no evaluator")
                results.append({"result_id": uuid.uuid4().hex, "benchmark_question_id": b.get("id"),
                                "question": question, "assessment": verdict[0], "reason": verdict[1]})
            run_id = uuid.uuid4().hex
            st.eval_runs[run_id] = {"space_id": sid, "results": results}
            return self._send(200, self._run_view(run_id, "RUNNING"))
        if "/eval-runs/" in p:
            run_id = parts[7]
            if run_id not in st.eval_runs:
                return self._missing("eval run not found")
            run = st.eval_runs[run_id]
            if p.endswith("/results"):
                return self._send(200, {"eval_results": [
                    {"result_id": r["result_id"], "space_id": sid, "benchmark_question_id": r["benchmark_question_id"],
                     "question": r["question"], "status": "DONE"} for r in run["results"]]})
            if "/results/" in p:
                r = next(r for r in run["results"] if r["result_id"] == parts[9])
                return self._send(200, {"result_id": r["result_id"], "space_id": sid,
                                        "benchmark_question_id": r["benchmark_question_id"], "assessment": r["assessment"],
                                        "assessment_reasons": [r["reason"]] if r["reason"] else [], "eval_run_status": "DONE"})
            return self._send(200, self._run_view(run_id, "DONE"))
        if p.endswith("/conversations") and method == "GET":
            convs = {}
            for m in st.messages.values():
                if m["space_id"] == sid:
                    convs.setdefault(m["conversation_id"], {"conversation_id": m["conversation_id"], "title": m["content"],
                                                            "created_timestamp": m["created_timestamp"]})
            return self._send(200, {"conversations": sorted(convs.values(), key=lambda c: -c["created_timestamp"])})
        if p.endswith("/messages") and method == "GET":
            cid = parts[-2]
            return self._send(200, {"messages": [m for m in st.messages.values() if m["conversation_id"] == cid]})
        if p.endswith("/start-conversation"):
            question = body.get("content", "")
            sql = self._answer(space, question)
            cid, mid = uuid.uuid4().hex, uuid.uuid4().hex
            msg = {"id": mid, "message_id": mid, "conversation_id": cid, "space_id": sid, "content": question,
                   "status": "COMPLETED", "created_timestamp": int(time.time() * 1000), "user_id": 4242,
                   "attachments": [{"attachment_id": uuid.uuid4().hex, "query": {"query": sql}}] if sql else
                                  [{"attachment_id": uuid.uuid4().hex, "text": {"content": "I could not answer that."}}]}
            st.messages[mid] = msg
            return self._send(200, {"conversation_id": cid, "message_id": mid, "message": msg})
        if p.endswith("/query-result") and "/attachments/" in p:
            mid = parts[9]
            att = next(a for a in st.messages[mid]["attachments"] if a["attachment_id"] == parts[11])
            cols, rows = self.state.evaluator.query(att["query"]["query"]) if hasattr(self.state.evaluator, "query") \
                else ([], [])
            return self._send(200, {"statement_response": {
                "statement_id": uuid.uuid4().hex, "status": {"state": "SUCCEEDED"},
                "manifest": {"schema": {"column_count": len(cols), "columns": [{"name": c, "position": i}
                                                                                for i, c in enumerate(cols)]}},
                "result": {"data_array": [[None if v is None else str(v) for v in r] for r in rows]}}})
        if "/messages/" in p:
            mid = parts[-1]
            return self._send(200, st.messages[mid]) if mid in st.messages else self._missing("message not found")
        return self._send(200, {})

    def _run_view(self, run_id, status):
        res = self.state.eval_runs[run_id]["results"]
        return {"eval_run_id": run_id, "eval_run_status": status, "num_questions": len(res),
                "num_done": len(res) if status == "DONE" else 0,
                "num_correct": sum(1 for r in res if r["assessment"] == "GOOD") if status == "DONE" else 0,
                "num_needs_review": sum(1 for r in res if r["assessment"] == "NEEDS_REVIEW") if status == "DONE" else 0}

    def _safe(self, method):
        try:
            self._handle(method)
        except Exception:                                             # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._send(500, {"error_code": "INTERNAL_ERROR", "message": "fake workspace error (see its stderr)"})

    def do_GET(self):
        self._safe("GET")

    def do_POST(self):
        self._safe("POST")

    def do_PATCH(self):
        self._safe("PATCH")

    def do_PUT(self):
        self._safe("PUT")

    def do_DELETE(self):
        self._safe("DELETE")


def _v1_fields(body: dict) -> str | None:
    """Like the real service: a version 2 space must not use version 1 column fields."""
    try:
        d = json.loads(body.get("serialized_space") or "{}")
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("version", 0) < 2:
        return None
    for ti, t in enumerate((d.get("data_sources") or {}).get("tables", [])):
        for ci, c in enumerate(t.get("column_configs") or []):
            if "get_example_values" in c or "build_value_dictionary" in c:
                return (f"Invalid export proto: data_sources.tables[{ti}].column_configs[{ci}] uses version 1 fields "
                        "(get_example_values, build_value_dictionary) but the export version is 2. Use "
                        "enable_format_assistance and enable_entity_matching for version 2.")
    return None


def _upgrade(serialized: str) -> str:
    """Like the real service, store the space canonically (and as version 2)."""
    try:
        d = json.loads(serialized)
    except ValueError:
        return serialized
    if isinstance(d, dict) and d.get("version") == 1:
        d["version"] = 2
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


class FakeWorkspace:
    def __init__(self, server, thread, state):
        self._server, self._thread, self.state = server, thread, state
        self.host = f"http://127.0.0.1:{server.server_address[1]}"

    @classmethod
    def start(cls, port: int = 0, evaluator=None) -> "FakeWorkspace":
        state = _State()
        state.evaluator = evaluator
        handler = type("Handler", (_Handler,), {"state": state})
        server = ThreadingHTTPServer(("127.0.0.1", port or _free_port()), handler)
        state.host = f"http://127.0.0.1:{server.server_address[1]}"
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return cls(server, t, state)

    @property
    def spaces(self) -> dict:
        return self.state.spaces

    def ui_edit(self, space_id: str, change) -> None:
        """Simulate an edit in the Genie UI: `change(space_dict)` mutates the live serialized space."""
        sp = self.state.spaces[space_id]
        d = json.loads(sp["serialized_space"])
        change(d)
        with self.state.lock:
            sp["serialized_space"] = json.dumps(d, sort_keys=True, separators=(",", ":"))
            sp["etag"] = str(int(sp["etag"]) + 1)

    def stop(self):
        self._server.shutdown()


class FreshCartEvaluator:
    """Answers and grades Genie questions on the local FreshCart SQLite warehouse.

    Not an AI: a question is answered with the space's trusted example SQL for that exact question
    (what Genie does when an example matches). A benchmark is GOOD when that answer returns the same
    rows as the benchmark's SQL, BAD when it doesn't, and NEEDS_REVIEW when no example matches.
    """

    def __init__(self):
        import pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
        from freshcart import benchmarks, config as C, pipeline
        from freshcart.db import connect
        if not (C.WAREHOUSE_DIR / "gold.db").exists():
            pipeline.run(full_refresh=True, verbose=False)
        self._benchmarks, self._connect = benchmarks, connect

    def _run(self, sql, params=None):
        con = self._connect()                     # one connection per call: requests arrive on server threads
        try:
            return self._benchmarks.run_sql(con, sql, params)[1]
        finally:
            con.close()

    @staticmethod
    def _example(space, question):
        key = " ".join(question.lower().split())
        for e in (space.get("instructions") or {}).get("example_question_sqls", []):
            if " ".join("".join(e.get("question", [])).lower().split()) == key:
                return e
        return None

    def query(self, sql):
        """(columns, rows) of a Genie answer, run on the local warehouse."""
        con = self._connect()
        try:
            return self._benchmarks.run_sql(con, sql)
        finally:
            con.close()

    def __call__(self, space, question):
        e = self._example(space, question)
        return "".join(e["sql"]) if e else None

    def grade(self, space, question, expected_sql):
        e = self._example(space, question)
        if e is None or not expected_sql:
            return "NEEDS_REVIEW", "no trusted example for this question; graded by Genie on Databricks"
        params = {p["name"]: (p.get("default_value") or {}).get("values", [None])[0] for p in e.get("parameters", [])}
        try:
            want, got = self._run(expected_sql), self._run("".join(e["sql"]), params)
        except Exception:                                         # noqa: BLE001
            return "BAD", "LLM_JUDGE_SYNTAX_ERROR"
        norm = lambda rows: sorted(tuple(round(v, 2) if isinstance(v, float) else v for v in r) for r in rows)  # noqa: E731
        if norm(want) == norm(got):
            return "GOOD", ""
        # the same reason codes Genie benchmark evaluation uses (databricks.sdk ScoreReason)
        if len(got) < len(want):
            return "BAD", "RESULT_MISSING_ROWS"
        if len(got) > len(want):
            return "BAD", "RESULT_EXTRA_ROWS"
        return "BAD", "SINGLE_CELL_DIFFERENCE"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    ws = FakeWorkspace.start(int(sys.argv[1]) if len(sys.argv) > 1 else 8765, evaluator=FreshCartEvaluator())
    print(f"fake workspace at {ws.host}  (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        ws.stop()
