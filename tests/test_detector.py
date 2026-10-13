from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agent.models import Alert
from detector import replay
from detector.model import load_rows, train, wear_limit
from tests.ai4i_fixture import write_fixture

NOW = datetime(2026, 10, 14, 9, 20, 30, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    return train(load_rows(write_fixture(tmp_path_factory.mktemp("ai4i") / "ai4i2020.csv")))


def test_model_catches_most_held_out_failures(trained):
    (_, _), (missed, caught) = trained.confusion
    assert caught >= missed and caught > 0


def test_wear_limit_is_in_the_failure_band(trained):
    healthy = [r for r in trained.test_rows if r["TWF"] == "0" and r["Tool wear [min]"] < 150]
    limits = [wear_limit(trained.model, [r]) for r in healthy[:20]]
    found = [limit for limit in limits if limit is not None]  # some odd readings never look like wear failure
    assert len(found) >= 0.8 * len(limits) and all(180 <= limit <= 245 for limit in found)


def test_sequence_is_one_machine_with_rising_healthy_wear(trained):
    rows = replay.sequence(trained.test_rows, step=8)
    wears = [r["Tool wear [min]"] for r in rows]
    assert wears == sorted(wears) and all(b - a >= 8 for a, b in zip(wears, wears[1:]))
    assert all(r["Machine failure"] == "0" for r in rows)


def test_replay_alerts_once_within_the_horizon_and_matches_the_contract(trained):
    rows = replay.sequence(trained.test_rows, step=8)
    seen = list(replay.watch(trained.model, rows, horizon=45, threshold=0.5))
    recent, remaining = seen[-1]
    row = recent[-1]
    assert remaining is not None and 45 - 8 - 1 <= remaining <= 45  # one wear step inside the horizon
    assert all(r is None or r > 45 for _, r in seen[:-1])

    alert = replay.make_alert(trained.model, recent, remaining, NOW, cutting_min_per_day=20, horizon=45)
    parsed = Alert(**alert)
    assert parsed.machine_id == "M-01" and parsed.failure_type == "tool_wear"
    assert alert["detected_at"] == "2026-10-14T09:20:00+00:00"
    expected = NOW.replace(second=0) + timedelta(days=remaining / 20)
    assert parsed.predicted_failure_at == expected
    assert 0.5 <= alert["risk"] <= 1  # the alert and the risk agree
    assert set(alert["readings"]) == {"air_temperature_k", "process_temperature_k", "rotational_speed_rpm", "torque_nm"}


def test_main_resets_the_scenario_and_sends_the_alert(trained, tmp_path, monkeypatch):
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/reset":
            sent["scenario"] = request.url.params["scenario"]
            return httpx.Response(200, json={"scenario": sent["scenario"]})
        sent["alert"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "awaiting_approval", "warnings": []})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(replay.httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw))
    monkeypatch.setenv("AI4I_CSV", str(write_fixture(tmp_path / "ai4i2020.csv")))
    assert replay.main(["--scenario", "in_stock", "--interval", "0"]) == 0
    assert sent["scenario"] == "in_stock" and sent["alert"]["machine_id"] == "M-01"


def test_one_odd_reading_does_not_hide_the_wear_limit(trained):
    healthy = [r for r in trained.test_rows if r["Machine failure"] == "0" and r["Tool wear [min]"] < 150]
    flagged = [r for r in healthy if wear_limit(trained.model, [r]) is not None][:2]
    odd = next(r for r in healthy if wear_limit(trained.model, [r]) is None)
    latest = {**odd, "Tool wear [min]": max(r["Tool wear [min]"] for r in flagged)}
    assert wear_limit(trained.model, [*flagged, latest]) is not None


def test_rows_load_with_a_byte_order_mark(tmp_path):
    assert "UDI" in load_rows(write_fixture(tmp_path / "ai4i2020.csv", rows=5))[0]
