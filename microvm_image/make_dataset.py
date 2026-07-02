"""Build the F1 dataset into ./data for baking into the MicroVM image.

Run from microvm_image/ as part of build_image.sh. Reuses pitwall.data, so the
source is whatever PITWALL_DATA_SOURCE selects:
  * openf1     (default) — real current-season data from the OpenF1 API (network)
  * synthetic            — deterministic simulated data (offline fallback)
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when run from microvm_image/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pitwall.data import build_dataset  # noqa: E402

if __name__ == "__main__":
    out = build_dataset(Path(__file__).resolve().parent / "data")
    print(f"Dataset written to {out}")
