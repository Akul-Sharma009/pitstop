# Setup guide: AMD GPU → model → public MCP server → Evolus

Step by step, from a Mac. One AMD GPU instance runs everything for now: the model (vLLM), the temporary mock factory systems, the MCP server, and the HTTPS proxy Evolus talks to. Plan about 2–3 hours, much of it waiting for downloads.

Commands in **[Mac]** blocks run in Terminal on your Mac. Commands in **[GPU]** blocks run on the GPU instance after you `ssh` in. Anything in `<angle brackets>` is yours to fill in.

Stop at each ✅ checkpoint. If it doesn't match, paste the output to Claude before going on.

---

## Step 0 — Credits and repo access (10 min)

**Credits.** Log in to AMD Developer Cloud and open the billing or credits page.
- Credits shown → continue.
- No credits → signing up isn't always enough. Check the AMD AI Developer Program page for a "claim credits" step and your email (including spam) for a credits or activation mail. If there's still nothing, ask in the lablab.ai Discord hackathon channel: credits for events are often granted by the organisers.

**Repo.** The GPU instance needs to download the code. Easiest: make `akul-sharma009/pitstop` public on GitHub now (Settings → General → Danger Zone → Change visibility). The submission must be public anyway, and no secrets are in the repo. If you'd rather keep it private for now, you'll need a GitHub personal access token for the clone in step 3.

## Step 1 — SSH key (5 min)

An SSH key lets your Mac log in to the GPU instance without a password.

**[Mac]**
```bash
ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -C "<your email>"   # press Enter at every question
pbcopy < ~/.ssh/id_ed25519.pub                                        # copies the public key
```

In AMD Developer Cloud, find the SSH keys page (account or security settings), choose "Add SSH key", and paste.

## Step 2 — Create the GPU instance (10 min)

In AMD Developer Cloud, create a GPU instance (it may be called a "GPU Droplet"):
- **GPU:** 1× MI300X.
- **Image:** a ROCm image; one with vLLM or "quick start" in its name is best. It must have Docker; we check below.
- **SSH key:** the one from step 1.

Note its **public IP address**.

⚠️ It costs credits for as long as it exists (see step 7).

**[Mac]**
```bash
ssh root@<IP>          # type "yes" the first time
```

**[GPU]**
```bash
docker --version && rocm-smi --showproductname | head -20
```
✅ A Docker version, and the GPU listed as an MI300X (Instinct). If `docker` is missing: `curl -fsSL https://get.docker.com | sh`.

## Step 3 — Start the model (20–40 min, mostly download)

**[GPU]**
```bash
git clone -b claude/practical-volta-yg4v5p https://github.com/akul-sharma009/pitstop.git
cd pitstop
cp .env.example .env
echo "VLLM_API_KEY=$(openssl rand -hex 24)"      # copy this line
echo "MCP_API_TOKEN=$(openssl rand -hex 24)"     # and this one
curl -s ifconfig.me; echo                         # your public IP, for PUBLIC_HOST
nano .env
```

In `nano`, set these lines (replace the existing ones), then save with `Ctrl+O`, `Enter`, and exit with `Ctrl+X`:

```
VLLM_API_KEY=<the value printed above>
VLLM_PORT=8080
VLLM_BASE_URL=http://172.17.0.1:8080/v1
MCP_API_TOKEN=<the value printed above>
PUBLIC_HOST=<IP>.sslip.io
```

`172.17.0.1` is the machine's own address as seen from its Docker containers, so this one URL works for every container. `<IP>.sslip.io` is a free hostname that points at your IP, so you get HTTPS without buying a domain.

Keep `.env` private. Save the two secrets in your password manager: you'll paste them into Evolus.

**[GPU]**
```bash
set -a && . ./.env && set +a
./deploy/vllm/serve.sh
docker logs -f pitstop-vllm          # Ctrl+C stops following; the model keeps running
```

The first run downloads the vLLM image (~20 GB) and the Qwen3-32B model (~65 GB).
✅ The log ends with `Application startup complete`. A crash or `out of memory` instead → paste the last 40 lines (`docker logs pitstop-vllm 2>&1 | tail -40`).

If `ufw` is active (`ufw status` says "active"), allow the ports Docker and HTTPS need:

```bash
ufw allow 80,443/tcp && ufw allow from 172.16.0.0/12 to any port 8080 proto tcp
```

## Step 4 — Test the model (10 min)

This runs our scripts inside the project's Docker image, so no Python setup is needed anywhere.

**[GPU]**
```bash
docker compose build agent
docker compose run --rm --no-deps agent python scripts/check_vllm.py
```
✅ `OK: called check_inventory({'part_number': 'EM-1004'})`

```bash
docker compose run --rm --no-deps agent python -m scripts.eval_agent --repeat 3 -v 2>&1 | tee eval.txt
```
✅ Ideally `9/9 passed`. Whatever it says, paste `eval.txt` to Claude: the failures tell us what to fix in the prompt or model settings. Print it again with `cat eval.txt`.

## Step 5 — Put the MCP server and the model online over HTTPS (10 min)

**[GPU]**
```bash
docker compose --profile devmocks --profile public up -d --build
docker compose ps                     # mcp, agent, mocks, caddy: all "running"
```

**[Mac]** Check from outside. Put your values in first:
```bash
export H=<IP>.sslip.io MCP_TOKEN=<MCP_API_TOKEN> VLLM_KEY=<VLLM_API_KEY>
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://$H/mcp                                   # ✅ 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://$H/mcp -H "Authorization: Bearer $MCP_TOKEN"  # ✅ not 401 (e.g. 400 or 406)
curl -s https://$H/v1/models -H "Authorization: Bearer $VLLM_KEY"                                  # ✅ JSON listing Qwen/Qwen3-32B
```

If the requests hang or fail with an SSL error, ports 80/443 aren't reachable. Check the instance's firewall in the AMD Developer Cloud console and `ufw` (step 3). Then run `docker compose logs caddy | tail -30` and paste the output.

## Step 6 — Evolus (30–60 min)

You need the **event code** from the kickoff (lablab.ai Discord or the kickoff email).

1. **Workspace:** create it at app.evolus.ai with the event code.
2. **Model endpoint** (wherever Evolus lets you add a model, provider or "bring your own model"):
   - Base URL: `https://<IP>.sslip.io/v1`
   - API key: your `VLLM_API_KEY`
   - Model name: `Qwen/Qwen3-32B`

   Screenshot this page for the submission.
3. **Knowledge library:** upload the manual. Until your teammate's manual exists, use the short placeholder:

   **[GPU]**
   ```bash
   docker compose run --rm --no-deps agent python -c "from devtools.fakes import MANUAL; print('# Machine manual M-01 (draft)\n\n' + '\n\n'.join(f'## {s.heading}\n{s.text}' for s in MANUAL))" > manual-draft.md
   ```

   **[Mac]**
   ```bash
   scp root@<IP>:pitstop/manual-draft.md ~/Downloads/
   ```

   Then upload `~/Downloads/manual-draft.md`.
4. **MCP connector:**
   - URL: `https://<IP>.sslip.io/mcp`
   - Auth: header `Authorization: Bearer <MCP_API_TOKEN>`, or "bearer token" = the token.

   ✅ Evolus lists 5 tools: `check_inventory`, `get_supplier_quote`, `draft_purchase_order`, `find_free_engineers`, `draft_booking`.
5. **Agent:** use our model, attach the library and the connector, and paste these instructions. Print them on the GPU with:

   ```bash
   docker compose run --rm --no-deps agent python -c "from agent.prompts import SYSTEM_PROMPT; print(SYSTEM_PROMPT)"
   ```

   Test it in Evolus's playground or chat by pasting the sample alert:

   ```bash
   docker compose run --rm --no-deps agent python -c "from devtools.fakes import sample_alert; from agent.prompts import alert_message; print(alert_message(sample_alert()))"
   ```

   ✅ It searches the manual, checks stock, drafts an order, finds a slot on 15 Oct 14:00 and drafts a booking. To start a fresh run, reset the mocks: `docker compose exec mocks python -c "import httpx; print(httpx.post('http://localhost:8000/admin/reset?scenario=out_of_stock').json())"`.
6. **Workflow:** build diagnose → check stock → order if missing → book or escalate → summary.
7. **API key:** create one and keep it with your other secrets.

**Send Claude** (no keys):
- the Evolus API docs page for chatting with an agent or running a workflow, as a link or copied text
- the base URL, and the auth header format
- how a specific agent or workflow is addressed (an id in the URL? the `model` field?)
- whether the docs show tool calls in the reply

Claude then updates `agent/evolus_client.py` and you test with `AGENT_BACKEND=evolus`.

## Step 7 — Stop spending credits

When you're done for the day:

**[GPU]**
```bash
docker compose down && docker stop pitstop-vllm
```

Then in the console, **destroy** the instance if you won't use it for a while. On most clouds, including DigitalOcean (which runs AMD Developer Cloud), a powered-off instance keeps billing; check AMD's pricing page to be sure. Destroying it deletes the downloaded model, and next time steps 2–5 take about 30–40 minutes again. Evolus will then need the new IP (update the model endpoint and connector URLs).

## Cheat sheet

| What | Where |
|---|---|
| Model logs | `docker logs -f pitstop-vllm` |
| Service logs | `docker compose logs -f mcp` (or `agent`, `mocks`, `caddy`) |
| Switch demo scenario | `docker compose exec mocks python -c "import httpx; print(httpx.post('http://localhost:8000/admin/reset?scenario=in_stock').json())"` (`out_of_stock`, `in_stock`, `no_engineer`) |
| Update code after a push | `git pull && docker compose --profile devmocks --profile public up -d --build` |
