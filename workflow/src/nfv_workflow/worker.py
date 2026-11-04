# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Private subprocess entrypoint: no GitHub or model credentials are needed."""

import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path


def main():
    from nfv.backends.dafny import DafnyResult, Outcome, _classify, _parse_errors
    from nfv.shared.config import DAFNY_PATH, Z3_PATH

    source, destination, timeout, *options = sys.argv[1:]
    operation = options[0] if options else "verify"
    symbol = options[1] if len(options) > 1 else ""
    path = Path(source).resolve()
    if operation not in ("resolve", "verify"):
        raise ValueError("Unsupported verifier operation")
    command = [DAFNY_PATH, operation, "--allow-warnings"]
    if operation == "verify":
        command.extend(["--verification-time-limit", str(math.ceil(float(timeout)))])
        if Z3_PATH:
            command.extend(["--solver-path", Z3_PATH])
        if symbol:
            command.append(f"--filter-symbol={symbol}")
    command.append(str(path))
    mode = "typecheck" if operation == "resolve" else "unbounded"
    started = time.monotonic()
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=float(timeout))
    except subprocess.TimeoutExpired:
        result = DafnyResult(Outcome.TIMEOUT, None, "", "", time.monotonic() - started, mode)
    else:
        output = (proc.stdout or "") + (proc.stderr or "")
        outcome = ((Outcome.VERIFIED if proc.returncode == 0 else Outcome.TYPE_ERROR)
                   if operation == "resolve" else _classify(proc.returncode, output))
        if (operation == "verify" and proc.returncode != 0 and re.search(
                r"(?m)^Dafny program verifier finished with [^\n]*\b[1-9]\d* time outs?\b", output)):
            outcome = Outcome.TIMEOUT
        result = DafnyResult(outcome, proc.returncode, proc.stdout, proc.stderr,
                             time.monotonic() - started, mode, errors=_parse_errors(output))
    record = result.to_dict()
    version = subprocess.run([DAFNY_PATH, "--version"], capture_output=True, text=True, timeout=10)
    record["verifier_version"] = version.stdout.strip()
    Path(destination).write_text(json.dumps(record, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
