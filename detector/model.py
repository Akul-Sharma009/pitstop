"""Tool-wear failure model on AI4I 2020, and the remaining cutting time it implies.

The model is the teammate's prototype (pitstop_failure_model_polished.ipynb on the failure-predictive-model
branch): a random forest on the five sensor features, 80/20 stratified split, balanced class weights. It is
trained on start-up (about a second), so no pickled model is shipped.

AI4I rows are independent synthetic snapshots, not a time series, so the model cannot say "fails in N
minutes" by itself. We ask it a what-if question instead: for the current readings, at what tool wear does the
model start flagging tool-wear failure? The gap to the current wear is the remaining cutting time. It is an
estimate from the model's learned boundary, not a validated time-to-failure.
"""

from __future__ import annotations

import csv
import io
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import confusion_matrix
from sklearn.model_selection import train_test_split

FEATURES = ["Air temperature [K]", "Process temperature [K]", "Rotational speed [rpm]", "Torque [Nm]", "Tool wear [min]"]
WEAR = FEATURES.index("Tool wear [min]")
DATA = Path("data/ai4i2020.csv")
# AI4I 2020 Predictive Maintenance Dataset, UCI Machine Learning Repository, CC BY 4.0 (Stephan Matzka, 2020).
URL = "https://archive.ics.uci.edu/static/public/601/ai4i+2020+predictive+maintenance+dataset.zip"
MAX_WEAR = 300  # sweep limit; AI4I tool wear never exceeds about 255 min


def ensure_data(path: Path = DATA) -> Path:
    """Download ai4i2020.csv from UCI on first use (the data is not committed)."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(URL, timeout=60) as response:
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
        name = next(n for n in archive.namelist() if Path(n).name.lower() == "ai4i2020.csv")
        path.write_bytes(archive.read(name))
    return path


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as f:
        return [{k: (float(v) if k in FEATURES else v) for k, v in row.items()} for row in csv.DictReader(f)]


def features(rows: list[dict[str, Any]]) -> np.ndarray:
    return np.array([[row[f] for f in FEATURES] for row in rows], dtype=float)


@dataclass
class Trained:
    model: RandomForestClassifier
    test_rows: list[dict[str, Any]]  # held out: never seen in training, used for the replay
    confusion: list[list[int]]  # [[true normal, false alarms], [missed, caught]] on the held-out rows


def train(rows: list[dict[str, Any]], threshold: float = 0.5) -> Trained:
    labels = np.array([int(row["TWF"]) for row in rows])
    train_idx, test_idx = train_test_split(np.arange(len(rows)), test_size=0.2, random_state=42, stratify=labels)
    model = RandomForestClassifier(n_estimators=100, max_depth=5, class_weight="balanced", random_state=42)
    model.fit(features([rows[i] for i in train_idx]), labels[train_idx])
    test_rows = [rows[i] for i in sorted(test_idx)]
    predicted = (model.predict_proba(features(test_rows))[:, 1] >= threshold).astype(int)
    confusion = confusion_matrix(labels[sorted(test_idx)], predicted, labels=[0, 1]).tolist()
    return Trained(model, test_rows, confusion)


def score(model: RandomForestClassifier, row: dict[str, Any]) -> float:
    """The model's tool-wear failure score for one reading (uncalibrated, not a probability)."""
    return float(model.predict_proba(features([row]))[0, 1])


def wear_limit(model: RandomForestClassifier, row: dict[str, Any], threshold: float = 0.5) -> float | None:
    """The lowest tool wear, from the current wear up, at which the model flags failure for these readings."""
    current = row["Tool wear [min]"]
    wears = np.arange(current, MAX_WEAR + 1, 1.0)
    grid = np.repeat(features([row]), len(wears), axis=0)
    grid[:, WEAR] = wears
    flagged = np.nonzero(model.predict_proba(grid)[:, 1] >= threshold)[0]
    return float(wears[flagged[0]]) if len(flagged) else None
