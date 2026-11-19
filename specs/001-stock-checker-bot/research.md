# Research: Stock Checker Bot (Feature 001)

**Branch**: `001-stock-checker-bot` | **Date**: 2026-11-18

Resolves every open item in the plan's Technical Context. Each entry: **Decision** / **Rationale** / **Alternatives considered**.

---

## §1 Approved dependencies (Constitution Principle IV)

**Decision**: The approved runtime dependency set is exactly three packages, all user-directed, all pinned:

| Package | Pin | Why stdlib (or an approved dep) can't cover it |
|---|---|---|
| `flask` | `3.1.3` | User-specified web framework. Stdlib `http.server` has no routing, no static-file serving with correct MIME types, and no threaded request handling; covering all of that by hand would be hundreds of lines of error-prone code for no simplicity win. |
| `discord.py` | `2.7.1` | User-specified Discord module (the sample bot uses it). A from-scratch Discord gateway client (WebSocket handshake, resume/identify sequences, opcode handling, heartbeat, interaction dispatch) is a large, security-sensitive surface that stdlib cannot cover. |
| `python-dotenv` | `1.2.4` | User-specified `.env` loading (matches the sample bot). A stdlib shim is possible but was explicitly directed by the user and costs one small, stable package. |

Dev-only, mandated by the Constitution (not "features"): `pytest==9.2.0` (Principle III) and `mypy==2.4.1` (Principle I). Neither ships in the container image.

**Rationale**: Constitution IV requires prior explicit approval with justification for every third-party package and demands pinning. All three runtime deps were named by the user in the spec input/sample-bot direction; pins were verified against live PyPI metadata on 2026-11-18 (all have `requires_python` compatible with Python 3.15).

**Alternatives considered**:
- *`aiohttp`/`httpx` for fetching/webhooks* — rejected: stdlib `urllib.request` covers sequential HTTP GET/POST with timeouts for this scale; an async HTTP client would force the whole checker into asyncio for no benefit (Constitution: simple solution).
- *`gunicorn` as WSGI server* — rejected: a 4th runtime dependency is not justified for a single-owner tool; Flask's built-in (Werkzeug) server with `threaded=True` is sufficient (see §5).
- *`SQLAlchemy`/SQLite for state* — rejected: the spec mandates a minimal durable store (FR-009: "a single file on a volume"); stdlib `json` + `os.replace` covers it.
- *`discord.py` in HTTP-only mode (pre-rev-2 design)* — rejected: spec rev 2 explicitly pivots to the standard persistent-gateway mode; it also removes the need for request-signature verification and a Discord-reachable public port.

---

## §2 Python version pin (was: NEEDS CLARIFICATION in plan)

**Decision**: Runtime pinned to **Python 3.15** — current stable is **3.15.2** (3.15 released 2026-10-09, in bugfix status per python.org on 2026-11-18). Base image: `python:3.15-slim`. Minimum supported version for local development: **3.13**. The mypy config sets `python_version = 3.13` so the type gate enforces the floor (no 3.14+/3.15-only syntax or typing features slip in).

**Rationale**: The plan's open item asked for the pin to be confirmed against the current stable release. 3.15 has been current stable for ~6 weeks; all pinned deps declare `requires_python >= 3.10` (verified via PyPI metadata), so 3.15 support is guaranteed. 3.13 as the floor keeps the code readable on any recent CPython while still allowing modern typing (3.12+ `Self`, 3.13 built-in generics) — 3.12/3.11 are in "security" phase and are no longer needed as a floor for a new project.

**Alternatives considered**:
- *`python:3.13-slim` (the pre-research draft pin)* — rejected as the primary pin: two minor versions behind current stable with no compensating benefit for a new project.
- *`python:3.14-slim` (3.14.10, still in bugfix phase until 2030)* — viable; rejected in favor of current stable per the plan's own research question, and to keep the floor (3.13) a full two releases behind the runtime.
- *Minimum supported 3.12* — rejected: 3.12 is in security-only phase (EOL 2028-10); a new project has no reason to promise support for it.

---

## §3 Discord architecture (rev-2 pivot)

**Decision**: Standard persistent-gateway mode with a **bare `discord.Client`** (not `commands.Bot`) in its own thread:
- `discord.Client(intents=discord.Intents.default())` — no privileged intents required: button/select/modal interactions are delivered over the gateway without `message_content` or `members` intents (only *reading message text* would need those, and botalert never reads message text). This keeps Discord app setup minimal (no "privileged intent" toggles in the developer portal).
- One dedicated thread runs `client.run(token)` (its own event loop). Flask runs on the main thread; the scheduler runs a third thread (§5).
- **One `on_interaction` router** keyed on `custom_id` for all buttons and select menus (`ctl.view`, `ctl.manage`, `ctl.remove:<id>`, `ctl.relabel:<id>`, `ctl.cadence`, select `sel.product`). **Modals** (`relabel`, `cadence`) are `discord.ui.Modal` subclasses with typed `on_submit` handlers — instantiated per trigger with their target product id, so the modal carries its own context.
- All owner-facing replies are **ephemeral**; operational notices (restock failure threshold, webhook delivery failure) are plain messages in the management channel.
- **Owner authorization**: `interaction.user.id == Config.owner_discord_id`; non-owner interactions get an ephemeral rejection and change nothing (FR-010, SC-007).
- **Control-message lifecycle**: on `on_ready`, the bot posts a fresh control message (buttons) to `DISCORD_MANAGE_CHANNEL_ID`, deletes its own previous one (a bot may always delete its own messages), and stores the new id in state. If the control message is deleted while the bot runs, `on_message_delete` detects it (id match) and re-posts — so the Discord management surface self-heals with zero extra API surface.
- **Cross-thread outbound** (scheduler → channel): `asyncio.run_coroutine_threadsafe(coro, client.loop)` from the scheduler thread, per discord.py's documented cross-thread pattern.
- **Testability seam**: the routing/authorization/business logic is pure functions over a typed `InteractionRequest` dataclass (action, product id, user id, text) returning typed `InteractionResponse` actions; `on_interaction` is a thin adapter that converts `discord.Interaction` → `InteractionRequest` and applies the result. Unit tests exercise the router with no gateway (Principle III).

**Rationale**: Rev 2 of the spec makes the persistent gateway the design of record and matches the user-provided sample bot's mode. A bare `Client` avoids the command-framework machinery (slash commands, guild registration, `cog` system) that botalert does not use — the simple-solution mandate. A single router (instead of per-component callback `View` classes) is required because the control content is *dynamic*: watch-list views and the product select must always reflect the current state, and router-based dispatch reads state at interaction time. Ephemeral replies keep the management channel uncluttered for a single owner.

**Alternatives considered**:
- *`commands.Bot` (as in the sample bot)* — rejected: the sample uses it because it needs prefix commands; botalert needs only components, and `commands.Bot` pulls in the command dispatcher, event-loop integration, and `!`-prefix surface that would go unused. Bare `Client` is the documented minimal base for component-only bots.
- *Per-component `View` callbacks with stored state* — rejected: stateful views go stale against a mutable watch list (a select built at post time would miss products added later); router + read-at-interaction-time is simpler to reason about and to test.
- *Pre-rev-2 inbound-HTTP mode (interaction POSTs + signature verification)* — rejected by spec rev 2 (explicit user decision); it also required the exposed port to be public to Discord and added HMAC-signature code.
- *`discord.app_commands` (slash commands) for management* — rejected: the spec mandates buttons/select menus/modals as the interaction model.

---

## §4 Typing strategy under mypy `--strict`

**Decision**:
- mypy `2.4.1`, config: `strict = true`, `python_version = 3.13` (enforces the §2 floor), run over `botalert/` and `tests/` as the acceptance gate (zero errors).
- All public functions fully annotated. discord.py ships `py.typed` (2.x), Flask ships `py.typed`, so the entire third-party surface is checked too.
- **Single documented `Any` boundary** (the Constitution Principle I justification recorded in plan.md): `state.py` receives the parsed durable store as `Any` from stdlib `json.loads` (the JSON API is not generic), then immediately validates the shape and narrows into typed dataclasses (`AppState`/`Product`) before anything leaves the module. No other module ever sees `Any`.
- Discord payloads are *not* an `Any` boundary in rev 2: gateway objects arrive as typed `discord.Interaction`; the only untyped extraction is `interaction.data["custom_id"]`-style access in the router adapter, which is confined to the same one-scope validate-then-narrow pattern in `discord_bot.py`.

**Rationale**: Keeps the strict gate honest (it must pass, not merely run) while isolating the two unavoidable external-data boundaries (disk JSON, gateway dispatch) at module edges — exactly the "documented justification" the constitution permits.

**Alternatives considered**:
- *Generic JSON parsing via `json` + `TypedDict`* — considered; `TypedDict` still flows as a `dict` subtype and requires the same runtime validation for untrusted disk content, so it adds ceremony without removing the boundary.
- *mypy per-module gradual typing* — rejected: the constitution requires zero errors across the package; a new codebase has no legacy debt to phase in.

---

## §5 Process & threading model

**Decision**: Three threads in one process, all state access funneled through `StateStore` (single `threading.Lock`):

| Thread | Responsibility |
|---|---|
| Main | Flask (Werkzeug, `threaded=True`) serving dashboard + `/api/*` |
| Bot | `asyncio.run(client.run(token))` — gateway, interaction router, control message, outbound channel sends (also receives cross-thread sends via `run_coroutine_threadsafe`) |
| Scheduler | Loop: `wakeup_event.wait(cadence)` → snapshot watch list → sequential check cycle → save state → re-read cadence. Never runs while another cycle is in flight. |

- **Cadence changes** (Discord modal or API) write state and set `wakeup_event` → the scheduler finishes its current wait, re-reads the cadence from state, and the new interval applies from the next cycle (SC-004).
- **`POST /api/check-now`** (justified addition, plan.md) sets `wakeup_event`; if a cycle is already in progress it returns `409 cycle_in_progress` instead of starting a second one (FR-018: no parallel fetches).
- **Cycle execution**: snapshot the product list (under lock), then per product: fetch (15 s timeout) → determine status → apply transition + failure counter (under lock, save after each product so a crash mid-cycle loses at most one product's update) → webhook on out→in. 2 s pause between fetches (FR-018, politeness assumption).
- **Shutdown**: `SIGTERM`/`SIGINT` → stop event for scheduler → `run_coroutine_threadsafe(client.close(), loop)` → exit 0. (Keep simple: no graceful-drain of in-flight fetch beyond the 15 s timeout.)

**Rationale**: Matches the spec's "single sequential scheduler loop" and "single process" constraints with the fewest moving parts. A single lock + atomic file writes is the standard, verifiable way to get "no lost updates" between the dashboard and Discord (edge case: concurrent interface use) at this scale (≤50 products, one owner). Threads beat asyncio here because the page fetcher is deliberately stdlib-`urllib` (synchronous): wrapping sync I/O in `asyncio.to_thread` per fetch would add complexity for zero throughput change.

**Alternatives considered**:
- *Run the checker as an asyncio task inside the bot's loop* — rejected: blocks the gateway loop on sync `urllib` calls unless wrapped in `to_thread`, which is more code and couples the checker's liveness to the gateway's.
- *`schedule`/APScheduler library* — rejected: a 15-line `while` loop with `Event.wait` covers the requirement (Principle IV).
- *One `threading.Lock` per field / no lock at all* — rejected: lost-update and torn-write risk between Flask request threads, the scheduler, and the bot thread; the constitution's simplicity bar is met by one coarse lock (contended only at millisecond scale).
- *Werkzeug "development server" in a deployed container* — accepted with recorded rationale: single-owner tool behind a shared secret on a private port; adding gunicorn would violate Principle IV's bar for a 4th runtime dep. Documented in README as a known trade-off.

---

## §6 Fetching & stock determination

**Decision**:
- **Fetcher** (`urllib.request`): GET with `User-Agent` (configurable, default `botalert/0.1 (personal stock checker)` — honest identification, politeness assumption), `timeout` (default 15 s), no redirect-following surprises (urllib follows redirects by default — acceptable; the *final* response is what's evaluated). Non-2xx final status → `FetchResult(error=...)` (a failure, never a status — FR-012). SSL verification stays ON (stdlib default); no JS rendering (spec assumption: server-rendered public pages).
- **Status determination** (`determine_status(html, product) → StockStatus`), evaluated in strict precedence order, all **case-insensitive substring** matches against the fetched page text:
  1. `product.in_stock_indicator` (if set) → `IN_STOCK`
  2. `product.out_of_stock_indicator` (if set) → `OUT_OF_STOCK`
  3. Built-in **in-stock phrase set**: "add to cart", "add to basket", "buy now", "in stock", "available now"
  4. Built-in **out-of-stock phrase set**: "out of stock", "sold out", "notify me", "currently unavailable", "unavailable"
  5. No match → `UNKNOWN`
- **Precedence semantics** (FR-006): per-product indicators are *evaluated first* when provided; if none of the provided indicators match, determination falls through to the heuristics (precedence, not exclusivity) — and if the heuristics also fail, the result is `UNKNOWN`.
- **In-before-out bias**: if a page matches both heuristic sets (e.g., a size variant is "out of stock" while the main button is "Add to cart"), the in-stock set wins. Rationale: a visible purchase CTA is the strongest in-stock signal, and the failure mode of the alternative (false restock alerts) is the one the spec makes costless to eliminate (SC-003). Owners with tricky pages are expected to set per-product indicators — which is exactly what they exist for.
- **UNKNOWN is a failed determination** (spec "stock-detection hybrid" assumption): it does not change last-known status, counts toward `consecutive_failures`, and at the threshold (default 10) triggers the once-only owner notice (FR-016, edge cases).
- **Heuristics are a data table in `stock.py`** (two frozen `set[str]`s), not code — trivially extendable, trivially testable, no per-retailer plugins in v1.

**Rationale**: The spec mandates exactly this hybrid (heuristic default + per-product override + unknown fallback) and explicitly scopes out JS-rendered/anti-bot pages. Case-insensitive substring against raw HTML is deliberately naive-but-predictable: the owner sees exactly what matched in quickstart validation and can pin the outcome with per-product indicators when a retailer is tricky.

**Alternatives considered**:
- *BeautifulSoup parsing to visible text only* — rejected: a 4th runtime dep for marginal precision; raw-HTML substring matching is transparent, dep-free, and the per-product indicator escape hatch covers the precision gap.
- *Regex patterns with word boundaries* — rejected: regex adds authoring error (and `re` catastrophic-backtracking surface) for phrases that are plain literals.
- *Score-based (count matches per set)* — rejected: non-deterministic-feeling behavior for a personal tool; strict precedence is easier to explain in README/quickstart.

---

## §7 Durable state file

**Decision**: Single JSON document (schema v1, see data-model.md §5) at `STATE_FILE` (default `state.json`; container default `/data/state.json`):
- **Atomic write**: serialize → write to `state.json.tmp` in the same directory → `os.replace()` over the target. `os.replace` is atomic on POSIX, so a crash never leaves a torn file.
- **Every mutation saves** under the `StateStore` lock (read-modify-write is one critical section); check cycles save after each product (crash mid-cycle loses at most one product's update — acceptable, statuses are re-derived on the next cycle).
- **Startup load**: missing file → fresh state (default cadence, empty watch list). **Corrupt/unparseable file** → log an error, rename the offender to `state.json.corrupt-<utcstamp>` (never delete owner data), start fresh. The control-message id resets, so the bot (re)posts its control message on the next `on_ready` — self-healing.
- **No history** (spec: "no check history in v1") — only current state + cadence + control-message id.
- Timestamps stored as UTC ISO-8601 strings; parsed back with `datetime.fromisoformat`.

**Rationale**: FR-009 mandates "a minimal durable store (for example, a single file on a volume)"; atomic replace + one lock is the minimal design that still survives the restart and concurrent-interface-use edge cases in the spec. Renaming (not deleting) corrupt files preserves forensics for a personal tool.

**Alternatives considered**:
- *SQLite (stdlib `sqlite3`)* — rejected: the spec names a single file as the example store and scopes out anything "more elaborate"; SQLite adds connection/lifecycle surface for data that is one JSON document.
- *In-memory only + rebuild from Discord* — rejected: FR-009 requires restart survival of statuses and cadence.
- *fsync after every write* — considered; `os.replace` on a mounted volume already gives durability for the common failure mode (process crash). Host-disk fsync policy is out of scope for v1.

---

## §8 Security & authentication

**Decision**:
- **Dashboard/API gate (FR-020, SC-008)**: single shared secret `BOTALERT_TOKEN`. The static HTML shell (`GET /`) is served **without** the token — it is a stateless page with no data and changes no state. **Every `/api/*` endpoint except `GET /api/health` requires** the token in the `X-Botalert-Token` header; comparison uses `hmac.compare_digest` (constant-time). The dashboard prompts for the token once, stores it in `localStorage`, and attaches it to every fetch. Missing/invalid token → `401` with the standard error shape; no state is touched (all auth checks precede any state access).
- **Discord gate (FR-010, SC-007)**: `interaction.user.id == OWNER_DISCORD_ID` for every management interaction; the gateway connection itself is authenticated by `DISCORD_TOKEN`, so interactions are inherently attributable (rev-2 benefit — no signature verification code at all).
- **Secrets handling**: all secrets come from the environment/`.env` at deployment (spec assumption "Secrets via environment"); none are written to the state file or the dashboard. `python-dotenv` loads `.env` in local dev only; the container receives vars via `--env-file`/`environment`.
- **Network posture**: the exposed port serves only the owner's devices (rev 2 removes the Discord-reachability requirement); no TLS in v1 (owner-side deployment, single owner) — noted as a known limitation in README.
- **Health endpoint is public** but exposes only `{"status": "ok", ...}` booleans (no state, no secrets) — needed for container health checks.

**Rationale**: Matches the spec's two authentication models (shared secret for the port; bot-token-authenticated gateway for Discord) with the least mechanism: one header check, one id comparison, both constant-time where they compare secrets. Serving the empty shell unauthenticated is the standard pattern for token-gated single-page tools and is the only way an owner can reach the token prompt.

**Alternatives considered**:
- *Require the token to serve `GET /` (e.g., `?token=` query)* — rejected: leaks the secret into URLs/logs/referrers, and complicates the prompt flow (no page to enter the token into).
- *HTTP Basic auth* — rejected: same secret, more plumbing, browser prompts behave inconsistently, and it doesn't map to the dashboard's fetch-based API.
- *Per-session tokens / CSRF tokens* — rejected: single owner, same-origin only, no cross-site surface worth the state.

---

## §9 Container & deployment

**Decision**:
- `Dockerfile`: `FROM python:3.15-slim` (pinned); `pip install --no-cache-dir -r requirements.txt`; copy package; create non-root user; `VOLUME /data`; `ENV STATE_FILE=/data/state.json PORT=8080`; `EXPOSE 8080`; `CMD ["python", "-m", "botalert"]`. No build stage (pure Python, no compiled extensions in the pinned set).
- **Port configurable at deployment** (FR-013): the app binds `0.0.0.0:$PORT` (env, default 8080); the operator maps it with `docker run -p <host>:<container>`. US4 scenario 3 (redeploy with a different port, no code changes) holds.
- State volume: `-v botalert-data:/data` persists `state.json` across redeploy (FR-009, SC-005).
- No `HEALTHCHECK` in the image beyond the documented `curl /api/health` (keeping the image tool-free: `python:slim` has no curl — the check is operator-side or via `CMD python -c ...` if desired; documented in quickstart).

**Rationale**: Constitution deployment constraints (pinned base + deps, container canonical) plus the spec's single-configurable-port requirement, with the lightest possible image (slim, 3 runtime deps, no build tools).

**Alternatives considered**:
- *Multi-stage build* — rejected: nothing to build; a single stage is the simple solution.
- *`python:3.15-alpine`* — rejected: glibc-linked wheels (discord.py's deps, e.g. aiohttp, ship glibc wheels) make slim the reliable choice; the size difference is immaterial for a single-container personal deployment.
- *Baking the state file into the image* — rejected: state is operator data, belongs on a volume (and keeps the image reproducible per the constitution).

---

## Open items

None. Every NEEDS CLARIFICATION marker in the plan's Technical Context is resolved above (§2 Python version; §1/§3/§5 dependency and architecture items were the rev-2 design decisions, now fixed). Remaining choices are implementation-phase details (exact phrase-list tuning, README wording) owned by tasks.md.
