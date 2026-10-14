# Pitstop — AI maintenance agent for factories

Hackathon entry for **AMD Developer Hackathon ACT III**, tracks **Intelligent Industry** + **Evolus** (partner track).
Submission deadline: **Sun 18 Oct 2026, 18:00 SGT** (the event itself ends 23:00 SGT). Our target: **submitted by 12:00 SGT Sunday**; Akul's exams start 19 Oct, so there is no time for anything after that. Thin path must run end to end by **Wed 14 Oct**; feature freeze **Sat 17 Oct, 12:00 SGT** (after that, only bug fixes and the pitch). Record a rough backup demo video as soon as the thin path works.

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
| Repo, Docker, README, licence, contracts | Akul | Done, on `master` (PRs #1–#7) |
| Agent service + guardrails | Akul | Done: 6/6 on `scripts.eval_agent` (Ollama stand-in, ~2 s per model call) |
| MCP server | Akul | Done; connected to Evolus as a manual Connector (5 tools) through a Cloudflare tunnel |
| Model choice | Akul | Qwen3-30B-A3B-Instruct-2507 (see Architecture) |
| vLLM on AMD | Akul | **Blocked**: credits pending; `deploy/vllm/serve.sh` ready but untested on AMD. Decision 13 Oct: if no credits by **Fri 16 Oct**, pay for an MI300X (~$2/h, on only while used) |
| Evolus | Akul | 14 Oct: all three scenarios run end to end through Evolus on the Ollama stand-in, no warnings (PR #3): detector alert → agent "Pitstop maintenance planner" (knowledge library + factory Connector) → order + booking → checked plan. Trace comes from the mocks' call log |
| Failure model + replay | Teammate, Akul | Done, on `master` (PR #2): teammate's model rebuilt as `detector/`; one machine, Alert contract, remaining cutting time from the model. Real AI4I: 7/9 caught, 113 false alarms; alert at wear 160 with 40 cutting min left |
| Mock factory systems | Teammate, Akul | Done, on `master` (PR #2): `mocks/` service, seed `mocks/seed.json` (teammate's fictional suppliers); logs agent calls; enforces the repair window |
| Manual, screens | Teammate, Akul | Done (PR #5): teammate's manual (7 failure types, cleaned of implementation notes) and screens, served by the `app` service at :8003. Verified live 14 Oct with Evolus: eval 6/6 on the new manual (~2.9 s per call), screens follow the run, manager approved both drafts |
| Stress tests, replan on reject | Akul | Done (PRs #6, #7): 14 eval scenarios (3 demo, 9 stress, 2 replan), 28/28 on the Ollama stand-in; replan verified live in Evolus (manager rejects with a reason → new plan, standing order reused, enforced by the mocks' one-open-order rule) |

Not yet proven: anything on AMD, and `docker compose up` on a clean machine.

Open decisions: hosting for the live URL (recommended: one small VM, or the AMD instance while it's on, with `docker compose --profile public`).

Submission (lablab): project title, short and long description, technology and category tags, cover image, video presentation, slide presentation, public GitHub repo, demo application platform, application URL. Claude drafts the texts, cover image, slides and the video script on Sat; Akul records the video on AMD.

Risks:
- **AMD credits pending** (account validation submitted 12 Oct; AMD says up to 3–5 business days, which could be after the deadline). Chase them: devcloudrequests@amd.com, the lablab.ai Discord, and a teammate application. Until then, develop against a stand-in (Ollama + the same model, 4-bit, on a Mac, see `docs/setup-guide.md`). The stand-in is never presented as the AMD part; the demo, the video and Evolus must use vLLM on AMD.
- **Thin path by Wed 14 Oct** needs AMD, Evolus and the teammate's three parts. Fallback: run the whole flow on the stand-in model and fake factory systems by Wednesday, then swap in AMD and the real parts as they land.

Plan to the deadline (agreed 14 Oct; one branch per item, Akul tests each before merge; fleet mode is cut first if time runs short):

| When | What |
|---|---|
| Thu 15 | Stress-test eval + fixes (`stress-tests`); replan on reject: the manager rejects with a reason, the agent replans (`replan-on-reject`); rough backup video |
| Fri 16 | Supplier choice: cheap/slow vs fast/dear, cheapest that arrives in time (`supplier-choice`); **AMD**: credits or pay by evening, `serve.sh`, eval on AMD, Evolus model switched; hosting set up (AMD instance or a small VM) |
| Fri–Sat AM | Fleet mode: 3–5 machines in parallel on one MI300X (`fleet-mode`), tested on AMD Sat morning, **cut first if anything slips**; operator chat via the Evolus widget once hosted |
| Sat 17, 12:00 | **Feature freeze** |
| Sat 17 PM | Fresh-clone README test; demo video on AMD; slides, cover image, descriptions and tags (Claude drafts, Akul reviews) |
| Sun 18 AM | Final checks and upload; **submit by 12:00 SGT**. No building or recording on Sunday: if the AMD video isn't done by Sat evening, submit the backup video plus AMD screenshots with an honest note |

Priority if time runs short: stress-test fixes → replan on reject → AMD → supplier choice → hosting → operator chat → fleet mode.

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
- After changing the prompt, tools or model, run `python -m scripts.eval_agent` against the real vLLM endpoint (15 scenarios from `devtools/scenarios.py`: 3 demo, 10 stress, 2 replan; checks what the agent actually did). New behaviour gets a new scenario there.
- Evolus setup steps and open questions: `docs/evolus-setup.md`.
- **Branch names**: short and descriptive so a third person understands them (e.g. `agent-mcp-server`, `thin-path`, `evolus-integration`). No `claude/` prefix and no random auto-generated names.
- Public GitHub repo, MIT licence, original work.
