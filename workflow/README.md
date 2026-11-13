<!-- Copyright (c) Microsoft Corporation.
Licensed under the MIT License. -->

# NFV PR reviewer

A prototype GitHub Agentic Workflow based on
[*Neuro-Formal Verification: Agentic Language-Agnostic Formal Program Reasoning*](https://arxiv.org/abs/2608.21516).

For each eligible PR, NFV reads the change and its stated intent, selects
behavioral properties, and translates relevant source into Dafny. It attempts
verification and refutation, then posts one evidence-linked PR comment.
PR code is inspected as data, never executed.

Same-repository PRs targeting any branch are eligible; fork PRs are
skipped. Each PR gets one automatic attempt when opened non-draft or marked ready.
Admission records attempts, including failures. Pushes and repeated
ready transitions neither cancel nor repeat it. The saved snapshot is reviewed
even after merge. Retries require an explicit rerun. Both sessions require
`NFV_RELEASE_SHA`.

Results concern translated models under stated assumptions. Findings are
**conditional MODEL refutations, not confirmed source bugs**; failure to refute
does not establish correctness.

## Paper algorithm vs. workflow

The [paper's evaluated algorithm](https://arxiv.org/html/2608.21516#S3) starts
with a supplied source-level assertion. It formalizes library contracts and
documented preconditions without seeing that assertion, then freezes them.
Translation repairs use only syntax/type feedback; subsequent proof search
cannot change frozen computation or contracts. The paper evaluates Dafny proofs
and CBMC bounded refutation.

The workflow instead selects whole properties from PR intent and code. It
jointly formalizes each property with scoped code, input conditions and helper
contracts, allowing bounded draft repairs before freezing. It then tries Dafny
verification first, followed, if needed, by witnessed refutation on the same
frozen model.

For refutations, it tries to **minimize the failure condition** by removing
dispensable assumptions with rechecks and seeking a shorter, separately proved
statement that still fails on the accepted counterexample. Independently, it
searches for a **model-checked candidate fix** that proves the complete original
property without weakening retained assumptions or helper contracts. These
bounded searches do not change the whole property's refuted outcome or guarantee
global minimality or a source-verified repair.

These adaptations aim to improve **review efficiency and strengthen model-level
evidence**. They are not the paper's evaluated pipeline; its experimental results
do not measure workflow accuracy or efficiency.

## From a PR to an evidence-linked report

```mermaid
flowchart LR
    PR["PR source + stated intent"] --> Properties["Selected properties<br/>+ Dafny models"]
    Properties --> Checks["Verification first,<br/>then refutation if needed"]
    Checks --> Verified["verified<br/>Property proved in the model"]
    Checks --> Refuted["refuted<br/>Model counterexample accepted"]
    Checks --> Unresolved["resources exhausted<br/>or type-check/audit failure"]
    Verified --> Report["One PR comment<br/>+ retained evidence"]
    Refuted --> Followups["Optional checked simplification<br/>+ model-checked candidate fix"]
    Followups --> Report
    Unresolved --> Report
    classDef proved fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    classDef refuted fill:#fff3e0,stroke:#e65100,color:#843600
    classDef unresolved fill:#f5f5f5,stroke:#616161,color:#333333
    class Verified proved
    class Refuted refuted
    class Unresolved unresolved
```

A refutation shows a counterexample input and a sketch of why the modeled
property fails, not an execution trace. Verification shows the checked
property and its assumptions. Both link to source and formal evidence;
neither establishes source-code correctness.

Reports are bounded by size and URL count; full source and formal evidence remain
in the run artifact when details are compacted.

## Report contents

| Report element | What the developer sees |
|---|---|
| Refutation | Property, counterexample input, failure sketch and relevant source |
| Verification | Property, retained assumptions and checked Dafny contract |
| Expandable details | Source context, model evidence and any model-checked candidate fix |
| Evidence links | Captured source and the run's full formal models and diagnostics |

## Report screenshots

![NFV report showing a model refutation, counterexample input and failure sketch](images/nfv-report-refutation.png)

![NFV report showing model verification with expanded assumptions and checked property](images/nfv-report-verification.png)

## Operational failures

The trusted admission job writes PR comments separately from read-only inference.
API failures report the method, endpoint, status, a bounded credential-redacted
GitHub message and available rate-limit headers. Admission failures block inference.

## Optional repository dashboards

The driver writes a separate `nfv-summary-<run-attempt>` artifact with versioned
repository/run/source identity, completion and the four outcome counts.
It contains no source, formal models, property prose, counterexamples or
diagnostics. An incomplete analysis has unknown counts, not zero findings.
Early failures before the driver starts may have no summary. Safe-output
submission is recorded as `submitted`, not confirmed publication.

NFV users can self-report public repository onboarding for nightly summary collection through
[onboarding](ONBOARDING.md#optional-public-dashboard). The existing dashboard
keeps these registrations separate from historical replay campaigns. Older
releases without summaries appear as coverage gaps; the collector never launches
audits or derives proof counts from green jobs or comment prose.

The maintainer-only [micro-benchmark](BENCHMARK.md) has a separate Discussion
dashboard with the latest completed full run, per-run NFV revision links and
persistent monthly statistics archives. Publication preserves custom Discussion
titles, including the pinned dashboard's title. It does not run in client repositories.

**Installation:** [Onboarding](ONBOARDING.md).

The workflows must exist in the target branch/PR merge ref; installing NFV does
not add them to older branches.

<a id="upgrade-an-existing-installation"></a>

**More:** [Configuration](REFERENCE.md#configure-github-agentic-workflows),
[upgrades](ONBOARDING.md#upgrade-an-existing-installation), and
[technical reference](REFERENCE.md).
For expired tokens and other common problems, see the [FAQ](FAQ.md).
