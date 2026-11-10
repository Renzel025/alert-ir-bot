# alert-ir-bot

MonitorFlow alert → Lark bot → policy check → AI triage → check person (N min) → PagerDuty → Zoho Flow posts status back.
Design notes live in the Obsidian vault `~/Desktop/Alert Incident Response` (see *Process Flow*).

```
app/
  main.py          FastAPI: /lark/events, /pagerduty/status, /health; starts the workers
  pipeline.py      the flow diagram as code (start reading here)
  ai_triage.py     AI decision — YOUR part (# TODO); until done, every alert pages
  alert_parser.py  MonitorFlow text template → Alert
  policy.py        policy.yaml lookup (always-page classes, check person, N min)
  lark_client.py   token, post, reply-in-thread, list messages, decrypt events
  pagerduty.py     Events API v2 trigger / resolve
  workers.py       timeout sweeper (+ optional group poller)
  db.py            SQLite: incidents, decisions, outcomes, processed_messages
scripts/
  find_sender.py   find ALERT_BOT_SENDER_ID
  simulate.sh      local test without Lark (DRY_RUN=1)
```

## 1. Run it locally (dry run, no credentials)

```bash
cd ~/Desktop/alert-ir-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # DRY_RUN=1 is the default; set ALERT_CHAT_ID=oc_test ALERT_BOT_SENDER_ID=cli_alertbot
# in policy.yaml set defaults.check_person: ou_checkperson
uvicorn app.main:app --port 8080 --reload
```

In a second terminal:

```bash
./scripts/simulate.sh alert            # → log shows: thread opened, AI triage failed (not wired yet) → paged
./scripts/simulate.sh alert            # same alert again → "Still firing" in the thread, no second page
curl -s localhost:8080/health
sqlite3 alert_ir.sqlite3 'select id,status,rule_name from incidents; select * from decisions;'
```

Once `ai_triage.triage()` returns `can_wait`, the flow continues:

```bash
./scripts/simulate.sh alert            # → "AI says this can wait … @check person … 5 min"
export ALERT_MSG_ID=<id printed above>
./scripts/simulate.sh reply normal     # → resolved_normal
# or wait N min → sweeper pages; or: ./scripts/simulate.sh reply escalate
```

## 2. Wire the AI (your part)

Open `app/ai_triage.py` and fill in the `# TODO` steps in `triage()` (and optionally `classify_reply()`).
The pipeline already handles every failure mode: exception, bad JSON, unknown decision → page.

## 3. Lark app

Developer Console → your app:

- **Permissions**: `im:message`, `im:message:send_as_bot`, `im:message.group_msg` (receive group messages), `im:chat:readonly`.
- **Event subscriptions**: request URL `https://<server>/lark/events`, add event `im.message.receive_v1`. Copy the *Verification Token* into `.env` (and the Encrypt Key if you enabled encryption).
- **Bot**: add it to the alert group (and the fixed group if different). Publish the app version.
- `ALERT_CHAT_ID`: the alert group's `oc_...`.
- `ALERT_BOT_SENDER_ID`: run `python -m scripts.find_sender` and copy the sender id of a MonitorFlow message.
- Lark may not deliver events for messages sent by other bots. If a test alert produces no event, set `LARK_POLL_INTERVAL_SEC=60`.
- For local testing with real Lark events, expose port 8080 with ngrok (`ngrok http 8080`) and use that URL.

## 4. PagerDuty (with Jeno)

Test service → Integrations → Events API v2 → integration key → `PD_ROUTING_KEY`.
Every trigger carries `custom_details.lark_thread` (the thread root message id) so Zoho Flow can reply in the right thread.
`dedup_key` is `<rule_fingerprint>-<unix time>` — one PagerDuty incident per bot incident.

## 5. Zoho Flow (PagerDuty status → bot)

One Zoho flow handles all PagerDuty events (a Decision step on event type). Its last action is a Webhook / custom function:

```
POST https://<server>/pagerduty/status
Header: X-Status-Secret: <PD_STATUS_SECRET from .env>
Body:   {"dedup_key": "<alert dedup_key>", "status": "acknowledged|escalated|resolved", "user": "...", "time": "..."}
```

The flow's Lark branches (post status in the Lark thread) are described in the vault's *Zoho Flow Setup Guide*. The bot does not post PagerDuty status itself unless `POST_PD_STATUS_TO_THREAD=1`.

### Card buttons (Acknowledge / Resolve in Lark)

The Zoho card carries two action buttons. A click arrives at `POST /lark/events` as a `card.action.trigger` callback (same URL as message events; set it under the app's callback configuration in the Lark developer console). `pipeline.handle_card_action()` reads `value.action` and `value.incident_id` and calls PagerDuty's REST API (`pagerduty.set_status()`, needs `PD_API_KEY` + `PD_FROM_EMAIL`). The bot never writes "acknowledged" to Lark itself: PagerDuty fires `incident.acknowledged`, Zoho posts the yellow card. If the REST call fails, nothing appears in Lark, so the thread never lies.

## 6. Policy

`policy.yaml` — reloaded automatically on change. `severity_always_page` is a hard floor; rules match the rule name by regex; the first match wins. Move it to Lark Base later by replacing `policy._doc()`.

## 7. Deploy on the ECS server

Lark's event callback and Zoho's webhook need a public **HTTPS** URL, so the server needs a DNS name pointing at its public IP, and ports 80/443 open in the ECS security group.

```bash
# on the server
git clone <repo> alert-ir-bot && cd alert-ir-bot        # or scp the folder
cp .env.example .env && nano .env                        # DRY_RUN=0, real keys
mkdir -p data && cp policy.yaml data/policy.yaml         # live policy file (edit without rebuilding)
nano deploy/Caddyfile                                    # put the real domain
docker compose up -d --build
docker compose logs -f bot
curl -s https://<domain>/health
```

Then set the Lark event URL to `https://<domain>/lark/events` and the Zoho webhook to `https://<domain>/pagerduty/status`.

- State is in `./data/` (SQLite + policy.yaml) — back it up; it survives `docker compose up --build`.
- Updates: `git pull && docker compose up -d --build`.
- No Docker? `deploy/alert-ir-bot.service` runs it under systemd like lark-ops-ai (put Caddy or nginx in front for TLS).
- Point an external monitor (MonitorFlow itself, or Uptime Kuma) at `/health` — it returns 503 when the sweeper/poller go quiet. The bot can't page about its own death.
- After the first `pip install` on the server, run `pip freeze > requirements.lock` and install from that next time so versions don't drift.

## Behaviour summary

| Situation | What happens |
|---|---|
| Always-page class / severity | PagerDuty immediately |
| AI says escalate | PagerDuty |
| AI says can wait | thread reply + @check person, N-minute timer |
| Check person says normal | incident resolved, outcome `human_normal` |
| Anyone says escalate | PagerDuty |
| Someone else says normal | ignored (only the tagged person can) |
| No reply in N min | PagerDuty (`timeout_paged`) |
| AI error / bad JSON / no check person | PagerDuty |
| PagerDuty unreachable | thread warning, retry every 60 s |
| Same alert fires again | "still firing" in the same thread |
| MonitorFlow posts `[RESOLVED]` | incident closed, PagerDuty resolve if it was paged |
| Bot restart | processed message ids and timers are in SQLite; nothing is re-handled or lost |
