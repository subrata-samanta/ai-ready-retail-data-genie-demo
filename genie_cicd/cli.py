"""Command line for Genie space version control and promotion.

    python -m genie_cicd validate [--sql]                 static checks (+ run every SQL on the local warehouse)
    python -m genie_cicd bootstrap                        compile genie/ design files into the space file (once)
    python -m genie_cicd diff     --base OLD.json         readable change list (used in pull requests)
    python -m genie_cicd render   --env qa [--out F]      the space file with the env's catalog filled in
    python -m genie_cicd pull     --env dev               live space -> space file in git (then open a PR)
    python -m genie_cicd snapshot --env dev               record the live space in history if it changed
    python -m genie_cicd plan     --env prod              what a deploy would change; drift since last release
    python -m genie_cicd deploy   --env qa [--file F] [--version V] [--allow-drift]
    python -m genie_cicd gate     --env qa                benchmark gate (exit 1 on failure)
    python -m genie_cicd smoke    --env prod              ask one question (exit 1 on failure)
    python -m genie_cicd promote  --from qa --to prod     deploy what was released (and passed) in qa
    python -m genie_cicd rollback --env prod [--to previous | <record id> | git:<ref>]
    python -m genie_cicd history  --env prod              the audit trail

Workspaces: `--target databricks` (default; standard Databricks auth: env vars in CI, a profile
from deploy/genie/environments.yml on a laptop) or `--target sim` (a local simulation in .genie_sim/).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import ops, spec as S
from .history import HistoryStore
from .workspace import DatabricksWorkspace, SimulatedWorkspace

ROOT = Path(__file__).resolve().parent.parent


def _git_sha() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.strip()
    except Exception:                                       # noqa: BLE001
        return None


def _actor() -> str:
    return os.environ.get("GITHUB_ACTOR") or os.environ.get("USER") or "unknown"


class Context:
    def __init__(self, args):
        self.config = ops.load_config(args.config)
        self.target = args.target
        self.sim_dir = Path(args.sim_dir)
        self._ws = {}

    def ws(self, env: ops.Env):
        if env.name not in self._ws:
            if self.target == "sim":
                self._ws[env.name] = SimulatedWorkspace(self.sim_dir / env.name, env.catalog)
            else:
                use_profile = env.profile and not os.environ.get("DATABRICKS_HOST")
                self._ws[env.name] = DatabricksWorkspace(host=env.host, profile=env.profile if use_profile else None)
        return self._ws[env.name]

    def history(self, env: ops.Env) -> HistoryStore:
        return HistoryStore(self.ws(env), env.history_root)


def _print_list(title: str, items: list[str]):
    print(title)
    for line in items or ["  (none)"]:
        print("  " + line if not line.startswith("  ") else line)


def _summary(text: str):
    """Also write to the GitHub Actions job summary when running in Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def cmd_validate(ctx, args):
    space = S.load(args.file or ctx.config.space_file)
    errors, warnings = S.validate(space, forbidden_catalogs=ctx.config.catalogs)
    if S.dumps(space) != Path(args.file or ctx.config.space_file).read_text(encoding="utf-8"):
        errors.append("the space file is not in canonical form; run `python -m genie_cicd pull` or `fmt`")
    if args.sql:
        errors += ops.check_sql_locally(space)
    print(f"space content {S.content_hash(space)}: {S.summary(space)}")
    _print_list("errors:", errors)
    _print_list("warnings:", warnings)
    _summary(f"### Genie space validation\n\n{len(errors)} error(s), {len(warnings)} warning(s)\n\n"
             + "\n".join(f"- ❌ {e}" for e in errors) + "\n" + "\n".join(f"- ⚠️ {w}" for w in warnings))
    return 1 if errors else 0


def cmd_fmt(ctx, args):
    path = Path(args.file or ctx.config.space_file)
    S.save(S.load(path), path)
    print(f"formatted {path}")
    return 0


def cmd_diff(ctx, args):
    base = S.load(args.base) if Path(args.base).exists() and Path(args.base).stat().st_size else {}
    new = S.load(args.file or ctx.config.space_file)
    changes = S.diff(base, new)
    _print_list(f"changes {S.content_hash(base) if base else '(none)'} -> {S.content_hash(new)}:", changes)
    _summary("### Genie space changes in this pull request\n\n```diff\n" + ("\n".join(changes) or "no changes") + "\n```")
    return 0


def cmd_bootstrap(ctx, args):
    from . import bootstrap
    bootstrap.main(Path(args.file) if args.file else ctx.config.space_file)
    return 0


def cmd_render(ctx, args):
    env = ctx.config.env(args.env)
    rendered = S.render(S.load(args.file or ctx.config.space_file), env.catalog)
    out = Path(args.out or ROOT / "build" / f"{ctx.config.space_file.stem}.{env.name}.json")
    S.save(rendered, out)
    print(f"wrote {out} for catalog {env.catalog}")
    return 0


def cmd_pull(ctx, args):
    env = ctx.config.env(args.env)
    out = Path(args.file or ctx.config.space_file)
    changes = ops.pull(ctx.ws(env), env, out)
    _print_list(f"pulled {env.name} into {out}; changes versus the file before:", changes)
    return 0


def cmd_snapshot(ctx, args):
    env = ctx.config.env(args.env)
    rec = ops.snapshot(ctx.ws(env), env, ctx.history(env), actor=_actor())
    if rec is None:
        print(f"{env.name}: no change since the last recorded version")
    else:
        _print_list(f"{env.name}: recorded {rec['record_id']}", rec.get("changes", []))
    return 0


def cmd_plan(ctx, args):
    env = ctx.config.env(args.env)
    p = ops.plan(ctx.ws(env), env, S.load(args.file or ctx.config.space_file), ctx.history(env))
    print(f"{env.name}: {p['action']} (live {p['live_hash']} -> new {p['new_hash']})")
    _print_list("changes:", p["changes"])
    _print_list("drift since the last release (edits made outside the pipeline):", p["drift"])
    _summary(f"### Genie plan for `{env.name}`: {p['action']}\n\n```diff\n" + "\n".join(p["changes"]) + "\n```"
             + (f"\n\n**Drift** (edited outside the pipeline):\n```diff\n" + "\n".join(p["drift"]) + "\n```" if p["drift"] else ""))
    return 0


def cmd_deploy(ctx, args):
    env = ctx.config.env(args.env)
    space = S.load(args.file or ctx.config.space_file)
    sha = args.git_sha or _git_sha()
    version = args.version or f"{datetime.now(timezone.utc):%Y%m%d.%H%M}-{sha or S.content_hash(space)}"
    rec = ops.deploy(ctx.ws(env), env, space, ctx.history(env), config=ctx.config, version=version, git_sha=sha,
                     actor=_actor(), allow_drift=args.allow_drift)
    _summary(f"### Deployed Genie space to `{env.name}`\n\nversion `{version}`, content `{rec['content_hash']}`, "
             f"space `{rec['space_id']}`, backup `{rec.get('backup')}`")
    return 0


def cmd_gate(ctx, args):
    env = ctx.config.env(args.env)
    res = ops.gate(ctx.ws(env), env, ctx.history(env))
    for r in res["results"]:
        if r["assessment"] == "BAD" or args.verbose:
            print(f"    {r['assessment']:<12} {r['question'][:70]}  {r.get('reason', '')[:160]}")
    if res["num_needs_review"] and not args.verbose:
        print(f"    ({res['num_needs_review']} benchmark(s) need human review; --verbose lists them)")
    _summary(f"### Benchmark gate on `{env.name}`: {'✅ PASS' if res['passed'] else '❌ FAIL'}\n\n"
             f"{res['num_correct']}/{res['graded']} graded correct, {res['num_bad']} bad, {res['num_needs_review']} need review\n\n"
             + "\n".join(f"- {r['assessment']}: {r['question']}" for r in res["results"] if r["assessment"] != "GOOD"))
    return 0 if res["passed"] else 1


def cmd_smoke(ctx, args):
    env = ctx.config.env(args.env)
    return 0 if ops.smoke(ctx.ws(env), env)["passed"] else 1


def cmd_promote(ctx, args):
    src, dst = ctx.config.env(args.source), ctx.config.env(args.dest)
    ops.promote(ctx.ws(src), src, ctx.history(src), ctx.ws(dst), dst, ctx.history(dst), config=ctx.config,
                actor=_actor(), allow_drift=args.allow_drift)
    return 0


def cmd_rollback(ctx, args):
    env = ctx.config.env(args.env)
    rec = ops.rollback(ctx.ws(env), env, ctx.history(env), to=args.to, config=ctx.config, actor=_actor())
    _summary(f"### Rolled back `{env.name}`\n\nnow at content `{rec['content_hash']}` ({rec.get('note')})")
    return 0


def cmd_history(ctx, args):
    env = ctx.config.env(args.env)
    rows = ctx.history(env).table(env.name)
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    print(f"{'record_id':<52} {'kind':<9} {'version':<26} {'actor':<12} note")
    for r in rows:
        print(f"{r['record_id']:<52} {r['kind']:<9} {str(r.get('version') or ''):<26} {str(r.get('actor') or ''):<12} {r.get('note') or ''}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m genie_cicd", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ops.DEFAULT_CONFIG))
    ap.add_argument("--target", choices=["databricks", "sim"], default=os.environ.get("GENIE_CICD_TARGET", "databricks"))
    ap.add_argument("--sim-dir", default=os.environ.get("GENIE_CICD_SIM_DIR", str(ROOT / ".genie_sim")))
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, env=True, file=True):
        p = sub.add_parser(name)
        if env:
            p.add_argument("--env", required=True)
        if file:
            p.add_argument("--file", help="space file (default: the one in environments.yml)")
        p.set_defaults(fn=fn)
        return p

    add("validate", cmd_validate, env=False).add_argument("--sql", action="store_true", help="also run every SQL locally")
    add("fmt", cmd_fmt, env=False)
    add("diff", cmd_diff, env=False).add_argument("--base", required=True, help="the space file to compare against")
    add("bootstrap", cmd_bootstrap, env=False)
    add("render", cmd_render).add_argument("--out")
    add("pull", cmd_pull)
    add("snapshot", cmd_snapshot, file=False)
    add("plan", cmd_plan)
    p = add("deploy", cmd_deploy)
    p.add_argument("--version"); p.add_argument("--git-sha"); p.add_argument("--allow-drift", action="store_true")
    add("gate", cmd_gate, file=False).add_argument("--verbose", action="store_true")
    add("smoke", cmd_smoke, file=False)
    p = add("promote", cmd_promote, env=False, file=False)
    p.add_argument("--from", dest="source", required=True); p.add_argument("--to", dest="dest", required=True)
    p.add_argument("--allow-drift", action="store_true")
    add("rollback", cmd_rollback, file=False).add_argument("--to", default="previous")
    add("history", cmd_history, file=False).add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    try:
        return args.fn(Context(args), args)
    except ops.DriftError as e:
        _print_list(f"ERROR: {e}", e.changes)
        _summary(f"### ❌ Drift detected\n\n{e}\n\n```diff\n" + "\n".join(e.changes) + "\n```")
        return 2
    except ops.DeployError as e:
        print(f"ERROR: {e}")
        _summary(f"### ❌ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
