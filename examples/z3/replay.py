"""Offline replay on merged Z3 PR #11123: handwritten translation, real NFV/Dafny checks."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from nfv_workflow.general import run

ROOT = Path(__file__).resolve().parent


class ReplayProposer:
    def __init__(self):
        self.requests = 0

    async def ask(self, system, prompt, timeout):
        self.requests += 1
        if self.requests != 1:
            raise RuntimeError("Replay fixture provides only the initial translation")
        value = json.loads((ROOT / "translation.json").read_text(encoding="utf-8"))
        return SimpleNamespace(text=json.dumps(value), model="offline-handwritten-fixture", api_calls=0)


async def main():
    bundle = json.loads((ROOT / "bundle.json").read_text(encoding="utf-8"))
    properties = json.loads((ROOT / "properties.json").read_text(encoding="utf-8"))
    report = await run(bundle, properties, ROOT / "evidence", ReplayProposer())
    if len(report["findings"]) != 1 or report["errors"] or report["abstentions"]:
        raise RuntimeError(json.dumps({k: report[k] for k in ("findings", "abstentions", "errors")},
                                      default=str)[:4000])
    print("One conditional model refutation on merged Z3 PR #11123; zero inference calls.")


if __name__ == "__main__":
    asyncio.run(main())
