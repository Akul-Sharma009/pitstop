"""Hosting: the screens' password, the model status and a plain message when the model is offline."""

from __future__ import annotations

import base64

import httpx
from fastapi.testclient import TestClient

from agent import api, runner
from app import server
from devtools.fakes import sample_alert


def test_screens_ask_for_a_password_when_one_is_set(monkeypatch):
    monkeypatch.setenv("SCREENS_PASSWORD", "s3cret")
    client = TestClient(server.app)
    refused = client.get("/")
    assert refused.status_code == 401 and refused.headers["www-authenticate"].startswith("Basic")
    assert client.post("/api/purchase-orders/PO-0001/approve").status_code == 401  # approvals too
    wrong = base64.b64encode(b"manager:nope").decode()
    assert client.get("/", headers={"Authorization": f"Basic {wrong}"}).status_code == 401
    right = base64.b64encode(b"manager:s3cret").decode()
    assert client.get("/", headers={"Authorization": f"Basic {right}"}).status_code == 200


def test_screens_are_open_without_a_password(monkeypatch):
    monkeypatch.delenv("SCREENS_PASSWORD", raising=False)
    assert TestClient(server.app).get("/").status_code == 200


def test_status_reports_agent_and_model(monkeypatch):
    def agent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "backend": "evolus", "model": "offline"})

    monkeypatch.setattr(server, "clients", {"agent": httpx.Client(base_url="http://agent", transport=httpx.MockTransport(agent))})
    assert TestClient(server.app).get("/api/status").json() == {"agent": "online", "model": "offline", "backend": "evolus"}

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(server, "clients", {"agent": httpx.Client(base_url="http://agent", transport=httpx.MockTransport(down))})
    assert TestClient(server.app).get("/api/status").json() == {"agent": "offline", "model": "unknown"}


def test_model_status_checks_the_model_endpoint(monkeypatch):
    monkeypatch.setenv("VLLM_BASE_URL", "http://gpu:8080/v1")
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "gpu" in url:
            raise httpx.ConnectError("GPU instance is off")
        return httpx.Response(200)

    monkeypatch.setattr(api.httpx, "get", fake_get)
    monkeypatch.setattr(api, "_model_check", {})
    assert api.model_status() == "offline" and calls == ["http://gpu:8080/v1/models"]
    assert api.model_status() == "offline" and len(calls) == 1  # reused for a while
    monkeypatch.setattr(api, "_model_check", {})
    monkeypatch.setenv("VLLM_BASE_URL", "http://ok/v1")
    assert api.model_status() == "online"


class Unreachable:
    def __init__(self, exc: Exception):
        self.exc = exc

    def run(self, alert, replan=None):
        raise self.exc


def test_an_offline_model_gives_a_plain_reason():
    import openai

    exc = openai.APIConnectionError(request=httpx.Request("POST", "http://gpu:8080/v1/chat/completions"))
    result = runner.run(sample_alert(), agent=Unreachable(exc), backend="local")
    assert result.status == "failed" and result.escalation_reason == runner.MODEL_OFFLINE
    assert any("APIConnectionError" in w for w in result.warnings)  # the technical detail stays for the manager
    evolus = runner.run(sample_alert(), agent=Unreachable(RuntimeError("422 own_models.unreachable")), backend="evolus")
    assert evolus.escalation_reason == runner.MODEL_OFFLINE
    other = runner.run(sample_alert(), agent=Unreachable(ValueError("bad plan")), backend="local")
    assert other.escalation_reason == "ValueError: bad plan"
