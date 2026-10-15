# Fleet mode: the teammate's two tasks

Fleet mode runs four machines that share one factory: the same engineers and the same parts store. Claude builds the backend (mocks, agent service, `/api/incidents`) on branch `fleet-mode`. These two tasks are the detector and screen parts, built against the interfaces in [`contracts.md`](contracts.md). Work on your own branch from an up-to-date `master`; Akul reviews and merges. Claude doesn't edit `detector/` or `app/static/` while these branches are open.

```bash
cd ~/pitstop
git checkout master && git pull origin master
git checkout -b fleet-replay        # Task 2: git checkout master && git checkout -b fleet-screens
```

## Task 1: fleet replay (branch `fleet-replay`, do first)

Files: `detector/replay.py`, `tests/test_detector.py`, plus the `simulated` field in `agent/models.py`.

- `python -m detector.replay --fleet [--scenario fleet] [--dry-run]` replays four machines at once.
- **M-01, M-02, M-03: tool wear.** Each is fed from its own share of the held-out AI4I rows (`test_rows[k::3]`, then the existing `sequence()`), so no two machines see the same readings. They start a few readings apart (e.g. 0, 3, 6) so the alerts arrive one after another. Print lines like `M-02 tool wear 152 min | torque 44.1 Nm | …`.
- `make_alert()` takes the machine ID as an argument instead of the fixed `M-01`.
- Each machine sends its alert to `ALERT_URL` the moment it fires, in a background thread, so the other machines keep replaying. With `--scenario`, the mock factory is reset first, as today.
- **M-04: a simulated coolant-pump alert,** sent after the others: `failure_type: "coolant_pump"`, `tool_wear_min` and `remaining_cutting_min` null, `predicted_failure_at` = now + 40 h, `risk: 0.8`, `simulated: true`. The detector only flags tool wear, so this alert is simulated and must say so.
- Add `simulated: bool = False` to `Alert` in `agent/models.py` (the contract already lists it).
- `--dry-run` prints all four alerts and sends nothing.
- Tests in `tests/test_detector.py` with the existing `ai4i_fixture`: four alerts, distinct feeds, M-04 marked simulated, `--dry-run` sends nothing.

Done when: `pytest -q` passes, and `python -m detector.replay --fleet --dry-run` prints four sensible alerts on the real data. **Send Akul the `remaining_cutting_min` of M-01, M-02 and M-03**: the fleet's engineer slots are set from them.

## Task 2: the Fleet page (branch `fleet-screens`)

Files: `app/static/` only (`index.html`, `app.js`, `styles.css`).

- A third sidebar page, **Fleet**, with one row per machine:
  - machine ID
  - failure type, with a "Simulated alert" tag when `alert.simulated` is true
  - predicted failure time
  - status pill: planning / awaiting approval / escalated / approved / rejected
  - part, and the order if any
  - engineer and time
  - number of drafts waiting for approval
- Poll `GET /api/incidents` every 2 s. It returns `{"incidents": [ … ]}`, one entry per machine, sorted by machine ID. Each entry is the same object `GET /api/latest` returns today: `{"alert", "result", "received_at", "feedback", "previous"}`.
- Clicking a row selects that machine. The approval and operator pages then follow `GET /api/incidents/{machine_id}` instead of `/api/latest`, and the header shows that machine (it says M-01 today).
- Until the backend lands: a **"Load demo fleet"** button with four sample machines, like the existing "Load demo result" (one awaiting approval from stock, two with orders, M-04 escalated with a simulated alert). If `/api/incidents` returns 404, show "Fleet view needs the fleet backend" instead of breaking.

Done when: the demo fleet renders in Chrome and Safari and at phone width, clicking a row opens that machine's plan, and the single-machine flow (`/api/latest`) still works.

To see the page: `docker compose up -d --build`, then open http://localhost:8003. Without Ollama the page still loads; plans just won't arrive.
