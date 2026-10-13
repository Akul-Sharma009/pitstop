"""A small synthetic stand-in for ai4i2020.csv (same columns), so tests need no download.

Tool-wear failures happen between 200 and 240 minutes of wear, as in AI4I."""

from __future__ import annotations

import csv
import random
from pathlib import Path

COLUMNS = ["UDI", "Product ID", "Type", "Air temperature [K]", "Process temperature [K]", "Rotational speed [rpm]",
           "Torque [Nm]", "Tool wear [min]", "Machine failure", "TWF", "HDF", "PWF", "OSF", "RNF"]


def write_fixture(path: Path, rows: int = 3000, seed: int = 7) -> Path:
    rng = random.Random(seed)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for udi in range(1, rows + 1):
            air = round(rng.gauss(300, 2), 1)
            wear = rng.randint(0, 253)
            twf = int(200 <= wear <= 240 and rng.random() < 0.25)
            writer.writerow([udi, f"L{udi:05d}", "L", air, round(air + 10 + rng.gauss(0, 1), 1),
                             round(rng.gauss(1538, 120)), round(max(5.0, rng.gauss(40, 10)), 1), wear,
                             twf, twf, 0, 0, 0, 0])
    return path
