"""Private subprocess entrypoint: no GitHub or model credentials are needed."""

import json
import subprocess
import sys
from pathlib import Path


def main():
    from nfv.backends.dafny import run_dafny, type_check
    from nfv.shared.config import DAFNY_PATH

    source, destination, timeout, *options = sys.argv[1:]
    operation = options[0] if options else "verify"
    symbol = options[1] if len(options) > 1 else ""
    path = Path(source).resolve()
    if operation == "resolve":
        result = type_check(path.read_text(encoding="utf-8"), timeout=float(timeout),
                            workdir=path.parent)
    elif operation == "verify":
        result = run_dafny(path.read_text(encoding="utf-8"), mode="unbounded",
                          timeout=float(timeout), workdir=path.parent,
                          filter_symbol=symbol or None)
    else:
        raise ValueError("Unsupported verifier operation")
    record = result.to_dict()
    version = subprocess.run([DAFNY_PATH, "--version"], capture_output=True, text=True, timeout=10)
    record["verifier_version"] = version.stdout.strip()
    Path(destination).write_text(json.dumps(record, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
