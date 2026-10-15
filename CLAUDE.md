# Pitstop — AI maintenance agent for factories

Hackathon entry for **AMD Developer Hackathon ACT III**, tracks **Intelligent Industry** + **Evolus** (partner track).
Submission deadline: **Sun 18 Oct 2026, 18:00 SGT** (the event itself ends 23:00 SGT). Our target: **submitted by 12:00 SGT Sunday**; Akul's exams start 19 Oct, so there is no time for anything after that. Thin path must run end to end by **Wed 14 Oct**; feature freeze **Sat 17 Oct, 12:00 SGT** (after that, only bug fixes and the pitch). No separate backup video: if AMD fails, the demo video is recorded on the stand-in in the AMD video slot (Sat PM), with an honest note.

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

Status as of **Thu 15 Oct, midday** (update this section at the end of each day).

| Part | Owner | Status |
|---|---|---|
| Repo, Docker, README, licence, contracts | Akul | Done, on `master` (PRs #1–#8) |
| Agent service + guardrails | Akul | Done: 6/6 on `scripts.eval_agent` (Ollama stand-in, ~2 s per model call) |
| MCP server | Akul | Done; connected to Evolus as a manual Connector (5 tools) through a Cloudflare tunnel |
| Model choice | Akul | Qwen3-30B-A3B-Instruct-2507 (see Architecture) |
| vLLM on AMD | Akul | **Blocked**: credits pending; `deploy/vllm/serve.sh` ready but untested on AMD. Decision 13 Oct: if no credits by **Fri 16 Oct**, pay for an MI300X (~$2/h, on only while used) |
| Evolus | Akul | 14 Oct: all three scenarios run end to end through Evolus on the Ollama stand-in, no warnings (PR #3): detector alert → agent "Pitstop maintenance planner" (knowledge library + factory Connector) → order + booking → checked plan. Trace comes from the mocks' call log |
| Failure model + replay | Teammate, Akul | Done, on `master` (PR #2): teammate's model rebuilt as `detector/`; one machine, Alert contract, remaining cutting time from the model. Real AI4I: 7/9 caught, 113 false alarms; alert at wear 160 with 40 cutting min left |
| Mock factory systems | Teammate, Akul | Done, on `master` (PR #2): `mocks/` service, seed `mocks/seed.json` (teammate's fictional suppliers); logs agent calls; enforces the repair window |
| Manual, screens | Teammate, Akul | Done (PR #5): teammate's manual (7 failure types, cleaned of implementation notes) and screens, served by the `app` service at :8003. Verified live 14 Oct with Evolus: eval 6/6 on the new manual (~2.9 s per call), screens follow the run, manager approved both drafts |
| Stress tests, replan on reject | Akul | Done (PRs #6, #7): 14 eval scenarios (3 demo, 9 stress, 2 replan; 15 with `fast_supplier`), 28/28 on the Ollama stand-in; replan verified live in Evolus (manager rejects with a reason → new plan, standing order reused, enforced by the mocks' one-open-order rule) |
| Supplier choice | Akul | Done (PR #8): two suppliers per part (cheap/slow, fast/dear); the agent orders the cheapest offer that arrives in time. Verified live in Evolus: `urgent` → fast supplier SGD 145, `out_of_stock` → cheap SGD 89. A late offer is refused by the local agent, the mock calendar rejects an empty search window (422), and the guard warns (covers Evolus). Eval on the stand-in: `fast_supplier` 5/5, core 6/6; 112 tests. Evolus instructions re-pasted for this prompt |

| Delivery delay | Akul | 15 Oct: built, **paused** on branch `delivery-delay` (978179e, 119 tests, not evaluated, not merged). The supplier delays an order (`POST /api/purchase-orders/{id}/delay`, demo control); a pending booking that now starts before the part arrives is cancelled and the agent replans after the new delivery (or escalates); an approved booking is left to the manager. Scenarios `delivery_delayed`, `delivery_delayed_too_late`. To resume: merge master into it, Akul runs `--scenario delivery_delayed --repeat 3`, `--scenario delivery_delayed_too_late --repeat 3`, `--core`; then PR and merge. The replan message changed, not SYSTEM_PROMPT: no Evolus re-paste |
| Fleet mode | Claude, teammate | 15 Oct: designed (below), not built. Split: Claude builds the backend on `fleet-mode`; the teammate builds the detector's fleet replay (`fleet-replay`) and the Fleet page (`fleet-screens`), specs in `docs/fleet-tasks.md` |

Not yet proven: anything on AMD, and `docker compose up` on a clean machine.

Open decisions: hosting for the live URL (recommended: one small VM, or the AMD instance while it's on, with `docker compose --profile public`).

Submission (lablab): project title, short and long description, technology and category tags, cover image, video presentation, slide presentation, public GitHub repo, demo application platform, application URL. Claude drafts the texts, cover image, slides and the video script on Sat; Akul records the video on AMD.

Risks:
- **AMD credits pending** (account validation submitted 12 Oct; AMD says up to 3–5 business days, which could be after the deadline). Chase them: devcloudrequests@amd.com, the lablab.ai Discord, and a teammate application. Until then, develop against a stand-in (Ollama + the same model, 4-bit, on a Mac, see `docs/setup-guide.md`). The stand-in is never presented as the AMD part; the demo, the video and Evolus must use vLLM on AMD.
- **Thin path by Wed 14 Oct** needs AMD, Evolus and the teammate's three parts. Fallback: run the whole flow on the stand-in model and fake factory systems by Wednesday, then swap in AMD and the real parts as they land.

Plan to the deadline (agreed 14 Oct, re-dated the same evening: Wed 14 already covered Thu's items and built Fri's supplier choice; one branch per item, Akul tests each before merge; fleet mode is cut first if time runs short):

| When | What |
|---|---|
| Wed 14 (done) | Thin path through Evolus; manual and screens; downtime saved; stress-test set (PR #6); replan on reject (PR #7); supplier choice (PR #8) |
| Thu 15 | Fleet mode (Claude: backend on `fleet-mode`; teammate: `fleet-replay`, `fleet-screens`); then delivery delay (resume `delivery-delay`); fleet mode: 3–5 machines in parallel, shared engineers (`fleet-mode`), tested on the stand-in; hosting prepared (basic_auth on the screens, "model offline" notice); Gemma 4 option in `serve.sh` + setup guide; check AMD credits; Akul checks the Google prize rules |
| Fri 16 | **AMD**: credits or pay by evening; `serve.sh`, eval on AMD for Qwen3-30B-A3B **and** Gemma 4 26B-A4B (see Gemma below), Evolus model switched; fleet mode on the MI300X; hosting live; operator chat via the Evolus widget |
| Sat 17, 12:00 | **Feature freeze** |
| Sat 17 PM | Fresh-clone README test; demo video on AMD (if AMD failed: on the stand-in, said plainly); slides, cover image, descriptions and tags (Claude drafts, Akul reviews) |
| Sun 18 AM | Final checks and upload; **submit by 12:00 SGT**. No building or recording on Sunday |

Next up (handed off from the cloud session, Thu 15 Oct midday), in this order:
1. **Fleet backend (Claude), branch `fleet-mode`.** That branch holds only old docs commits: reset it to `master` first (`git checkout -B fleet-mode master`). Build Claude's part of the design below. Don't edit `detector/` or `app/static/` while the teammate's branches are open.
2. **Review the teammate's PRs** (`fleet-replay`, `fleet-screens`) when he opens them. Take the remaining cutting minutes he reports from the real AI4I data to finalise the `fleet` seed slots.
3. **Delivery delay:** resume `delivery-delay` (see the status table).
4. **Hosting prep:** basic_auth on the screens, a "model offline" notice.
5. **Gemma 4 option:** in `deploy/vllm/serve.sh` and `docs/setup-guide.md` (see Gemma below).
6. Fri: AMD (credits or pay by evening), eval on AMD for both models, operator chat via the Evolus widget. Sat PM: submission materials (Claude drafts title, descriptions, tags, cover image, slides, video script).

Teammate (@pradeepshenram on GitHub; wrote the failure model, the manual, the screens and the seed suppliers). He works on Akul's Mac, on his own branches; Akul reviews and merges. The walkthrough guide he was given: https://claude.ai/artifact/FyikhyoniycDQ45Zcai9g7. Handed to him on 15 Oct:
- **Task 1, fleet replay** (`fleet-replay`): `detector/replay.py --fleet`, `Alert.simulated`. Spec in `docs/fleet-tasks.md`. He reports each machine's remaining cutting minutes.
- **Task 2, Fleet page** (`fleet-screens`): `app/static/` only, against `GET /api/incidents`. Spec in `docs/fleet-tasks.md`.
- Also:
  - Google prize rules (amount; must Gemma be the core model; can one project win Evolus + Google)
  - README fixes (status line says the manual and screens are to come; "12 scenarios")
  - check the manual's coolant pump section for M-04
  - pitch drafts (cover image idea, slide outline, video storyboard)
  - manual QA of the screens in Chrome/Safari
- **Overlap:** his Task 1 adds `simulated` to `agent/models.py`; the contract line is already on `master`. Whoever merges second resolves the conflict.

Fleet mode design (14 Oct; nothing built yet). Parts marked **(teammate)** are his tasks in `docs/fleet-tasks.md`; everything else is Claude's:

**Mocks (`mocks/factory.py`)**
- Machine context: `X-Machine-Id` header (local agent), or an admin focus (`POST /admin/focus?machine_id=`) set by the Evolus backend before each queued run. Log `machine_id` with each call.
- `_free` delivery 409 rule is global today: any machine's order blocks every machine's search. Scope it to the requesting machine's orders.
- Pending bookings don't hold their slot (only approval removes it), so two machines could draft the same slot. Make non-rejected bookings hold it, in both `_free` and `_draft_booking`, with a 409 naming the holder.
- Stock isn't reserved, so two machines could plan with the same single EM-1004. Add an optional `part_number` to `draft_booking`: with no PO, it reserves one unit. `quantity_on_hand` = stock minus reservations. 409 when none is left ("reserved for BK-0001 (M-01): order it").
- `threading.Lock` in `handle` (the parallel fake factory in the eval).
- Seed scenario `fleet`:
  - stock: EM-1004 1, CP-220 0 (72 h lead → M-04 escalates)
  - slots: E-01 [7, 26], E-03 [27], E-02 [26]
  - this fits alerts with 25–45 cutting minutes left; cheap delivery ≈ +25 h

**Tools**
- `draft_booking` gets `part_number` (`mcp_server/tools.py` spec, `mcp_server/server.py`, `FactoryClient`).
- This is a tool change: Akul refreshes the Evolus Connector and re-pastes the prompt.

**Local agent**
- Sets the `X-Machine-Id` header.
- Fills `part_number` on a booking from stock when the model leaves it out.

**Prompt**
- Pass `part_number` when booking from stock.
- Other machines share engineers and stock: on a 409 for a held slot or reserved part, search again or order.

**Agent API (`agent/api.py`)**
- Incidents per machine: `GET /incidents`, `GET /incidents/{machine_id}`, `DELETE /incidents` (the replay calls it on reset).
- `/incidents/latest` is kept.
- `Replan` gets an optional `machine_id`.

**Evolus (`agent/evolus_client.py`)**
- A run lock (queued) plus the mocks focus, so the call-log trace stays per machine.

**Alert (teammate, Task 1)**
- Optional `simulated: bool` (the screens show "simulated alert"); already in `docs/contracts.md`.

**Detector (`detector/replay.py`) (teammate, Task 1)**
- `--fleet`: M-01 to M-03 from disjoint held-out row subsets, replayed in lockstep with staggered starts.
- Alerts are posted in threads.
- M-04 gets a simulated coolant-pump alert (failure in ~40 h).
- He reports the real remaining cutting minutes per machine (the AI4I data is on the Mac in `data/`); Claude sets the `fleet` seed slots from them.

**App (`app/server.py`)**
- `/api/incidents` and `/api/incidents/{m}`, exactly as in `docs/contracts.md` section 5.
- Replan takes the machine from the decided order or booking (`machine_id`).

**Fleet page (`app/static/`) (teammate, Task 2)**
- One row per machine; clicking a row opens the approval/operator view for that machine.

**Eval**
- `FLEET` in `devtools/scenarios.py`; `eval_agent --fleet` runs 4 local agents in threads on one `FakeFactory`.
- Checks:
  - no slot is held twice
  - reservations stay within stock
  - exactly one tool-wear machine uses stock, two order from ToolCo
  - all three are booked; M-04 escalates
- Plus pytest with scripted LLMs.

Gemma (decided 14 Oct evening): Google DeepMind joined as a partner. There is an extra Google prize for building with Gemma on AMD GPUs; lablab says a project can enter the main challenge plus partner prizes. Akul checks the amount, whether Gemma must be the core model, and whether Evolus + Google can both be won.
- **The model:** Gemma 4 26B-A4B (April 2026), a mixture-of-experts twin of our Qwen3-30B-A3B (~4B active), with native tool calling (`--tool-call-parser gemma4`) and a vLLM MI300X recipe (`vllm-openai-rocm` image). Our agent is model-agnostic (`VLLM_MODEL`, `LLM_EXTRA_BODY`), so trying it is mostly configuration.
- **Risk:** Ollama's Gemma 4 tool calling is reportedly buggy (calls land in the reasoning field), so the Mac stand-in can't judge it. Test it on vLLM on AMD only.
- **Plan:**
  - Thu: Claude adds a Gemma 4 option to `deploy/vllm/serve.sh` and `docs/setup-guide.md`, and checks that the eval handles Gemma's reasoning field.
  - Fri: full eval on AMD for both models (about 30 min of GPU time).
  - If Gemma matches Qwen, the agent switches to Gemma ("one Gemma model on one MI300X runs the whole agent").
  - Otherwise Qwen keeps planning and Gemma powers the operator side (operator chat via the Evolus widget, operator summary in the operator's language), served next to Qwen. Both fit in the MI300X's 192 GB.

Operational notes (learned 13–15 Oct):
- **Since 15 Oct: Claude Code runs locally on Akul's Mac**, in `~/Desktop/College/Projects/pitstop` (the old `~/pitstop` worktree is gone). `.env` and the AI4I data (`data/`) live only there. The local session opens PRs and merges them after Akul's eval passes, merging with the full head SHA.
- The Cloudflare quick tunnel dies after a while ("Tunnel not found", HTTP 530). After restarting `cloudflared tunnel --url http://localhost:8088`, update the new URL in Evolus: Your models (`…/v1`) and the Connector (`…/mcp`).
- After a tool's arguments or description change, refresh the Connector's tool list in Evolus. After a `SYSTEM_PROMPT` change, re-paste `python scripts/evolus_text.py prompt` into the agent's instructions.
- On the Mac, `.env` has `VLLM_BASE_URL=http://host.docker.internal:11434/v1` for Docker; run the eval with `VLLM_BASE_URL=http://localhost:11434/v1 python -m scripts.eval_agent`.
- `docker compose up -d --build` doesn't rebuild the detector: use `docker compose run --build --rm detector …`.
- Merged branches to delete on GitHub: agent-mcp-server, detector-mocks, evolus-integration, downtime-and-docs, manual-and-screens, stress-tests, replan-on-reject, supplier-choice, claude/practical-volta-yg4v5p, handoff (after it merges).

Priority if time runs short: stress-test fixes → replan on reject → AMD → supplier choice → delivery delay → hosting → operator chat → Gemma → fleet mode.

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
