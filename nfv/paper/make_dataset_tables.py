"""Dataset-only generators extracted from make_tables.py."""
from __future__ import annotations

import argparse

import json

import re

from collections import Counter, defaultdict

from pathlib import Path

from nfv.data.build_subset import _calls_used, higher_order_tasks, tier_label

TIERS = ("0", "1", "2a", "2b", "3")

ROOT = Path(__file__).resolve().parents[2]

OUT = Path(__file__).resolve().parent / "tables"

def esc(s: str) -> str:
    for a, b in [("\\", r"\textbackslash{}"), ("_", r"\_"), ("%", r"\%"),
                 ("&", r"\&"), ("#", r"\#"), ("$", r"\$")]:
        s = s.replace(a, b)
    return s

def write(name: str, body: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.tex").write_text(body.rstrip() + "\n", encoding="utf-8")
    print(f"  wrote tables/{name}.tex")

def t_dataset() -> None:
    hof = higher_order_tasks()
    tiers = Counter(tier_label(t, c, hof) for t, c in _calls_used().items())
    tot = sum(tiers.values())
    desc = {"0": "nothing to axiomatize",
            "1": "total built-ins only",
            "2a": "collections, first-order only",
            "2b": "a function passed as an argument",
            "3": "module imports, unmodeled built-ins"}
    rows = "\n".join(
        rf"L{k} & {desc[k]} & {tiers.get(k,0)} & {2*tiers.get(k,0)} & "
        rf"{100*tiers.get(k,0)/tot:.0f}\% \\"
        for k in TIERS)
    write("dataset", rf"""
\begin{{table}}[t]\centering\small
\caption{{\texttt{{nl2post-safe}} by library tier.}}
\label{{tab:dataset}}
\begin{{tabular}}{{llrrr}}
\toprule
Tier & What the program touches & Tasks & Entries & Share \\
\midrule
{rows}
\midrule
\textbf{{All}} & & \textbf{{{tot}}} & \textbf{{{2*tot}}} & \textbf{{100\%}} \\
\bottomrule
\end{{tabular}}
\end{{table}}""")

def t_specs() -> None:
    """Every pair with the postcondition both of its entries are scored against.

    One row per task: the reference and the mutant share a postcondition, so 103
    rows cover all 206 entries. The specification is the artifact a reader needs
    to judge any verdict we report, which is why it is reproduced in full rather
    than summarized.
    """
    def tt(s: str) -> str:
        """Escape for \\texttt and permit line breaks inside a long assertion."""
        sub = {"\\": r"\textbackslash{}", "_": r"\_", "%": r"\%", "&": r"\&",
               "#": r"\#", "$": r"\$", "{": r"\{", "}": r"\}",
               "^": r"\^{}", "~": r"\~{}"}
        out = []
        for ch in s:
            out.append(sub.get(ch, ch))
            if ch in "(),.[]{}=<>+-*/'\"|: ":
                out.append(r"\allowbreak{}")
        return "".join(out)

    safe = json.loads((ROOT / "nfv" / "data" / "nl2post_safe.json")
                      .read_text(encoding="utf-8"))["entries"]
    s1 = {(e["task_id"], e["response_num"]): e
          for e in json.loads((ROOT / "paper/data/upstream/nl2post" / "nl2post_dataset.json")
                              .read_text(encoding="utf-8"))["specs"]
          if e["code_type"] == "mutant"}
    refs = [e for e in safe if e["code_type"] == "reference"]
    rows = []
    for e in sorted(refs, key=lambda e: int(e["task_id"].split("/")[1])):
        task = int(e["task_id"].split("/")[1])
        m = s1[(e["task_id"], e["response_num"])]["mutant_response_num"]
        rows.append(
            rf"{task} & \texttt{{{esc(e['entry_point'])}}} & {e['response_num']} "
            rf"& {m} & \texttt{{{tt(e['postcondition'])}}} \\")

    head = (r"\textbf{task} & \textbf{entry point} & \textbf{p} & \textbf{m} & "
            r"\textbf{postcondition} \\")
    write("specs", rf"""
\begin{{footnotesize}}
\begin{{longtable}}{{@{{}}r l r r p{{0.52\linewidth}}@{{}}}}
\caption{{The {len(rows)} pairs --- all {2*len(rows)} entries --- with the
postcondition each pair is scored against, and the upstream identifiers that pin
it. \textbf{{task}} is the HumanEval number. \textbf{{p}} is the
\texttt{{response\_num}} selecting the postcondition in
\texttt{{summary\_eval\_results.jsonl}} and
\texttt{{raw\_preprocessed\_samples.jsonl}}. \textbf{{m}} is the
\texttt{{response\_num}} selecting the mutant in
\texttt{{distinct\_code\_mutants.jsonl}}; below 200 it is a natural mutant, at
200 and above an artificial one. The reference and the mutant of a task share a
postcondition, so one row covers two entries. Postconditions are reproduced
after sanitization (Appendix~\ref{{sec:dsdetails}}), which is the form the
pipeline sees.}}
\label{{tab:specs}} \\
\toprule
{head}
\midrule
\endfirsthead
\toprule
{head}
\midrule
\endhead
\bottomrule
\endfoot
{chr(10).join(rows)}
\end{{longtable}}
\end{{footnotesize}}
""")

if __name__ == "__main__":
    t_dataset()
    t_specs()
