# botalert

A personal stock-checker bot for one owner. It watches a list of product
page URLs (your watch list), rechecks them on a configurable cadence, and
sends **one** Discord webhook alert per out-of-stock → in-stock transition.
Management happens two ways, against one shared source of truth:

- **Discord** — a control message posted in your management channel
  (view watch list, manage products, relabel, remove, change cadence
  live, no restart), with every interaction authorized to your user id.
- **Browser dashboard** — a token-gated static page on the exposed port
  (add / edit / remove products, set cadence, "Check now"), plain
  HTML + JavaScript, no framework or build step.

Restock alerts arrive in the management channel via webhook. Failed or
indeterminate checks never change a product's last-known status and
never alert.

## How stock determination works

Each check fetches the page (configurable `User-Agent`, 15 s timeout,
SSL verification on, no JS rendering — see *Bot protection* below for
the fetch backend) and evaluates the raw page text in strict precedence
order, case-insensitive substring matches:

1. the product's **in-stock indicator** (if set) → `in_stock`
2. the product's **out-of-stock indicator** (if set) → `out_of_stock`
3. the built-in **in-stock phrase set**: `add to cart`, `add to basket`,
   `buy now`, `in stock`, `available now`
4. the built-in **out-of-stock phrase set**: `out of stock`, `sold out`,
   `notify me`, `currently unavailable`, `unavailable`
5. nothing matched → `unknown`

Provided indicators that don't match fall through to the heuristics
(precedence, not exclusivity). If a page matches *both* heuristic sets,
the in-stock set wins ("in-before-out" bias: a visible purchase CTA is
the strongest signal, and the cost of the alternative — a false restock
alert — is the one failure the design is built to eliminate). `unknown`
is a failed determination: it does **not** change the last-known status,
counts toward `consecutive_failures`, and at the threshold (default 10
consecutive) triggers a once-only ⚠️ owner notice.

**Status legend:** `🟢 in stock` · `🔴 out of stock` · `⚪ unknown (not
determined yet)`

**Bot protection (WAF).** Several major retailers refuse to serve their
pages to a plain HTTP client — Walmart returns "Robot or human?",
Newegg a Cloudflare "Just a moment..." wall, Amazon a soft cookie gate
(a ~4 KB `validateCaptcha` page served as a 200). `FETCH_BACKEND`
(default `auto`) handles this without a browser: the plain `urllib`
fetch goes first, and when the result looks like bot protection (WAF
status 403/407/429/444/451/503, a 2xx page carrying a known gate
marker, or a timeout — some WAF edges drop plain clients instead of
answering with a wall page) the fetch is retried once through
`curl_cffi` with a full
Chrome TLS/HTTP-2 identity (`impersonate="chrome"`), which these WAFs
accept — a *headless browser* is actually refused by Walmart, while the
impersonated curl client passes. On that pass, Amazon's cookie gate is
solved in-session (submit the hidden form, refetch with the returned
cookie). Set `FETCH_BACKEND=http` for the original behavior, or
`FETCH_BACKEND=curl` to always use the impersonated client (e.g. from
an IP with a worse reputation). Pages that *still* can't be fetched
are handled exactly as before: `unknown`, no false alerts, and the
once-only repeated-failure ⚠️ notice.

**Extending the phrase sets:** the heuristics are two frozen data tables
(`IN_STOCK_PHRASES`, `OUT_OF_STOCK_PHRASES`) at the top of
`botalert/stock.py` — plain string sets, no regex, no HTML parsing. Add
a phrase to the set and the matching (and its unit tests) stay trivially
testable. For a specific tricky retailer, prefer the per-product
indicators in the dashboard's edit form — they exist exactly for that
and take precedence over the built-ins.

## Run locally

```bash
cd botalert                                  # repo root
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                         # then fill in every required value
python -m botalert
```

On startup: the five required env vars are validated (fail fast with a
named-variable error if any is missing/empty), state is loaded from
`STATE_FILE`, the control message is posted to the management channel,
the scheduler is armed, and Flask serves the dashboard + API on `:8080`
(env `PORT`).

For deterministic restock testing you can serve the included fixture
pages (`fixtures/out.html`, `fixtures/in.html`) with
`cd fixtures && python3 -m http.server 8000` and watch
`http://localhost:8000/out.html` — see `specs/001-stock-checker-bot/quickstart.md`
for the full validation scenarios.

## Run in a container (canonical)

```bash
docker build -t botalert .
docker run -d --name botalert \
  -p 8080:8080 \
  -v botalert-data:/data \
  --env-file .env \
  botalert
```

- `python:3.15-slim`, pinned runtime deps only (dev deps never enter the
  image), non-root user, `VOLUME /data` — the state file lives at
  `/data/state.json` (image default) and survives redeploy on the
  `botalert-data` volume.
- The app binds `0.0.0.0:$PORT` (default `8080`); map any host port
  with `-p <host>:8080` — no code changes needed.
- Health: `curl -s localhost:8080/api/health` →
  `{"status": "ok", "gateway_connected": ..., "cycle_in_progress": ...}`
  (public, exposes no state or secrets).
- If a corrupt state file is found at startup it is **renamed** to
  `state.json.corrupt-<utcstamp>` (never deleted) and the bot starts
  fresh — inspect the rename if state "looks lost" after a rebuild.

## One-time Discord setup

1. **App + bot**: Discord Developer Portal → New Application → Bot tab →
   Reset Token → copy the token (`DISCORD_TOKEN`).
   **No privileged intents** — buttons, selects, and modals are
   delivered over the gateway without `message_content`/`members`;
   botalert never reads message text.
2. **Invite**: OAuth2 → URL Generator → scope `bot`; permissions
   **View Channels, Send Messages, Embed Links** → open the generated
   URL in your server (the management channel).
3. **Ids**: enable Developer Mode (User Settings) → copy your own
   **user id** (`OWNER_DISCORD_ID`) and the management channel's
   **channel id** (`DISCORD_MANAGE_CHANNEL_ID`).
4. **Webhook**: management channel → Channel Settings → Integrations →
   New Webhook → copy the URL (`BOTALERT_WEBHOOK_URL`). Restock alerts
   land here.

## Configuration (`.env`)

All configuration is environment-only (never persisted). The five
required variables fail fast at startup if missing or empty.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DISCORD_TOKEN` | ✅ | — | bot token (gateway auth) |
| `OWNER_DISCORD_ID` | ✅ | — | the only Discord id allowed to manage (FR-010) |
| `DISCORD_MANAGE_CHANNEL_ID` | ✅ | — | control message + operational notices channel |
| `BOTALERT_WEBHOOK_URL` | ✅ | — | restock-alert webhook destination |
| `BOTALERT_TOKEN` | ✅ | — | shared secret for the dashboard/management API |
| `PORT` | ❌ | `8080` | exposed port (dashboard + API) |
| `BOTALERT_BASE_URL` | ❌ | — | dashboard URL shown by the "➕ Add product → dashboard" Discord button (button rendered only when set) |
| `STATE_FILE` | ❌ | `state.json` (container: `/data/state.json`) | durable state file path |
| `CHECK_TIMEOUT_SECONDS` | ❌ | `15` | per-fetch timeout |
| `CHECK_PAUSE_SECONDS` | ❌ | `2` | pause between consecutive fetches (politeness) |
| `FAILURE_THRESHOLD` | ❌ | `10` | consecutive-failure notice threshold (once-only ⚠️) |
| `DEFAULT_CADENCE_MINUTES` | ❌ | `5` | initial recheck cadence |
| `MIN_CADENCE_MINUTES` | ❌ | `1` | enforced cadence floor |
| `MAX_CADENCE_MINUTES` | ❌ | `1440` | enforced cadence ceiling (24 h) |
| `USER_AGENT` | ❌ | `botalert/0.1 (personal stock checker)` | User-Agent sent with page fetches (`http` backend only; the `curl` backend presents a full Chrome identity) |
| `FETCH_BACKEND` | ❌ | `auto` | page fetch backend: `http` (plain urllib) / `curl` (Chrome-impersonated curl_cffi, passes retailer WAF walls) / `auto` (http first, one curl retry when a bot wall or WAF-induced timeout is detected) |
| `LOG_LEVEL` | ❌ | `INFO` | process-wide logging level (`DEBUG` adds stock-phrase match details and page snippets for `unknown` results) |

`python-dotenv` loads `.env` in local development only; the container
receives the same variables via `--env-file`.

## Security model

- **Dashboard/API** (the exposed port): the static shell (`GET /`) is
  served without the token — it holds no data and changes no state.
  Every `/api/*` endpoint except `GET /api/health` requires
  `X-Botalert-Token`; the comparison is constant-time
  (`hmac.compare_digest`) and always precedes any state access. The
  dashboard prompts once, stores the token in `localStorage`, and
  attaches it to every fetch; a 401 clears it and re-prompts.
- **Discord**: every management interaction is authorized by
  `interaction.user.id == OWNER_DISCORD_ID` before any state access;
  non-owners get an ephemeral rejection and nothing else.
- **Secrets** live only in the environment/`.env` — never in the state
  file, the dashboard, or the logs.

## Known limitations (accepted, v1)

- **Werkzeug dev server in the container** — the built-in Flask server
  (`threaded=True`) is the server in the image too. Acceptable for a
  single-owner personal deployment (research.md §5); swap in gunicorn
  or similar if you ever front it with a real load.
- **No TLS in v1** — the exposed port is for the owner's own devices
  only; serve it behind a TLS-terminating reverse proxy if the network
  isn't trusted.
- **No check history** — the state file holds current state + cadence +
  control-message id only; v1 deliberately keeps no per-check log.
- **Dashboard refresh model** — the page re-renders on load and after
  each of your operations only; there is no background auto-refresh and
  no push. Re-open (or perform an operation) to see scheduler-driven
  changes.
- **Owner-side port reachability only** — after the rev-2 gateway
  pivot, Discord input arrives over the authenticated gateway; the
  exposed port does not need to be publicly reachable at all.
- **Server-rendered pages only** — no JS rendering; pages that
  require JS (or block plain HTTP clients) will read `unknown` — set
  per-product indicators or pick a different page.

## Development

```bash
pytest -q                            # full unit suite (well under a minute)
mypy --strict botalert tests         # zero errors
```

Specs, contracts, and the runbook live in
`specs/001-stock-checker-bot/` — start with `spec.md`, then
`quickstart.md` for the end-to-end validation scenarios.
