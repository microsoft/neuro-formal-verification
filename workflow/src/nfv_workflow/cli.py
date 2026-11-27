# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path

from . import read_prompt
from .check import check
from .github import Abstain, GitHub, Superseded, collect, reanchor, repository, sha
from .publish import LABEL, publication_allowed, publish


def load(path):
    path = Path(path)
    if path.stat().st_size > 12_000_000:
        raise Abstain("Input file exceeds 12 MB")
    return json.loads(path.read_text(encoding="utf-8"))


def collection_status(path):
    status = load(path)
    if (not isinstance(status, dict) or status.get("schema") != 1
            or status.get("kind") != "nfv-collection-attempt"
            or status.get("status") not in ("collected", "superseded")):
        raise Abstain("Invalid trusted collection status")
    ident = status.get("identity")
    if not isinstance(ident, dict) or type(ident.get("number")) is not int or ident["number"] < 1:
        raise Abstain("Invalid collection snapshot identity")
    repository(ident["repository"])
    for field in ("head_sha", "base_sha", "source_sha"):
        sha(ident[field])
    if status["status"] == "superseded" and (
            not isinstance(status.get("diagnostic"), str) or not status["diagnostic"].strip()):
        raise Abstain("Superseded collection requires an explicit diagnostic")
    return status


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def write_sources(bundle, directory):
    root = Path(directory).resolve()
    for path, record in bundle.get("sources", {}).items():
        target = (root / path).resolve()
        if root not in target.parents:
            raise Abstain(f"Unsafe source path: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Leading blank lines keep viewer line numbers equal to repository line numbers.
        padding = "\n" * (record.get("line_start", 1) - 1)
        target.write_text(padding + record["text"], encoding="utf-8")


def input_index(bundle):
    from .source_context import function_catalog
    return {**{key: bundle[key] for key in ("identity", "signals", "changed")},
            "function_catalog": function_catalog(bundle),
            "repository_context": bundle.get("repository_context", []),
            "captured_paths": sorted(bundle["sources"]),
            "context_omissions": bundle.get("context_omissions", [])}


async def propose(bundle, model, intent_only=False, output=None):
    async with inference_chat(model, "extraction") as chat:
        if intent_only:
            from .extraction import extract
            return await extract(bundle, chat, output=output)
        prompt = read_prompt("proposal_prompt.txt")
        from .general import json_answer
        return json_answer((await chat.ask(prompt, json.dumps(bundle), timeout=240)).text)


def inference_chat(model, stage):
    from .chat import WorkflowChat
    if not model:
        raise Abstain(f"{stage.capitalize()} requires an explicitly configured model")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        if not os.environ.get("COPILOT_GITHUB_TOKEN") and os.environ.get("GITHUB_AW") != "true":
            raise Abstain(f"Actions {stage} inference requires a dedicated COPILOT_GITHUB_TOKEN")
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("GITHUB_TOKEN", None)
    provider = awf_provider() if os.environ.get("GITHUB_AW") == "true" else None
    return WorkflowChat(model=model, concurrency=1, provider=provider)


async def run_general(bundle, properties, output, model, limits, followup_options=None):
    from .general import run, select_properties
    from .followups import Options, run as run_followups
    options = followup_options if followup_options is not None else Options.from_env()
    (Path(output) / "followups.json").unlink(missing_ok=True)
    if not select_properties(properties, bundle, rejected=[]):
        # Nothing valid to translate; run records each rejected property's audit failure.
        return await run(bundle, properties, output, None, limits)
    if not model:
        raise Abstain("General translation/repair requires an explicitly configured model")
    from nfv.shared.config import DAFNY_PATH
    if not DAFNY_PATH:
        raise Abstain("Install Dafny before starting billable model translation")
    async with inference_chat(model, "general") as chat:
        report = await run(bundle, properties, output, chat, limits)
        if report.get("completed") is True and not report["errors"] and report["findings"]:
            await run_followups(report, bundle, chat, output, limits, options)
        return report


def optional_artifact(report_path, name, explicit=None):
    path = Path(explicit) if explicit else Path(report_path).with_name(name)
    return load(path) if explicit or path.is_file() else None


def followup_arguments(parser):
    parser.add_argument("--simplify-refutations", action=argparse.BooleanOptionalAction, default=None,
                        help="Check and display shorter refuted properties (default: true)")
    parser.add_argument("--check-fixes", action=argparse.BooleanOptionalAction, default=None,
                        help="Check candidate repairs against the complete property (default: true)")


def repair_arguments(parser):
    parser.add_argument("--repair-model-validation", action=argparse.BooleanOptionalAction, default=None,
                        help="Repair draft model-validation failures; checks always remain enabled (default: true)")


def followup_options(args):
    from .followups import Options
    if args.simplify_refutations is None and args.check_fixes is None:
        return None
    env = Options.from_env()
    return Options(env.simplify if args.simplify_refutations is None else args.simplify_refutations,
                   env.fixes if args.check_fixes is None else args.check_fixes)


AWF_PLACEHOLDER_KEY = "dummy-byok-key-for-offline-mode"
# The AWF API proxy's Copilot route; the agent's shell does not inherit its URL.
AWF_COPILOT_PROXY = "http://api-proxy:10002"


def awf_provider():
    """BYOK provider for the nested Copilot client inside the gh-aw sandbox.

    The agent's shell has no Copilot credential; the AWF API proxy injects it,
    so a placeholder key suffices.
    """
    env = os.environ
    provider_type = env.get("COPILOT_PROVIDER_TYPE") or "openai"
    if provider_type not in ("openai", "anthropic", "azure"):
        raise Abstain("Unsupported AWF SDK provider type")
    provider = {"type": provider_type,
                "base_url": env.get("COPILOT_PROVIDER_BASE_URL") or env.get("COPILOT_API_URL") or AWF_COPILOT_PROXY,
                "api_key": AWF_PLACEHOLDER_KEY}
    if provider_type != "anthropic":
        provider["wire_api"] = env.get("COPILOT_PROVIDER_WIRE_API") or "responses"
    return provider


async def refresh_presentation(report, bundle, model, timeout):
    from .presentation import refresh, validate_report
    validate_report(report, bundle)
    async with inference_chat(model, "presentation") as chat:
        return await refresh(report, bundle, chat, timeout)


def submission(path):
    data = load(path)
    items = [x for x in data.get("items", []) if x.get("type") == "nfv_submit"]
    if len(items) != 1 or not isinstance(items[0].get("proposals"), str):
        raise Abstain("Exactly one nfv_submit item containing a JSON string is required")
    return json.loads(items[0]["proposals"])


BUDGET_VARIABLES = (("NFV_SYNTAX_ROUNDS", "syntax_rounds"), ("NFV_PROOF_ROUNDS", "proof_rounds"),
                    ("NFV_VERIFIER_TIMEOUT", "verifier_timeout"), ("NFV_PROPERTY_SECONDS", "entry_seconds"))


def audit_limits(env=None):
    """Repair/time budget from trusted-step environment variables; unset or empty keeps defaults."""
    from .general import Limits
    env = os.environ if env is None else env
    values = {}
    for variable, field in BUDGET_VARIABLES:
        raw = (env.get(variable) or "").strip()
        if raw:
            if not raw.isdigit():
                raise Abstain(f"{variable} must be a non-negative integer")
            values[field] = int(raw)
    raw = (env.get("NFV_REPAIR_MODEL_VALIDATION") or "").strip().lower()
    if raw:
        if raw not in ("true", "false"):
            raise Abstain("NFV_REPAIR_MODEL_VALIDATION must be true or false")
        values["repair_model_validation"] = raw == "true"
    try:
        return Limits(**values)
    except ValueError as exc:
        raise Abstain(str(exc)) from exc


def audit(proposals_path, limits, followup_options=None, *, render_report=True):
    """Audit entry point: all inputs are fixed by trusted collection/extraction steps."""
    from .general import Limits
    from .publish import comment
    bundle_path, model = os.environ.get("NFV_BUNDLE"), os.environ.get("NFV_MODEL", "")
    output = Path(os.environ.get("NFV_OUT") or "nfv-evidence")
    if not bundle_path:
        raise Abstain("NFV_BUNDLE is not set by a trusted collection step")
    proposals = load(proposals_path)
    if not isinstance(proposals, list):
        raise Abstain("Proposals must be a JSON array")
    output.mkdir(parents=True, exist_ok=True)
    for name in ("comment.md", "presentation.json", "presentation-diagnostics.json", "report.json", "followups.json"):
        (output / name).unlink(missing_ok=True)
    bundle = load(bundle_path)
    save(output / "bundle.json", bundle)
    save(output / "proposals.json", proposals)
    limits = limits if isinstance(limits, Limits) else Limits(*limits)
    extra = {"followup_options": followup_options} if followup_options is not None else {}
    report = asyncio.run(run_general(bundle, proposals, output, model, limits, **extra))
    if report["errors"] or report.get("completed") is not True:
        raise Abstain("NFV audit incomplete: " + "; ".join(report["errors"] or ["analysis interrupted"]))
    summary = {"findings": len(report["findings"]), "verified": len(report.get("verified", [])),
               "outcomes": {status: sum(item["status"] == status for item in report.get("properties", []))
                            for status in ("refuted", "verified", "resources exhausted", "type-check/audit failure")},
               "errors": report["errors"]}
    if not render_report:
        return summary
    presentation = None
    if report["findings"]:
        from .presentation import validate, validate_report
        validate_report(report, bundle)
        diagnostics = []
        for finding in report["findings"]:
            translation = finding["translation"]
            try:
                validate(translation.get("presentation"), translation, bundle)
            except Abstain as exc:
                diagnostics.append({"id": finding["candidate"]["id"], "diagnostic": str(exc)})
        if diagnostics:
            save(output / "presentation-diagnostics.json", diagnostics)
            presentation = asyncio.run(refresh_presentation(report, bundle, model, limits.model_timeout))
            save(output / "presentation.json", presentation)
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    evidence = f"{server}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    (output / "comment.md").write_text(
        comment(report, bundle, evidence, len(proposals), presentation=presentation,
                followups=optional_artifact(output / "report.json", "followups.json")), encoding="utf-8")
    summary["comment"] = str(output / "comment.md")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Conditional Dafny model analysis; no source execution")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("collect", help="Capture merged-PR event source and commentary using read-only API")
    p.add_argument("--event", required=True)
    p.add_argument("--event-snapshot", action="store_true",
                   help="Keep the event's immutable source and commit intent after PR updates or merge")
    p.add_argument("--out", required=True)
    p.add_argument("--side", choices=("head", "base"), default="head",
                   help="base: pre-merge snapshot anchored on removed lines (before/after analysis; not publishable)")
    p.add_argument("--sources-dir", help="Also write captured sources as plain files with repository line numbers")
    p.add_argument("--status-out", help="Record collection status; explicitly superseded snapshots skip without a bundle")
    p = commands.add_parser("context-manifest", help="Validate source-context session output; never execute source")
    p.add_argument("--bundle", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--hints-only", action="store_true",
                   help="Bind an agent-authored files list to the trusted bundle identity")
    p = commands.add_parser("enrich", help="Capture additional context by immutable blob SHA")
    p.add_argument("--bundle", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--sources-dir")
    p = commands.add_parser("reanchor", help="Map head-side properties onto a base-side bundle's removed lines")
    p.add_argument("--bundle", required=True)
    p.add_argument("--properties", required=True)
    p.add_argument("--out", required=True)
    p = commands.add_parser("propose", help="Optional strict scalar-expression proposals (billable)")
    p.add_argument("--bundle", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--out", required=True)
    p = commands.add_parser("extract", help="Postulate nontrivial properties from intent and source")
    p.add_argument("--bundle", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--out", required=True)
    p = commands.add_parser("run", help="Dafny-only verification followed by conditional model refutation")
    p.add_argument("--bundle", required=True)
    p.add_argument("--properties", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--syntax-rounds", type=int, default=6)
    p.add_argument("--proof-rounds", type=int, default=10)
    p.add_argument("--verifier-timeout", type=int, default=60)
    repair_arguments(p)
    followup_arguments(p)
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
    repair_arguments(p)
    followup_arguments(p)
    p = commands.add_parser("audit", help="Agent-sandbox run on the trusted bundle; writes a PR review")
    p.add_argument("--proposals", required=True)
    repair_arguments(p)
    followup_arguments(p)
    p = commands.add_parser("present", help="Agent reverse-mapping of accepted evidence; no proof rerun or publication")
    p.add_argument("--bundle", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--out", required=True, help="Directory for presentation.json and deterministic comment.md")
    p.add_argument("--evidence-url", required=True)
    p.add_argument("--model-timeout", type=int, choices=range(1, 601), default=240, metavar="1..600")
    p.add_argument("--followups", help="Saved checked follow-ups (default: beside the report); no proof reruns")
    p = commands.add_parser("publish", help="Publish locally checked evidence; preview by default")
    p.add_argument("--bundle", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--evidence-url", required=True)
    p.add_argument("--category", required=True)
    p.add_argument("--label", default=LABEL, help="Existing repository label applied to created Discussions")
    p.add_argument("--repository", default=None,
                   help="Repository for the Discussion (default: the PR repository)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--followups", help="Saved checked follow-ups (default: beside the report)")
    p.add_argument("--presentation", help="Saved source presentation (default: beside the report)")
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            try:
                bundle = collect(load(args.event), GitHub(), side=args.side,
                                 event_snapshot=args.event_snapshot)
            except Superseded as exc:
                if not args.status_out:
                    raise
                status = {"schema": 1, "kind": "nfv-collection-attempt", "status": "superseded",
                          "identity": exc.identity, "diagnostic": str(exc)}
                save(args.status_out, status)
                print(json.dumps(status))
            else:
                save(args.out, bundle)
                if args.sources_dir:
                    write_sources(bundle, args.sources_dir)
                if args.status_out:
                    save(args.status_out, {"schema": 1, "kind": "nfv-collection-attempt",
                                           "status": "collected", "identity": bundle["identity"]})
        elif args.command == "context-manifest":
            from .repository_context import sanitize_manifest
            sanitize_manifest(load(args.bundle), args.manifest, args.out, hints_only=args.hints_only)
        elif args.command == "enrich":
            from .repository_context import enrich
            bundle = enrich(load(args.bundle), load(args.manifest), GitHub())
            save(args.out, bundle)
            if args.sources_dir:
                write_sources(bundle, args.sources_dir)
        elif args.command == "reanchor":
            result = reanchor(load(args.properties), load(args.bundle))
            save(args.out, result["properties"])
            print(json.dumps({"properties": len(result["properties"]), "dropped": result["dropped"]}))
        elif args.command == "propose":
            save(args.out, asyncio.run(propose(load(args.bundle), args.model)))
        elif args.command == "extract":
            save(args.out, asyncio.run(propose(load(args.bundle), args.model, intent_only=True,
                                              output=Path(args.out).parent)))
        elif args.command == "run":
            from .general import Limits
            limits = Limits(args.syntax_rounds, args.proof_rounds, args.verifier_timeout,
                            repair_model_validation=args.repair_model_validation is not False)
            options = followup_options(args)
            extra = {"followup_options": options} if options is not None else {}
            report = asyncio.run(run_general(load(args.bundle), load(args.properties), args.out, args.model, limits, **extra))
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
                limits = Limits(args.syntax_rounds, args.proof_rounds, args.verifier_timeout,
                                repair_model_validation=args.repair_model_validation is not False)
                options = followup_options(args)
                extra = {"followup_options": options} if options is not None else {}
                report = asyncio.run(run_general(bundle, proposals, output, args.model, limits, **extra))
            else:
                report = check(bundle, proposals, output)
            has_findings = bool(report["findings"]) and not report["errors"]
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
                    stream.write(f"has_findings={str(has_findings).lower()}\n")
            print(json.dumps({"findings": len(report["findings"]), "errors": report["errors"]}))
            if report["errors"]:
                return 1
        elif args.command == "audit":
            limits = audit_limits()
            if args.repair_model_validation is not None:
                limits = replace(limits, repair_model_validation=args.repair_model_validation)
            summary = audit(args.proposals, limits, followup_options(args))
            print(json.dumps(summary))
            if summary["errors"]:
                return 1
        elif args.command == "present":
            from .presentation import validate_report
            from .publish import checked_findings, render
            bundle, report = load(args.bundle), load(args.report)
            # Validate evidence and publication inputs before making a billable request.
            validate_report(report, bundle)
            if checked_findings(report, bundle, args.evidence_url) is None:
                raise Abstain("No accepted findings to present")
            saved_followups = optional_artifact(args.report, "followups.json", args.followups)
            if saved_followups is not None:
                from .followups import validate_artifact
                validate_artifact(saved_followups, report, bundle)
            output = Path(args.out)
            targets = [output / "presentation.json", output / "comment.md"]
            if any(target.resolve() in {Path(args.report).resolve(), Path(args.bundle).resolve()}
                   for target in targets):
                raise Abstain("Presentation output cannot overwrite original evidence")
            artifact = asyncio.run(refresh_presentation(report, bundle, args.model, args.model_timeout))
            body = render(report, bundle, args.evidence_url, presentation=artifact,
                          followups=saved_followups)
            targets[0].parent.mkdir(parents=True, exist_ok=True)
            targets[0].write_text(json.dumps(artifact, indent=2), encoding="utf-8", newline="\n")
            targets[1].write_text(body, encoding="utf-8", newline="\n")
            print(json.dumps({"presentation": str(targets[0]), "comment": str(targets[1]),
                              "findings": len(artifact["findings"]), "proof_reruns": 0}))
        elif args.command == "publish":
            bundle, report = load(args.bundle), load(args.report)
            # The AW publisher only consumes files generated on its own trusted
            # runner, never a report or source bundle supplied by the agent.
            api = GitHub()
            apply = args.apply and publication_allowed(api)
            result = publish(api, report, bundle, args.evidence_url, args.category, apply, args.label,
                             args.repository,
                             presentation=optional_artifact(args.report, "presentation.json", args.presentation),
                             followups=optional_artifact(args.report, "followups.json", args.followups))
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
