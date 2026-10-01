"""Version history of every environment, stored as one JSON file per event.

On Databricks the files live on a Unity Catalog volume, for example
`/Volumes/freshcart_ops/genie/history/<env>/`, so the history sits next to the data, is governed by
Unity Catalog grants and survives even if git or CI is unavailable during an incident.

Record kinds
    snapshot   the space as it was at a point in time (taken on a schedule in dev, catching UI edits)
    backup     the live space exactly as it was immediately before a deployment overwrote it
    deployed   a release that the pipeline deployed (version, git commit, who, when)
    rollback   a deployment that restored an earlier record

Every record holds the full environment-neutral space, so any record can be redeployed as is.
File names sort chronologically: <UTC timestamp>_<kind>_<content hash>.json
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import spec as S


class HistoryStore:
    def __init__(self, workspace, root: str, clock=None):
        self.ws = workspace
        self.root = root.rstrip("/")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last = None

    def _stamp(self) -> str:
        now = self._clock()
        if self._last is not None and now <= self._last:    # keep names unique and ordered within one process
            now = self._last + timedelta(microseconds=1)
        self._last = now
        return now.strftime("%Y%m%dT%H%M%S%fZ")

    def put(self, env: str, kind: str, space: dict, **meta) -> dict:
        h = S.content_hash(space)
        stamp = self._stamp()
        record_id = f"{stamp}_{kind}_{h}"
        record = {"record_id": record_id, "env": env, "kind": kind, "created_at": stamp, "content_hash": h,
                  **{k: v for k, v in meta.items() if v is not None}, "space": S.normalise(space)}
        self.ws.write_file(f"{self.root}/{env}/{record_id}.json", json.dumps(record, indent=1, sort_keys=True))
        return record

    def ids(self, env: str) -> list[str]:
        return [p.rsplit("/", 1)[-1][:-5] for p in self.ws.list_files(f"{self.root}/{env}") if p.endswith(".json")]

    def get(self, env: str, record_id: str) -> dict:
        matches = [i for i in self.ids(env) if i == record_id or i.startswith(record_id) or i.endswith(record_id)]
        if len(matches) != 1:
            raise KeyError(f"{record_id!r} matches {len(matches)} records in {env} history")
        return json.loads(self.ws.read_file(f"{self.root}/{env}/{matches[0]}.json"))

    def records(self, env: str, kinds: tuple[str, ...] | None = None) -> list[dict]:
        """All records, oldest first (with their spaces)."""
        out = [json.loads(self.ws.read_file(f"{self.root}/{env}/{i}.json")) for i in self.ids(env)]
        return [r for r in out if kinds is None or r["kind"] in kinds]

    def latest(self, env: str, kinds: tuple[str, ...]) -> dict | None:
        recs = self.records(env, kinds)
        return recs[-1] if recs else None

    def table(self, env: str) -> list[dict]:
        """One line per record, without the space itself: the audit trail."""
        keep = ("record_id", "kind", "content_hash", "version", "git_sha", "actor", "note")
        return [{k: r.get(k) for k in keep} for r in self.records(env)]
