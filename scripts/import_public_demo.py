"""Import the checked-in public web snapshot into the local application DB.

Usage from the repository root:
    python scripts/import_public_demo.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.public_demo import PUBLIC_DATA_PATH, import_public_demo


def main() -> int:
    parser = argparse.ArgumentParser(description="Import public-web demo snapshot")
    parser.add_argument("--source", type=Path, default=PUBLIC_DATA_PATH)
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args()
    result = import_public_demo(args.source, args.db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
