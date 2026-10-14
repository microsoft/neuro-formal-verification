"""Scratch directories for verifier invocations, cleaned up after use.

Every backend writes the program to a temp directory before shelling out to the
tool. Those directories used to be created with `mkdtemp` and never removed, so a
single 206-entry run with 5 attempts and 10 repair rounds left tens of thousands
of them behind (48k observed), which is slow to enumerate and confusing to triage.

Only directories this module created are removed: when a caller passes an explicit
`workdir` it owns that directory and it is left alone. Set NFV_KEEP_SCRATCH=1 to
retain everything for debugging a specific failure.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import uuid
from pathlib import Path


@contextlib.contextmanager
def scratch_dir(workdir: str | Path | None, prefix: str):
    owned = workdir is None
    tmpdir = (Path(workdir) if workdir else
              Path.cwd() / ".nfv-scratch" / (prefix + uuid.uuid4().hex))
    tmpdir.mkdir(parents=True, exist_ok=True)
    try:
        yield tmpdir
    finally:
        if owned and os.environ.get("NFV_KEEP_SCRATCH", "") not in ("1", "true", "yes"):
            shutil.rmtree(tmpdir, ignore_errors=True)
