# Quickstart & Validation Guide: Stock Checker Bot (Feature 001)

**Branch**: `001-stock-checker-bot` | **Date**: 2026-11-18

Runnable, end-to-end validation for the feature. Design details live in [plan.md](./plan.md), [research.md](./research.md), [data-model.md](./data-model.md), and [contracts/](./contracts/) — this guide only says *how to run and check*.

## Prerequisites

- Python 3.13+ (container path: Docker only)
- A Discord account with a server (guild) you control
- ~10 minutes for one-time Discord setup (below)

### One-time Discord setup (see contracts/discord.md §Application setup)

1. **App + bot**: Discord Developer Portal → New Application → Bot tab → Reset Token → copy the token. No privileged intents to enable.
2. **Invite**: OAuth2 → URL Generator → scope `bot`; permissions: View Channels, Send Messages, Embed Links → open the generated URL in your server.
3. **Ids**: Developer Mode (User Settings) → copy your own **user id** (`OWNER_DISCORD_ID`) and the **channel id** of the management channel (`DISCORD_MANAGE_CHANNEL_ID`).
4. **Webhook**: management channel → Channel Settings → Integrations → New Webhook → copy the URL (`BOTALERT_WEBHOOK_URL`). Restock alerts land here.

## Setup (local development)

```bash
cd botalert                                   # repo root
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                          # then fill in every value:
```

Required in `.env` (full table: data-model.md §5):

```dotenv
DISCORD_TOKEN=...            # bot token
OWNER_DISCORD_ID=...         # your user id
DISCORD_MANAGE_CHANNEL_ID=...# management channel id
BOTALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...
BOTALERT_TOKEN=<long random secret>
```

Optional: `PORT=8080`, `BOTALERT_BASE_URL`, `STATE_FILE`, `CHECK_TIMEOUT_SECONDS=15`, `CHECK_PAUSE_SECONDS=2`, `FAILURE_THRESHOLD=10`, `DEFAULT_CADENCE_MINUTES=5`, `MIN_CADENCE_MINUTES=1`, `MAX_CADENCE_MINUTES=1440`, `USER_AGENT`.

### Local fixture pages (for deterministic restock testing)

Create `fixtures/out.html` and `fixtures/in.html` (test *data*, not product code):

```html
<!-- fixtures/out.html -->
<h1>Example Sneakers</h1>
<p>Sorry, this item is currently <b>out of stock</b>. Notify me when available.</p>

<!-- fixtures/in.html -->
<h1>Example Sneakers</h1>
<p>In stock and ready to ship.</p>
<button>Add to Cart</button>
```

```bash
cd fixtures && python3 -m http.server 8000   # leave running
```

## Run

**Local:**

```bash
python -m botalert
# log: control message posted to the management channel, scheduler armed, Flask on :8080
```

**Container (canonical):**

```bash
docker build -t botalert .
docker run -d --name botalert \
  -p 8080:8080 \
  -v botalert-data:/data \
  --env-file .env \
  botalert
```

## Validation scenarios

Each step lists what to do and the **expected outcome** (requirements cited). Run in order; 1–3 need no Discord, 4–11 need the bot connected.

### 1. Static gates (no bot running)

```bash
pytest -q                          # full unit suite passes, well under a minute
mypy --strict botalert tests       # zero errors
```

→ Expect: all green (Constitution I & III).

### 2. Health + auth boundary (bot running)

```bash
curl -s localhost:8080/api/health
curl -s localhost:8080/api/products
curl -s localhost:8080/api/products -H "X-Botalert-Token: <token>"
curl -s localhost:8080/api/products -H "X-Botalert-Token: wrong"
```

→ `{ "status": "ok", ... }` · `401 unauthorized` · `200 { "products": [] }` · `401 unauthorized` (SC-008: secretless requests rejected, state untouched).

### 3. Watch-list CRUD via API/dashboard (US4)

Open `http://localhost:8080/`, enter the token once (prompted; stored in localStorage).

| Action | Expected |
|---|---|
| Add `http://localhost:8000/out.html` (label "Fixture OOS") | 201, row appears with status `unknown` (FR-001, US4-4) |
| Add the same URL again | `409 duplicate_url`, no second row (US4-6) |
| Add `not-a-url` | `400 invalid_url`, list unchanged (US4-5) |
| Relabel to "Fixture Shoes" | confirmation; new label shown (FR-004) |
| Remove it, re-add for later steps | confirmation; row gone (FR-002) |

### 4. First check — no false alert (US1-4, SC-003)

The page is out of stock. Wait for the next cycle (or hit **Check now** → 202, then poll the list).

→ Status becomes `out_of_stock`, **no webhook message in the management/webhook channel** (adding-while-OOS never alerts; assumption "No immediate alert on add").

### 5. Restock → exactly one alert (US1-1/3, SC-002)

The product's status is `out_of_stock` from scenario 4. The URL is immutable (changing it = remove + add, which would reset status to `unknown` and *never* alert — US1-4), so change the **content** of the page at the same URL:

1. Overwrite `fixtures/out.html` in place with the in-stock content (e.g. copy the body of `fixtures/in.html` into it — the local file server re-reads on each request, no restart needed).
2. Wait for the next cycle (or hit **Check now** → 202). When the cycle sees `in_stock` after the known `out_of_stock`: **one** webhook message in the channel: `🔔 Restock: <label>` + URL + detection time (FR-007, FR-008).
3. Trigger **Check now** again while still in stock → **no further alerts** (US1-2).

### 6. Failed checks never flip status (US1-5, FR-012, SC-003)

Add `http://localhost:9999/absent` (nothing listens). Run cycles.

→ Status stays `unknown`/unchanged, no alert, `consecutive_failures` climbs; at 10 consecutive failures **one** ⚠️ notice appears in the management channel and does not repeat (FR-016, edge cases).

### 7. Discord management (US2, FR-017, SC-007)

In the management channel:

1. Control message present: **botalert — watch list controls** with the four buttons (posted on startup; delete it and watch it re-post — self-healing).
2. **View watch list** → ephemeral list with labels, status emojis, URLs (FR-003).
3. **Manage products** → select "Fixture Shoes" → **Remove** → ephemeral confirm, product gone (US2-2).
4. **Manage products** → select another product → **Relabel** → type a new label in the modal → confirm; **View** shows the new label (US2-3).
5. **Change cadence** → modal: `2` → confirm "new cadence 2 minutes"; a non-numeric entry (`abc`) → rejected with the reason; `0.5` → rejected stating the 1-minute minimum (US3-2/3). Wait: the next cycle lands ~2 minutes later (SC-004, no restart).
6. **Non-owner**: have a second Discord account (or a friend) click any control → ephemeral `❌ Not authorized…`, watch list unchanged (US2-4, SC-007).

### 8. Persistence across restart (FR-009, SC-005)

```bash
docker stop botalert && docker start botalert   # or: Ctrl-C locally, then re-run
```

→ Watch list, last-known statuses, cadence, and the control message id all survive (control message re-posted once); a product already `in_stock` does **not** re-alert after the restart (no duplicate notifications — the dedup state is the persisted status, data-model.md §6).

### 9. Port configurability (FR-013, US4-3)

```bash
docker run -d --name botalert2 -p 9090:8080 --env-file .env -v botalert-data:/data botalert
curl -s localhost:9090/api/health
```

→ Same app reachable on the new host port, no code changes. (The app binds `0.0.0.0:$PORT`, default `8080` — a different host-port mapping is the primary mechanism; setting `PORT` to a different value and mapping that instead also satisfies deployment-time configurability.)

### 10. Interface parity (US4-2)

Add a product via the dashboard; immediately **View watch list** in Discord → the new product (label, URL) is present. Remove it in Discord; refresh the dashboard → gone. One source of truth, both views agree (data-model.md §6 concurrency rule).

### 11. Scale sanity (SC-006)

Add ~25 fixture URLs (e.g. the local http.server on 10 paths), cadence 5 min.

→ Full sequential cycle completes well under the interval (25 × (fetch + 2 s pause)); every product's `last_checked_at` advances each cycle; no missed cycles (watch for the ⚠️ threshold notices — none expected for healthy fixture pages).

## Troubleshooting pointers

| Symptom | Check |
|---|---|
| Bot never posts the control message | Token/channel-id env vars; does the bot have View/Send/Embed perms in that channel? (contracts/discord.md §Application setup) |
| Dashboard stuck on token prompt | `BOTALERT_TOKEN` in `.env` matches the typed token exactly; check container `--env-file` actually loaded |
| Every check is `unknown` | Retailer page changed layout or blocks plain HTTP clients — set per-product indicators in the dashboard edit form (research.md §6), or the page needs JS (out of scope, spec Assumptions) |
| Restock alert didn't arrive | Look for the 📡 webhook-failure notice in the management channel — the webhook URL may have been revoked (edge case "Invalid webhook destination") |
| State looks lost after rebuild | Volume not mounted (`-v botalert-data:/data`); or a corrupt-file rename exists: `/data/state.json.corrupt-*` — inspect it (research.md §7) |
