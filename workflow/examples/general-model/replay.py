"""Offline, explicitly fake proposer responses with the real NFV/Dafny oracle."""

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
        if self.requests == 1:
            value = json.loads((ROOT / "translation.json").read_text(encoding="utf-8"))
        elif self.requests == 2:
            request = json.loads(prompt)
            after = next(int(line.split(":")[0]) for line in request["frozen_model_lines"]
                         if "while i < n" in line)
            value = {"annotations": [{"after": after, "text": "invariant r == i"}]}
        else:
            raise RuntimeError("Replay fixture expected exactly one annotation repair")
        return SimpleNamespace(text=json.dumps(value), model="offline-handwritten-fixture", api_calls=0)


async def main():
    bundle = json.loads((ROOT / "bundle.json").read_text(encoding="utf-8"))
    properties = json.loads((ROOT / "properties.json").read_text(encoding="utf-8"))
    report = await run(bundle, properties, ROOT / "evidence", ReplayProposer())
    if len(report["findings"]) != 1 or report["errors"]:
        raise RuntimeError(json.dumps(report))
    finding = report["findings"][0]
    if finding["counters"]["proof_repairs"] != 1:
        raise RuntimeError("The expected real negative-check repair did not occur")
    print("One conditional model refutation; one real annotation repair; zero inference calls.")


if __name__ == "__main__":
    asyncio.run(main())
