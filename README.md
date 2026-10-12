# Pitstop

An AI maintenance agent for factories. A failure model spots a CNC milling machine whose cutting tool is wearing out. An agent then plans the whole fix: it finds the repair and the part number in the machine manual, checks the parts store, drafts a purchase order if the part is missing, and books a free engineer after the part arrives and before the machine fails. A manager approves every order and booking, and the operator gets a plain-language summary.

The agent's language model is an open model served with **vLLM on AMD Developer Cloud** (AMD Instinct MI300X, ROCm). Built for the AMD Developer Hackathon ACT III (Intelligent Industry + Evolus tracks).

> Status: work in progress. The agent service and the MCP server are built and tested with fakes but not yet run against the real model; the detector, the mock factory systems and the screens are being built. Interfaces: [`docs/contracts.md`](docs/contracts.md).

## Architecture

```
sensor replay (AI4I) → failure model → app ──alert──▶ agent service ──▶ LLM (vLLM on AMD)
                                        ▲                │
                                        │                ├─ machine manual (knowledge library)
                     manager approves ──┤                └─ MCP server ──▶ inventory, supplier, calendar (mocks)
                                        └── plan: drafts + operator summary
```

| Part | Folder | Port |
|---|---|---|
| LLM: vLLM on AMD Developer Cloud | `deploy/vllm/` | 8000 on the GPU instance |
| MCP server (factory tools for the agent / Evolus) | `mcp_server/` | 8001 |
| Agent service (`AGENT_BACKEND=local` or `evolus`) | `agent/` | 8002 |
| Mock factory systems | `mocks/` (in progress) | 8000 |
| Failure model + sensor replay | `detector/` (in progress) | – |
| Approval screen + operator summary | `app/` (in progress) | – |

**About the data:** AI4I 2020 is a synthetic predictive maintenance dataset modelled on a real milling machine. The live feed is a sequence we build from its rows. The machine manual and the factory systems are mock-ups we wrote.

## 1. Serve the model on AMD Developer Cloud

On a GPU instance on AMD Developer Cloud (ROCm and Docker installed):

```bash
git clone https://github.com/akul-sharma009/pitstop.git && cd pitstop
export VLLM_API_KEY=$(openssl rand -hex 24)   # keep it; clients need it
./deploy/vllm/serve.sh                         # Qwen/Qwen3-32B by default; set VLLM_MODEL to change
docker logs -f pitstop-vllm                    # wait for "Application startup complete"
```

Shut the instance down when you're not using it: it uses credits.

## 2. Configure

```bash
cp .env.example .env
# Fill in VLLM_BASE_URL=http://<gpu-instance-ip>:8000/v1, VLLM_API_KEY, and the rest as needed.
# If you run the scripts outside Docker against the dev mocks, MOCKS_URL=http://localhost:8000.
```

All keys and endpoints come from environment variables. Never commit `.env`.

Check that the model is up and calls tools:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
set -a && . ./.env && set +a
python scripts/check_vllm.py
```

Then run the real model through the three demo scenarios (part out of stock, part in stock, no engineer free) against fake factory systems. It checks what the agent actually did, not just what it says:

```bash
python -m scripts.eval_agent            # add --repeat 3 to check reliability, -v to see every tool call
```

## 3. Run

```bash
docker compose up --build                       # MCP server + agent
docker compose --profile devmocks up --build    # + temporary mock factory systems (until mocks/ exists)
```

- MCP server: `http://localhost:8001/mcp` (streamable HTTP; send `Authorization: Bearer $MCP_API_TOKEN` if set)
- Agent service: `http://localhost:8002/docs`

To expose the MCP server over HTTPS for Evolus, see [`docs/evolus-setup.md`](docs/evolus-setup.md).

Send an alert to the agent (needs the model and the mock factory systems running):

```bash
curl -s localhost:8002/incidents -H 'Content-Type: application/json' -d '{
  "machine_id": "M-01", "failure_type": "tool_wear", "risk": 0.93,
  "tool_wear_min": 172, "remaining_cutting_min": 38,
  "detected_at": "2026-10-14T08:00:00+00:00",
  "predicted_failure_at": "2026-10-16T08:00:00+00:00"
}'
```

The reply is the plan (`docs/contracts.md`, AgentResult): diagnosis, part, draft purchase order and booking (both `pending_approval`), operator summary, guardrail warnings and the trace of tool calls.

## Tests

```bash
pytest
```

The tests use a fake LLM and fake factory systems, so they need no GPU, network or keys.

## Licence

MIT, see [LICENSE](LICENSE).
