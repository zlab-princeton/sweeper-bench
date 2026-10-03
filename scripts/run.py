"""Run Sweeper Bench directly from a checkout; no package installation required."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cli import main

if __name__ == "__main__":
    raise SystemExit(main())
