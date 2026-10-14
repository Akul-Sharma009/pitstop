# Evolus setup (runbook)

Goal: the Pitstop agent runs inside Evolus on our model, with the manual in a knowledge library and the factory tools from our MCP server. Our agent service drives it through Evolus agent chat (`AGENT_BACKEND=evolus`).

Source: the Evolus developer page (evolus.ai/en/developers) and the API reference inside the workspace. Fix this file as you go.

## What Evolus gives us

| Way in | URL | Auth |
|---|---|---|
| Agent chat (OpenAI-compatible) | `https://api.evolus.ai/v1` | `Authorization: Bearer <application API key>` |
| REST API (workflows, libraries, extractors, AI Tools) | `https://api.evolus.ai/api/v1` | `X-ApiKey` + `X-Application-Id`, or `X-User-ApiKey` |
| Evolus MCP server (Evolus as tools for AI clients) | `https://api.evolus.ai/mcp` | same as REST |

- **Agent chat:** `model` is `<applicationId>:<agentId>`, and `GET /v1/models` lists them. Messages are text; temperature and other settings come from the agent. A request without `X-Conversation-Id` starts a new conversation (we send one per alert). The reply is the agent's final text only, so our trace comes from the mock factory's call log (`GET /admin/calls`, see `docs/contracts.md`).
- **Workflows:** `POST /api/v1/workflows/{id}/execute` (multipart: `input`, `parameters`) returns `202` with an `executionId`. Then poll `GET …/executions/{executionId}` until `Completed` or `Failed`. A workflow that is off answers `409`.
- **Libraries:** `POST /api/v1/memories/{libraryId}/documents?language=en` (multipart `file`). Indexing runs in the background and uses platform credits. Set `language=en`: the default is Italian.
- **Your model:** any OpenAI-compatible endpoint with function calling (base URL, model name, API key), with a connection test. Calls to our own model use no Evolus credits.
- **Limits:** 60 requests/min and 50,000 platform credits per workspace (credits pay for document reading, AI Tools and indexing). Out of credits → `402`.
- **External tools:** *Your company → Connectors → Add manually* takes our MCP server (type HTTP, a URL, fixed header credentials). Confirmed 14 Oct: Evolus lists all 5 tools.

## 1. Put the model and the MCP server online (stand-in: Ollama on the Mac)

Until vLLM runs on AMD, Evolus reaches the Ollama stand-in through a Cloudflare quick tunnel. Ollama has no API key of its own, so Caddy (compose profile `tunnel`) only passes `/v1` requests that carry `VLLM_API_KEY`. The stand-in is for development only: never in screenshots or the video as the AMD model.

In `.env`, set `VLLM_API_KEY` and `MCP_API_TOKEN` to long random values (`openssl rand -hex 24`), then:

```bash
docker compose --profile tunnel up -d --build
cloudflared tunnel --url http://localhost:8088      # prints https://<random>.trycloudflare.com
```

Check from another terminal (`T=https://<random>.trycloudflare.com`):

```bash
curl -s -o /dev/null -w "%{http_code}\n" $T/v1/models                                   # 401
curl -s $T/v1/models -H "Authorization: Bearer $VLLM_API_KEY"                            # Ollama's models
```

The address changes every time the tunnel restarts; update it in Evolus when it does.

With vLLM on AMD, use the HTTPS host from `setup-guide.md` steps 3–5 instead (`docker compose --profile public up -d --build`).

## 2. Workspace and model

- [ ] Workspace "Machine Whisperers" (event workspace, created from the event link).
- [ ] **Your models**: add our endpoint, then test the connection.
  - Stand-in: base URL `$T/v1`, model `qwen3:30b-a3b-instruct-2507-q4_K_M`, API key `VLLM_API_KEY`.
  - AMD: base URL `https://<IP>.sslip.io/v1`, model `Qwen/Qwen3-30B-A3B-Instruct-2507`, API key `VLLM_API_KEY`.
- [ ] Screenshot the AMD endpoint settings for the submission (not the stand-in).

## 3. Building blocks

- [x] **Knowledge library**: upload `manual/cnc_mill_m01.md` (the upload screen had no language option; retrieval works in English). Re-upload it whenever the manual changes: delete the old document first, so the agent never sees two versions.
- [x] **Factory tools**: *Your company → Connectors → Add manually*: type HTTP, URL `$T/mcp`, header `Authorization: Bearer $MCP_API_TOKEN`, credentials Fixed. Evolus lists 5 tools: `check_inventory`, `get_supplier_quote`, `draft_purchase_order`, `find_free_engineers`, `draft_booking` (it files the two `draft_` tools under "Mail"; harmless). There is no approve tool, on purpose. Evolus strips spaces from connector names. When the tunnel address changes, edit the URL here and in Your models.
- [ ] **Agent**: our model; instructions = the system prompt; attach the library and the tools.
  Print the instructions with `python scripts/evolus_text.py prompt` (the Evolus version of the system prompt: the manual is the knowledge library, and the final reply is the JSON plan instead of a `submit_plan` call).
- [ ] **Workflow**: diagnose → check stock → order (only if out of stock) → book engineer (or escalate) → summary.

Test in the Evolus chat with an alert dated now, exactly as our backend sends it. Reset the mock factory first, so the engineer slots line up with now:

```bash
curl -X POST 'localhost:8000/admin/reset?scenario=out_of_stock'
python scripts/evolus_text.py alert
```

Then check what the agent did: `curl -s localhost:8000/admin/calls | python -m json.tool`.

## 4. Drive it from our agent service

- [ ] Create an application API key on the workspace's API keys page → `EVOLUS_API_KEY` in `.env`. The same page shows the space ID (`X-Application-Id`, for the REST API).
- [ ] `python scripts/evolus_check.py` lists the agents; put ours in `.env` as `EVOLUS_AGENT_ID` and run it again: the agent should answer.
- [ ] Run it end to end: `AGENT_BACKEND=evolus` in `.env`, `docker compose up -d --build`, then `docker compose run --build --rm detector --scenario out_of_stock`. The plan should come back with a trace, a purchase order and a booking, and no warnings.

## Submission evidence

Screenshots of: the AMD model endpoint, the knowledge library, the factory tools with their 5 tools, the workflow, the API keys page (key hidden), and a run log of an API call.
