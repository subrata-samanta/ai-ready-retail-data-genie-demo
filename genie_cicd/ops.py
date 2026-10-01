"""The operations behind the CLI and the CI/CD workflows.

    pull       export a live space into the environment-neutral file in git
    snapshot   record the live space in the environment's history if it changed
    plan       what a deploy would change, and whether the live space drifted
    deploy     validate -> check dependencies -> back up live -> drift guard -> create/update (etag) -> verify -> record
    gate       run the space's benchmarks and apply the environment's pass rules
    smoke      ask one question and expect a SQL answer
    promote    deploy to the next environment exactly what was deployed (and tested) in the previous one
    rollback   redeploy an earlier record: the previous release, a backup, a snapshot or a git version
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import spec as S
from .history import HistoryStore
from .workspace import NotFound

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "deploy" / "genie" / "environments.yml"


class DeployError(Exception):
    pass


class DriftError(DeployError):
    """The live space was changed outside the pipeline since the last deployment."""

    def __init__(self, env: str, changes: list[str]):
        self.changes = changes
        super().__init__(f"{env}: the live space was edited outside the pipeline since the last deployment "
                         f"({S.count_changes(changes)} change(s)). Pull it into git, or redeploy with --allow-drift to overwrite.")


class GateFailed(DeployError):
    pass


# ------------------------------------------------------------------------------- configuration
@dataclass
class Env:
    name: str
    catalog: str
    warehouse_id: str
    title: str
    description: str
    parent_path: str | None = None
    space_id: str | None = None
    host: str | None = None
    profile: str | None = None
    protected: bool = False
    history_root: str = "/genie_history"
    gate: dict = field(default_factory=dict)
    smoke_question: str | None = None


@dataclass
class Config:
    space_file: Path
    envs: dict[str, Env]
    order: list[str]

    def env(self, name: str) -> Env:
        if name not in self.envs:
            raise KeyError(f"unknown environment {name!r}; expected one of {', '.join(self.envs)}")
        return self.envs[name]

    @property
    def catalogs(self) -> list[str]:
        return [e.catalog for e in self.envs.values()]


def _expand(value):
    if isinstance(value, str):
        if "$" not in value:
            return value
        out = os.path.expandvars(value)
        return "" if "${" in out or out.startswith("$") else out      # unset variable -> empty
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    space = raw["space"]
    envs = {}
    for name, e in raw["environments"].items():
        e = _expand(e)
        envs[name] = Env(
            name=name, catalog=e["catalog"], warehouse_id=str(e.get("warehouse_id") or ""),
            title=space["title"] + e.get("title_suffix", ""), description=space.get("description", "").strip(),
            parent_path=e.get("parent_path") or None, space_id=e.get("space_id") or None, host=e.get("host") or None,
            profile=e.get("profile") or None, protected=bool(e.get("protected", False)), history_root=e.get("history_root", "/genie_history"),
            gate=e.get("gate") or {}, smoke_question=e.get("smoke_question"),
        )
    return Config(space_file=(Path(path).parent / space["file"]).resolve(), envs=envs, order=list(raw["environments"]))


# ----------------------------------------------------------------------------------- reading
def find_space(ws, env: Env) -> dict | None:
    """The environment's space: by configured id, otherwise by exact title (and folder)."""
    if env.space_id:
        try:
            return ws.get_space(env.space_id)
        except NotFound:
            return None
    for s in ws.list_spaces():
        if s["title"] == env.title and (env.parent_path is None or s.get("parent_path") in (None, env.parent_path)):
            return ws.get_space(s["space_id"])
    return None


def live_neutral(ws, env: Env) -> tuple[dict | None, dict | None]:
    live = find_space(ws, env)
    if live is None:
        return None, None
    return live, S.neutralise(live["serialized_space"], env.catalog)


def pull(ws, env: Env, out: Path) -> list[str]:
    """Export the live space into the git file. Returns the change list versus the file before."""
    live, neutral = live_neutral(ws, env)
    if live is None:
        raise DeployError(f"{env.name}: no space titled {env.title!r} found")
    before = S.load(out) if Path(out).exists() else {}
    S.save(neutral, out)
    return S.diff(before, neutral)


def snapshot(ws, env: Env, history: HistoryStore, actor: str | None = None) -> dict | None:
    """Store the live space as a new version if it differs from the newest record. None = unchanged."""
    live, neutral = live_neutral(ws, env)
    if live is None:
        return None
    last = history.latest(env.name, ("snapshot", "deployed", "rollback"))
    if last and last["content_hash"] == S.content_hash(neutral):
        return None
    changes = S.diff(last["space"], neutral) if last else ["first snapshot"]
    return history.put(env.name, "snapshot", neutral, space_id=live["space_id"], actor=actor,
                       note=f"{S.count_changes(changes)} change(s) since {last['record_id'] if last else 'nothing'}", changes=changes)


def check_dependencies(ws, env: Env, space: dict) -> list[str]:
    """Every table, metric view and function the space uses must exist in the target catalog."""
    missing = []
    rendered = S.declared_objects(S.render(space, env.catalog))
    for ident, kind in sorted(rendered.items()):
        if not ws.object_exists(ident, "function" if kind == "function" else "table"):
            missing.append(f"{kind} {ident}")
    return missing


def check_sql_locally(space: dict) -> list[str]:
    """Run every example query and benchmark answer on the local SQLite warehouse (built if missing).

    Catches SQL that no longer parses or references a column that doesn't exist, before any deploy.
    """
    from freshcart import config as C, metrics, pipeline
    from freshcart.db import connect
    if not (C.WAREHOUSE_DIR / "gold.db").exists():
        pipeline.run(full_refresh=True, verbose=False)
    con = connect()
    local = S.render(space, "freshcart")
    failures = []
    for e in S._get(local, ("instructions", "example_question_sqls")):
        params = {p["name"]: (p.get("default_value") or {}).get("values", [None])[0] for p in e.get("parameters", [])}
        try:
            metrics.run(con, S.sql_text(e["sql"]), params)
        except Exception as ex:                             # noqa: BLE001
            failures.append(f"example {S.sql_text(e['question'])!r} fails locally: {ex}")
    for b in S._get(local, ("benchmarks", "questions")):
        for a in b.get("answer", []):
            try:
                metrics.run(con, S.sql_text(a["content"]))
            except Exception as ex:                         # noqa: BLE001
                failures.append(f"benchmark {S.sql_text(b['question'])!r} answer fails locally: {ex}")
    return failures


def _last_release(history: HistoryStore, env: Env) -> dict | None:
    return history.latest(env.name, ("deployed", "rollback"))


def plan(ws, env: Env, space: dict, history: HistoryStore) -> dict:
    live, neutral = live_neutral(ws, env)
    last = _last_release(history, env)
    drift = S.diff(last["space"], neutral) if (live and last and last["content_hash"] != S.content_hash(neutral)) else []
    return {
        "env": env.name,
        "action": "create" if live is None else ("noop" if S.content_hash(neutral) == S.content_hash(space)
                                                 and live.get("title") == env.title else "update"),
        "space_id": live and live["space_id"],
        "changes": S.diff(neutral or {}, space),
        "drift": drift,
        "live_hash": neutral and S.content_hash(neutral),
        "new_hash": S.content_hash(space),
    }


# ---------------------------------------------------------------------------------- deploying
def deploy(ws, env: Env, space: dict, history: HistoryStore, *, config: Config | None = None, version: str | None = None,
           git_sha: str | None = None, actor: str | None = None, allow_drift: bool = False,
           kind: str = "deployed", note: str | None = None, log=print) -> dict:
    space = S.normalise(space)
    if getattr(ws, "requires_warehouse", True) and not env.warehouse_id:
        raise DeployError(f"{env.name}: no warehouse_id configured; set GENIE_{env.name.upper()}_WAREHOUSE_ID "
                          "(in CI: the GENIE_WAREHOUSE_ID variable of the GitHub environment)")
    errors, _ = S.validate(space, forbidden_catalogs=config.catalogs if config else [env.catalog])
    if errors:
        raise DeployError(f"{env.name}: the space definition is invalid: " + "; ".join(errors))
    missing = check_dependencies(ws, env, space)
    if missing:
        raise DeployError(f"{env.name}: objects missing in catalog {env.catalog}: " + ", ".join(missing)
                          + ". Deploy the data layer to this environment first.")

    live, neutral = live_neutral(ws, env)
    backup = None
    if live is not None:
        last = _last_release(history, env)
        drift = S.diff(last["space"], neutral) if last and last["content_hash"] != S.content_hash(neutral) else []
        if drift and env.protected and not allow_drift:
            raise DriftError(env.name, drift)                # nothing written: the live space is untouched
        backup = history.put(env.name, "backup", neutral, space_id=live["space_id"], actor=actor,
                             note=f"live state before deploying {version or S.content_hash(space)}")
        if drift:
            log(f"  ! {env.name}: overwriting {S.count_changes(drift)} change(s) made outside the pipeline "
                f"(kept in backup {backup['record_id']})")

    rendered = S.render(space, env.catalog)
    if live is None:
        result = ws.create_space(title=env.title, description=env.description, warehouse_id=env.warehouse_id,
                                 parent_path=env.parent_path, serialized_space=rendered)
        action = "created"
    else:
        result = ws.update_space(live["space_id"], title=env.title, description=env.description,
                                 warehouse_id=env.warehouse_id, serialized_space=rendered, etag=live.get("etag"))
        action = "updated"

    after = S.neutralise(ws.get_space(result["space_id"])["serialized_space"], env.catalog)
    if S.content_hash(after) != S.content_hash(space):
        raise DeployError(f"{env.name}: verification failed, the live space differs from what was deployed: "
                          + "; ".join(S.diff(space, after)[:5]))
    record = history.put(env.name, kind, space, space_id=result["space_id"], version=version, git_sha=git_sha,
                         actor=actor, backup=backup and backup["record_id"], note=note)
    log(f"  {action} {env.name} space {result['space_id']} -> content {record['content_hash']}"
        + (f" (version {version})" if version else ""))
    return record


def gate(ws, env: Env, history: HistoryStore | None = None, log=print) -> dict:
    """Run the benchmarks; pass only if nothing is BAD and accuracy on graded questions is high enough."""
    live = find_space(ws, env)
    if live is None:
        raise DeployError(f"{env.name}: no space to evaluate")
    res = ws.run_benchmarks(live["space_id"])
    graded = res["num_questions"] - res["num_needs_review"]
    accuracy = res["num_correct"] / graded if graded else 0.0
    rules = {"min_accuracy": float(env.gate.get("min_accuracy", 1.0)), "max_bad": int(env.gate.get("max_bad", 0)),
             "min_graded": int(env.gate.get("min_graded", 1))}
    reasons = []
    if res["num_bad"] > rules["max_bad"]:
        reasons.append(f"{res['num_bad']} benchmark(s) answered incorrectly (allowed {rules['max_bad']})")
    if graded < rules["min_graded"]:
        reasons.append(f"only {graded} benchmark(s) could be graded (need {rules['min_graded']})")
    if accuracy < rules["min_accuracy"]:
        reasons.append(f"accuracy {accuracy:.0%} is below {rules['min_accuracy']:.0%}")
    res.update(accuracy=accuracy, graded=graded, rules=rules, passed=not reasons, reasons=reasons)
    log(f"  benchmarks on {env.name}: {res['num_correct']}/{graded} graded correct, {res['num_bad']} bad, "
        f"{res['num_needs_review']} need review -> {'PASS' if res['passed'] else 'FAIL: ' + '; '.join(reasons)}")
    if history is not None:
        rel = _last_release(history, env)
        if rel:
            history.put(env.name, "gate", rel["space"], version=rel.get("version"), passed=res["passed"],
                        note=f"accuracy {accuracy:.0%}, bad {res['num_bad']}", results=res["results"])
    return res


def smoke(ws, env: Env, log=print) -> dict:
    live = find_space(ws, env)
    if live is None:
        raise DeployError(f"{env.name}: no space to test")
    question = env.smoke_question or "What were net sales and margin by region last week?"
    res = ws.ask(live["space_id"], question)
    ok = res["status"] == "COMPLETED" and bool(res.get("sql"))
    res.update(question=question, passed=ok)
    log(f"  smoke test on {env.name}: {question!r} -> {res['status']}{' with SQL' if res.get('sql') else ' without SQL'}"
        f" -> {'PASS' if ok else 'FAIL'}")
    return res


def promote(src_ws, src: Env, src_history: HistoryStore, dst_ws, dst: Env, dst_history: HistoryStore, *,
            config: Config | None = None, actor: str | None = None, allow_drift: bool = False, log=print) -> dict:
    """Deploy to `dst` the exact content last released to `src`: what was tested is what ships."""
    rel = _last_release(src_history, src)
    if rel is None:
        raise DeployError(f"nothing has been released to {src.name} yet")
    last_gate = src_history.latest(src.name, ("gate",))
    if src.gate and not (last_gate and last_gate.get("passed") and last_gate["content_hash"] == rel["content_hash"]):
        raise GateFailed(f"{src.name} release {rel['content_hash']} has not passed its benchmark gate; refusing to promote")
    return deploy(dst_ws, dst, rel["space"], dst_history, config=config, version=rel.get("version"),
                  git_sha=rel.get("git_sha"), actor=actor, allow_drift=allow_drift,
                  note=f"promoted from {src.name} record {rel['record_id']}", log=log)


def resolve_target(env: Env, history: HistoryStore, to: str, *, live_hash: str | None, space_file: Path) -> dict:
    """Turn a rollback target into a record-like dict with a `space`.

    previous        the release before the one that is live now (repeatable: v3 -> v2 -> v1)
    git:<ref>       the space file as it was at a git commit or tag
    <record id>     any history record (a backup, snapshot or release), by full id or unique prefix/suffix
    """
    if to == "previous":
        releases = history.records(env.name, ("deployed",))
        idx = max((i for i, r in enumerate(releases) if r["content_hash"] == live_hash), default=None)
        if idx is None:
            if not releases:
                raise DeployError(f"{env.name}: no earlier release to roll back to")
            return releases[-1]                              # live drifted: go back to the last real release
        if idx == 0:
            raise DeployError(f"{env.name}: the live space is the first release; there is nothing earlier")
        return releases[idx - 1]
    if to.startswith("git:"):
        ref = to[4:]
        proc = subprocess.run(["git", "show", f"{ref}:./{Path(space_file).name}"], cwd=Path(space_file).parent,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise DeployError(f"git: cannot read {Path(space_file).name} at {ref}: {proc.stderr.strip()}")
        return {"record_id": to, "space": json.loads(proc.stdout), "version": ref, "git_sha": ref}
    return history.get(env.name, to)


def rollback(ws, env: Env, history: HistoryStore, *, to: str = "previous", config: Config | None = None,
             actor: str | None = None, log=print) -> dict:
    live, neutral = live_neutral(ws, env)
    target = resolve_target(env, history, to, live_hash=neutral and S.content_hash(neutral),
                            space_file=config.space_file if config else ROOT / "genie" / "space")
    log(f"  rolling {env.name} back to {target['record_id']} (content {S.content_hash(target['space'])})")
    return deploy(ws, env, target["space"], history, config=config, version=target.get("version"),
                  git_sha=target.get("git_sha"), actor=actor, allow_drift=True, kind="rollback",
                  note=f"rollback to {target['record_id']}", log=log)
