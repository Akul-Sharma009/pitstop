"""HTTP server for the mock factory systems (docs/contracts.md section 3).

Run: uvicorn mocks.server:app --port 8000
Before each demo run: curl -X POST 'localhost:8000/admin/reset?scenario=out_of_stock'
Env: MOCKS_SEED (default mocks/seed.json), MOCKS_SCENARIO (default out_of_stock).
"""

from __future__ import annotations

import os

import httpx
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from mocks.factory import SEED, MockFactory, load_seed

factory = MockFactory(load_seed(os.environ.get("MOCKS_SEED") or SEED), os.environ.get("MOCKS_SCENARIO", "out_of_stock"))


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] != "http":
        return
    request = Request(scope, receive)
    reply = factory.handle(httpx.Request(request.method, str(request.url), content=await request.body()))
    await Response(reply.content, reply.status_code, media_type="application/json")(scope, receive, send)
