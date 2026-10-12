# Evolus setup (runbook)

Goal: the Pitstop agent runs inside Evolus on our vLLM model on AMD, with the manual in a knowledge library, the factory tools through our MCP server, and a workflow. Our app drives it through the Evolus API (`AGENT_BACKEND=evolus`).

Written before we had Evolus access: names of screens and fields may differ. Fix this file as you go.

## Before you start

- [ ] vLLM is serving on AMD Developer Cloud and `python scripts/check_vllm.py` passes (README, step 1–2).
- [ ] `python -m scripts.eval_agent` passes all three scenarios with the local backend. If it doesn't, fix the model or prompt first: Evolus won't make it better.
- [ ] Ports 80 and 443 open on the machine that hosts the MCP server (for now the GPU instance).

## 1. Put the MCP server on the internet

Follow [`setup-guide.md`](setup-guide.md) steps 3–5: on the GPU instance, `docker compose --profile devmocks --profile public up -d --build` serves the MCP server at `https://<IP>.sslip.io/mcp` (bearer `MCP_API_TOKEN`) and the model at `https://<IP>.sslip.io/v1`. On a separate server, the same command works without vLLM; then run `serve.sh` with `VLLM_PUBLIC_HOST` on the GPU instance for HTTPS on the model.

`devmocks` is the temporary mock factory (`devtools/fakes.py`); drop the profile once `mocks/` exists.

## 2. Workspace and model

- [ ] Create the workspace at app.evolus.ai with the event code from kickoff.
- [ ] Add our model endpoint: base URL `https://<IP>.sslip.io/v1`, API key `VLLM_API_KEY`, model `VLLM_MODEL`.
- [ ] Screenshot the endpoint settings (submission: "Evolus workspace shows our AMD model endpoint").

## 3. Building blocks

- [ ] **Knowledge library**: upload the machine manual (`manual/*.md`, or a PDF of it).
- [ ] **MCP connector**: URL `https://$PUBLIC_HOST/mcp`, header `Authorization: Bearer $MCP_API_TOKEN`. It should list 5 tools: `check_inventory`, `get_supplier_quote`, `draft_purchase_order`, `find_free_engineers`, `draft_booking`. There is no approve tool, on purpose.
- [ ] **Agent**: model = our vLLM endpoint; instructions = the system prompt; attach the library and the connector.
  Print the prompt with `python -c "from agent.prompts import SYSTEM_PROMPT; print(SYSTEM_PROMPT)"`.
  In Evolus, "search the machine manual" is the knowledge library.
- [ ] **Workflow**: diagnose → check stock → order (only if out of stock) → book engineer (or escalate) → summary.
- [ ] Optional: chat widget for the operator view.

Test in the Evolus playground with the sample alert:

```bash
python -c "from devtools.fakes import sample_alert; from agent.prompts import alert_message; print(alert_message(sample_alert()))"
```

## 4. Drive it from our app (API)

- [ ] Create an API key → `EVOLUS_API_KEY`.
- [ ] Find the OpenAI-compatible base URL → `EVOLUS_BASE_URL`, and how to address our agent (agent id / model name) → `EVOLUS_AGENT_ID`.
- [ ] Confirm the open questions in `agent/evolus_client.py` (endpoint path, auth header, agent addressing, whether tool calls come back in the reply), and update the code if they differ. If the workflow has its own REST trigger, decide whether the app calls the workflow or the agent.
- [ ] Run it: `AGENT_BACKEND=evolus`, then `POST /incidents` with the sample alert (README). The plan should come back with a purchase order and booking that exist in the mock systems.

## Submission evidence

Screenshots of: model endpoint, knowledge library, MCP connector with its tools, workflow, API key page (key hidden), and a run log of an API call.
