"""A minimal, in-memory stand-in for a Databricks workspace, for running the real Databricks CLI
(`databricks bundle validate / plan / deploy / generate / summary` and `databricks genie ...`) offline.

It implements just the REST endpoints those commands use for a bundle that contains Genie spaces:
current user, workspace files (bundle files, deployment lock and state), Genie spaces (create, get,
update with etag, trash) and permissions. It is a test double, not an emulator of Databricks.

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
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

USER = {"id": "1001", "userName": "dana@freshcart.example", "displayName": "Dana Developer",
        "groups": [{"display": "users"}, {"display": "admins"}, {"display": "freshcart-genie-deployers"},
                   {"display": "freshcart-genie-developers"}]}


class _State:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/", "/Workspace", "/Workspace/Users", "/Workspace/Shared", "/Users", "/Shared"}
        self.spaces: dict[str, dict] = {}
        self.permissions: dict[str, list] = {}
        self.jobs: dict[int, dict] = {}
        self.eval_runs: dict[str, dict] = {}
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
            if p == "/api/2.0/genie/spaces" and method == "POST":
                sid = uuid.uuid4().hex
                sp = {"space_id": sid, "title": body.get("title", ""), "description": body.get("description", ""),
                      "warehouse_id": body.get("warehouse_id"), "parent_path": body.get("parent_path"),
                      "serialized_space": _upgrade(body.get("serialized_space", "{}")), "etag": "1"}
                st.spaces[sid] = sp
                return self._send(200, self._space_view(sp, True))
            if p == "/api/2.0/genie/spaces" and method == "GET":
                return self._send(200, {"spaces": [self._space_view(s, False) for s in st.spaces.values()]})
            if "/eval-runs" in p or "/start-conversation" in p or "/conversations/" in p:
                return self._genie_runtime(method, p, q, body)
            if p.startswith("/api/2.2/jobs/") or p.startswith("/api/2.1/jobs/"):
                return self._jobs(method, p, q, body)
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
                return self._send(200, {"warehouses": [{"id": "wh-0001", "name": "Serverless Starter Warehouse"}]})
            return self._send(200, {})

    # ------------------------------------------------------------------- jobs (deploy only)
    def _jobs(self, method, p, q, body):
        st = self.state
        op = p.rsplit("/", 1)[-1]
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
        if p.endswith("/start-conversation"):
            question = body.get("content", "")
            sql = self._answer(space, question)
            cid, mid = uuid.uuid4().hex, uuid.uuid4().hex
            msg = {"id": mid, "message_id": mid, "conversation_id": cid, "space_id": sid, "content": question,
                   "status": "COMPLETED",
                   "attachments": [{"attachment_id": uuid.uuid4().hex, "query": {"query": sql}}] if sql else
                                  [{"attachment_id": uuid.uuid4().hex, "text": {"content": "I could not answer that."}}]}
            st.messages[mid] = msg
            return self._send(200, {"conversation_id": cid, "message_id": mid, "message": msg})
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

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


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
