# NFV Paper Maintenance Notes

This directory is the active self-contained arXiv submission for
**Neuro-Formal Verification: Agentic Language-Agnostic Formal Program
Reasoning**.

## Files

- `paper.tex`: main manuscript.
- `northstar.tex`: NFV architecture, workflow, and running example.
- `tables/`: submitted tables and generated number macros.
- `figs/northstar.pdf`: architecture figure.
- `paper.pdf`: compiled manuscript; keep synchronized with the TeX sources.
- `diff.pdf`: highlighted comparison against the submitted baseline tagged
  `paper-arxiv-version`; keep synchronized with the TeX sources.
- `arxiv_submission.zip`: packaged submission. Do not update it unless
  explicitly requested.

## Editing Rules

- Do not make unsolicited wording changes. When asked for suggestions, propose
  text first and edit only after approval.
- Preserve user edits already present in the working tree.
- After every change to `paper.tex` or `northstar.tex`, rebuild both
  `paper.pdf` and `diff.pdf`.
- Commit the changed TeX sources and generated PDFs together.
- Do not include unrelated files in commits.
- Result macros are preferable to copied numbers where suitable.

## Paper Framing

NFV combines an AI coding agent with an established formal verifier. The agent
translates a source-language program, property, and environment into formal
artifacts; the verifier checks the resulting formal model.

The key distinction is:

- NFV provides empirically accurate formal program reasoning.
- A verdict is backed by a machine-checked proof over a formal model.
- This is not necessarily an end-to-end sound proof about the original source
  program because the source semantics, environment description, and
  translations may not all be formally attested.
- The framework does not exclude future instances that provide stronger or
  end-to-end guarantees.

The current introductory formulation says that answers are:

1. empirically accurate without necessarily providing end-to-end guarantees
   of soundness or completeness;
2. produced at the push of a button by default; and
3. accompanied by a proof over a formal model of the program.

The environment is bidirectional: it includes both the context that calls the
program and the libraries and services the program calls.

The explicit artifacts `P_F`, `S_F`, and `E_F` are valuable beyond the final
verdict. They let a user inspect the modeled program, checked property, and
assumptions; identify mismodeling; and provide feedback for subsequent
formalization.

The longer-term vision is not only cost-effective defect finding. NFV should
help developers build confidence in their code gradually and understand the
assumptions required for correct behavior.

## Headline Results

The benchmark contains 206 entries: 103 correct Python references and 103 buggy
mutants.

- Combined Dafny pipeline: 128 verdicts, 118 correct; **92% precision** and
  **57% recall**.
- Dafny verification arm: 72/103 correct programs proved; 5 incorrect claims;
  **94% precision**, **70% recall**.
- Dafny refutation arm: 47/103 buggy programs refuted; 6 incorrect claims;
  **89% precision**, **46% recall**.
- CBMC refutation: **63% recall** and **90% precision**.
- LLM-as-judge: answers every entry at **72% precision** and produces no
  checkable artifact.

The abstract keeps the Dafny comparison with LLM-as-judge explicit and reports
the CBMC result separately.

Relevant generated macros:

- `tables/pipeline_numbers.tex`
- `tables/analysis_numbers.tex`
- `tables/judge_numbers.tex`
- `tables/cbmc_falsecex.tex`

## Analysis Details Already Checked

### Library tiers

The overall Dafny verification result is 15/25 for L2a and 3/9 for L3.
Separately, the nine L2a string-handling programs prove at 3/9:

- proved: HumanEval/6, HumanEval/66, HumanEval/156;
- abstained: HumanEval/1, HumanEval/50, HumanEval/65, HumanEval/67,
  HumanEval/111, HumanEval/161.

HumanEval/1 handles a string through iteration and concatenation without using
one of the named string methods. Thus the L2a string subset and L3 happen to
share the same 3/9 result; the prose is not conflating the tiers.

### HumanEval/64: Unicode lowercasing

The missing precondition is that `s` contain only ASCII characters. Python
lowercases Latin capital I with dot above, **İ** (`"\u0130"`), to `i` followed
by a combining dot. The specification counts the resulting `i`, whereas the
implementation tests the original character against an ASCII vowel set and
counts none.

### HumanEval/79: negative binary input

The missing precondition is that `decimal` be nonnegative. For `-3`,
`bin(-3)` returns `"-0b11"`; slicing from index 2 leaves `"b11"`, so the
function returns `"dbb11db"`, which does not match `^db[01]+db$`.

### Naive baseline categories

The primary mechanisms among 101 proofs of known-wrong programs are mutually
exclusive:

- program rewritten: 58/101, reported as 57%;
- goal restated using invented helpers: 35/101, reported as 35%;
- proof rests on an explicit assumption: 8/101, reported as 8%.

These total 100%. Separately, 57/101, reported as 56%, contain an unjustified
`requires` clause. That statistic overlaps the three primary categories and
must not be presented as a fourth mutually exclusive category.

## Build `paper.pdf`

Run from this directory:

```powershell
pdflatex -interaction=nonstopmode -halt-on-error paper.tex
pdflatex -interaction=nonstopmode -halt-on-error paper.tex
```

Confirm that `paper.log` reports an output PDF and contains no fatal errors,
undefined references, or undefined citations.

## Build `diff.pdf`

`diff.pdf` compares the current paper with the submitted baseline tagged
`paper-arxiv-version`. MiKTeX's ordinary `latexdiff` wrapper does not work in
this environment because its Perl module is unavailable. The working toolchain
already installed on this machine is:

- Perl: `C:\Program Files\Git\usr\bin\perl.exe`
- Git Unix tools, including `diff`: `C:\Program Files\Git\usr\bin`
- standalone latexdiff:
  `%LOCALAPPDATA%\Programs\MiKTeX\scripts\latexdiff\latexdiff-fast`

The known-working process is:

1. Export the `paper-arxiv-version` tree to a temporary directory.
2. Put Git's `usr\bin` first on `PATH`.
3. Invoke `latexdiff-fast` directly with Git's Perl using `--flatten` and
   `--encoding=utf8`.
4. Write the generated source temporarily as `diff.tex` in this directory.
5. Compile twice with `pdflatex -jobname=diff diff.tex`.
6. Remove `diff.tex`, `diff.aux`, `diff.log`, and `diff.out`; retain only
   `diff.pdf`.

Example commands when run from this directory:

```powershell
$temp = Join-Path $env:TEMP "nfv-paper-diff"
Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $temp | Out-Null

git archive --format=zip --output="$temp\baseline.zip" paper-arxiv-version
Expand-Archive -LiteralPath "$temp\baseline.zip" -DestinationPath "$temp\baseline"

$env:PATH = "C:\Program Files\Git\usr\bin;" + $env:PATH
$perl = "C:\Program Files\Git\usr\bin\perl.exe"
$latexdiff = "$env:LOCALAPPDATA\Programs\MiKTeX\scripts\latexdiff\latexdiff-fast"

& $perl $latexdiff --flatten --encoding=utf8 `
  "$temp\baseline\paper.tex" ".\paper.tex" |
  Set-Content -LiteralPath ".\diff.tex" -Encoding utf8

pdflatex -interaction=nonstopmode -halt-on-error -jobname=diff diff.tex
pdflatex -interaction=nonstopmode -halt-on-error -jobname=diff diff.tex

Remove-Item .\diff.tex,.\diff.aux,.\diff.log,.\diff.out `
  -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $temp -Recurse -Force
```

The `Wide character in print` warning from `latexdiff-fast` is expected for the
visible **İ** character and did not prevent successful PDF generation.

## Git

The active branch is `neuro-formal-v1`, tracking
`origin/neuro-formal-v1`.

Commits created through Copilot in this repository should include:

```text
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Copilot-Session: 5d75b441-471b-4374-95e8-6cbdf4bee051
```
