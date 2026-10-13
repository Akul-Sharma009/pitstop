"""Train the failure model and print its held-out results. Usage: python -m detector.train"""

from __future__ import annotations

import os
from pathlib import Path

from detector.model import DATA, ensure_data, load_rows, train

if __name__ == "__main__":
    rows = load_rows(ensure_data(Path(os.environ.get("AI4I_CSV") or DATA)))
    trained = train(rows, float(os.environ.get("DETECTOR_THRESHOLD", 0.5)))
    (normal, false_alarms), (missed, caught) = trained.confusion
    print(f"{len(rows)} rows, {len(trained.test_rows)} held out.")
    print(f"Tool-wear failures caught {caught}/{caught + missed}, false alarms {false_alarms}, "
          f"normal readings passed {normal}.")
