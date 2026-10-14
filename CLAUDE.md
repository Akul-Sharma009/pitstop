# Pitstop — AI maintenance agent for factories

Hackathon entry for **AMD Developer Hackathon ACT III**, tracks **Intelligent Industry** + **Evolus** (partner track).
Submission deadline: **Sun 18 Oct 2026, 18:00** (time zone to confirm). Thin path must run end to end by **Wed 14 Oct**; feature freeze **Sat 17 Oct** (after that, only bug fixes and the pitch). Record a rough backup demo video as soon as the thin path works.

## The project

Warning signs on a machine are often caught, but the follow-up (read the manual, check stock, raise a purchase order, find an engineer) is slow and manual, so the machine still breaks down. Pitstop closes that gap: it turns one sensor warning into an approved repair plan.

The flow, for one CNC milling machine:

1. **Detect** — sensor rows from the AI4I 2020 predictive maintenance dataset are replayed as if live. A failure model trained on AI4I flags tool-wear failure (TWF) and estimates the remaining cutting time.
2. **Diagnose** — the agent searches the machine manual for the fix (replace the cutting tool) and the part number.
3. **Check stock** — the agent checks the parts inventory.
4. **Order** — if the part is missing, the agent drafts a purchase order; the supplier quote gives a lead time and an expected delivery time.
5. **Schedule** — the agent books a qualified, free engineer **after the part arrives and before the predicted failure**. If no slot fits that window, it escalates to the manager instead of booking.
6. **Approve** — a manager approves every order and every booking.
7. **Explain** — the operator gets a plain-language summary (what is wrong, what has been done, what happens next) plus **downtime saved**: planned tool swap (minutes) vs. unplanned breakdown (hours), also shown in money using a stated cost-per-hour assumption.

Architecture:

- **LLM** — an open model served with **vLLM on AMD Developer Cloud** (MI300X). This is the AMD part of the product and must be visible in the demo. Model: **Qwen/Qwen3-30B-A3B-Instruct-2507** (no thinking mode) with tool calling on. Chosen 12 Oct on the scenario eval: 6/6 at ~2 s per model call, vs 6/6 at ~35 s for thinking Qwen3-30B-A3B (Ollama stand-ins on a Mac). Re-run `scripts.eval_agent` on AMD to confirm; Qwen3-32B is the fallback (`VLLM_MODEL`, plus `VLLM_EXTRA_ARGS="--reasoning-parser qwen3"`). The failure model is small and runs on CPU; say so clearly in the pitch.
- **Agent** — runs in **Evolus** (app.evolus.ai) on our own vLLM endpoint. Building blocks: knowledge library (manual), workflow (diagnose → stock → order → book), MCP connectors (inventory, supplier, calendar); consider the chat widget for the operator view. Our app drives Evolus through its REST API / OpenAI-compatible endpoint. A local agent loop calling vLLM directly is kept as a fallback (`AGENT_BACKEND=local|evolus`).
- **Mock factory systems** — parts inventory, supplier and engineer calendar are simple REST APIs we write, exposed to the agent through one MCP server. The machine manual (4–6 pages, several failure types and similar part numbers so retrieval has to pick the right one) is also ours.
- **Approval boundary** — the agent only drafts; our app shows drafts to the manager; approving in our app confirms them in the mock systems.
- **Screens** — a manager approval screen and an operator summary.

The interfaces between the parts are defined in [`docs/contracts.md`](docs/contracts.md). Change the contract first, then the code on both sides.

Judging: Application of Technology, Business Value, Originality, Presentation. Lead the pitch with the agent (warning → approved action), not the failure model; predictive maintenance on AI4I alone is common.

## Data honesty (AI4I)

AI4I 2020 is a **synthetic** dataset (10,000 rows) modelled on a real milling machine. Rows are independent samples, not a time series, and tool wear is measured in **minutes of cutting** (TWF occurs at roughly 200–240 min). Therefore:

- The replay is a rising-tool-wear sequence **we build from AI4I rows**; say so.
- The model predicts **remaining cutting minutes**; we convert to calendar time with a stated assumption (e.g. "cutting runs N hours/day"). Never claim AI4I itself predicts days.
- The manual and mock systems are ours; say so.

## Scope: the thin path

Build **one complete path** before anything else:

- one machine (CNC mill `M-01`)
- one failure type (tool wear)
- one part (the end-mill cutter, one part number)
- one engineer
- part out of stock → PO drafted (with lead time) → engineer booked after delivery, before failure → manager approves both → operator summary shown

The two branches are **demo scenarios selected by seed data**, not stretch goals: `in_stock` (skip ordering) and `no_engineer` (escalate). They prove the agent reasons rather than following a script.

Stretch goals, only after the thin path works end to end: several engineers and shifts, a second failure type or machine. Cut them without debate.

## Guardrails

- **The agent never acts alone.** Orders and bookings are created as `pending_approval` drafts; only the manager's approval (in our app) confirms them. Approve/reject endpoints are **never** exposed to the agent or the MCP server.
- The agent may only use part numbers and repair steps returned by the manual or the tools. The agent service checks this and flags violations.

## Hosting

Needed for the submission (live URL) and for Evolus (MCP server must be publicly reachable). Decide the host early (small VM or free host for app + mocks + MCP server). The vLLM GPU instance costs credits: when it is off, the live demo must say the model is offline (and the demo video covers the full run).

## Status and risks

Status as of **Wed 14 Oct** (update this section at the end of each day).

| Part | Owner | Status |
|---|---|---|
| Repo, Docker, README, licence, contracts | Akul | Done, merged to `master` (PR #1) |
| Agent service + guardrails | Akul | Done: 6/6 on `scripts.eval_agent` (Ollama stand-in, ~2 s per model call) |
| MCP server | Akul | Done; connected to Evolus as a manual Connector (5 tools) through a Cloudflare tunnel |
| Model choice | Akul | Qwen3-30B-A3B-Instruct-2507 (see Architecture) |
| vLLM on AMD | Akul | **Blocked**: credits pending; `deploy/vllm/serve.sh` ready but untested on AMD. Decision 13 Oct: if no credits by **Fri 16 Oct**, pay for an MI300X (~$2/h, on only while used) |
| Evolus | Akul | 14 Oct: thin path runs end to end through Evolus on the Ollama stand-in (branch `evolus-integration`): detector alert → agent "Pitstop maintenance planner" (knowledge library + factory connector) → order + booking → checked plan, no warnings. Trace comes from the mocks' call log |
| Failure model + replay | Teammate, Akul | 13 Oct: teammate's model (branches `failure-predictive-model`, `sensor_script`) rebuilt as `detector/` on branch `detector-mocks`: one machine, Alert contract, remaining cutting time from the model. Ran on real AI4I 13 Oct: 7/9 caught, 113 false alarms; alert at wear 160 with 38 cutting min left; all three scenarios ran end to end in Docker on the Ollama stand-in |
| Mock factory systems | Teammate, Akul | 13 Oct: `mocks/` service on branch `detector-mocks` (seed `mocks/seed.json`, using the teammate's fictional suppliers) |
| Manual, screens | Teammate | Not started; the teammate's manual JSON is too short (see the 13 Oct review); `manual/cnc_mill_m01.md` is a 3-section placeholder |

Not yet proven: anything on AMD, the `in_stock` and `no_engineer` scenarios through Evolus, the screens, and `docker compose up` on a clean machine.

Risks:
- **AMD credits pending** (account validation submitted 12 Oct; AMD says up to 3–5 business days, which could be after the deadline). Chase them: devcloudrequests@amd.com, the lablab.ai Discord, and a teammate application. Until then, develop against a stand-in (Ollama + the same model, 4-bit, on a Mac, see `docs/setup-guide.md`). The stand-in is never presented as the AMD part; the demo, the video and Evolus must use vLLM on AMD.
- **Thin path by Wed 14 Oct** needs AMD, Evolus and the teammate's three parts. Fallback: run the whole flow on the stand-in model and fake factory systems by Wednesday, then swap in AMD and the real parts as they land.

Plan for **Thu 15 Oct**:
1. Evolus: rerun the three scenarios with `AGENT_BACKEND=evolus`; merge `evolus-integration`.
2. Teammate: full manual (4–6 pages) and the approval / operator screens; review and join them up.
3. Check AMD credits (cut-off Fri 16 Oct, see above).

## Work split

| Area | Owner |
|---|---|
| vLLM on AMD Developer Cloud (`deploy/vllm/`) | Akul |
| Agent service, local + Evolus backends (`agent/`) | Akul |
| MCP server (`mcp_server/`) and Evolus workspace | Akul |
| Repo, Docker, README | Akul |
| Failure model + sensor replay (`detector/`) | Teammate (model), Akul (replay, Alert) |
| Mock inventory / supplier / calendar (`mocks/`) | Akul (seed data: teammate) |
| Machine manual (`manual/`) | Teammate |
| Approval screen + operator summary (`app/`) | Teammate |

## Conventions

- **Python 3.12** for all services and scripts. Dependencies in `requirements.txt` (runtime) and `requirements-dev.txt` (tests).
- **Docker** — every service is containerised; `docker compose up` runs the stack (vLLM runs separately on AMD Developer Cloud and is reached by URL).
- **A README that runs** — its commands must work from a clean clone; re-test before submission.
- **Environment variables for all keys and endpoints** (vLLM, Evolus, MCP token, etc.). `.env.example` holds placeholders; `.env` is gitignored. **Never commit secrets** — check the diff before every push.
- Tests with `pytest`; tests must not need the GPU, Evolus or network (use the fakes in `devtools/fakes.py`, which also implement the mock API contract).
- After changing the prompt, tools or model, run `python -m scripts.eval_agent` against the real vLLM endpoint (three scenarios, checks what the agent actually did).
- Evolus setup steps and open questions: `docs/evolus-setup.md`.
- **Branch names**: short and descriptive so a third person understands them (e.g. `agent-mcp-server`, `thin-path`, `evolus-integration`). No `claude/` prefix and no random auto-generated names.
- Public GitHub repo, MIT licence, original work.
