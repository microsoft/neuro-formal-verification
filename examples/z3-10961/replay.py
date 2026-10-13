"""Offline before/after replay of the live gpt-5.6-sol run on merged Z3 PR #10961.

The recorded model responses are fed back unchanged; every NFV gate and Dafny check reruns.
Expected: two refutations on the pre-fix base, none on the fixed head.
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from nfv_workflow.general import run

ROOT = Path(__file__).resolve().parent
EXPECTED = {"base": 2, "head": 0}


class RecordedModel:
    """Serves each property's recorded responses in their original order."""

    def __init__(self, side):
        self.side, self.queue = side, []

    async def ask(self, system, prompt, timeout):
        if prompt.startswith("Translate this intended property"):
            claim_id = re.search(r'"claim": \{[^{}]*"id": "([0-9a-f]{20})"', prompt)[1]
            path = ROOT / "transcripts" / self.side / f"{claim_id}.json"
            self.queue = json.loads(path.read_text(encoding="utf-8"))
        if not self.queue:
            raise RuntimeError("Replay needs more model responses than were recorded")
        record = self.queue.pop(0)
        return SimpleNamespace(text=record["response"], model=record.get("model", "recorded"), api_calls=0)


async def main():
    ok = True
    for side in ("base", "head"):
        bundle = json.loads((ROOT / f"bundle-{side}.json").read_text(encoding="utf-8"))
        properties = json.loads((ROOT / f"properties-{side}.json").read_text(encoding="utf-8"))
        report = await run(bundle, properties, ROOT / "evidence" / side, RecordedModel(side))
        print(f"{side}: {len(report['findings'])} refutation(s), {len(report['abstentions'])} abstention(s)")
        for finding in report["findings"]:
            print(f"  refuted  {finding['candidate']['id']}: {finding['candidate']['property']}")
        for item in report["abstentions"]:
            print(f"  abstain  {item['id']}: {item['reason'][:110]}")
        ok &= not report["errors"] and len(report["findings"]) == EXPECTED[side]
    if not ok:
        sys.exit("Replay outcome differs from the recorded live run")


if __name__ == "__main__":
    asyncio.run(main())
