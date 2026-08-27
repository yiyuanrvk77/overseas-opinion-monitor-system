"""Compatibility entry point for the public-web collector.

The implementation lives in :mod:`scripts.collect_public_web` so the
deployment and scheduled-job documentation has one source of truth.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.collect_public_web import main


if __name__ == "__main__":
    raise SystemExit(main())
