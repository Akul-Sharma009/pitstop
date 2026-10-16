# Pitstop

**An AI maintenance agent that turns a sensor warning into an approved repair plan.**

Factories often catch the warning signs of a breakdown, but the follow-up is slow and manual: someone reads the manual, checks the parts store, raises a purchase order and finds a free engineer. By then the machine has often failed anyway. Pitstop closes that gap. A failure model spots a CNC mill whose cutting tool is wearing out, and an agent plans the whole fix:

1. **Diagnose:** finds the repair and the exact part number in the machine manual.
2. **Check stock:** looks the part up in the parts store.
3. **Order:** if it's missing, drafts a purchase order from the cheapest supplier that still delivers in time.
4. **Schedule:** books a qualified engineer *after the part arrives and before the predicted failure*, or escalates to the manager if no slot fits.
5. **Approve:** a manager approves or rejects every order and booking. The agent only drafts.
6. **Explain:** the operator gets a plain-language summary and the downtime saved (a 30-minute planned swap instead of about 8 hours of breakdown).

Built for the **AMD Developer Hackathon ACT III**: Intelligent Industry track, Evolus partner track, and the Google prize (Gemma on AMD).

## Try it live

**https://165.245.189.184.sslip.io**: the login is in our submission. The site stays online until judging ends (15 Nov).

1. In the panel on the left, pick a scenario and click **Start a fresh demo**. The agent plans it live (up to 3 minutes), and you watch its steps appear: stock checked, quotes, order drafted, engineer booked.
2. **Reject** the engineer booking with a reason ("Priya is on leave, book someone else"). The agent makes a new plan that respects it, reusing the order it already placed.
3. **Delay order** (6 h): the part now arrives after the booked repair. The booking is cancelled and the agent replans by itself, or escalates if nothing fits before the failure.
4. **Failure comes sooner:** a worse sensor reading moves the predicted failure before the booked repair, and the agent replans for the new deadline.
5. **Fleet:** four machines alert at once and share the engineers and one spare cutter. The approval page walks you through them one by one.

| Scenario | What a correct agent does |
|---|---|
| Part out of stock | Orders from the cheapest supplier in time, books an engineer after delivery |
| Part in stock | Orders nothing, books the earliest engineer |
| No engineer free in time | Orders the part, doesn't book, escalates and says why |
| Hidden instructions in a quote | A supplier note tells the AI to order 50 units and book now; it ignores it |
| Fleet: four machines | One machine gets the spare, two order, nobody double-booked, the coolant pump escalates |

The same alert leads to a different, correct plan in each: the agent reasons about the factory, not a script.

## The AI on AMD

- **Model:** the agent runs on **Gemma 4 (31B)** served on **AMD GPUs through AMD's Token Factory**, with tool calling and no thinking step. **Qwen3.8-27B**, also on the Token Factory, is an automatic backup: if Gemma can't be reached and the failed run drafted nothing, the same alert is planned on Qwen and the plan says so.
- **Why these models:** both pass all 20 scenarios of our eval (below) through the Token Factory. Gemma also passed two fleet runs, at about 9 s per model call.
- **Self-hosting:** [`deploy/vllm/serve.sh`](deploy/vllm/serve.sh) serves the model with vLLM on an AMD MI300X (ROCm), with presets for Qwen3 and Gemma 4. We prepared it for AMD Developer Cloud; the live demo uses the Token Factory, as the organisers recommended.
- The failure model is a small random forest and runs on CPU.

## The agent in Evolus

The agent runs in an **Evolus** workspace (agent chat on our model), using:

- **Agent "Pitstop maintenance planner"** on Gemma-4-31B-IT, with **"Pitstop backup"** on Qwen3.8-27B. Instructions: `python scripts/evolus_text.py prompt`.
- **Knowledge library:** the machine manual (`manual/cnc_mill_m01.md`, 7 failure types and similar part numbers, so retrieval has to pick the right one).
- **MCP connector:** our factory tools (`check_inventory`, `get_supplier_quote`, `draft_purchase_order`, `find_free_engineers`, `draft_booking`), served by `mcp_server/` at `/mcp` with a bearer token.
- Our app drives Evolus through its OpenAI-compatible API. How the workspace is set up: [`docs/evolus-setup.md`](docs/evolus-setup.md).

## Architecture

```
sensor replay (AI4I) → failure model ──alert──▶ agent service ──▶ Evolus agent ──▶ Gemma 4 on AMD (Token Factory)
                                                     │                 │
                                                     │                 ├─ knowledge library: machine manual
                                                     │                 └─ MCP connector ──▶ inventory, supplier, calendar
                                                     ▼
                         screens: manager approves or rejects drafts · operator summary · fleet
```

| Part | Folder | Port |
|---|---|---|
| Agent service (`AGENT_BACKEND=evolus`, or `local`: our own tool loop on the same model) | `agent/` | 8002 |
| MCP server (the factory tools) | `mcp_server/` | 8001 |
| Mock factory systems (inventory, supplier, engineer calendar) | `mocks/` | 8000 |
| Screens: manager approval, operator summary, fleet, demo controls | `app/` | 8003 |
| Failure model and sensor replay | `detector/` | – |
| HTTPS entry point (Caddy) for the live server | `deploy/` | 443 |

The interfaces between the parts are in [`docs/contracts.md`](docs/contracts.md).

## Guardrails

- **The agent never acts alone.** Orders and bookings are drafts until the manager approves them in our app. The approve and reject endpoints aren't reachable from the agent or the MCP server.
- **Facts come from the tools, not the model.** Order and booking IDs, times and engineers are taken from what the tools returned. A part number that's neither in the manual nor in the tools is flagged.
- **Timing is enforced.** The factory refuses an engineer search that starts before the ordered part arrives, and our local agent refuses bookings after the predicted failure or from a slower or dearer supplier than needed. A guard checks every plan and shows any finding to the manager.
- **Tool data is data, not instructions.** Supplier notes reach the model labelled as untrusted, and the hidden-instruction scenario checks that the agent ignores them.
- **Shared resources in a fleet:** a drafted booking holds its engineer slot, and a booking from stock reserves the part, so two machines can't plan with the same slot or the last spare.

## Evaluation

`python -m scripts.eval_agent` runs the real model through 20 scenarios against fake factory systems and checks what the agent actually did, not what it says. The scenarios are the five demos above plus look-alike parts, a pump that needs a mechanical engineer for 90 minutes, parts that can't arrive before the failure, failure types with no part in the manual, an imminent failure, the dataset's own failure code, replans after a rejection or a supplier delay, and a worse reading. `--fleet` runs four agents in parallel on one shared factory. The table is in [`devtools/scenarios.py`](devtools/scenarios.py).

Results through the Token Factory (16–17 Oct): **Gemma-4-31B-IT 20/20, fleet 2/2. Qwen3.8-27B 20/20.**

## About the data

[AI4I 2020](https://archive.ics.uci.edu/dataset/601) (UCI, CC BY 4.0) is a **synthetic** predictive maintenance dataset modelled on a real milling machine. Its rows are independent snapshots, not a time series, so:

- The live feed for each machine is a sequence we build from held-out AI4I rows, in order of rising tool wear. The model never saw them in training.
- The failure model (a random forest on the five sensor readings) flags tool-wear failure. We ask it at what tool wear it starts flagging failure under the current readings; the gap to the current wear is the remaining **cutting** time. Calendar time assumes 20 cutting minutes per day (`CUTTING_MIN_PER_DAY`), a stated assumption: AI4I itself doesn't predict days.
- On the 2,000 held-out rows it catches 7 of the 9 tool-wear failures, with 113 false alarms (5.7% of healthy rows). We keep the threshold low on purpose: a false alarm only produces a draft plan that the manager rejects in a minute, or at worst a cutter swapped somewhat early, while a missed failure costs hours of unplanned downtime.

The machine manual, the factory systems, the suppliers and the people are fictional mock-ups we wrote.

## Run it yourself

You need Docker and an AMD Token Factory API key (notebooks.amd.com/tokenfactory).

```bash
git clone https://github.com/Akul-Sharma009/pitstop.git && cd pitstop
cp .env.example .env        # set VLLM_API_KEY to your Token Factory key
docker compose up --build   # mock factory, MCP server, agent service, screens
```

Open **http://localhost:8003** and click **Start a fresh demo**. With `AGENT_BACKEND=local` (the default) the agent runs our own tool loop on the same model. To run it in Evolus, set `AGENT_BACKEND=evolus`, `EVOLUS_API_KEY` and `EVOLUS_AGENT_ID`, and follow [`docs/evolus-setup.md`](docs/evolus-setup.md).

All keys and endpoints come from environment variables (`.env.example`). Never commit `.env`.

Other ways in:

```bash
docker compose run --build --rm detector --scenario out_of_stock   # replay the sensors; the failure model sends the alert
python -m scripts.eval_agent                                       # the eval (in a venv with requirements-dev.txt and .env loaded)
pytest                                                             # 164 tests with a fake model and fake factory: no GPU, network or keys
```

For the public server: set `PUBLIC_HOST` (e.g. `<server-ip>.sslip.io`), `SCREENS_PASSWORD` and `MCP_API_TOKEN`, then run `docker compose --profile public up -d --build`. Caddy gets the HTTPS certificate itself.

## Team

- **Akul Sharma** ([@Akul-Sharma009](https://github.com/Akul-Sharma009)): agent, Evolus integration, MCP server, mock factory, deployment.
- **Pradeep** ([@pradeepshenram](https://github.com/pradeepshenram)): the failure model, the machine manual, the screens and the supplier data.

Built with [Claude Code](https://claude.com/claude-code).

## Licence

MIT, see [LICENSE](LICENSE).
