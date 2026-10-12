"""Final-paper model comparison and attribution generators."""
from __future__ import annotations

import json

import re

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

TABLES = Path(__file__).resolve().parent / "tables"

BASE_VERIFY, BASE_REFUTE = "a5r10_verify.jsonl", "a5r10_refute.jsonl"

LEMMA4_VERIFY, LEMMA4_REFUTE = "lemma4_verify.jsonl", "lemma4_refute.jsonl"

LEMMA4_MODEL = {
    "haiku": "lemma4_haiku_verify.jsonl",
    "mini": "lemma4_mini_verify.jsonl",
    "sonnet": "lemma4_sonnet46_verify.jsonl",
}

LEMMA4_MODEL_REFUTE = {
    "haiku": "lemma4_haiku_refute.jsonl",
    "mini": "lemma4_mini_refute.jsonl",
    "sonnet": "lemma4_sonnet46_refute.jsonl",
}

PLACEHOLDER: set[str] = set()

COMPARISONS = {
    "weakmodel": {
        "caption": (
            r"Comparison of different LLMs for the proof-based tasks. "
            r"\emph{proved} is over the 103 correct programs, \emph{refuted} "
            r"over the 103 buggy ones, \emph{bad} counts claims contradicting "
            r"the label, \emph{prec} is precision over claims, and \emph{F1} "
            r"combines that precision with recall over the 103, counting an "
            r"abstention as a wrong answer would count. "
            r"\emph{type} and \emph{audit} "
            r"split the entries that never reached the proof step: code that "
            r"failed the type checker, and code that resolved but failed the "
            r"artifact checks."
        ),
        "label": "tab:weakmodel",
        "rows": [],
    },
}
EXPECTED = 206  # the full benchmark; a short file means the run is still going

ABANDONED = {
    # Stopped after ~7h with 3 entries unfinished. The three were burning
    # 450-1300s calls against the 96k output-token cap and would have taken
    # hours more; every other model in the table finished the same benchmark in
    # under two. The unfinished entries are not a random sample -- they are the
    # hardest ones -- so the row's refutation recall is an upper bound.
    "lemma4_sonnet46_refute.jsonl": 203,
}

def load(name: str) -> list[dict] | None:
    """Rows of a completed run, or None if it is missing or still in flight.

    A partial file is treated as pending rather than scored. The store appends
    as each entry finishes, so a run in progress is a perfectly readable file
    with the wrong denominator -- scoring it would quietly report, say, 12/45
    as though it were the whole benchmark.

    A run that was resumed after a transport failure appends a second row for
    entries it redid, so line count is not entry count: the last row per
    entry_id wins, and completeness is measured in distinct entries.
    """
    p = ROOT / "nfv/results" / name
    if not p.exists():
        return None
    rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    latest: dict[str, dict] = {}
    for r in rows:
        latest[str(r.get("entry_id"))] = r
    rows = list(latest.values())
    return rows if len(rows) >= ABANDONED.get(name, EXPECTED) else None

def status(name: str) -> str:
    p = ROOT / "nfv/results" / name
    if not p.exists():
        return "not started"
    # Distinct entries, not lines: a resumed run appends a second row for every
    # entry it redid, so the line count overstates progress.
    seen = {
        json.loads(ln).get("entry_id")
        for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()
    }
    return f"{len(seen)}/{EXPECTED} entries"

def pct(a: int, b: int) -> str:
    return f"{100.0 * a / b:.0f}\\%" if b else "--"

def prec(good: int, bad: int) -> str:
    n = good + bad
    return f"{100.0 * good / n:.0f}\\%" if n else "--"

def f1(good: int, bad: int, pop: int) -> str:
    """Harmonic mean of precision and recall, in percent.

    Recall is over the whole ground-truth population, so an abstention counts
    against it exactly as a wrong answer would. That is deliberate: the two
    columns either side of this one move in opposite directions across models
    -- the gates hold precision by refusing to answer, which costs coverage --
    and F1 is the single number that prices that trade instead of letting a
    row look good on one column alone. It is not a soundness measure; verdict
    soundness is the verifier's and is reported by \\emph{bad}.

    Reported as a fraction rather than a percentage, so that the column cannot
    be misread as another rate over the population like the two it sits beside.
    """
    den = good + pop + bad
    return f"{2.0 * good / den:.2f}" if den else "--"

def arm(rows: list[dict], claim: str) -> tuple[int, int, int, int, int]:
    """(hits, population, bad claims, type-check stalls, audit stalls).

    For verification the population is the correct programs and a claim of
    `correct` on a buggy one is bad; for refutation it is the mirror image.

    The two translation-stall counts are kept apart because they are different
    failures. A type-check stall is code the backend would not resolve after the
    syntactic repair rounds; an audit stall is code that resolved and was then
    rejected by the artifact checks -- provenance, integrity, or reproduction of
    the frozen phase-0 theory. The first is a limit of the target language, the
    second is a discipline the pipeline imposes on itself.
    """
    want = "correct" if claim == "verify" else "incorrect"
    pop = [r for r in rows if r.get("label") == want]
    other = [r for r in rows if r.get("label") != want]
    hits = sum(1 for r in pop if r.get("prediction") == want)
    bad = sum(1 for r in other if r.get("prediction") == want)
    stalled = [r for r in rows if r.get("phase_reached") == "translation"]
    typ = sum(1 for r in stalled if "type check" in (r.get("reason") or ""))
    aud = len(stalled) - typ
    return hits, len(pop), bad, typ, aud

def row_cells(name: str, vfile: str, rfile: str | None) -> str:
    """One table row.

    `rfile` may be None for a backend whose refutation arm has not been run,
    which is not the same as one that scored zero: the cells say so rather than
    being filled with a number nobody produced.
    """
    ver = load(vfile)
    ref = load(rfile) if rfile else None
    if ver is None:
        detail = f"{vfile.replace('_', chr(92) + '_')}: {status(vfile)}"
        if rfile and ref is None:
            detail += f"; {rfile.replace('_', chr(92) + '_')}: {status(rfile)}"
        return rf"{name} & \multicolumn{{10}}{{c}}{{\emph{{run pending --- {detail}}}}} \\"
    vp, vn, vb, vty, vau = arm(ver, "verify")
    if ref is None:
        # A half that is missing and a half still in flight are different
        # claims, and the cells say which. A partial file is never scored.
        note = "not yet run" if not rfile else f"in flight, {status(rfile)}"
        return (
            rf"{name} & {vp}/{vn} ({pct(vp, vn)}) & {vb} & {prec(vp, vb)} & "
            rf"{f1(vp, vb, vn)} & "
            rf"\multicolumn{{4}}{{c}}{{\emph{{{note}}}}} & "
            rf"{vty} & {vau} \\"
        )
    rp, rn, rb, rty, rau = arm(ref, "refute")
    return (
        rf"{name} & {vp}/{vn} ({pct(vp, vn)}) & {vb} & {prec(vp, vb)} & "
        rf"{f1(vp, vb, vn)} & "
        rf"{rp}/{rn} ({pct(rp, rn)}) & {rb} & {prec(rp, rb)} & "
        rf"{f1(rp, rb, rn)} & "
        rf"{vty + rty} & {vau + rau} \\"
    )

def cost_note(files: list[tuple[str, str]]) -> str:
    """A footnote pricing each row, read from the run files' own usage records.

    Cost belongs beside capability or the table invites the wrong conclusion:
    a stronger translator is only worth having if what it buys exceeds what it
    costs, and on this pipeline the spread across models is large. AIU is what
    the API reports per call, so this is measured and not a list price. A run
    still in flight is priced over the entries it has finished, and says so.
    """
    parts = []
    for name, f in files:
        p = ROOT / "nfv/results" / f
        if not p.exists():
            continue
        rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not rows:
            continue
        aiu = sum(r.get("nano_aiu") or 0 for r in rows) / 1e9
        per = aiu / len(rows)
        tail = "" if len(rows) >= EXPECTED else rf" (over {len(rows)} entries so far)"
        parts.append(rf"{name} {aiu:.0f}~AIU, {per:.0f}/entry{tail}")
    if not parts:
        return ""
    return "Measured cost of the verification arm: " + "; ".join(parts) + "."

def verify_row(name: str, vfile: str) -> str:
    """One row of a verification-only table.

    Kept apart from `row_cells` so that the stall columns count the same arm on
    every row. Mixing a two-arm row with a one-arm row in the same table would
    make `type` and `audit` incomparable down the column, which is precisely
    the comparison the model table exists to support.
    """
    ver = load(vfile)
    if ver is None:
        return (rf"{name} & \multicolumn{{6}}{{c}}{{\emph{{run pending --- "
                rf"{vfile.replace('_', chr(92) + '_')}: {status(vfile)}}}}} \\")
    vp, vn, vb, vty, vau = arm(ver, "verify")
    return (rf"{name} & {vp}/{vn} ({pct(vp, vn)}) & {vb} & {prec(vp, vb)} & "
            rf"{f1(vp, vb, vn)} & {vty} & {vau} \\")

def build(key: str, spec: dict) -> str:
    def emit(r):
        if spec.get("verify_only"):
            return verify_row(r[0], r[1])
        return row_cells(*r)

    body = "\n".join(emit(r) for r in spec["rows"])
    note = spec.get("note")
    if note is None and spec.get("note_files"):
        note = cost_note(spec["note_files"]) or None
    note_tex = rf"\\[3pt]\footnotesize {note}" if note else ""
    if spec.get("verify_only"):
        return rf"""% generated by nfv/paper/make_compare_tables.py -- do not edit by hand
\begin{{table}}[t]\centering\footnotesize
\caption{{{spec['caption']}}}
\label{{{spec['label']}}}
\begin{{tabular}}{{lrrrrrr}}
\toprule
& \multicolumn{{4}}{{c}}{{verification}} & \multicolumn{{2}}{{c}}{{no proof attempted}} \\
\cmidrule(lr){{2-5}}\cmidrule(lr){{6-7}}
& proved & \emph{{bad}} & \emph{{prec}} & \emph{{F1}} & \emph{{type}} & \emph{{audit}} \\
\midrule
{body}
\bottomrule
\end{{tabular}}
{note_tex}
\end{{table}}
"""
    return rf"""% generated by nfv/paper/make_compare_tables.py -- do not edit by hand
\begin{{table}}[t]\centering\footnotesize
\caption{{{spec['caption']}}}
\label{{{spec['label']}}}
\begin{{tabular}}{{lrrrrrrrrrr}}
\toprule
& \multicolumn{{4}}{{c}}{{verification}} & \multicolumn{{4}}{{c}}{{refutation}} & \multicolumn{{2}}{{c}}{{no proof attempted}} \\
\cmidrule(lr){{2-5}}\cmidrule(lr){{6-9}}\cmidrule(lr){{10-11}}
& proved & \emph{{bad}} & \emph{{prec}} & \emph{{F1}} & refuted & \emph{{bad}} & \emph{{prec}} & \emph{{F1}} & \emph{{type}} & \emph{{audit}} \\
\midrule
{body}
\bottomrule
\end{{tabular}}
{note_tex}
\end{{table}}
"""

def resolve_weakmodel() -> None:
    """Fill in Table~\\ref{tab:weakmodel}'s rows from whatever has been run.

    This table now carries three translators rather than two, and it reports
    both tasks, so a row needs a matched pair of files. The rule per row:

    * If the lemma-aware verification run is complete, the row is lemma-aware.
      Its refutation half is shown only if that run is complete too; while it is
      in flight the row prints the verification cells and says the refutation
      arm has not been run, rather than suppressing numbers that already exist.
    * Otherwise the row falls back to its plain-arm pair and is marked, exactly
      as in the model table, so the mark always means the same thing.

    Rows are ordered by decreasing verification F1, computed from the very file
    each row ends up scored against, so the order cannot disagree with the
    column it claims to sort. A row whose verification half is not complete has
    no F1 and sorts last.
    """
    order = ["sol", "sonnet", "mini", "haiku"]
    names = {
        "sol": r"\texttt{gpt-5.6-sol}",
        "sonnet": r"\texttt{claude-sonnet-4.6}",
        "mini": r"\texttt{gpt-5-mini}",
        "haiku": r"\texttt{claude-haiku-4.5}",
    }
    plain = {
        "sol": (BASE_VERIFY, BASE_REFUTE),
        "mini": ("mini_verify.jsonl", "mini_refute.jsonl"),
        # haiku and sonnet were only ever run on the lemma-aware arm, so they
        # have no plain-arm pair to fall back to.
        "haiku": (None, None),
        "sonnet": (None, None),
    }
    rows, stale = [], set()
    short: dict[str, int] = {}
    for tag in order:
        v4 = LEMMA4_VERIFY if tag == "sol" else LEMMA4_MODEL[tag]
        r4 = LEMMA4_REFUTE if tag == "sol" else LEMMA4_MODEL_REFUTE[tag]
        has_plain = plain[tag][0] is not None
        # A row switches arms only when it can switch *both* halves, so the two
        # tasks in one row are always the same configuration. The exception is a
        # model with no plain-arm pair to wait on, which shows what it has.
        if load(v4) is not None and (load(r4) is not None or not has_plain):
            # Keep the refutation file name even when it is incomplete: the row
            # then says the half is in flight and how far it has got, which is a
            # different claim from never having been run.
            vf, rf = v4, r4
        elif has_plain:
            vf, rf = plain[tag]
            stale.add(tag)
        else:
            vf, rf = v4, None  # nothing complete either way: say pending
        mark = r"\,\textbf{\large *}" if tag in stale else ""
        for f in (vf, rf):
            if f in ABANDONED:
                short[tag] = ABANDONED[f]
                mark += r"$^{\dagger}$"
        rows.append((names[tag] + mark, vf, rf))

    def verify_f1(vfile: str) -> float:
        """The row's own verification F1, or -1 while that half is incomplete."""
        ver = load(vfile)
        if ver is None:
            return -1.0
        vp, vn, vb, _, _ = arm(ver, "verify")
        return float(f1(vp, vb, vn))

    paired = sorted(zip(rows, order), key=lambda t: -verify_f1(t[0][1]))
    rows = [r for r, _ in paired]
    order = [t for _, t in paired]
    spec = COMPARISONS["weakmodel"]
    spec["rows"] = rows
    spec["caption"] += r" Rows are ordered by decreasing verification \emph{F1}."
    if stale:
        spec["caption"] += (
            r" Rows marked \textbf{\large *} are the plain \textsc{Dafny} "
            r"theory arm rather than the lemma-aware arm the paper reports; "
            r"the two differ only in the stage-4 repair prompt, so their "
            r"\emph{type} and \emph{audit} columns are unaffected and their "
            r"\emph{proved} and \emph{refuted} columns are floors."
        )
    if short:
        n = min(short.values())
        spec["caption"] += (
            rf" A row marked $^{{\dagger}}$ was stopped before finishing: its "
            rf"refutation half is scored over the {n} entries that completed, "
            rf"not {EXPECTED}. The {EXPECTED - n} that did not finish are the "
            r"ones the model spent longest on."
        )
    return {t: (v, r) for (_, v, r), t in zip(rows, order)}, stale

def weak_macros(files: dict[str, tuple[str, str | None]], stale: set[str]) -> None:
    """Inline numbers for Section~\\ref{sec:weakmodel}, from the table's own files.

    Section~\\ref{sec:weakmodel} quotes more figures in prose than any other
    part of the paper --- both tasks, three translators, and the stall split ---
    and every one of them moves when a row switches arms. They are therefore
    generated from exactly the files ``resolve_weakmodel`` chose, so the text
    and the table can never disagree, whichever arm a row is currently on.

    ``wmmark`` is a sentence rather than a number: while any row is still on the
    plain arm the section is comparing two arms, and it says so. It empties
    itself when the last rerun lands.
    """
    out: dict[str, object] = {}
    for tag, (vf, rf) in files.items():
        typ = aud = 0
        rows = load(vf)
        if rows is not None:
            hits, pop, bad, vty, vau = arm(rows, "verify")
            typ, aud = vty, vau
            out[f"wm{tag}proved"] = hits
            out[f"wm{tag}pop"] = pop
            out[f"wm{tag}bad"] = bad
            out[f"wm{tag}prec"] = f"{100.0 * hits / (hits + bad):.0f}" if hits + bad else "--"
        rrows = load(rf) if rf else None
        if rrows is not None:
            hits, pop, bad, rty, rau = arm(rrows, "refute")
            typ, aud = typ + rty, aud + rau
            out[f"wm{tag}refuted"] = hits
            out[f"wm{tag}refpop"] = pop
            out[f"wm{tag}refbad"] = bad
            out[f"wm{tag}refprec"] = f"{100.0 * hits / (hits + bad):.0f}" if hits + bad else "--"
        out[f"wm{tag}type"] = typ
        out[f"wm{tag}audit"] = aud
    out["wmmark"] = (
        r" Rows marked \textbf{\large *} in Table~\ref{tab:weakmodel} are still "
        r"the plain repair arm, so a comparison that crosses a mark also "
        r"crosses arms; the lemma-aware reruns are in flight."
        if stale else ""
    )
    lines = [r"% GENERATED by nfv/paper/make_compare_tables.py -- do not edit."]
    lines += [rf"\newcommand{{\{k}}}{{{v}}}" for k, v in out.items()]
    (TABLES / "weakmodel_numbers.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote weakmodel_numbers.tex:", out)

def analysis_macros() -> None:
    """Counts for Section~\\ref{sec:analysis}, plus a guard on its per-entry prose.

    That section is the one place in the paper that argues about *named*
    entries: it walks each claim contradicting a ground-truth label and says
    what is wrong with it. Prose like that cannot be generated -- the argument
    about ``HumanEval/35`` is an argument, not a number -- but it can be
    guarded, and it needs to be, because the set it discusses is a property of
    the reported run. When the reference arm moved from the plain to the
    lemma-aware repair prompt this set changed on both tasks (verification lost
    ``111`` and gained ``35``; refutation went from eight entries to six), and
    nothing in the build would have said so. The expected sets are therefore
    written down here and checked against the files: a future arm change that
    moves them stops the build instead of leaving the section quietly
    describing a run the paper no longer reports.
    """
    expected = {
        "verify": {"HumanEval/6", "HumanEval/35", "HumanEval/116",
                   "HumanEval/133", "HumanEval/151"},
        "refute": {"HumanEval/11", "HumanEval/20", "HumanEval/49",
                   "HumanEval/64", "HumanEval/79", "HumanEval/111"},
    }
    # Refutation claims the section argues are right despite contradicting the
    # label, because the label is wrong: see the ``Flipped labels'' subsection.
    # Split by direction, because the direction is the finding. A reference
    # that should be labeled incorrect can only be exposed by refutation, and
    # a mutant that is really correct only by verification, so these two sets
    # are drawn from different run files by construction.
    # Under the policy of Section~\\ref{sec:analysis} the reference of a pair
    # keeps its ``correct'' label: where the refutation arm exhibits a real
    # counterexample to a reference, the specification is charged with an
    # unstated precondition rather than the program with a bug. Only a mutant
    # can therefore change class, and exactly one does. ``missing_pre'' is the
    # set of references whose refutation is attributed that way; it is checked
    # against the run file below, and each member is executed by
    # nfv/paper/check_causes.py.
    flipped_refs: set[str] = set()
    flipped_muts = {"HumanEval/116"}
    missing_pre = {"HumanEval/11", "HumanEval/20", "HumanEval/49",
                   "HumanEval/64", "HumanEval/79", "HumanEval/111"}
    undetermined: set[str] = set()
    # Refutation claims that contradict a label and are nevertheless sound
    # against the specification as the docstring writes it. They set the
    # adjusted precision below.
    mislabeled = missing_pre

    out = {}
    for claim, fname in (("verify", LEMMA4_VERIFY), ("refute", LEMMA4_REFUTE)):
        rows = load(fname)
        if rows is None:
            print(f"analysis macros: {fname} incomplete, skipping")
            return
        want = "correct" if claim == "verify" else "incorrect"
        got = {r.get("task_id") for r in rows
               if r.get("label") != want and r.get("prediction") == want}
        if got != expected[claim]:
            raise SystemExit(
                f"sec:analysis names the wrong entries for {claim}: the run "
                f"file says {sorted(got)}, the section argues about "
                f"{sorted(expected[claim])}. Update the prose and this guard "
                f"together."
            )
        hits, pop, bad, _, _ = arm(rows, claim)
        out[f"an{claim}bad"] = bad
        out[f"an{claim}claims"] = hits + bad
        out[f"an{claim}pop"] = pop

    # Each direction of label error is only visible to one of the two tasks,
    # so check that the entries are where that claim says they must be.
    if not flipped_muts <= expected["verify"]:
        raise SystemExit("flipped mutants must appear in the verification arm")
    if missing_pre != expected["refute"]:
        raise SystemExit(
            f"every bad refutation should be attributed to an unstated "
            f"precondition; the run file says {sorted(expected['refute'])} and "
            f"the attribution covers {sorted(missing_pre)}."
        )

    out["anfliprefs"] = len(flipped_refs)
    out["anflipmuts"] = len(flipped_muts)
    out["anmisspre"] = len(missing_pre)
    # The prose names this entry, so derive the number from the same set the
    # guard checks rather than repeating it in the text.
    out["anflipmutid"] = sorted(flipped_muts)[0].split("/")[1]
    out["anflipboth"] = len(flipped_refs) + len(flipped_muts)
    out["anundet"] = len(undetermined)
    # The corpus is one reference and one mutant per task, so correcting a
    # label moves an entry from one half to the other.
    out["ancorrpos"] = out["anverifypop"] - len(flipped_refs) + len(flipped_muts)
    out["ancorrneg"] = out["anrefutepop"] + len(flipped_refs) - len(flipped_muts)

    # Rescore with the disputed pairs removed entirely. A reviewer's first
    # question about a hand-adjudicated label is whether the conclusion depends
    # on it, and dropping the whole pair -- reference and mutant together --
    # answers it without needing our reading to be accepted: the corpus is
    # balanced by construction, so removing a task keeps it balanced.
    vrows, rrows = load(LEMMA4_VERIFY), load(LEMMA4_REFUTE)
    tasks = {r["task_id"] for r in vrows}
    disputed = flipped_refs | flipped_muts
    if not (disputed | undetermined) <= tasks:
        raise SystemExit("a disputed entry is not in the corpus")

    def rescore(drop: set[str]) -> tuple[int, int, int, int, int]:
        keep = tasks - drop
        vp = sum(1 for r in vrows if r["task_id"] in keep
                 and r["label"] == "correct" and r["prediction"] == "correct")
        vb = sum(1 for r in vrows if r["task_id"] in keep
                 and r["label"] != "correct" and r["prediction"] == "correct")
        rp = sum(1 for r in rrows if r["task_id"] in keep
                 and r["label"] == "incorrect" and r["prediction"] == "incorrect")
        rb = sum(1 for r in rrows if r["task_id"] in keep
                 and r["label"] != "incorrect" and r["prediction"] == "incorrect")
        return len(keep), vp, vb, rp, rb

    for tag, drop in (("cln", disputed), ("und", disputed | missing_pre)):
        n, vp, vb, rp, rb = rescore(drop)
        out[f"an{tag}pairs"] = n
        out[f"an{tag}entries"] = 2 * n
        out[f"an{tag}vproved"] = vp
        out[f"an{tag}vbad"] = vb
        out[f"an{tag}vprec"] = f"{100.0 * vp / (vp + vb):.0f}" if vp + vb else "--"
        out[f"an{tag}rrefuted"] = rp
        out[f"an{tag}rbad"] = rb
        out[f"an{tag}rprec"] = f"{100.0 * rp / (rp + rb):.0f}" if rp + rb else "--"

    # Rescore against CORRECTED labels rather than by dropping entries. This is
    # the number the per-tier table used to carry in a ``prec*'' column, moved
    # here because a corrected figure belongs next to the evidence for the
    # correction rather than in a table of raw results.
    #
    # Both arms ran on all 206 entries and are scored on the half the arm is
    # about, so flipping a label moves an entry between the two populations and
    # changes recall as well as precision -- which is why this cannot be done by
    # adjusting a precision cell alone.
    def relabel(r: dict) -> str:
        lab, t = r.get("label"), r.get("task_id")
        if lab == "correct" and t in flipped_refs:
            return "incorrect"
        if lab == "incorrect" and t in flipped_muts:
            return "correct"
        return lab

    relabel_rows = {}
    for claim, rows in (("verify", vrows), ("refute", rrows)):
        want = "correct" if claim == "verify" else "incorrect"
        cells = []
        for tag, lab in (("raw", lambda r: r.get("label")), ("corr", relabel)):
            pop = sum(1 for r in rows if lab(r) == want)
            claims = [r for r in rows if r["prediction"] == want]
            good = sum(1 for r in claims if lab(r) == want)
            bad = len(claims) - good
            out[f"an{claim}{tag}pop"] = pop
            out[f"an{claim}{tag}good"] = good
            out[f"an{claim}{tag}bad"] = bad
            out[f"an{claim}{tag}prec"] = f"{100.0 * good / len(claims):.0f}" if claims else "--"
            out[f"an{claim}{tag}rec"] = f"{100.0 * good / pop:.0f}" if pop else "--"
            out[f"an{claim}{tag}fone"] = f"{2 * good / (good + pop + bad):.2f}"
            cells.append((pop, good, bad))
        relabel_rows[claim] = cells

    # Correcting the label is a uniform improvement here, and that is worth
    # asserting rather than assuming: no reference changes class under this
    # policy, so nothing the verification arm proved can become an unsound
    # proof in exchange for the mutant the correction rescues.
    proved_bad_refs = {r["task_id"] for r in vrows
                       if r.get("label") == "correct" and r["prediction"] == "correct"
                       and r["task_id"] in flipped_refs}
    if proved_bad_refs:
        raise SystemExit(
            f"no reference should change class under the stated policy, but "
            f"{sorted(proved_bad_refs)} would. Update the prose and this guard "
            f"together."
        )

    rl = (
        "% generated by nfv/paper/make_compare_tables.py -- do not edit by hand\n"
        r"\begin{table}[t]\centering\footnotesize" "\n"
        r"\caption{The reported run rescored against the corrected labels. "
        r"\emph{given} scores against the benchmark's labels as published, as "
        r"every other table in this paper does; \emph{corrected} moves the "
        r"\anflipboth{} entry argued above to the other class. Because both "
        r"arms ran on all \anverifypop{} pairs and each is scored on the half "
        r"it is about, a correction changes the population as well as the "
        r"claims, so recall moves too. The \anmisspre{} references whose "
        r"specification omits a precondition keep their published label, per "
        r"the policy of Section~\ref{sec:analysis}.}" "\n"
        r"\label{tab:relabel}" "\n"
        r"\begin{tabular}{llrrrr}" "\n"
        r"\toprule" "\n"
        r"& labels & proved/refuted & \emph{bad} & \emph{prec} & \emph{F1} \\" "\n"
        r"\midrule" "\n"
    )
    for claim, name in (("verify", "verification"), ("refute", "refutation")):
        for tag, lbl in (("raw", "given"), ("corr", "corrected")):
            head = rf"\multirow{{2}}{{*}}{{{name}}}" if tag == "raw" else ""
            g = out[f"an{claim}{tag}good"]
            p = out[f"an{claim}{tag}pop"]
            b = out[f"an{claim}{tag}bad"]
            pr = out[f"an{claim}{tag}prec"]
            f1 = out[f"an{claim}{tag}fone"]
            bold = (lambda s: rf"\textbf{{{s}}}") if tag == "corr" else (lambda s: s)
            rl += (rf"{head} & {lbl} & {bold(f'{g}/{p}')} ({out[f'an{claim}{tag}rec']}\%) "
                   rf"& {b} & {bold(pr + chr(92) + '%')} & {bold(f1)} \\" "\n")
        rl += r"\midrule" "\n" if claim == "verify" else ""
    rl += (r"\bottomrule" "\n" r"\end{tabular}" "\n" r"\end{table}" "\n")
    (TABLES / "relabel.tex").write_text(rl, encoding="utf-8")
    print("wrote relabel.tex")

    # The same four entries as a table. Written here rather than by hand so
    # that it cannot drift from the sets the guard above checks: every row is
    # keyed by an entry that has just been verified to be in the arm the
    # ``exposed by'' column names.
    #
    # The fourth field is the fact whose status as a PRECONDITION is what the
    # dispute actually turns on, or None where the disagreement is of some
    # other kind. This is the organizing claim of the subsection, so it is
    # recorded per entry and counted, not asserted.
    reasons = {
        "HumanEval/11": (r"\texttt{string\_xor}", "refutation",
                         r"postcondition asserts $|rv|=|a|=|b|$ but nothing "
                         r"requires equal-length operands, so no implementation "
                         r"satisfies it when they differ",
                         r"$|a| = |b|$"),
        "HumanEval/20": (r"\texttt{find\_closest\_elements}", "refutation",
                         r"witness is a pair of equal elements with "
                         r"$\neg(x \le x)$, admitted because \texttt{PyFloat} "
                         r"is abstract and realisable in Python as "
                         r"\texttt{NaN}, so no pair satisfies $x \le y$",
                         r"no \texttt{NaN} in \texttt{numbers}"),
        "HumanEval/64": (r"\texttt{vowels\_count}", "refutation",
                         r"specification counts vowels in \texttt{s.lower()} "
                         r"while the program tests the un-lowercased character, "
                         r"and \texttt{\textbackslash u0130} lowercases to two "
                         r"code points",
                         r"\texttt{s} is ASCII"),
        "HumanEval/116": (r"\texttt{sort\_array}", "verification",
                          r"the two implementations differ in the sort key only "
                          r"for negative values, which the docstring's "
                          r"``non-negative integers'' excludes",
                          r"\texttt{arr} is non-negative \emph{(stated)}"),
        "HumanEval/49": (r"\texttt{modp}", "refutation",
                         r"witness is a \emph{negative} modulus "
                         r"(\texttt{n == 0}, \texttt{p == -1}); the docstring "
                         r"(``return $2^n$ modulo $p$'') fixes no sign, and "
                         r"under $p > 0$ the reference holds on every sampled "
                         r"input",
                         r"$p > 0$"),
        "HumanEval/79": (r"\texttt{decimal\_to\_binary}", "refutation",
                         r"witness is a negative input: "
                         r"\texttt{bin(-3)[2:]} is \texttt{'b11'}, so the "
                         r"result carries a non-binary character and fails the "
                         r"\texttt{\textasciicircum db[01]+db\$} match",
                         r"$decimal \ge 0$"),
        "HumanEval/111": (r"\texttt{histogram}", "refutation",
                          r"postcondition scores a key with "
                          r"\texttt{test.count(key)}, which counts "
                          r"\emph{substrings} while the program counts "
                          r"whitespace-separated words; the two agree only "
                          r"when every token is one character",
                          r"tokens are single letters"),
    }
    order = ("HumanEval/11", "HumanEval/20", "HumanEval/49", "HumanEval/64",
             "HumanEval/79", "HumanEval/111", "HumanEval/116")
    body = []
    for eid in order:
        name, found, why, atissue = reasons[eid]
        num = eid.split("/")[1]
        effect = (r"label $\to$ \emph{correct}" if eid in flipped_muts
                  else "label kept")
        body.append(rf"\texttt{{{num}}} & {name} & {found} & "
                    rf"{atissue if atissue else '---'} & {effect} & "
                    rf"\footnotesize {why} \\")

    # Every one of these disputes is an argument about the precondition, which
    # is the organizing claim of the subsection, so it is recorded per entry and
    # counted rather than asserted.
    prec_disputes = [e for e in order if reasons[e][3] is not None]
    out["anprecdisp"] = len(prec_disputes)
    out["anprecdisptotal"] = len(order)
    if set(order) != (missing_pre | flipped_muts):
        raise SystemExit("tab:flipped rows and the attributed sets disagree")
    if len(prec_disputes) != len(order):
        raise SystemExit("every row of tab:flipped should name a precondition")

    table = (
        "% generated by nfv/paper/make_compare_tables.py -- do not edit by hand\n"
        r"\begin{table}[t]\centering\footnotesize" "\n"
        r"\caption{Every entry on which a verdict and a ground-truth label "
        r"disagree because the specification omits a precondition. "
        r"\emph{exposed by} is the task whose claim revealed it. "
        r"\emph{omitted} is the fact that, had it been stated, would have made "
        r"the verdict and the label agree; all \anprecdisptotal{} rows name "
        r"one, and only \texttt{116} has it written in the docstring already. "
        r"The reference of a pair keeps its label by policy, so the omission "
        r"changes a label only for \texttt{116}, which is a mutant. Every row "
        r"is executed rather than asserted.}" "\n"
        r"\label{tab:flipped}" "\n"
        r"\begin{tabular}{lllp{0.13\textwidth}p{0.11\textwidth}p{0.28\textwidth}}" "\n"
        r"\toprule" "\n"
        r"\# & function & exposed by & omitted & effect & why \\" "\n"
        r"\midrule" "\n"
        + "\n".join(body) + "\n"
        r"\bottomrule" "\n"
        r"\end{tabular}" "\n"
        r"\end{table}" "\n"
    )
    (TABLES / "flipped.tex").write_text(table, encoding="utf-8")
    print("wrote flipped.tex:", len(body), "rows")

    # The evidence behind each row: the docstring the precondition was read
    # from and the postcondition the entry was scored against. Emitted from the
    # benchmark file rather than retyped, because the whole argument of the
    # section is that these two texts do not say what the label says.
    blob = json.loads((ROOT / "nfv/data/nl2post_safe.json").read_text(encoding="utf-8"))
    records: dict[str, dict] = {}

    def walk(o):
        if isinstance(o, dict):
            eid = o.get("entry_id")
            if isinstance(eid, str):
                m = re.match(r"HumanEval_(\d+)__.*__(reference|mutant)$", eid)
                if m:
                    records.setdefault(f"{m.group(1)}/{m.group(2)}", o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(blob)
    docs = []
    for eid in order:
        num = eid.split("/")[1]
        # 116 is the entry whose *mutant* we dispute; the rest are references.
        kind = "mutant" if eid in flipped_muts else "reference"
        rec = records.get(f"{num}/{kind}") or records.get(f"{num}/reference")
        if rec is None:
            continue
        name, _, _, _ = reasons[eid]
        doc = " ".join((rec.get("docstring") or "").split())
        post = (rec.get("postcondition") or "").strip()
        docs.append(
            rf"\paragraph{{\texttt{{HumanEval\_{num}}} ({name}).}}\leavevmode" "\n"
            r"\begin{lstlisting}[style=fig,language=Python,"
            rf"title={{docstring, as supplied}}]" "\n"
            + doc + "\n"
            r"\end{lstlisting}" "\n"
            r"\begin{lstlisting}[style=fig,language=Python,"
            rf"title={{postcondition, as supplied}}]" "\n"
            + post + "\n"
            r"\end{lstlisting}" "\n"
        )
    (TABLES / "flipped_docs.tex").write_text("\n".join(docs), encoding="utf-8")
    print("wrote flipped_docs.tex:", len(docs), "entries")

    out["anbadtotal"] = out["anverifybad"] + out["anrefutebad"]
    claims = out["anrefuteclaims"]
    right = claims - out["anrefutebad"] + len(mislabeled)
    out["anrefprecadj"] = f"{100.0 * right / claims:.0f}"
    out["anrefprecraw"] = f"{100.0 * (claims - out['anrefutebad']) / claims:.0f}"
    lines = [rf"\newcommand{{\{k}}}{{{v}}}" for k, v in out.items()]
    # A TeX control sequence is letters only, so a macro named e.g. ``...f1''
    # silently parses as ``...f'' followed by a stray ``1'' and the build fails
    # somewhere else entirely. Catch it here, where the name is chosen.
    bad_names = sorted(k for k in out if not k.isalpha())
    if bad_names:
        raise SystemExit(f"macro names must be letters only: {bad_names}")
    (TABLES / "analysis_numbers.tex").write_text("\n".join(lines) + "\n",
                                                 encoding="utf-8")
    print("wrote analysis_numbers.tex:", out)

def main():
    TABLES.mkdir(parents=True, exist_ok=True)
    files, stale = resolve_weakmodel()
    (TABLES / "weakmodel.tex").write_text(
        build("weakmodel", COMPARISONS["weakmodel"]), encoding="utf-8")
    weak_macros(files, stale)
    analysis_macros()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
