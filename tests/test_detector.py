from __future__ import annotations

import json
import threading
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



def test_fleet_feeds_are_distinct_held_out_sequences(trained):
    feeds = replay.fleet_feeds(trained.test_rows, step=8)
    assert list(feeds) == ["M-01", "M-02", "M-03"]
    seen = set()
    for k, rows in enumerate(feeds.values()):
        assert rows == replay.sequence(trained.test_rows[k::3], step=8)
        ids = {row["UDI"] for row in rows}
        assert ids and not seen.intersection(ids)
        seen.update(ids)


def test_fleet_dry_run_prints_four_contract_alerts_without_requests(trained, monkeypatch, capsys):
    monkeypatch.setattr(replay, "ensure_data", lambda path: path)
    monkeypatch.setattr(replay, "load_rows", lambda path: [])
    monkeypatch.setattr(replay, "train", lambda rows, threshold: trained)

    def unexpected_request(*args, **kwargs):
        pytest.fail("dry-run must not send alerts or reset the factory")

    monkeypatch.setattr(replay.httpx, "post", unexpected_request)
    assert replay.main(["--fleet", "--scenario", "fleet", "--dry-run", "--interval", "0"]) == 0
    output = capsys.readouterr().out
    decoder = json.JSONDecoder()
    alerts = [decoder.raw_decode(chunk)[0] for chunk in output.split("ALERT: ")[1:]]
    assert [alert["machine_id"] for alert in alerts] == ["M-01", "M-02", "M-03", "M-04"]
    for alert in alerts[:3]:
        parsed = Alert(**alert)
        assert parsed.failure_type == "tool_wear" and not parsed.simulated
        assert 0 <= parsed.remaining_cutting_min <= 45
        assert parsed.predicted_failure_at > parsed.detected_at
    coolant = Alert(**alerts[3])
    assert coolant.simulated and coolant.failure_type == "coolant_pump"
    assert coolant.tool_wear_min is None and coolant.remaining_cutting_min is None
    assert coolant.risk == 0.8
    assert coolant.predicted_failure_at - coolant.detected_at == timedelta(hours=40)
    # Each feed starts three replay ticks after the preceding one.
    lines = [line for line in output.splitlines() if "tool wear " in line and " | torque " in line]
    assert [line[:4] for line in lines[:5]] == ["M-01", "M-01", "M-01", "M-01", "M-02"]
    assert next(i for i, line in enumerate(lines) if line.startswith("M-03")) == 11


def test_fleet_sends_in_background_after_reset(trained, monkeypatch):
    monkeypatch.setattr(replay, "ensure_data", lambda path: path)
    monkeypatch.setattr(replay, "load_rows", lambda path: [])
    monkeypatch.setattr(replay, "train", lambda rows, threshold: trained)
    coolant_sent = threading.Event()
    sent = []
    main_thread = threading.get_ident()

    def post(url, **kwargs):
        if url.endswith("/admin/reset"):
            assert not sent and kwargs["params"] == {"scenario": "fleet"}
            sent.append("reset")
        else:
            assert sent[:2] == ["reset", "clear"]
            assert threading.get_ident() != main_thread
            machine = kwargs["json"]["machine_id"]
            sent.append(machine)
            if machine == "M-04":
                coolant_sent.set()
            else:
                # Earlier requests remain blocked while all other machines replay and send.
                assert coolant_sent.wait(timeout=10)
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    def delete(url, **kwargs):  # the replay clears the previous run's incidents after the reset
        assert sent == ["reset"] and url.endswith("/incidents")
        sent.append("clear")
        return httpx.Response(200, json={"cleared": 0}, request=httpx.Request("DELETE", url))

    monkeypatch.setattr(replay.httpx, "post", post)
    monkeypatch.setattr(replay.httpx, "delete", delete)
    assert replay.main(["--fleet", "--scenario", "fleet", "--interval", "0"]) == 0
    assert sent[:2] == ["reset", "clear"] and sorted(sent[2:]) == ["M-01", "M-02", "M-03", "M-04"]


def test_fleet_reports_background_send_failure(trained, monkeypatch):
    def fail(alert):
        raise httpx.ConnectError("agent offline")

    monkeypatch.setattr(replay, "send_alert", fail)
    assert replay.replay_fleet(trained, 20, 45, 0.5, 8, 3, 0, False) == 1


def test_fleet_prints_one_line_per_result():
    line = replay.summary({"status": "awaiting_approval", "part_number": "EM-1004", "purchase_order_id": "PO-0001",
                           "booking_id": "BK-0001", "engineer_name": "Priya Nair", "booking_start": "2026-10-16T15:00:00Z",
                           "warnings": [], "trace": [{"tool": "search_manual", "result": "pages of manual text"}]})
    assert line == "awaiting_approval: EM-1004, PO-0001, Priya Nair 2026-10-16T15:00:00Z" and "\n" not in line
    escalated = replay.summary({"status": "escalated", "part_number": "CP-220", "escalation_reason": "Pump arrives too late."})
    assert escalated == "escalated: CP-220, no order, no booking (Pump arrives too late.)"
