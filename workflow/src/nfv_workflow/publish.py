# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Only trusted deterministic code renders and publishes accepted findings."""

from __future__ import annotations

import hashlib
import html
import json
import base64
import os
import re
from urllib.parse import quote

from .check import accepted
from .github import Abstain, repository, sha
from .model import expression, render_model, select_candidates
from .source_binding import bind_source


def text(value):
    # Do not turn an untrusted explanation into HTML, mentions, hidden metadata,
    # arbitrary links, or a GitHub Markdown code-fence escape.
    value = html.escape(str(value), quote=False).replace("@", "@\u200b")
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", value)


def dafny_block(source):
    longest = max((len(x) for x in re.findall(r"`+", source)), default=0)
    fence = "`" * max(3, longest + 1)
    return fence + "dafny\n" + source.rstrip() + "\n" + fence


LABEL = "NFV-formal-review"
COMMENT_LIMIT = 60000
LINK_LIMIT = 50
COMMENT_URL = re.compile(r"https?://[^\s]+")


def comment_fits(body):
    # gh-aw counts URL occurrences even inside code spans and fences.
    return len(body) <= COMMENT_LIMIT and len(COMMENT_URL.findall(body)) <= LINK_LIMIT


def marker(identity):
    # One publication per PR source snapshot, across reruns and attempts.
    key = ":".join(str(identity[k]) for k in
                   ("repository", "number", "base_sha", "source_sha"))
    return "<!-- nfv-merged-pr:" + hashlib.sha256(key.encode()).hexdigest() + " -->"


def publication_allowed(api):
    if os.environ.get("GH_AW_SAFE_OUTPUTS_STAGED") == "true":
        return False
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return True
    # gh-aw 0.88.7 does not propagate the staged flag into custom safe jobs.
    # Read its value from the immutable *executing workflow*, never agent data.
    repo = repository(os.environ.get("GITHUB_REPOSITORY"))
    revision = sha(os.environ.get("GITHUB_WORKFLOW_SHA"))
    reference = os.environ.get("GITHUB_WORKFLOW_REF", "")
    if not reference.startswith(repo + "/.github/workflows/") or "@" not in reference:
        raise Abstain("Cannot identify the executing trusted workflow")
    path = reference[len(repo) + 1:].split("@", 1)[0]
    response = api.request(f"repos/{repo}/contents/{quote(path, safe='/')}?ref={revision}")
    if response.get("encoding") != "base64":
        raise Abstain("Cannot inspect workflow staged mode")
    source = base64.b64decode(response["content"]).decode("utf-8")
    modes = re.findall(r'(?m)^\s+GH_AW_INFO_STAGED:\s*["\']?(true|false)["\']?\s*$', source)
    if not modes:
        raise Abstain("Executing workflow has no compiler-generated staged-mode indicator")
    return all(mode == "false" for mode in modes)


PAPER = "https://arxiv.org/abs/2608.21516"
INTRO = (f"[Neuro-formal verification (NFV)]({PAPER}) extracts the intended properties stated "
         "in a PR, translates the changed code to Dafny, and asks the verifier for a concrete "
         "input that violates each property. See the paper for how to read its findings.")
FENCES = {"c": "c", "cpp": "cpp", "java": "java", "csharp": "csharp", "go": "go",
          "javascript": "javascript", "typescript": "typescript", "python": "python", "rust": "rust"}


def code_span(value):
    # Code spans render literally; only backticks and line breaks could escape them.
    return "`" + " ".join(str(value).replace("`", "'").split()) + "`"


def fenced(language, body):
    longest = max((len(x) for x in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{body.rstrip()}\n{fence}"


def source_link(path, low, high, repo, commit, linked=True):
    label = f"{code_span(path)} lines {low}-{high}"
    if not linked:
        return label
    url = f"https://github.com/{repo}/blob/{commit}/{quote(path, safe='/')}#L{low}-L{high}"
    return f"[{label}]({url})"


def source_block(bundle, path, low, high, language, repo, commit, notes=None):
    """A linked, line-numbered excerpt of the PR's own source."""
    record = bundle["sources"][path]
    first = record.get("line_start", 1)
    source = record["text"].splitlines()
    low, high = max(low, first), min(high, first + len(source) - 1)
    width = len(str(high))
    body = []
    comment = "#" if language == "python" else "//"
    for n in range(low, high + 1):
        code = source[n - first].rstrip()
        body.append(f"{n:>{width}}  {code}")
        for note in (notes or {}).get((path, n), []):
            indent = " " * (width + 2 + len(code) - len(code.lstrip()))
            body.append(f"{indent}{comment} NFV: {' '.join(note.split())}")
    return source_link(path, low, high, repo, commit), fenced(language, "\n".join(body))


def source_calls(finding):
    """Connect helper calls to their source-tagged declarations, without executing code."""
    declarations, calls = {}, []
    owner = None
    for row in finding["source_map"]:
        declaration = re.match(r"(?:ghost\s+)?(?:method|function|predicate|lemma)\s+(\w+)\s*\(",
                               row["model"])
        if declaration:
            owner = declaration[1]
            declarations[owner] = row
        else:
            calls.append((owner, row))
    helpers = {item["name"] for item in finding["translation"].get("environment", [])}
    result, seen = [], set()
    for owner, row in calls:
        code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*', "", row["model"])
        if re.match(r"\s*(?:assert|invariant|decreases|requires|ensures)\b", code):
            continue
        for name in sorted(helpers):
            key = (owner, row["path"], row["line"], name)
            if re.search(r"\b" + re.escape(name) + r"\s*\(", code) and key not in seen:
                result.append((declarations[owner], row, declarations[name]))
                seen.add(key)
    return declarations, result


def relevant_code(claim, translation, finding, bundle, repo, commit, display_property=None):
    """The changed code and each called helper the check relied on, each headed by its
    natural-language specification. The refutation is a counterexample input, not a trace."""
    language = FENCES.get(claim["language"], "")
    declarations, calls = source_calls(finding)
    out, notes = ["**Relevant code**"], {}
    for note in translation.get("source_notes", []):
        if note.get("kind") in ("invariant", "spec"):
            label = "Invariant" if note["kind"] == "invariant" else "Spec"
            notes.setdefault((note["path"], note["line"]), []).append(f"{label}: {note['note']}")
    if calls:
        out.append("**Call relationships (static call sites):**")
        for caller, site, callee in calls:
            caller_link = source_link(caller["path"], caller["line"], caller["line"], repo, commit)
            callee_link = source_link(callee["path"], callee["line"], callee["line"], repo, commit)
            site_link = source_link(site["path"], site["line"], site["line"], repo, commit)
            out.append(f"- {code_span(caller['source'])} ({caller_link}) calls "
                       f"{code_span(callee['source'])} ({callee_link}); call site: {site_link}.")
            notes.setdefault((site["path"], site["line"]), []).append(
                f"Call site for the helper at {callee['path']}:{callee['line']}; see helper definition.")
    span = translation.get("source_span") or [min(claim["lines"]), max(claim["lines"])]
    spec = ["Spec (requires): " + translation["assumptions"][i]["description"]
            for i in finding["retained_assumptions"]]
    spec.append("Spec (ensures, intended): " + (display_property or claim["property"]))
    notes.setdefault((claim["path"], span[0]), [])[:0] = spec
    link, block = source_block(bundle, claim["path"], span[0], span[1], language, repo, commit, notes)
    entry = declarations[translation["entry_point"]]
    out.extend([f"**Changed code** — {link}\n"
                f"Function: {code_span(entry['source'])}", block])
    for helper in translation.get("environment", []):
        origin = helper["source"]
        lines = origin.get("lines") or []
        declaration = declarations[helper["name"]]
        low, high = min(lines), max(lines)
        spec_line = low
        if declaration["path"] == origin["path"]:
            low, high = min(low, declaration["line"]), max(high, declaration["line"])
            spec_line = declaration["line"]
        notes.setdefault((origin["path"], spec_line), []).insert(0, "Spec: " + helper["description"])
        link, block = source_block(bundle, origin["path"], low, high, language, repo, commit, notes)
        out.extend([f"**Helper definition** — {link}\n"
                    f"Function: {code_span(declaration['source'])}",
                    block])
    return out


def followup_lines(followup, compact=False):
    if not followup:
        return []
    lines = []
    simple, fix = followup["simplification"], followup["fix"]
    if simple["status"] == "not-established":
        lines.append("A shorter checked property was not established; diagnostics are in `followups.json`.")
    if fix["status"] == "checked":
        lines.extend([
            "**refutation-with-fix (model-checked candidate)**",
            "**Suggested fix:** " + text(fix["description"]),
        ])
        if compact:
            lines.append("The suggested source patch and model checks are retained in the linked run artifact.")
        else:
            lines.append(fenced("diff", fix["patch"]))
        lines.extend([
            "**Model checks:** The patched model verifies the property with unchanged input assumptions "
            "and helper contracts. The counterexample input remains feasible.",
            "**Limits:** The source patch is not applied, compiled or source-verified. " + text(fix["caveat"]),
        ])
    elif fix["status"] == "not-established":
        lines.append("No model-checked fix was established; diagnostics are in `followups.json`.")
    return lines


def general_section(index, finding, bundle, repo, commit, presentation=None, compact=False, followup=None):
    from .general import validate_finding
    validate_finding(finding, bundle)
    claim, translation = finding["candidate"], finding["translation"]
    has_presentation = presentation is not None or "presentation" in translation
    presentation = presentation if presentation is not None else translation.get("presentation")
    explanation = translation["explanation"]
    mapping_details = []
    if has_presentation:
        from .presentation import validate
        validate(presentation, translation, bundle)
        mapped_inputs, abstract_inputs = [], []
        for key, value in sorted(translation["witness"].items()):
            item = presentation["inputs"][key]
            if item["kind"] in ("expression", "per-element"):
                shown = code_span(f"{item['expression']} = {json.dumps(value)}")
                if item["kind"] == "per-element":
                    abstract_inputs.append("per-element results of " + shown)
                else:
                    mapped_inputs.append(shown)
                links = [source_link(row["path"], row["line"], row["line"], repo, commit, linked=not compact)
                         for row in item["anchors"]]
                mapping_details.append(f"- {code_span(key)} maps to {code_span(item['expression'])}: "
                                       + ", ".join(links)
                                       + (" (abstract per-element predicate results)" if item["kind"] == "per-element" else ""))
            else:
                abstract_inputs.append(f"{text(item['label'])} = {code_span(json.dumps(value))}")
                mapping_details.append(f"- {code_span(key)}: abstract input, {text(item['label'])} "
                                       f"= {code_span(json.dumps(value))}. {text(item['reason'])}")
        if abstract_inputs:
            mapped_inputs.append("abstract state: " + "; ".join(abstract_inputs))
        inputs = "; ".join(mapped_inputs)
        explanation = presentation["explanation"]
        failure = presentation["failure"]
        mapping_details.extend([
            f"Source-level failure expression: {code_span(failure['expression'])}; "
            + ", ".join(source_link(row["path"], row["line"], row["line"], repo, commit, linked=not compact)
                        for row in failure["anchors"]),
            "The checked witness and proof evidence are unchanged.",
        ])
    else:
        inputs = ", ".join(code_span(f"{k} = {json.dumps(v)}") for k, v in sorted(translation["witness"].items()))
    shown = (followup["simplification"]["property"]
             if followup and followup["simplification"]["status"] == "checked" else claim["property"])
    lines = [
        f"\n### {index}. Potential issue",
        f"**{'Inferred property' if claim.get('origin') == 'source' else 'Intended property'}:** {text(shown)}",
        "> " + text(" ".join(str(claim["quote"]).split())),
        f"**Input on which it fails:** {inputs}",
        f"**Sketch of the failure:** {text(explanation)}",
        *followup_lines(followup, compact),
    ]
    if compact:
        conditions = [translation["assumptions"][i]["description"]
                      for i in finding["retained_assumptions"]]
        span = translation["source_span"]
        return lines + [
            "**Retained conditions:** " + text("; ".join(conditions) or "No additional input restrictions."),
            "**Environment models:** " + text("; ".join(
                helper["description"] for helper in translation.get("environment", [])) or "None."),
            "**Source:** " + source_link(claim["path"], *span, repo, commit),
            *mapping_details,
            f"Evidence: {code_span(finding['evidence'])}; SHA-256 {code_span(finding['dafny_sha256'])}.",
            "Source, specifications and full Dafny are retained in the linked run artifact.",
        ]
    lines.extend([
        "<details><summary>Details</summary>\n",
        *relevant_code(claim, translation, finding, bundle, repo, commit, shown),
    ])
    removed = [value["description"] for i, value in enumerate(translation["assumptions"])
               if i not in finding["retained_assumptions"]]
    details = ["<details><summary>Minimized Dafny</summary>\n"]
    if has_presentation:
        details.extend(["**Source presentation (agent-inferred)**", *mapping_details])
    if removed:
        details.append("Dropped while minimizing: " + text("; ".join(removed)))
    details.extend([
        f"Repairs: {finding['counters']['syntax_repairs']} syntax, "
        f"{finding['counters']['proof_repairs']} annotation. "
        f"{code_span(finding['evidence'])}, SHA-256 {code_span(finding['dafny_sha256'])}.",
        dafny_block(finding["dafny"]),
        "</details>",
        "</details>",
    ])
    return lines + details


def checked_findings(report, bundle, evidence_url):
    ident = report["identity"]
    if ident != bundle["identity"]:
        raise Abstain("Report and source identity mismatch")
    if not (re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+(?:/artifacts/\d+)?",
                         evidence_url)
            or re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/(?:tree|blob)/[0-9a-f]{40}(?:/[\w./-]+)?",
                            evidence_url)):
        raise Abstain("Evidence URL must point to a GitHub Actions run/artifact or an immutable commit path")
    findings = report["findings"]
    if not findings or report.get("errors"):
        return None
    if ident.get("source_kind") != "pull-request-head":
        raise Abstain("Synthetic or non-PR source identities cannot be published")
    if ident.get("merged") and not ident.get("merge_sha"):
        raise Abstain("Merged pull request identity lacks its merge commit")
    if len(findings) > 10 or any(not accepted(x["result"]) for x in findings):
        raise Abstain("Unaccepted or excessive findings")
    return findings


def preamble(ident, evidence_url):
    line = (f"PR head `{sha(ident['source_sha'])[:12]}`, base `{sha(ident['base_sha'])[:12]}`"
            f" · [run and evidence]({evidence_url})")
    if ident.get("merged"):
        line += (f" · merged as `{sha(ident['merge_sha'])}`; this analyzes the saved head, "
                 "not the merge result")
    return [line]


def finding_lines(index, finding, bundle, repo, commit, presentation=None):
    if finding.get("kind") == "conditional-model-refutation":
        return general_section(index, finding, bundle, repo, commit, presentation)
    out = []
    candidate, model = finding["candidate"], finding["candidate"]["model"]
    if select_candidates([candidate], bundle)[0]["id"] != candidate["id"]:
        raise Abstain("Candidate identity changed after checking")
    binding = bind_source(candidate, bundle)
    expected_dafny = render_model(candidate, finding["retained_assumptions"])
    if (finding.get("source_binding") != binding or finding["dafny"] != expected_dafny
            or finding["dafny_sha256"] != hashlib.sha256(expected_dafny.encode()).hexdigest()):
        raise Abstain("Publication evidence does not match the source-bound checked model")
    predicate = expression(model["property"],
                           [x["name"] for x in model["parameters"]] + ["result"],
                           [x["name"] for x in model.get("environment", [])])
    failure = (f"The source-matched scalar calculation produces {json.dumps(binding['result'])}. "
               f"For these inputs, the displayed checked expectation is false.")
    path = candidate["path"]
    record = bundle["sources"][path]
    source_lines = record["text"].splitlines()
    first_line = record.get("line_start", 1)
    anchor = min(candidate["lines"])
    source_url = f"https://github.com/{repo}/blob/{commit}/{quote(path, safe='/')}#L{anchor}"
    out.extend([
        f"\n## {index}. {text(candidate['property'])}",
        f"**Quoted developer claim:** {text(candidate['quote'])}",
        f"**Inputs:** {text(json.dumps(model['witness'], sort_keys=True))}",
        f"**Checked expectation:** {text(predicate)}",
        f"**Why the checked model fails:** {text(failure)}",
        "**Conditional on:**",
        *("- " + text(condition) for condition in binding["conditions"]),
        "**Inferred input restrictions retained by the model (checked on these inputs):**",
    ])
    optional = model.get("assumptions", [])
    conditions = model.get("preconditions", []) + [
        optional[i] for i in finding["retained_assumptions"]]
    out.extend("- " + text(x["description"]) for x in conditions)
    if not conditions:
        out.append("- No additional input conditions are needed by the retained model.")
    contracts = binding["source_derived_callee_contracts"]
    if contracts:
        out.append("**Source-derived environment definitions (not assumed axioms):**")
    for contract in contracts:
        body_expression = expression(contract["body"],
                                     [p["name"] for p in contract["parameters"]],
                                     [x["function"] for x in contracts])
        out.append(f"- `{text(contract['function'])}` computes {text(body_expression)}; "
                   f"matched definition: {text(contract['definition'])}, "
                   f"declaration: {text(contract['declaration'] or 'same-file definition')}.")
    out.append(f"\n**Source overlay:** [immutable source]({source_url})")
    for line in candidate["lines"][:12]:
        out.append(f"- Line {line}: {text(source_lines[line - first_line])}")
    out.append("  - Model annotation: " + text(failure))
    out.extend([
        "<details><summary>Evidence and assumption details</summary>\n",
        f"Modeler's additional caveat (unverified): {text(candidate['correspondence'])}",
        f"Evidence file in the linked artifact: `{finding['evidence']}`.",
        f"SHA-256: `{finding['dafny_sha256']}`.",
        f"Optional function assumptions retained after deletion and recheck: "
        f"{len(finding['retained_assumptions'])}/{len(model.get('assumptions', []))}. "
        "This is greedy dependency minimization, not a globally smallest set. "
        "Original applicability checks remain; source correspondence and environment "
        "definitions are not mechanically minimized.",
        "Removed optional restrictions (still checked as facts about this witness): "
        + text("; ".join(x["description"] for i, x in enumerate(optional)
                         if i not in finding["retained_assumptions"]) or "none"),
        dafny_block(finding["dafny"]),
        "</details>",
    ])
    return out


def render(report, bundle, evidence_url, presentation=None, followups=None):
    if report.get("schema") == 3 and report.get("mode") == "general-model-analysis":
        return analysis_comment(report, bundle, evidence_url, presentation, followups)
    if followups is not None:
        raise Abstain("Follow-ups require whole-property general analysis evidence")
    mappings = {}
    if presentation is not None:
        from .presentation import validate_artifact
        mappings = validate_artifact(presentation, report, bundle)
    findings = checked_findings(report, bundle, evidence_url)
    if findings is None:
        return None
    ident = report["identity"]
    repo, commit = repository(ident["repository"]), sha(ident["source_sha"])
    count = f"{len(findings)} potential issue{'' if len(findings) == 1 else 's'}"
    header = [marker(ident), INTRO, f"# NFV: {count} in PR #{ident['number']}",
              *preamble(ident, evidence_url)]
    for index, finding in enumerate(findings, 1):
        header.extend(finding_lines(index, finding, bundle, repo, commit,
                                    mappings.get(finding["candidate"]["id"])))
    header.append("<!-- gh-aw-workflow-id: nfv-pr-review -->")
    body = "\n\n".join(header)
    if not comment_fits(body):
        raise Abstain("Publication exceeds bounded size or link count")
    return body


def comment(report, bundle, evidence_url, proposed, presentation=None, followups=None):
    """The PR comment: the findings report, or a short summary when nothing was refuted."""
    body = render(report, bundle, evidence_url, presentation=presentation, followups=followups)
    if body is not None:
        return body
    ident = report["identity"]
    abstentions, errors = report.get("abstentions", []), report.get("errors", [])
    lines = [marker(ident), INTRO, f"# NFV: no refutations in PR #{ident['number']}",
             *preamble(ident, evidence_url),
             f"Extracted {proposed} intended propert{'y' if proposed == 1 else 'ies'}; "
             f"{len(abstentions)} abstained and {len(errors)} errored. None was refuted, "
             "which is not evidence that the code is correct."]
    properties = report.get("properties", [])
    if properties:
        lines.append("## Intended properties checked")
    for index, item in enumerate(properties, 1):
        where = f"{item['path']}:{min(item['lines'])}" if item.get("lines") else item.get("path", "")
        entry = [f"{index}. **{text(item['property'])}** (`{text(where)}`) — {text(item['status'])}",
                 f"   - Quoted claim: {text(item['quote'])}"]
        if item.get("formalized"):
            entry.append("   - Formalized as:\n\n" + dafny_block("ensures " + str(item["formalized"])))
        lines.append("\n".join(entry))
    if abstentions or errors:
        lines.append("<details><summary>Abstentions and errors</summary>\n")
        lines.extend(f"- {text(x.get('id', '?'))}: {text(str(x.get('reason', ''))[:300])}" for x in abstentions[:10])
        lines.extend(f"- error: {text(str(x)[:300])}" for x in errors[:5])
        lines.append("</details>")
    lines.append("<!-- gh-aw-workflow-id: nfv-pr-review -->")
    body = "\n\n".join(lines)
    if not comment_fits(body):
        raise Abstain("Publication exceeds bounded size or link count")
    return body


def analysis_comment(report, bundle, evidence_url, presentation=None, followups=None):
    from .general import validate_verification
    from .outcomes import OUTCOMES, REFUTED, VERIFIED

    if report.get("completed") is False:
        raise Abstain("Analysis is incomplete; publication stopped")
    checked_findings(report, bundle, evidence_url)
    if report.get("errors"):
        raise Abstain("Analysis contains infrastructure errors; publication stopped")
    ident = report["identity"]
    if (report["findings"] or report.get("verified")) and ident.get("source_kind") != "pull-request-head":
        raise Abstain("Synthetic or non-PR source identities cannot be published")
    mappings = {}
    if presentation is not None:
        from .presentation import validate_artifact
        mappings = validate_artifact(presentation, report, bundle)
    enrichments = {}
    if followups is not None:
        from .followups import validate_artifact as validate_followups
        enrichments = validate_followups(followups, report, bundle)
    repo, commit = repository(ident["repository"]), sha(ident["source_sha"])
    verified = {item["candidate"]["id"]: item for item in report.get("verified", [])}
    for item in verified.values():
        validate_verification(item, bundle)
    findings = {item["candidate"]["id"]: item for item in report["findings"]}
    properties = report.get("properties", [])
    accepted_ids = {item["id"] for item in properties if item["status"] in (REFUTED, VERIFIED)}
    if (len(verified) != len(report.get("verified", [])) or len(findings) != len(report["findings"])
            or set(verified) & set(findings) or len(verified) + len(findings) > 10
            or set(verified) | set(findings) != accepted_ids):
        raise Abstain("Analysis outcomes do not match their accepted evidence")
    if any(item["status"] not in OUTCOMES for item in properties):
        raise Abstain("Unrecognized property outcome")
    counts = {status: sum(item["status"] == status for item in properties) for status in OUTCOMES}
    lines = [
        marker(ident),
        f"[Neuro-formal verification (NFV)]({PAPER}) selects nontrivial properties "
        "from PR intent and source, tries verification, then tries refutation.",
        f"# NFV: {counts[REFUTED]} refuted, {counts[VERIFIED]} verified in PR #{ident['number']}",
        *preamble(ident, evidence_url),
        "**These are results for translated models under their stated assumptions, "
        "not proofs of source-code correctness or confirmed source bugs.**",
        " · ".join(f"{status}: {counts[status]}" for status in OUTCOMES),
    ]
    if report.get("selection_omissions"):
        lines.append(f"**Selection limit:** {len(report['selection_omissions'])} additional candidate "
                     "checks were not selected within the ten-property budget.")
    compact_sections = []
    for index, item in sorted(enumerate(properties, 1),
                              key=lambda pair: pair[1]["status"] != REFUTED):
        status = item["status"]
        label = "Inferred property" if item.get("origin") == "source" else "Intended property"
        extra = enrichments.get(item["id"])
        shown = (extra["simplification"]["property"]
                 if extra and extra["simplification"]["status"] == "checked" else item["property"])
        lines.extend([f"### P{index}", f"**{label}:** {text(shown)}",
                      f"**Outcome: {status}**"])
        if status == REFUTED:
            evidence = findings.get(item.get("duplicate_of", item["id"]))
            if evidence is None:
                raise Abstain("Refuted property lacks accepted evidence")
            if any(item.get(key) != evidence["candidate"].get(key)
                   for key in ("path", "lines", "property", "quote", "origin")):
                raise Abstain("Displayed refuted property differs from its checked evidence")
            mapping = mappings.get(evidence["candidate"]["id"])
            compact_sections.append((status, len(lines), "\n\n".join(
                general_section(index, evidence, bundle, repo, commit, mapping, compact=True, followup=extra)[2:])))
            lines.append("\n\n".join(
                general_section(index, evidence, bundle, repo, commit, mapping, followup=extra)[2:]))
        elif status == VERIFIED:
            evidence = verified.get(item["id"])
            if evidence is None:
                raise Abstain("Verified property lacks accepted evidence")
            if any(item.get(key) != evidence["candidate"].get(key)
                   for key in ("path", "lines", "property", "quote", "origin")):
                raise Abstain("Displayed verified property differs from its checked evidence")
            translation = evidence["translation"]
            conditions = [condition["description"] for condition in translation["assumptions"]]
            lines.append("The property was verified for the translated model.")
            label = "Captured source (inferred property)" if evidence["candidate"].get("origin") == "source" else "Quoted intention"
            assumptions = "**Assumptions:** " + text("; ".join(conditions) or "No additional input restrictions.")
            evidence_line = (f"Evidence: {code_span(evidence['evidence'])}; "
                             f"SHA-256 {code_span(evidence['dafny_sha256'])}.")
            details = [
                "<details><summary>Verification details</summary>\n",
                f"**{label}:** " + text(evidence["candidate"]["quote"]),
                assumptions,
            ]
            if translation.get("environment"):
                details.append("**Environment models:** " + text("; ".join(
                    helper["description"] for helper in translation["environment"])))
            details.extend([
                "**Checked property:**\n\n" + dafny_block(
                    "\n".join("ensures " + clause for clause in translation["original_ensures"])),
                evidence_line,
                "The full verified Dafny is retained in the linked run artifact.",
                "</details>",
            ])
            compact_sections.append((status, len(lines), "\n\n".join([
                assumptions, evidence_line,
                "Full verification details and the full verified Dafny are retained in the linked run artifact.",
            ])))
            lines.append("\n\n".join(details))
        elif item.get("diagnostic"):
            diagnostic = re.sub("reachability", "input-domain check", item["diagnostic"], flags=re.I)
            excerpt = text(diagnostic[:300])
            if len(diagnostic) > 300:
                excerpt += "\n\nFull diagnostic is retained in the linked run artifact."
            compact_sections.append((status, len(lines),
                                     "The full diagnostic is retained in the linked run artifact."))
            lines.append("<details><summary>Diagnostic</summary>\n\n" + excerpt + "\n\n</details>")
    lines.append("<!-- gh-aw-workflow-id: nfv-pr-review -->")
    body = "\n\n".join(lines)
    for _, index, compact in sorted(
            compact_sections,
            key=lambda item: (item[0] == REFUTED, -(len(lines[item[1]]) - len(item[2])))):
        if comment_fits(body):
            break
        if (len(compact) < len(lines[index])
                or len(COMMENT_URL.findall(compact)) < len(COMMENT_URL.findall(lines[index]))):
            lines[index] = compact
            body = "\n\n".join(lines)
    if not comment_fits(body):
        raise Abstain("Publication exceeds bounded size or link count")
    return body


def publish(api, report, bundle, evidence_url, category, apply=False, label=LABEL, target=None,
            presentation=None, followups=None):
    body = render(report, bundle, evidence_url, presentation=presentation, followups=followups)
    if body is None:
        return {"status": "no-findings"}
    ident = report["identity"]
    # Source links always point at the PR repository; only the Discussion may live elsewhere.
    owner, repo = repository(target or ident["repository"]).split("/")
    cursor = None
    repository_id = None
    category_id = None
    label_id = None
    # Enumerate, rather than relying on eventually consistent search indexing.
    for _ in range(100):
        result = api.graphql("""
          query($owner:String!,$repo:String!,$cursor:String,$label:String!) {
            repository(owner:$owner,name:$repo) {
              id
              label(name:$label) { id }
              discussionCategories(first:100) { nodes { id name } pageInfo { hasNextPage } }
              discussions(first:100,after:$cursor) {
                nodes { body url }
                pageInfo { hasNextPage endCursor }
              }
            }
          }""", {"owner": owner, "repo": repo, "cursor": cursor, "label": label})["repository"]
        if not result:
            raise Abstain("Repository unavailable")
        repository_id = result["id"]
        label_id = (result.get("label") or {}).get("id")
        categories = result["discussionCategories"]
        if categories["pageInfo"]["hasNextPage"]:
            raise Abstain("Discussion category list truncated")
        category_id = next((x["id"] for x in categories["nodes"] if x["name"] == category), None)
        for discussion in result["discussions"]["nodes"]:
            if marker(ident) in discussion["body"]:
                return {"status": "duplicate", "url": discussion["url"]}
        page = result["discussions"]["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]
    else:
        raise Abstain("Discussion deduplication budget exhausted")
    if not category_id:
        raise Abstain("Configured Discussion category is unavailable")
    if not apply:
        return {"status": "preview", "body": body}
    result = api.graphql("""
      mutation($input:CreateDiscussionInput!) {
        createDiscussion(input:$input) { discussion { id url } }
      }""", {"input": {"repositoryId": repository_id, "categoryId": category_id,
                       "title": f"NFV: conditional findings for PR #{ident['number']} "
                                f"({ident['source_sha'][:12]})", "body": body}})
    discussion = result["createDiscussion"]["discussion"]
    created = {"status": "created", "url": discussion["url"], "label": label}
    # A missing or unapplied label must not discard an already accepted finding.
    if not label_id:
        created["label_warning"] = f"Repository label {label!r} does not exist; Discussion left unlabeled"
        return created
    try:
        api.graphql("""
          mutation($input:AddLabelsToLabelableInput!) {
            addLabelsToLabelable(input:$input) { clientMutationId }
          }""", {"input": {"labelableId": discussion["id"], "labelIds": [label_id]}})
    except Abstain as exc:
        created["label_warning"] = f"Could not apply label {label!r}: {exc}"
    return created
