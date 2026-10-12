from __future__ import annotations

import argparse
import asyncio
import importlib.resources
import json
import os
from pathlib import Path

from .check import check
from .github import Abstain, GitHub, collect
from .publish import publication_allowed, publish


def load(path):
    path = Path(path)
    if path.stat().st_size > 12_000_000:
        raise Abstain("Input file exceeds 12 MB")
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


async def propose(bundle, model, intent_only=False):
    from nfv.shared.copilot_client import CopilotChat

    resource = "intent_prompt.txt" if intent_only else "proposal_prompt.txt"
    prompt = importlib.resources.files("nfv_workflow").joinpath(resource).read_text()
    async with CopilotChat(model=model, concurrency=1) as chat:
        answer = await chat.ask(prompt, json.dumps(bundle), timeout=240)
        raw = answer.text.strip()
        if raw.startswith("```json") and raw.endswith("```"):
            raw = raw[7:-3].strip()
        return json.loads(raw)


async def run_general(bundle, properties, output, model, limits):
    from .general import run, select_properties
    if not select_properties(properties, bundle):
        return await run(bundle, [], output, None, limits)
    if not model:
        raise Abstain("General translation/repair requires an explicitly configured model")
    from nfv.shared.config import DAFNY_PATH
    if not DAFNY_PATH:
        raise Abstain("Install Dafny before starting billable model translation")
    from nfv.shared.copilot_client import CopilotChat
    # In Actions, the repository token is for collection/publication, not model
    # identity. The dedicated Copilot token authenticates this tool-free client.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        if not os.environ.get("COPILOT_GITHUB_TOKEN"):
            raise Abstain("Actions general inference requires a dedicated COPILOT_GITHUB_TOKEN")
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("GITHUB_TOKEN", None)
    async with CopilotChat(model=model, concurrency=1) as chat:
        return await run(bundle, properties, output, chat, limits)


def submission(path):
    data = load(path)
    items = [x for x in data.get("items", []) if x.get("type") == "nfv_submit"]
    if len(items) != 1 or not isinstance(items[0].get("proposals"), str):
        raise Abstain("Exactly one nfv_submit item containing a JSON string is required")
    return json.loads(items[0]["proposals"])


def main(argv=None):
    parser = argparse.ArgumentParser(description="Conditional Dafny model refutations; no source execution")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("collect", help="Capture closed-event source and commentary using read-only API")
    p.add_argument("--event", required=True)
    p.add_argument("--out", required=True)
    p = commands.add_parser("propose", help="Optional strict scalar-expression proposals (billable)")
    p.add_argument("--bundle", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--out", required=True)
    p = commands.add_parser("extract", help="Extract intended properties without bug-likelihood triage")
    p.add_argument("--bundle", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--out", required=True)
    p = commands.add_parser("run", help="General multi-language Dafny-only translation and refutation")
    p.add_argument("--bundle", required=True)
    p.add_argument("--properties", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--syntax-rounds", type=int, default=6)
    p.add_argument("--proof-rounds", type=int, default=10)
    p.add_argument("--verifier-timeout", type=int, default=60)
    p = commands.add_parser("check", help="Offline strict scalar-path checking; no inference or publication")
    p.add_argument("--bundle", required=True)
    p.add_argument("--proposals", required=True)
    p.add_argument("--out", required=True)
    p = commands.add_parser("evaluate", help="Fresh trusted collection and checking of AW output")
    p.add_argument("--event", required=True)
    p.add_argument("--submission", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--mode", choices=("general", "scalar"), default="general")
    p.add_argument("--model", default=os.environ.get("NFV_MODEL", ""))
    p.add_argument("--syntax-rounds", type=int, default=6)
    p.add_argument("--proof-rounds", type=int, default=10)
    p.add_argument("--verifier-timeout", type=int, default=60)
    p = commands.add_parser("publish", help="Publish locally checked evidence; preview by default")
    p.add_argument("--bundle", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--evidence-url", required=True)
    p.add_argument("--category", required=True)
    p.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            save(args.out, collect(load(args.event), GitHub()))
        elif args.command == "propose":
            save(args.out, asyncio.run(propose(load(args.bundle), args.model)))
        elif args.command == "extract":
            save(args.out, asyncio.run(propose(load(args.bundle), args.model, intent_only=True)))
        elif args.command == "run":
            from .general import Limits
            limits = Limits(args.syntax_rounds, args.proof_rounds, args.verifier_timeout)
            report = asyncio.run(run_general(load(args.bundle), load(args.properties), args.out, args.model, limits))
            print(json.dumps({"findings": len(report["findings"]), "errors": report["errors"]}))
            if report["errors"]:
                return 1
        elif args.command == "check":
            report = check(load(args.bundle), load(args.proposals), args.out)
            print(json.dumps({"findings": len(report["findings"]), "errors": report["errors"]}))
            if report["errors"]:
                return 1
        elif args.command == "evaluate":
            output = Path(args.out)
            output.mkdir(parents=True, exist_ok=True)
            bundle = collect(load(args.event), GitHub())
            proposals = submission(args.submission)
            save(output / "bundle.json", bundle)
            save(output / "proposals.json", proposals)
            if args.mode == "general":
                from .general import Limits
                limits = Limits(args.syntax_rounds, args.proof_rounds, args.verifier_timeout)
                report = asyncio.run(run_general(bundle, proposals, output, args.model, limits))
            else:
                report = check(bundle, proposals, output)
            has_findings = bool(report["findings"]) and not report["errors"]
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
                    stream.write(f"has_findings={str(has_findings).lower()}\n")
            print(json.dumps({"findings": len(report["findings"]), "errors": report["errors"]}))
            if report["errors"]:
                return 1
        elif args.command == "publish":
            bundle, report = load(args.bundle), load(args.report)
            # The AW publisher only consumes files generated on its own trusted
            # runner, never a report or source bundle supplied by the agent.
            api = GitHub()
            apply = args.apply and publication_allowed(api)
            result = publish(api, report, bundle, args.evidence_url, args.category, apply)
            if result.get("body"):
                Path(args.report).with_suffix(".md").write_text(result["body"], encoding="utf-8")
                result.pop("body")
            print(json.dumps(result))
    except (Abstain, OSError, KeyError, TypeError, ValueError, ImportError, RuntimeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
