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


def general_section(index, finding, bundle, repo, commit):
    from .general import validate_finding
    validate_finding(finding, bundle)
    claim, translation = finding["candidate"], finding["translation"]
    clauses = " and ".join(translation["original_ensures"])
    lines = [
        f"\n## {index}. {text(claim['property'])}",
        "**Checked conditional MODEL refutation — source correspondence is not established.** "
        "This is not a source-confirmed bug.",
        f"**Quoted intended behavior:** {text(claim['quote'])}",
        f"**Checked expectation:** {text(clauses)}",
        f"**Proposed concrete inputs:** {text(json.dumps(translation['witness'], sort_keys=True))}",
        "Dafny checked the model's failure of that expectation and a concrete call satisfying "
        "its input conditions. It also checked termination; a separate returning-case check passed.",
        f"**Model-based explanation (not an independent source diagnosis):** {text(translation['explanation'])}",
        f"**Conditional on the translation/environment choices:** {text(translation['correspondence'])}",
        "**Retained inferred input/environment conditions:**",
    ]
    retained = finding["retained_assumptions"]
    lines.extend("- " + text(translation["assumptions"][i]["description"]) for i in retained)
    if not retained:
        lines.append("- No additional entry conditions remain; translation correspondence is still unproved.")
    for environment in translation.get("environment", []):
        origin = environment["source"]
        lines.append("- " + text(environment["name"]) + ": " + text(environment["description"])
                     + " (modeler-labelled " + text(environment["kind"]) + "; captured context "
                     + text(origin["path"]) + "). These definitions are checked in the model, "
                     "not certified as equivalent to the external implementation.")
    lines.append("\n**Annotated immutable source overlay:**")
    notes = {(x["path"], x["line"]): x["note"] for x in translation.get("source_notes", [])}
    selected, seen = [], set()
    for mapping in finding["source_map"]:
        key = (mapping["path"], mapping["line"])
        if key not in seen:
            seen.add(key)
            selected.append(mapping)
    selected.sort(key=lambda x: (x["path"] != claim["path"], x["line"] not in claim["lines"], x["line"]))
    for mapping in selected[:16]:
        path, line = mapping["path"], mapping["line"]
        url = f"https://github.com/{repo}/blob/{commit}/{quote(path, safe='/')}#L{line}"
        lines.append(f"- [{text(path)}:{line}]({url}): {text(mapping['source'])}")
        lines.append("  - Checked-model mapping: " + text(mapping["model"]))
        if (path, line) in notes:
            lines.append("  - Modeler's annotation: " + text(notes[(path, line)]))
    lines.extend([
        "<details><summary>Dafny evidence, repairs, and assumptions</summary>\n",
        f"Artifact file: `{finding['evidence']}`; SHA-256 `{finding['dafny_sha256']}`.",
        f"Syntax repairs used: {finding['counters']['syntax_repairs']}/{finding['limits']['syntax_rounds']}. "
        f"Annotation-only repairs used: {finding['counters']['proof_repairs']}/{finding['limits']['proof_rounds']}.",
        "Optional restrictions were deleted one at a time only when the complete negative check, "
        "concrete-call consistency, and returning-case check still passed. Required conditions, "
        "the witness, goal, and computation were not repaired. Source fidelity is not established.",
        "Removed optional restrictions: " + text("; ".join(
            value["description"] for i, value in enumerate(translation["assumptions"])
            if i not in retained) or "none"),
        dafny_block(finding["dafny"]),
        "</details>",
    ])
    return lines


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
    commit = sha(ident["source_sha"])
    lines = [f"Analyzed PR head `{commit}` against base `{sha(ident['base_sha'])}`."]
    if ident.get("merged"):
        lines.append(f"The PR was merged as `{sha(ident['merge_sha'])}`; this analyzes its saved head, "
                     "**not** the merge result.")
    return lines + [
        "Dafny checked that the stated expectation fails for the concrete inputs in the "
        "translated model. This is **not** an execution of the original program or a "
        "machine-checked guarantee that the translation matches it. Each finding is "
        "conditional on the source-to-model correspondence and environment choices below.",
        f"[Dafny files, verifier logs, and minimization rechecks]({evidence_url})",
    ]


def finding_lines(index, finding, bundle, repo, commit):
    if finding.get("kind") == "conditional-model-refutation":
        return general_section(index, finding, bundle, repo, commit)
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


def render(report, bundle, evidence_url):
    findings = checked_findings(report, bundle, evidence_url)
    if findings is None:
        return None
    ident = report["identity"]
    repo, commit = repository(ident["repository"]), sha(ident["source_sha"])
    header = [marker(ident), f"# Conditional model findings for PR #{ident['number']}",
              "This was found by a neuro-formal verifier.", *preamble(ident, evidence_url)]
    for index, finding in enumerate(findings, 1):
        header.extend(finding_lines(index, finding, bundle, repo, commit))
    header.extend([f"\nGenerated by [Neuro-formal verification]({evidence_url}).",
                   "<!-- gh-aw-workflow-id: nfv-pr-review -->"])
    body = "\n\n".join(header)
    if len(body) > 60000:
        raise Abstain("Publication exceeds bounded size")
    return body


def comment(report, bundle, evidence_url, proposed):
    """The PR comment: the findings report, or a short summary when nothing was refuted."""
    body = render(report, bundle, evidence_url)
    if body is not None:
        return body
    ident = report["identity"]
    abstentions, errors = report.get("abstentions", []), report.get("errors", [])
    lines = [marker(ident), f"# Neuro-formal verification of PR #{ident['number']}: no refutations",
             f"Analyzed PR head `{sha(ident['source_sha'])}` against base `{sha(ident['base_sha'])}`.",
             f"Extracted {proposed} intended propert{'y' if proposed == 1 else 'ies'}; "
             f"{len(abstentions)} abstained and {len(errors)} errored. Dafny refuted none of "
             "them in the translated models.",
             "No refutation is **not** evidence that the code is correct: properties may be "
             "missed, abstained on, or translated imperfectly."]
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
    lines.extend([f"\nGenerated by [Neuro-formal verification]({evidence_url}).",
                  "<!-- gh-aw-workflow-id: nfv-pr-review -->"])
    return "\n\n".join(lines)


def publish(api, report, bundle, evidence_url, category, apply=False, label=LABEL, target=None):
    body = render(report, bundle, evidence_url)
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
