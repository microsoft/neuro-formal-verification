#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Verify nl2post_dataset.json: for each spec, confirm:
  - incorrect (mutant): killing_test triggers AssertionError via postcondition
  - correct (reference): killing_test from paired mutant does NOT trigger AssertionError

Uses the same wrapping logic as the nl2post evaluation (response_preprocessing.py).
"""

import json
import os
import sys
import copy
import ast

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_PATH = os.path.join(SCRIPT_DIR, 'nl2post_dataset.json')


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
    """Wrap code with postcondition check (nl2post style)."""
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


def verify_entry(entry, killing_test):
    """
    Run code + postcondition on killing_test.
    Returns: 'assertion_error', 'passed', 'other_error:<type>'
    """
    ep = entry['entry_point']
    try:
        wrapped = wrap_code(entry['code'], ep, entry['postcondition'])
    except Exception as e:
        return f'wrap_error:{type(e).__name__}'

    try:
        ns = {}
        exec(wrapped, ns)
        ns[ep](*copy.deepcopy(killing_test))
        return 'passed'
    except AssertionError:
        return 'assertion_error'
    except Exception as e:
        return f'other_error:{type(e).__name__}'


def main():
    print(f"Loading dataset from {DATASET_PATH}...")
    with open(DATASET_PATH) as f:
        ds = json.load(f)

    specs = ds['specs']
    print(f"Total entries: {ds['total']} ({ds['num_correct']} correct, {ds['num_incorrect']} incorrect)")
    print()

    # Group by (task_id, response_num) to pair correct/incorrect
    pairs = {}
    for s in specs:
        key = (s['task_id'], s['response_num'])
        if key not in pairs:
            pairs[key] = {}
        pairs[key][s['label']] = s

    mut_pass = 0
    mut_fail = 0
    ref_pass = 0
    ref_fail = 0
    errors = []

    for (task_id, resp_num), pair in pairs.items():
        if 'incorrect' not in pair or 'correct' not in pair:
            errors.append(f"  MISSING PAIR: {task_id} resp={resp_num}")
            continue

        mut = pair['incorrect']
        ref = pair['correct']
        killing_test = mut.get('killing_test')

        if killing_test is None:
            errors.append(f"  NO KILLING TEST: {task_id} resp={resp_num}")
            continue

        # Verify mutant: should get AssertionError
        mut_result = verify_entry(mut, killing_test)
        if mut_result == 'assertion_error':
            mut_pass += 1
        else:
            mut_fail += 1
            errors.append(f"  MUTANT SHOULD FAIL: {task_id} resp={resp_num} got={mut_result}")

        # Verify reference: should pass (no error)
        ref_result = verify_entry(ref, killing_test)
        if ref_result == 'passed':
            ref_pass += 1
        else:
            ref_fail += 1
            errors.append(f"  REF SHOULD PASS: {task_id} resp={resp_num} got={ref_result}")

    print(f"=== Verification Results ===")
    print(f"Mutant (should AssertionError): {mut_pass}/{mut_pass + mut_fail} passed")
    print(f"Reference (should pass):        {ref_pass}/{ref_pass + ref_fail} passed")
    print()

    if errors:
        print(f"Errors ({len(errors)}):")
        for e in errors[:20]:
            print(e)
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")
        sys.exit(1)
    else:
        print("ALL VERIFIED OK")
        sys.exit(0)


if __name__ == '__main__':
    main()
