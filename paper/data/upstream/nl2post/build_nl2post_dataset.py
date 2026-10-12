#!/usr/bin/env python3
"""
Build nl2post_dataset.json: 100 specs x 2 (reference + mutant) = 200 entries.

For each spec (task_id, postcondition):
  - reference code: postcondition is sound (correct label)
  - mutant code: postcondition assertion fails on a specific test (incorrect label)
  - killing_test: the BOPI input that triggers AssertionError on the mutant

Data sources (in experiments/eval_results/):
  - gpt4_simple_no_ref.zip: nl2post evaluation results (individual_postcondition_results)
  - distinct_code_mutants.jsonl.zip: mutant code with BOPI test inputs

Only includes entries where:
  1. The mutant raises AssertionError (not other runtime errors)
  2. Verified by execution: mutant fails, reference passes on killing_test

Usage:
    python experiments/build_nl2post_dataset.py
"""

import json
import os
import sys
import copy
import ast
import zipfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EVAL_ZIP = os.path.join(SCRIPT_DIR, 'eval_results', 'gpt4_simple_no_ref.zip')
MUT_ZIP = os.path.join(SCRIPT_DIR, 'eval_results', 'distinct_code_mutants.jsonl.zip')
OUTPUT_PATH = os.path.join(SCRIPT_DIR, 'nl2post_dataset.json')

MIN_KILL_RATE = 0.0
MAX_SPECS_PER_PROBLEM = 1
TARGET_SPECS = 999  # no cap — take all available


def change_func_name_in_code(code, old_name, new_name):
    code_ast = ast.parse(code)
    for node in ast.walk(code_ast):
        if isinstance(node, ast.FunctionDef) and node.name == old_name:
            node.name = new_name
    return ast.unparse(code_ast)


def get_func_args(func):
    func_ast = ast.parse(func)
    return [arg.arg for arg in func_ast.body[0].args.args]


def wrap_code(code, entry_point, postcondition):
    """Wrap code with postcondition (nl2post style from response_preprocessing.py)."""
    new_name = entry_point + '_original'
    code = change_func_name_in_code(code, entry_point, new_name)
    code = code.replace(entry_point + '(', new_name + '(')
    postcondition = postcondition.replace(entry_point + '(', new_name + '(')
    func_start = code.find('def ' + new_name)
    args_start = func_start + len('def ' + new_name)
    args_end = code.find(':\n', args_start)
    full_func = code[func_start:args_end + 2] + '\n    pass\n'
    argsDec = code[args_start:args_end] + ':\n'
    argsCall = '(' + ', '.join(get_func_args(full_func)) + ')'
    toAdd = '\n'.join(['    ' + line for line in postcondition.split('\n')])
    wrapper = (
        f"\ndef {entry_point}{argsDec}"
        f"\n    return_value = {new_name}{argsCall}"
        f"\n    import re"
        f"\n{toAdd}"
        f"\n    return return_value\n"
    )
    return code + '\n' + wrapper


def verify_kills(code, entry_point, postcondition, killing_test):
    """Returns True if executing code+postcondition on killing_test raises AssertionError."""
    try:
        wrapped = wrap_code(code, entry_point, postcondition)
        ns = {}
        exec(wrapped, ns)
        ns[entry_point](*copy.deepcopy(killing_test))
        return False
    except AssertionError:
        return True
    except Exception:
        return False


def main():
    print("Loading evaluation results zip...")
    ze = zipfile.ZipFile(EVAL_ZIP)

    # Load summary: plausible postconditions
    summary_content = ze.read('gpt4_simple_no_ref/summary_eval_results.jsonl').decode('utf-8')
    all_specs = [json.loads(l) for l in summary_content.strip().split('\n')]
    plausible = [s for s in all_specs if s.get('test_set_correct') == True]
    print(f"Plausible specs: {len(plausible)}")

    print("Loading mutants with BOPI...")
    zm = zipfile.ZipFile(MUT_ZIP)
    mutants = {}
    with zm.open('distinct_code_mutants.jsonl') as f:
        for line in f:
            entry = json.loads(line)
            key = (entry['task_id'], entry['response_num'])
            mutants[key] = entry

    print("Loading EvalPlus reference code...")
    from evalplus.data import get_human_eval_plus
    evalplus_data = get_human_eval_plus()

    # For each plausible spec, find a mutant killed via AssertionError + get killing test
    valid_pairs = []

    for spec in plausible:
        task_id = spec['task_id']
        resp_num = spec['response_num']
        ep = spec['entry_point']
        postcondition = spec['post_condition']
        task_dir = task_id.replace('/', '_')

        if task_id not in evalplus_data:
            continue

        # Load eval results for this postcondition
        fname = f'gpt4_simple_no_ref/individual_postcondition_results/{task_dir}/postcondition_{resp_num}.json'
        try:
            content = ze.read(fname)
        except KeyError:
            continue
        result = json.loads(content)
        d = result[task_id]
        buggy_nums = d['buggy_code_response_num']
        test_results = d['test_results']

        # Find a mutant killed via AssertionError with available BOPI
        found = False
        for mut_idx, mut_num in enumerate(buggy_nums):
            mut_key = (task_id, mut_num)
            if mut_key not in mutants:
                continue

            tr = test_results[mut_idx]
            kills = tr[1]
            errors = tr[2]

            # Find first BOPI index with AssertionError kill
            kill_bopi_idx = None
            for i, (k, e) in enumerate(zip(kills, errors)):
                if k == 1 and e is not None and 'AssertionError' in str(e):
                    kill_bopi_idx = i
                    break

            if kill_bopi_idx is None:
                continue

            bopi = mutants[mut_key]['unique_bopi']
            if kill_bopi_idx >= len(bopi):
                continue

            killing_test = bopi[kill_bopi_idx]
            mutant_code = mutants[mut_key]['solution']
            ref_entry = evalplus_data[task_id]
            reference_code = ref_entry['prompt'] + ref_entry['canonical_solution']

            # Verify by execution: mutant fails with AssertionError
            if not verify_kills(mutant_code, ep, postcondition, killing_test):
                continue

            # Verify reference passes
            try:
                wrapped_ref = wrap_code(reference_code, ep, postcondition)
                ns = {}
                exec(wrapped_ref, ns)
                ns[ep](*copy.deepcopy(killing_test))
            except AssertionError:
                continue
            except Exception:
                continue

            # Count kill rate (AssertionError only)
            n_assertion_kills = sum(
                1 for k, e in zip(kills, errors)
                if k == 1 and e is not None and 'AssertionError' in str(e)
            )
            kill_rate = n_assertion_kills / len(kills) if kills else 0

            valid_pairs.append({
                'task_id': task_id,
                'entry_point': ep,
                'response_num': resp_num,
                'reference_code': reference_code,
                'mutant_code': mutant_code,
                'mutant_response_num': mut_num,
                'postcondition': postcondition,
                'killing_test': killing_test,
                'killing_test_bopi_idx': kill_bopi_idx,
                'kill_rate': kill_rate,
            })
            found = True
            break  # one mutant per spec is enough

    print(f"Execution-verified pairs: {len(valid_pairs)}")

    # Select: max per problem, top by kill rate, up to TARGET_SPECS
    by_problem = {}
    for p in valid_pairs:
        tid = p['task_id']
        if tid not in by_problem:
            by_problem[tid] = []
        by_problem[tid].append(p)

    selected = []
    for tid, pairs in by_problem.items():
        pairs_sorted = sorted(pairs, key=lambda x: -x['kill_rate'])
        selected.extend(pairs_sorted[:MAX_SPECS_PER_PROBLEM])

    selected = sorted(selected, key=lambda x: -x['kill_rate'])
    if len(selected) > TARGET_SPECS:
        selected = selected[:TARGET_SPECS]

    # Build output: 2 entries per spec (reference + mutant)
    output_specs = []
    for p in selected:
        output_specs.append({
            'task_id': p['task_id'],
            'entry_point': p['entry_point'],
            'response_num': p['response_num'],
            'code': p['reference_code'],
            'postcondition': p['postcondition'],
            'label': 'correct',
            'code_type': 'reference',
            'killing_test': None,
        })
        output_specs.append({
            'task_id': p['task_id'],
            'entry_point': p['entry_point'],
            'response_num': p['response_num'],
            'code': p['mutant_code'],
            'postcondition': p['postcondition'],
            'label': 'incorrect',
            'code_type': 'mutant',
            'mutant_response_num': p['mutant_response_num'],
            'killing_test': p['killing_test'],
            'killing_test_bopi_idx': p['killing_test_bopi_idx'],
            'kill_rate': p['kill_rate'],
        })

    n_problems = len(set(p['task_id'] for p in selected))
    output = {
        'description': 'nl2post dataset: 100 specs x 2 (reference + mutant) = 200 entries, execution-verified.',
        'source': 'intent-formalization/nl2postcondition-fse2024 (GPT4/SimplePromptNoRef)',
        'labeling': {
            'correct': 'Postcondition sound on reference (verified: passes on killing_test)',
            'incorrect': 'Mutant fails postcondition assertion on killing_test (verified: AssertionError)',
        },
        'total': len(output_specs),
        'num_specs': len(selected),
        'num_problems': n_problems,
        'num_correct': len(selected),
        'num_incorrect': len(selected),
        'specs': output_specs,
    }

    with open(OUTPUT_PATH, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"\n=== Dataset Created ===")
    print(f"{len(selected)} specs from {n_problems} problems, {len(output_specs)} entries")
    print(f"Saved to: {OUTPUT_PATH}")


if __name__ == '__main__':
    main()
