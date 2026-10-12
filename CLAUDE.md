# Pitstop — AI maintenance agent for factories

Hackathon entry for **AMD Developer Hackathon ACT III**, tracks **Intelligent Industry** + **Evolus** (partner track).
Submission deadline: **Sun 18 Oct 2026, 18:00** (time zone to confirm). Thin path must run end to end by **Wed 14 Oct**; feature freeze **Sat 17 Oct** (after that, only bug fixes and the pitch).

## The project

Warning signs on a machine are often caught, but the follow-up (read the manual, check stock, raise a purchase order, find an engineer) is slow and manual, so the machine still breaks down. Pitstop closes that gap.

The flow, for one CNC milling machine:

1. **Detect** — sensor rows from the AI4I 2020 predictive maintenance dataset are replayed as if live. A failure model trained on AI4I flags tool-wear failure (TWF) and estimates time to failure.
2. **Diagnose** — the agent reads the machine manual to find the fix (replace the cutting tool) and the part number.
3. **Check stock** — the agent checks the parts inventory.
4. **Order** — if the part is missing, the agent drafts a purchase order to the supplier.
5. **Schedule** — the agent books a qualified, free engineer before the predicted failure.
6. **Approve** — a manager approves every order and every booking.
7. **Explain** — the operator gets a plain-language summary: what is wrong, what has been done, what happens next, plus the "downtime saved" (planned tool swap in minutes vs. hours of unplanned stop).

Architecture:

- **LLM** — an open model served with **vLLM on AMD Developer Cloud**. This is the AMD part of the product and must be visible in the demo. The failure model is small and runs on CPU; say so clearly in the pitch.
- **Agent** — runs in **Evolus** (app.evolus.ai) using our own vLLM endpoint. Building blocks: knowledge library (manual), workflow (diagnose → stock → order → book), MCP connectors (inventory, supplier, calendar). Our app drives Evolus through its REST API / OpenAI-compatible endpoint. A local agent loop calling vLLM directly is kept as a fallback.
- **Mock factory systems** — parts inventory, supplier orders and engineer calendar are simple REST APIs we write, exposed to the agent through one MCP server. The 1–2 page machine manual is also ours.
- **Screens** — a manager approval screen and an operator summary.

Judging: Application of Technology, Business Value, Originality, Presentation.

## Scope: the thin path

Build **one complete path** before anything else:

- one machine (CNC mill `M-01`)
- one failure type (tool wear)
- one part (the end-mill cutter, one part number)
- one engineer
- part out of stock → PO drafted → manager approves; engineer free → booking drafted → manager approves; operator summary shown

Stretch goals, **only after the thin path works end to end**: part-in-stock branch (skip ordering), no-engineer-free escalation to the manager, several engineers and shifts, a second failure type or machine. Cut stretch goals without debate.

## Guardrails

- **The agent never acts alone.** Orders and bookings are created as `pending` drafts; only the manager's approval confirms them. Enforce this in the APIs, not only in the prompt.
- The agent may only use part numbers and repair steps returned by the manual or the tools; it must not invent them.
- Be honest in the pitch and README: AI4I is a synthetic dataset modelled on a real milling machine; the manual and mock systems are ours.

## Conventions

- **Python** (3.11) for all services and scripts.
- **Docker** — every service is containerised; `docker compose up` runs the stack (vLLM runs separately on AMD Developer Cloud and is reached by URL).
- **A README that runs** — its commands must work from a clean clone; re-test before submission.
- **Environment variables for all keys and endpoints** (vLLM, Evolus, etc.). Commit a `.env.example` with placeholder values; `.env` is gitignored. **Never commit secrets** — check the diff before every push.
- Public GitHub repo, MIT licence, original work.
