#!/usr/bin/env python3
"""
Test generation to validate nl2post specifications.

For each 'correct' entry, generates test inputs using an LLM, runs the reference
code, and checks if the postcondition holds. Entries where the reference code
fails the postcondition have buggy specs (false ground truth).
"""

import asyncio
import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "verifiers" / "cbmc"))

TESTGEN_SYSTEM = """You are an expert Python test generator.

Given a Python function and its postcondition, generate diverse test inputs that exercise different paths,
edge cases, boundary conditions, and adversarial inputs.

Output a Python script that:
1. Defines the function (copy it exactly)
2. Calls the function with each test input
3. Checks the postcondition on the return value
4. Prints "PASS" if all tests pass, "FAIL: <reason>" if any test fails

The postcondition uses `return_value` to refer to the function's return value.
Make the script self-contained. No imports beyond what the function needs.
Output ONLY ```python ... ``` code block."""

TESTGEN_PROMPT = """Generate {num_tests} diverse test inputs for this function and postcondition:

```python
{code}
```

Postcondition (must hold for ALL valid inputs):
```
{postcondition}
```

Generate {num_tests} diverse test inputs covering:
- Normal cases
- Edge cases (empty lists, zero, negative, single element, large values)
- Boundary conditions
- Random-like diverse values

Run the function on each, check the postcondition on the return value.
Print "PASS" if all hold, "FAIL: <details>" with the first failing input if any fails."""


async def check_entry(entry, client, model, semaphore, num_tests=100):
    """Generate tests and run them to check if reference code satisfies spec."""
    from py2c import call_llm_async

    task_id = entry["task_id"]
    code = entry.get("code", "")
    postcondition = entry.get("postcondition", "")
    entry_point = entry.get("entry_point", "")

    async with semaphore:
        prompt = TESTGEN_PROMPT.format(code=code, postcondition=postcondition, num_tests=num_tests)
        resp = await call_llm_async(
            client=client,
            system_prompt=TESTGEN_SYSTEM,
            user_prompt=prompt,
            model=model,
        )

        if resp.get("error") or not resp.get("response"):
            return {"task_id": task_id, "entry_point": entry_point,
                    "status": "error", "reason": "llm_failed"}

        # Extract Python code
        import re
        m = re.search(r"```(?:python)?\s*\n(.*?)```", resp["response"], re.DOTALL)
        if not m:
            return {"task_id": task_id, "entry_point": entry_point,
                    "status": "error", "reason": "no_code_extracted"}

        test_code = m.group(1)

        # Run the test
        import subprocess
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(test_code)
            tmp_path = f.name

        try:
            result = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True, timeout=30,
                text=True,
            )
            output = (result.stdout + result.stderr).strip()

            if result.returncode != 0:
                # Runtime error
                return {"task_id": task_id, "entry_point": entry_point,
                        "status": "runtime_error", "output": output[:500]}
            elif "FAIL" in output:
                return {"task_id": task_id, "entry_point": entry_point,
                        "status": "spec_fails", "output": output[:500]}
            elif "PASS" in output:
                return {"task_id": task_id, "entry_point": entry_point,
                        "status": "pass"}
            else:
                return {"task_id": task_id, "entry_point": entry_point,
                        "status": "unclear", "output": output[:500]}
        except subprocess.TimeoutExpired:
            return {"task_id": task_id, "entry_point": entry_point,
                    "status": "timeout"}
        finally:
            os.unlink(tmp_path)


async def main():
    parser = argparse.ArgumentParser(description="nl2post spec validation via test generation")
    parser.add_argument("--dataset", default=str(SCRIPT_DIR / "nl2post_dataset.json"))
    parser.add_argument("--model", default="claude-sonnet-4.5")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--output", default="results/nl2post_spec_validation.json")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--num-tests", type=int, default=100,
                        help="Number of test inputs to generate per entry")
    args = parser.parse_args()

    with open(args.dataset) as f:
        data = json.load(f)
    entries = data if isinstance(data, list) else data.get('results', data.get('specs', []))

    # Only check 'correct' entries — these should satisfy the postcondition
    correct_entries = [e for e in entries if e.get('label') == 'correct']
    if args.limit:
        correct_entries = correct_entries[:args.limit]

    print(f"nl2post Spec Validation (test generation)")
    print(f"Entries: {len(correct_entries)} correct | Model: {args.model} | Concurrency: {args.concurrency}")
    print()

    from copilot import CopilotClient
    client = CopilotClient(log_level="error")
    await client.start()

    semaphore = asyncio.Semaphore(args.concurrency)
    start = time.time()

    try:
        tasks = [check_entry(e, client, args.model, semaphore, num_tests=args.num_tests) for e in correct_entries]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await client.stop()

    # Process results
    valid_results = []
    for r in results:
        if isinstance(r, Exception):
            valid_results.append({"status": "exception", "reason": str(r)[:100]})
        else:
            valid_results.append(r)

    elapsed = time.time() - start

    # Summary
    statuses = {}
    for r in valid_results:
        s = r.get("status", "unknown")
        statuses[s] = statuses.get(s, 0) + 1

    print(f"{'='*60}")
    print(f"RESULTS ({len(valid_results)} entries, {elapsed:.1f}s):")
    for s, cnt in sorted(statuses.items(), key=lambda x: -x[1]):
        print(f"  {s}: {cnt}")

    spec_fails = [r for r in valid_results if r.get("status") == "spec_fails"]
    print(f"\nSpec failures (reference code violates postcondition): {len(spec_fails)}")
    for r in spec_fails[:10]:
        print(f"  {r.get('task_id')} ({r.get('entry_point')}): {r.get('output','')[:100]}")

    # Save
    output = {
        "config": {"model": args.model, "concurrency": args.concurrency, "num_tests": args.num_tests},
        "summary": statuses,
        "results": valid_results,
    }
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved to: {args.output}")

    # Generate nl2post_verified subset: only task_ids where correct passes spec
    passing_task_ids = set()
    for r in valid_results:
        if r.get("status") == "pass":
            passing_task_ids.add(r.get("task_id"))

    # Build verified subset: 1 correct + 1 incorrect per passing task_id
    all_entries = data if isinstance(data, list) else data.get('results', data.get('specs', []))
    verified_subset = []
    for e in all_entries:
        if e.get("task_id") in passing_task_ids:
            verified_subset.append(e)

    # Count pairs
    verified_correct = sum(1 for e in verified_subset if e.get("label") == "correct")
    verified_incorrect = sum(1 for e in verified_subset if e.get("label") == "incorrect")

    verified_path = str(Path(args.output).parent / "nl2post_verified_subset.json")
    with open(verified_path, "w") as f:
        json.dump(verified_subset, f, indent=2)

    print(f"\nnl2post_verified subset: {len(verified_subset)} entries "
          f"({verified_correct} correct + {verified_incorrect} incorrect)")
    print(f"Excluded {len(correct_entries) - len(passing_task_ids)} task_ids where reference code fails spec")
    print(f"Saved to: {verified_path}")


if __name__ == "__main__":
    asyncio.run(main())
