#!/usr/bin/env python3
"""
Resumable, append-only JSONL result store.

Runs are expensive (232 entries x backends x models x agent loops), so every
entry is flushed as soon as it completes and re-runs skip anything already
recorded. Append-only means a crashed run never corrupts earlier results.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterator


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def done_ids(self) -> set[str]:
        if not self.path.exists():
            return set()
        ids: set[str] = set()
        for rec in self.read():
            eid = rec.get("entry_id")
            if eid:
                ids.add(eid)
        return ids

    def read(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a torn final line from a killed run

    def append(self, record: dict[str, Any]) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()

    def records(self) -> list[dict[str, Any]]:
        """Latest record per entry_id (a re-run supersedes an earlier attempt)."""
        latest: dict[str, dict[str, Any]] = {}
        for rec in self.read():
            eid = rec.get("entry_id")
            if eid:
                latest[eid] = rec
        return list(latest.values())
