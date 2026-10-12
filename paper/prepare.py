"""Materialize frozen benchmark programs without changing the released dataset."""
import json
from pathlib import Path

from nfv.data.build_safe import materialize


def main():
    path = Path(__file__).resolve().parents[1] / "nfv/data/nl2post_safe.json"
    entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    materialize(entries)
    print(f"Materialized {len(entries)} frozen programs under nfv/work.")


if __name__ == "__main__":
    main()
