"""Two interchangeable workspaces with the same methods.

* DatabricksWorkspace: the real thing, through the Databricks SDK (`pip install databricks-sdk`).
  Every call maps to one public API:
      list / get / create / update space  ->  /api/2.0/genie/spaces[/{id}]
      run_benchmarks                      ->  Genie benchmark evaluation runs (eval-runs API)
      ask                                 ->  Genie conversation API (start-conversation)
      object_exists                       ->  Unity Catalog tables / functions APIs
      read/write/list files               ->  Files API on a Unity Catalog volume (deployment history)

* SimulatedWorkspace: a local stand-in that keeps spaces and files in a folder and runs SQL on the
  local SQLite warehouse. It lets the notebook, the tests and CI exercise every step of the
  version-control and promotion process without a Databricks workspace. It is not an AI: when asked
  a question it answers with the space's trusted example SQL for that exact question, and reports
  NEEDS_REVIEW otherwise, which is how Genie benchmarks treat answers they can't grade automatically.
"""
from __future__ import annotations

import hashlib
import io
import json
import time
from datetime import timedelta
from pathlib import Path

from . import spec as S


class NotFound(Exception):
    pass


class ConflictError(Exception):
    """The space changed since it was read (etag mismatch): someone edited it concurrently."""


# ======================================================================================== real
class DatabricksWorkspace:
    def __init__(self, host: str | None = None, profile: str | None = None, client=None):
        if client is None:
            from databricks.sdk import WorkspaceClient          # imported lazily: optional dependency
            client = WorkspaceClient(host=host or None, profile=profile or None)
        self.w = client
        self.name = host or "databricks"

    # ---- spaces
    def list_spaces(self) -> list[dict]:
        out, token = [], None
        while True:
            resp = self.w.genie.list_spaces(page_size=100, page_token=token)
            out += [{"space_id": s.space_id, "title": s.title, "parent_path": s.parent_path} for s in (resp.spaces or [])]
            token = resp.next_page_token
            if not token:
                return out

    def get_space(self, space_id: str) -> dict:
        from databricks.sdk.errors import NotFound as SdkNotFound
        try:
            s = self.w.genie.get_space(space_id, include_serialized_space=True)
        except SdkNotFound as e:
            raise NotFound(space_id) from e
        return {"space_id": s.space_id, "title": s.title, "description": s.description, "warehouse_id": s.warehouse_id,
                "parent_path": s.parent_path, "etag": s.etag, "serialized_space": S.parse(s.serialized_space)}

    def create_space(self, *, title, description, warehouse_id, parent_path, serialized_space: dict) -> dict:
        s = self.w.genie.create_space(warehouse_id=warehouse_id, serialized_space=S.to_wire(serialized_space),
                                      title=title, description=description, parent_path=parent_path)
        return self.get_space(s.space_id)

    def update_space(self, space_id, *, title, description, warehouse_id, serialized_space: dict, etag=None) -> dict:
        from databricks.sdk.errors import DatabricksError
        try:
            self.w.genie.update_space(space_id, serialized_space=S.to_wire(serialized_space), title=title,
                                      description=description, warehouse_id=warehouse_id, etag=etag)
        except DatabricksError as e:                              # a stale etag is rejected by the API
            if "etag" in str(e).lower() or getattr(e, "error_code", "") in ("ABORTED", "RESOURCE_CONFLICT"):
                raise ConflictError(str(e)) from e
            raise
        return self.get_space(space_id)

    # ---- Unity Catalog objects the space depends on
    def object_exists(self, full_name: str, kind: str) -> bool:
        from databricks.sdk.errors import NotFound as SdkNotFound
        if kind == "function":
            try:
                self.w.functions.get(full_name)
                return True
            except SdkNotFound:
                return False
        return bool(self.w.tables.exists(full_name).table_exists)       # tables and metric views

    # ---- quality gates
    def run_benchmarks(self, space_id: str, timeout_s: int = 1800) -> dict:
        run = self.w.genie.genie_create_eval_run(space_id)
        deadline = time.time() + timeout_s
        while True:
            run = self.w.genie.genie_get_eval_run(space_id, run.eval_run_id)
            status = getattr(run.eval_run_status, "value", run.eval_run_status)
            if status not in ("NOT_STARTED", "RUNNING", None):
                break
            if time.time() > deadline:
                raise TimeoutError(f"benchmark run {run.eval_run_id} did not finish in {timeout_s}s")
            time.sleep(10)
        results, token = [], None
        while True:
            page = self.w.genie.genie_list_eval_results(space_id, run.eval_run_id, page_size=100, page_token=token)
            for r in page.eval_results or []:
                d = self.w.genie.genie_get_eval_result_details(space_id, run.eval_run_id, r.result_id)
                assessment = getattr(d.assessment, "value", d.assessment) or "NEEDS_REVIEW"
                reasons = [str(getattr(x, "value", x)) for x in (d.assessment_reasons or [])]
                results.append({"question": r.question, "assessment": assessment, "reason": "; ".join(reasons)})
            token = page.next_page_token
            if not token:
                break
        bad = sum(1 for r in results if r["assessment"] == "BAD")
        return {"eval_run_id": run.eval_run_id, "status": status, "num_questions": run.num_questions or len(results),
                "num_correct": run.num_correct or 0, "num_needs_review": run.num_needs_review or 0,
                "num_bad": bad, "results": results}

    def ask(self, space_id: str, question: str, timeout_s: int = 600) -> dict:
        msg = self.w.genie.start_conversation_and_wait(space_id, question, timeout=timedelta(seconds=timeout_s))
        sql = next((a.query.query for a in (msg.attachments or []) if a.query and a.query.query), None)
        status = getattr(msg.status, "value", msg.status)
        return {"status": status, "sql": sql, "error": msg.error.error if msg.error else None}

    # ---- files (deployment history on a Unity Catalog volume)
    def write_file(self, path: str, text: str) -> None:
        self.w.files.upload(path, io.BytesIO(text.encode("utf-8")), overwrite=True)

    def read_file(self, path: str) -> str:
        return self.w.files.download(path).contents.read().decode("utf-8")

    def list_files(self, directory: str) -> list[str]:
        from databricks.sdk.errors import NotFound as SdkNotFound
        try:
            return sorted(e.path for e in self.w.files.list_directory_contents(directory) if not e.is_directory)
        except SdkNotFound:
            return []


# =================================================================================== simulated
class SimulatedWorkspace:
    """One simulated Databricks workspace (one environment) stored in `state_dir`."""

    requires_warehouse = False                           # nothing runs on a SQL warehouse here

    def __init__(self, state_dir: str | Path, catalog: str, *, missing_objects: set[str] = frozenset(), con=None):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.catalog = catalog
        self.missing_objects = set(missing_objects)      # pretend these UC objects were not deployed
        self.name = f"simulated workspace ({catalog})"
        self._con = con

    # ---- state
    @property
    def _state_file(self) -> Path:
        return self.dir / "spaces.json"

    def _load(self) -> dict:
        return json.loads(self._state_file.read_text()) if self._state_file.exists() else {"counter": 0, "spaces": {}}

    def _save(self, st: dict) -> None:
        self._state_file.write_text(json.dumps(st, indent=1, sort_keys=True))

    def _write(self, st: dict, space_id: str, **fields) -> dict:
        st["counter"] += 1
        sp = st["spaces"].setdefault(space_id, {"space_id": space_id})
        sp.update(fields, etag=str(st["counter"]))
        self._save(st)
        return self.get_space(space_id)

    # ---- spaces
    def list_spaces(self) -> list[dict]:
        return [{"space_id": s["space_id"], "title": s["title"], "parent_path": s.get("parent_path")}
                for s in self._load()["spaces"].values()]

    def get_space(self, space_id: str) -> dict:
        s = self._load()["spaces"].get(space_id)
        if s is None:
            raise NotFound(space_id)
        return {**s, "serialized_space": S.parse(s["serialized_space"])}

    def create_space(self, *, title, description, warehouse_id, parent_path, serialized_space: dict) -> dict:
        st = self._load()
        space_id = hashlib.md5(f"{self.catalog}:{title}:{st['counter']}".encode()).hexdigest()
        return self._write(st, space_id, title=title, description=description, warehouse_id=warehouse_id,
                           parent_path=parent_path, serialized_space=S.to_wire(serialized_space))

    def update_space(self, space_id, *, title, description, warehouse_id, serialized_space: dict, etag=None) -> dict:
        st = self._load()
        if space_id not in st["spaces"]:
            raise NotFound(space_id)
        if etag is not None and st["spaces"][space_id]["etag"] != etag:
            raise ConflictError(f"etag {etag} is stale; the space is now at {st['spaces'][space_id]['etag']}")
        return self._write(st, space_id, title=title, description=description, warehouse_id=warehouse_id,
                           serialized_space=S.to_wire(serialized_space))

    def ui_edit(self, space_id: str, change) -> dict:
        """Simulate someone editing the space in the Genie UI: `change(space_dict)` mutates it in place."""
        live = self.get_space(space_id)
        space = live["serialized_space"]
        change(space)
        st = self._load()
        return self._write(st, space_id, serialized_space=S.to_wire(space))

    # ---- local execution against the SQLite warehouse
    def _connection(self):
        if self._con is None:
            from freshcart.db import connect
            self._con = connect()
        return self._con

    def _local_sql(self, sql: str) -> str:
        return S._catalog_ref(self.catalog).sub("freshcart.", sql)

    def _run(self, sql: str, params: dict | None = None):
        from freshcart import metrics
        return metrics.run(self._connection(), self._local_sql(sql), params or {})

    def object_exists(self, full_name: str, kind: str) -> bool:
        if full_name in self.missing_objects:
            return False
        parts = full_name.split(".")
        if len(parts) != 3 or parts[0] != self.catalog:
            return False                                     # wrong catalog = not in this workspace
        schema, name = parts[1], parts[2]
        if schema == "semantic":
            from freshcart import metrics
            return name in metrics.METRIC_VIEWS or name == "fn_like_for_like_sales"
        con = self._connection()
        try:
            return con.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None
        except Exception:                                   # noqa: BLE001  unknown schema
            return False

    @staticmethod
    def _key(question) -> str:
        return " ".join(S.sql_text(question).lower().split())

    def _example_for(self, space: dict, question) -> dict | None:
        q = self._key(question)
        return next((e for e in S._get(space, ("instructions", "example_question_sqls")) if self._key(e.get("question")) == q), None)

    @staticmethod
    def _defaults(example: dict) -> dict:
        return {p["name"]: (p.get("default_value") or {}).get("values", [None])[0] for p in example.get("parameters", [])}

    @staticmethod
    def _same(a, b) -> bool:
        norm = lambda rows: sorted(tuple(round(v, 2) if isinstance(v, float) else v for v in r) for r in rows)  # noqa: E731
        return norm(a) == norm(b)

    def run_benchmarks(self, space_id: str, timeout_s: int = 1800) -> dict:
        space = self.get_space(space_id)["serialized_space"]
        results = []
        for b in S._get(space, ("benchmarks", "questions")):
            question = S.sql_text(b["question"])
            expected_sql = S.sql_text(b["answer"][0]["content"]) if b.get("answer") else None
            example = self._example_for(space, b["question"])
            if example is None or expected_sql is None:
                results.append({"question": question, "assessment": "NEEDS_REVIEW",
                                "reason": "no trusted example for this question; graded by a human or by Genie on Databricks"})
                continue
            try:
                _, expected = self._run(expected_sql)
                _, actual = self._run(S.sql_text(example["sql"]), self._defaults(example))
            except Exception as e:                           # noqa: BLE001
                results.append({"question": question, "assessment": "BAD", "reason": f"SQL failed: {e}"})
                continue
            if self._same(expected, actual):
                results.append({"question": question, "assessment": "GOOD", "reason": "result matches the benchmark answer"})
            else:
                results.append({"question": question, "assessment": "BAD",
                                "reason": f"result differs: expected {expected[:2]} got {actual[:2]}"})
        counts = {a: sum(1 for r in results if r["assessment"] == a) for a in ("GOOD", "BAD", "NEEDS_REVIEW")}
        return {"eval_run_id": f"sim-{int(time.time())}", "status": "DONE", "num_questions": len(results),
                "num_correct": counts["GOOD"], "num_needs_review": counts["NEEDS_REVIEW"], "num_bad": counts["BAD"],
                "results": results}

    def ask(self, space_id: str, question: str, timeout_s: int = 600) -> dict:
        space = self.get_space(space_id)["serialized_space"]
        example = self._example_for(space, [question])
        if example is None:
            return {"status": "COMPLETED", "sql": None, "error": None}
        try:
            _, rows = self._run(S.sql_text(example["sql"]), self._defaults(example))
            return {"status": "COMPLETED", "sql": S.sql_text(example["sql"]), "rows": len(rows), "error": None}
        except Exception as e:                               # noqa: BLE001
            return {"status": "FAILED", "sql": S.sql_text(example["sql"]), "error": str(e)}

    # ---- files
    def _path(self, path: str) -> Path:
        return self.dir / "files" / path.lstrip("/")

    def write_file(self, path: str, text: str) -> None:
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def read_file(self, path: str) -> str:
        return self._path(path).read_text(encoding="utf-8")

    def list_files(self, directory: str) -> list[str]:
        d = self._path(directory)
        return sorted("/" + str(p.relative_to(self.dir / "files")) for p in d.glob("*") if p.is_file()) if d.exists() else []
