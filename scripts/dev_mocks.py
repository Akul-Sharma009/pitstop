"""Temporary mock factory server (devtools.fakes.FakeFactory over HTTP) until mocks/ exists.

Lets the MCP server and Evolus be tested against the real endpoints in docs/contracts.md.
Run: uvicorn scripts.dev_mocks:app --port 8000
Switch scenario: curl -X POST 'localhost:8000/admin/reset?scenario=in_stock'
"""

from __future__ import annotations

import os

import httpx
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from devtools.fakes import FakeFactory

factory = FakeFactory(os.environ.get("DEV_SCENARIO", "out_of_stock"))


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] != "http":
        return
    request = Request(scope, receive)
    reply = factory.handle(httpx.Request(request.method, str(request.url), content=await request.body()))
    await Response(reply.content, reply.status_code, media_type="application/json")(scope, receive, send)
