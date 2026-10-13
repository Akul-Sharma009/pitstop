"""Replay AI4I readings as one machine's live feed and send an Alert when the tool nears its wear limit.

The feed for M-01 is a rising-tool-wear sequence we build from held-out AI4I rows (healthy readings, one per
wear step); AI4I itself has no time series. Calendar time comes from a stated assumption:
CUTTING_MIN_PER_DAY minutes of cutting per day.

Usage: python -m detector.replay [--scenario out_of_stock|in_stock|no_engineer] [--dry-run] [--interval S]
Env: ALERT_URL (agent's /incidents), MOCKS_URL (for --scenario), CUTTING_MIN_PER_DAY (20),
     ALERT_HORIZON_MIN (45), WEAR_STEP_MIN (8), DETECTOR_THRESHOLD (0.5), AI4I_CSV (data/ai4i2020.csv).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from detector.model import DATA, ensure_data, load_rows, score, train, wear_limit

MACHINE = "M-01"


def sequence(test_rows: list[dict[str, Any]], step: float) -> list[dict[str, Any]]:
    """One healthy held-out reading per `step` minutes of tool wear, in rising order."""
    healthy = sorted((r for r in test_rows if r.get("Machine failure") in ("0", 0)), key=lambda r: int(r["UDI"]))
    picked, target = [], 0.0
    for row in sorted(healthy, key=lambda r: r["Tool wear [min]"]):
        if row["Tool wear [min]"] >= target:
            picked.append(row)
            target = row["Tool wear [min]"] + step
    return picked


def readings(row: dict[str, Any]) -> dict[str, float]:
    return {
        "air_temperature_k": row["Air temperature [K]"],
        "process_temperature_k": row["Process temperature [K]"],
        "rotational_speed_rpm": row["Rotational speed [rpm]"],
        "torque_nm": row["Torque [Nm]"],
    }


def watch(model: Any, rows: list[dict[str, Any]], horizon: float, threshold: float) -> Iterator[tuple[dict, float | None]]:
    """Yield (reading, remaining cutting minutes or None) until the first reading within the horizon."""
    for row in rows:
        limit = wear_limit(model, row, threshold)
        remaining = None if limit is None else limit - row["Tool wear [min]"]
        yield row, remaining
        if remaining is not None and remaining <= horizon:
            return


def make_alert(model: Any, row: dict[str, Any], remaining: float, now: datetime, cutting_min_per_day: float,
               horizon: float) -> dict[str, Any]:
    """The Alert of docs/contracts.md. risk: the model's score for these readings at the end of the horizon."""
    at_horizon = {**row, "Tool wear [min]": row["Tool wear [min]"] + horizon}
    detected = now.replace(second=0, microsecond=0)
    return {
        "machine_id": MACHINE,
        "failure_type": "tool_wear",
        "risk": round(score(model, at_horizon), 2),
        "tool_wear_min": round(row["Tool wear [min]"]),
        "remaining_cutting_min": round(remaining),
        "detected_at": detected.isoformat(),
        "predicted_failure_at": (detected + timedelta(days=remaining / cutting_min_per_day)).isoformat(),
        "readings": readings(row),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", help="reset the mock factory to this demo scenario first")
    parser.add_argument("--dry-run", action="store_true", help="print the alert instead of sending it")
    parser.add_argument("--interval", type=float, default=float(os.environ.get("REPLAY_INTERVAL_S", 0.5)),
                        help="seconds between readings")
    args = parser.parse_args(argv)
    env = os.environ.get
    per_day, horizon = float(env("CUTTING_MIN_PER_DAY", 20)), float(env("ALERT_HORIZON_MIN", 45))
    threshold, step = float(env("DETECTOR_THRESHOLD", 0.5)), float(env("WEAR_STEP_MIN", 8))

    trained = train(load_rows(ensure_data(Path(env("AI4I_CSV") or DATA))), threshold)
    (normal, false_alarms), (missed, caught) = trained.confusion
    print(f"Failure model (AI4I 2020, synthetic): on {len(trained.test_rows)} held-out rows caught {caught} of "
          f"{caught + missed} tool-wear failures, {false_alarms} false alarms.")
    print(f"Replaying {MACHINE} from held-out AI4I readings; assumption: {per_day:g} cutting minutes per day.\n")

    if args.scenario and not args.dry_run:
        mocks = env("MOCKS_URL", "http://localhost:8000")
        httpx.post(f"{mocks}/admin/reset", params={"scenario": args.scenario}, timeout=10).raise_for_status()
        print(f"Mock factory reset to scenario {args.scenario}.")

    row: dict[str, Any] = {}
    remaining: float | None = None
    for row, remaining in watch(trained.model, sequence(trained.test_rows, step), horizon, threshold):
        left = "no failure in sight" if remaining is None else f"about {remaining:.0f} cutting min to the wear limit"
        print(f"{MACHINE} tool wear {row['Tool wear [min]']:>5.0f} min | torque {row['Torque [Nm]']:>4.1f} Nm | {left}")
        time.sleep(args.interval)
    if remaining is None or remaining > horizon:
        print("The replay ended without an alert.")
        return 1

    alert = make_alert(trained.model, row, remaining, datetime.now(timezone.utc), per_day, horizon)
    print(f"\nALERT: {json.dumps(alert, indent=2)}")
    if args.dry_run:
        return 0
    url = env("ALERT_URL", "http://localhost:8002/incidents")
    print(f"\nSending to {url} (the agent plans the repair; this can take a minute)...")
    result = httpx.post(url, json=alert, timeout=600).raise_for_status().json()
    print(json.dumps({k: result.get(k) for k in ("status", "part_number", "purchase_order_id", "expected_delivery",
                                                  "booking_id", "engineer_name", "booking_start", "escalation_reason",
                                                  "operator_summary", "warnings")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
