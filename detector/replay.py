"""Replay AI4I readings as live machine feeds and alert when tools near their wear limits.

The feed for M-01 is a rising-tool-wear sequence we build from held-out AI4I rows (healthy readings, one per
wear step); AI4I itself has no time series. The wear limit (detector/model.py) uses the model's score averaged
over the last few readings, since one odd reading can swing it. Calendar time comes from a stated assumption:
CUTTING_MIN_PER_DAY minutes of cutting per day. Fleet mode partitions held-out rows among three
tool-wear feeds, staggered by three readings, then sends an explicitly simulated coolant-pump alert.

Usage: python -m detector.replay [--fleet] [--scenario out_of_stock|in_stock|no_engineer|fleet] [--dry-run] [--interval S]
Env: ALERT_URL (agent's /incidents), MOCKS_URL (for --scenario), CUTTING_MIN_PER_DAY (20),
     ALERT_HORIZON_MIN (45), WEAR_STEP_MIN (8), WEAR_LIMIT_WINDOW (3), DETECTOR_THRESHOLD (0.5),
     AI4I_CSV (data/ai4i2020.csv).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from detector.model import DATA, ensure_data, load_rows, peak_score, train, wear_limit

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


def watch(model: Any, rows: list[dict[str, Any]], horizon: float, threshold: float,
          window: int = 3) -> Iterator[tuple[list[dict], float | None]]:
    """Yield (last `window` readings, remaining cutting minutes or None) until the estimate is within the horizon."""
    recent: list[dict[str, Any]] = []
    for row in rows:
        recent = [*recent, row][-window:]
        limit = wear_limit(model, recent, threshold)
        remaining = None if limit is None else limit - row["Tool wear [min]"]
        yield recent, remaining
        if remaining is not None and remaining <= horizon:
            return


def make_alert(model: Any, recent: list[dict[str, Any]], remaining: float, now: datetime, cutting_min_per_day: float,
               horizon: float, machine_id: str = MACHINE) -> dict[str, Any]:
    """The Alert of docs/contracts.md for the latest reading.

    risk: the model's highest score, averaged over the recent readings, within the horizon (uncalibrated).
    """
    row = recent[-1]
    detected = now.replace(second=0, microsecond=0)
    return {
        "machine_id": machine_id,
        "failure_type": "tool_wear",
        "risk": round(peak_score(model, recent, horizon), 2),
        "tool_wear_min": round(row["Tool wear [min]"]),
        "remaining_cutting_min": round(remaining),
        "detected_at": detected.isoformat(),
        "predicted_failure_at": (detected + timedelta(days=remaining / cutting_min_per_day)).isoformat(),
        "readings": readings(row),
    }


def fleet_feeds(test_rows: list[dict[str, Any]], step: float) -> dict[str, list[dict[str, Any]]]:
    """Partition held-out rows before sequencing: machines never share readings."""
    return {f"M-{k + 1:02d}": sequence(test_rows[k::3], step) for k in range(3)}


def send_alert(alert: dict[str, Any]) -> None:
    url = os.environ.get("ALERT_URL", "http://localhost:8002/incidents")
    print(f"Sending {alert['machine_id']} to {url} (the agent plans the repair; this can take a minute)...")
    result = httpx.post(url, json=alert, timeout=600).raise_for_status().json()
    print(f"{alert['machine_id']} result: {summary(result)}")


def summary(result: dict[str, Any]) -> str:
    """One line per machine (the full plan, with its trace, is on the screens)."""
    order = result.get("purchase_order_id") or "no order"
    booking = (f"{result.get('engineer_name')} {result.get('booking_start')}" if result.get("booking_id")
               else "no booking")
    reason = f" ({result.get('escalation_reason')})" if result.get("status") != "awaiting_approval" else ""
    warnings = f", {len(result['warnings'])} warning(s)" if result.get("warnings") else ""
    return f"{result.get('status')}: {result.get('part_number')}, {order}, {booking}{reason}{warnings}"


def replay_fleet(trained: Any, per_day: float, horizon: float, threshold: float, step: float,
                 window: int, interval: float, dry_run: bool) -> int:
    feeds = fleet_feeds(trained.test_rows, step)
    watchers = {machine: watch(trained.model, rows, horizon, threshold, window)
                for machine, rows in feeds.items()}
    starts = {machine: k * 3 for k, machine in enumerate(feeds)}
    limits: dict[str, float] = {}
    alerted: set[str] = set()
    pending: list[tuple[str, Future]] = []
    tick = 0
    # Four workers let every alert start sending even while earlier agents are planning.
    with ThreadPoolExecutor(max_workers=4) as sender:
        def emit(alert: dict[str, Any]) -> None:
            print(f"\nALERT: {json.dumps(alert, indent=2)}")
            if not dry_run:
                pending.append((alert["machine_id"], sender.submit(send_alert, alert)))

        while watchers:
            for machine in list(watchers):
                if tick < starts[machine]:
                    continue
                try:
                    recent, remaining = next(watchers[machine])
                except StopIteration:
                    print(f"{machine}: the replay ended without an alert.")
                    del watchers[machine]
                    continue
                row = recent[-1]
                if remaining is not None:
                    limits[machine] = row["Tool wear [min]"] + remaining
                    left = f"about {remaining:.0f} cutting min to the wear limit"
                elif machine in limits:
                    left = f"about {max(limits[machine] - row['Tool wear [min]'], 0):.0f} cutting min (previous estimate)"
                else:
                    left = "no estimate yet"
                print(f"{machine} tool wear {row['Tool wear [min]']:>5.0f} min | torque {row['Torque [Nm]']:>4.1f} Nm | {left}")
                if remaining is not None and remaining <= horizon:
                    emit(make_alert(trained.model, recent, remaining, datetime.now(timezone.utc),
                                    per_day, horizon, machine_id=machine))
                    alerted.add(machine)
                    del watchers[machine]
            tick += 1
            if watchers:
                time.sleep(interval)

        now = datetime.now(timezone.utc)
        emit({
            "machine_id": "M-04",
            "failure_type": "coolant_pump",
            "risk": 0.8,
            "simulated": True,
            "tool_wear_min": None,
            "remaining_cutting_min": None,
            "detected_at": now.isoformat(),
            "predicted_failure_at": (now + timedelta(hours=40)).isoformat(),
            "readings": {},
        })
        failed = len(alerted) != 3
        for machine, future in pending:
            try:
                future.result()
            except Exception as exc:
                print(f"{machine}: alert sending failed: {exc}", file=sys.stderr)
                failed = True
    return int(failed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fleet", action="store_true", help="replay four machines sharing one factory")
    parser.add_argument("--scenario", help="reset the mock factory to this demo scenario first")
    parser.add_argument("--dry-run", action="store_true", help="print the alert instead of sending it")
    parser.add_argument("--interval", type=float, default=float(os.environ.get("REPLAY_INTERVAL_S", 0.5)),
                        help="seconds between readings")
    args = parser.parse_args(argv)
    env = os.environ.get
    per_day, horizon = float(env("CUTTING_MIN_PER_DAY", 20)), float(env("ALERT_HORIZON_MIN", 45))
    threshold, step = float(env("DETECTOR_THRESHOLD", 0.5)), float(env("WEAR_STEP_MIN", 8))
    window = int(env("WEAR_LIMIT_WINDOW", 3))

    trained = train(load_rows(ensure_data(Path(env("AI4I_CSV") or DATA))), threshold)
    (normal, false_alarms), (missed, caught) = trained.confusion
    print(f"Failure model (AI4I 2020, synthetic): on {len(trained.test_rows)} held-out rows caught {caught} of "
          f"{caught + missed} tool-wear failures, {false_alarms} false alarms.")
    print(f"Replaying {'M-01/M-02/M-03/M-04' if args.fleet else MACHINE} from held-out AI4I readings; assumption: {per_day:g} cutting minutes per day.\n")

    if args.scenario and not args.dry_run:
        mocks = env("MOCKS_URL", "http://localhost:8000")
        httpx.post(f"{mocks}/admin/reset", params={"scenario": args.scenario}, timeout=10).raise_for_status()
        print(f"Mock factory reset to scenario {args.scenario}.")
        try:  # the screens should not show the previous run's plans next to the new ones
            httpx.delete(env("ALERT_URL", "http://localhost:8002/incidents"), timeout=10).raise_for_status()
        except httpx.HTTPError as exc:
            print(f"Could not clear the agent's previous incidents: {exc}", file=sys.stderr)

    if args.fleet:
        return replay_fleet(trained, per_day, horizon, threshold, step, window, args.interval, args.dry_run)

    recent: list[dict[str, Any]] = []
    remaining: float | None = None
    limit = None  # the last wear limit, shown when the latest readings give none (they never alert)
    for recent, remaining in watch(trained.model, sequence(trained.test_rows, step), horizon, threshold, window):
        row = recent[-1]
        if remaining is not None:
            limit = row["Tool wear [min]"] + remaining
            left = f"about {remaining:.0f} cutting min to the wear limit"
        elif limit is not None:
            left = f"about {max(limit - row['Tool wear [min]'], 0):.0f} cutting min (previous estimate)"
        else:
            left = "no estimate yet"
        print(f"{MACHINE} tool wear {row['Tool wear [min]']:>5.0f} min | torque {row['Torque [Nm]']:>4.1f} Nm | {left}")
        time.sleep(args.interval)
    if remaining is None or remaining > horizon:
        print("The replay ended without an alert.")
        return 1

    alert = make_alert(trained.model, recent, remaining, datetime.now(timezone.utc), per_day, horizon)
    print(f"\nALERT: {json.dumps(alert, indent=2)}")
    if args.dry_run:
        return 0
    url = env("ALERT_URL", "http://localhost:8002/incidents")
    print(f"\nSending to {url} (the agent plans the repair; this can take a minute)...")
    result = httpx.post(url, json=alert, timeout=600).raise_for_status().json()
    print(json.dumps({k: result.get(k) for k in ("status", "part_number", "repair_steps", "purchase_order_id", "expected_delivery",
                                                  "booking_id", "engineer_name", "booking_start", "escalation_reason",
                                                  "operator_summary", "downtime", "warnings")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
