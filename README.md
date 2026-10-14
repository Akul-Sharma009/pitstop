# Pitstop

An AI maintenance agent for factories. A failure model spots a CNC milling machine whose cutting tool is wearing out. An agent then plans the whole fix: it finds the repair and the part number in the machine manual, checks the parts store, drafts a purchase order if the part is missing, and books a free engineer after the part arrives and before the machine fails. A manager approves every order and booking, and the operator gets a plain-language summary.

The agent's language model is an open model served with **vLLM on AMD Developer Cloud** (AMD Instinct MI300X, ROCm). Built for the AMD Developer Hackathon ACT III (Intelligent Industry + Evolus tracks).

> Status (14 Oct): work in progress. The whole path runs end to end: the detector replays M-01 and sends an alert; the agent, in Evolus or as our local loop, reads the manual, checks stock, drafts the order and books an engineer after the part arrives; the plan comes back checked, with the downtime saved. All three demo scenarios pass through Evolus. For now the model is Qwen3-30B-A3B-Instruct-2507 on a local stand-in while AMD Developer Cloud credits are pending. Still to come: the model on AMD, the full machine manual and the approval and operator screens. Interfaces: [`docs/contracts.md`](docs/contracts.md).

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
| LLM: vLLM on AMD Developer Cloud | `deploy/vllm/` | 8080 on the GPU instance |
| MCP server (factory tools for the agent / Evolus) | `mcp_server/` | 8001 |
| Agent service (`AGENT_BACKEND=local` or `evolus`) | `agent/` | 8002 |
| Mock factory systems (inventory, supplier, calendar) | `mocks/` | 8000 |
| Failure model + sensor replay | `detector/` | – |
| Approval screen + operator summary | `app/` | 8003 |

**About the data:** [AI4I 2020](https://archive.ics.uci.edu/dataset/601) (UCI, CC BY 4.0) is a synthetic predictive maintenance dataset modelled on a real milling machine. Its rows are independent snapshots, not a time series, so:

- The live feed for M-01 is a sequence we build from held-out AI4I rows (healthy readings in order of rising tool wear). The model never saw them in training.
- The failure model (a random forest on the five sensor readings, by our teammate @pradeepshenram) flags tool-wear failure. It doesn't predict time by itself. We ask it at what tool wear it starts flagging failure under the current readings; the gap to the current wear is the remaining cutting time. One odd reading can swing that estimate, so the model's score is averaged over the last three readings. Calendar time assumes 20 cutting minutes per day (`CUTTING_MIN_PER_DAY`).
- The model's score is not a calibrated probability. On the 2,000 held-out rows it catches 7 of the 9 tool-wear failures, with 113 false alarms (`python -m detector.train`, which also prints the scikit-learn version; the teammate's notebook got 6 of 9 and 100 with the same settings, probably on another version).

The machine manual (`manual/`, 7 failure types, written by @pradeepshenram) and the factory systems are mock-ups we wrote; suppliers and people are fictional.

## 1. Serve the model on AMD Developer Cloud

First time? Follow the step-by-step [`docs/setup-guide.md`](docs/setup-guide.md) (Mac → GPU instance → model → public MCP server → Evolus). The short version:

On a GPU instance on AMD Developer Cloud (ROCm and Docker installed):

```bash
git clone https://github.com/akul-sharma009/pitstop.git && cd pitstop
export VLLM_API_KEY=$(openssl rand -hex 24)   # keep it; clients need it
./deploy/vllm/serve.sh                         # Qwen/Qwen3-30B-A3B-Instruct-2507 by default; set VLLM_MODEL to change
docker logs -f pitstop-vllm                    # wait for "Application startup complete"
```

Shut the instance down when you're not using it: it uses credits.

## 2. Configure

```bash
cp .env.example .env
# Fill in VLLM_BASE_URL=http://<gpu-instance-ip>:8080/v1, VLLM_API_KEY, and the rest as needed.
# If you run the scripts outside Docker, MOCKS_URL=http://localhost:8000.
```

All keys and endpoints come from environment variables. Never commit `.env`.

Check that the model is up and calls tools:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
set -a && . ./.env && set +a
python scripts/check_vllm.py
```

Then run the real model through the scenarios against fake factory systems. It checks what the agent actually did in the factory systems, not just what it says. There are the three demo scenarios (part out of stock, part in stock, no engineer free) and nine stress scenarios the prompt was not tuned on: look-alike parts, a pump that needs a mechanical engineer, parts that arrive after the failure, failure types the manual has no part for, an imminent failure, and an alert using the dataset's own failure code. The whole table is in `devtools/scenarios.py`.

```bash
python -m scripts.eval_agent            # all 12 scenarios; --core for the demo three, --repeat 3 for reliability, -v to see every tool call
```

## 3. Run

```bash
docker compose up --build    # mock factory systems + MCP server + agent
```

- Mock factory systems: `http://localhost:8000` (seed data in `mocks/seed.json`)
- MCP server: `http://localhost:8001/mcp` (streamable HTTP; send `Authorization: Bearer $MCP_API_TOKEN` if set)
- **Screens: `http://localhost:8003`**: manager approval and operator summary (by @pradeepshenram). They follow the latest alert by themselves; the manager approves or rejects each draft there.
- Agent service: `http://localhost:8002/docs`

**Run the agent in Evolus** (`AGENT_BACKEND=evolus`): the Evolus agent uses our model, the manual as its knowledge library and our MCP server as a Connector. Setup, the tunnel for development and the HTTPS host for the demo: [`docs/evolus-setup.md`](docs/evolus-setup.md).

Replay the machine and let the detector send its alert to the agent (needs the model; first run downloads AI4I from UCI into `data/`):

```bash
docker compose run --build --rm detector --scenario out_of_stock   # or in_stock, no_engineer; --dry-run prints the alert only
```

`--scenario` resets the mock factory first: engineer slots are set relative to that moment. `--build` matters: `docker compose up --build` doesn't rebuild the detector, which only runs on demand.

Or send an alert by hand:

```bash
curl -s localhost:8002/incidents -H 'Content-Type: application/json' -d '{
  "machine_id": "M-01", "failure_type": "tool_wear", "risk": 0.93,
  "tool_wear_min": 172, "remaining_cutting_min": 38,
  "detected_at": "2026-10-14T08:00:00+00:00",
  "predicted_failure_at": "2026-10-16T08:00:00+00:00"
}'
```

The reply is the plan (`docs/contracts.md`, AgentResult): diagnosis, part, draft purchase order and booking (both `pending_approval`), operator summary, downtime saved, guardrail warnings and the trace of tool calls.

**Downtime saved** compares the booked tool swap (30 minutes, from the manual) with an unplanned breakdown, using two stated assumptions you can change in `.env`: a breakdown stops the machine for about 8 hours (`UNPLANNED_DOWNTIME_H`), and downtime costs about 500 SGD per hour (`DOWNTIME_COST_PER_H`). With those, one planned swap saves about 7.5 hours and 3,750 SGD.

## Tests

```bash
pytest
```

The tests use a fake LLM and fake factory systems, so they need no GPU, network or keys.

## Licence

MIT, see [LICENSE](LICENSE).
