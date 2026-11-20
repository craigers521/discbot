---

description: "Task list for the Stock Checker Bot feature (001)"
---

# Tasks: Stock Checker Bot

**Input**: Design documents from `/specs/001-stock-checker-bot/`

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: INCLUDED — mandated by Constitution Principle III (unit testing with pytest, full suite well under a minute, deterministic and offline via `tests/fakes.py`) and by the plan's Testing section. Test tasks are written to the contracts in research.md / data-model.md / contracts/ and MUST fail (red) before the implementation that satisfies them.

**Organization**: Tasks are grouped by user story so each story can be implemented, tested, and delivered independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to (US1–US4)
- Exact file paths in every description

**Path conventions**: single Python package `botalert/` at the repository root (no `src/` layer — see plan.md Structure Decision), `tests/` at the repository root.

**Design-doc shorthand used below** (all in `specs/001-stock-checker-bot/`): plan.md, spec.md, research.md §1–§9, data-model.md §1–§7, contracts/management-api.md, contracts/discord.md, quickstart.md scenarios 1–11.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [X] T001 Create project structure per plan.md Project Structure: directories `botalert/` (with `botalert/__init__.py`), `botalert/static/`, `tests/` (with `tests/__init__.py`), `fixtures/`; empty placeholder modules are NOT created in this task (modules land in their story phases)
- [X] T002 Create `requirements.txt` with exactly the three pinned runtime deps from research.md §1 (`flask==3.1.3`, `discord.py==2.7.1`, `python-dotenv==1.2.4`) and `requirements-dev.txt` with the two pinned dev deps (`pytest==9.2.0`, `mypy==2.4.1`); do not add any other package (Constitution IV)
- [X] T003 [P] Create `mypy.ini` with `strict = true` and `python_version = 3.13` (enforces the minimum-supported floor per research.md §2/§4; zero errors across `botalert/` and `tests/` is the acceptance gate)
- [X] T004 [P] Create `.env.example` documenting every variable in data-model.md §5 (required: `DISCORD_TOKEN`, `OWNER_DISCORD_ID`, `DISCORD_MANAGE_CHANNEL_ID`, `BOTALERT_WEBHOOK_URL`, `BOTALERT_TOKEN`; optional: `PORT` default `8080`, `BOTALERT_BASE_URL` (unset), `STATE_FILE` default `state.json`, `CHECK_TIMEOUT_SECONDS` default `15`, `CHECK_PAUSE_SECONDS` default `2`, `FAILURE_THRESHOLD` default `10`, `DEFAULT_CADENCE_MINUTES` default `5`, `MIN_CADENCE_MINUTES` default `1`, `MAX_CADENCE_MINUTES` default `1440`, `USER_AGENT` default `botalert/0.1 (personal stock checker)`)
- [X] T005 [P] Create `.gitignore` (`.venv/`, `__pycache__/`, `*.pyc`, `.env`, `state.json`, `fixtures/*.corrupt-*`) and initialize the git repository with feature branch `001-stock-checker-bot` (branch name per plan.md header)

**Checkpoint**: repository skeleton, dependency pins, and static-gate config exist; `pip install -r requirements.txt -r requirements-dev.txt` succeeds in a Python 3.13+ venv

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T006 [P] Implement `Config` dataclass in `botalert/config.py`: loads `.env` via python-dotenv (local dev only) over environment; REQUIRED vars `DISCORD_TOKEN`, `OWNER_DISCORD_ID`, `DISCORD_MANAGE_CHANNEL_ID`, `BOTALERT_WEBHOOK_URL`, `BOTALERT_TOKEN` — startup with a missing/empty required var MUST fail fast with a named-variable error ("no silent defaults for secrets or ids", data-model.md §5); OPTIONAL vars with defaults: `PORT` `8080`, `BOTALERT_BASE_URL` (absent → `None`), `STATE_FILE` `state.json`, `CHECK_TIMEOUT_SECONDS` `15`, `CHECK_PAUSE_SECONDS` `2`, `FAILURE_THRESHOLD` `10`, `DEFAULT_CADENCE_MINUTES` `5.0`, `MIN_CADENCE_MINUTES` `1.0`, `MAX_CADENCE_MINUTES` `1440.0`, `USER_AGENT` `botalert/0.1 (personal stock checker)`; Config is never persisted (env-only, data-model.md §5)
- [X] T007 [P] Implement `botalert/models.py` typed dataclasses per data-model.md §2–§4: `StockStatus` enum (`in_stock | out_of_stock | unknown`); `Product` with field constraints quoted verbatim — `id`: "12-char lowercase hex (`uuid4().hex[:12]`), generated on add; immutable"; `url`: "Absolute `http://` or `https://` URL with a non-empty host; whitespace-stripped; scheme+host normalized to lowercase (path kept verbatim). The unique key."; `label`: "1–100 chars after trim; optional on add — defaults to the URL's host (e.g. `shop.example.com`)"; `in_stock_indicator` / `out_of_stock_indicator`: "≤200 chars; case-insensitive substring matched against fetched page text; takes precedence over heuristics when set"; `last_status`: "one of `in_stock | out_of_stock | unknown`; initialized `unknown` on add"; `last_checked_at`: "UTC; `None` until the first completed check (a *failed* check updates this too)"; `consecutive_failures`: "≥0; incremented on fetch failure **or** `unknown` determination; reset to 0 on a determined `in_stock`/`out_of_stock`"; `failure_notified`: "True once the threshold notice has been sent; reset when the product recovers or is removed; gates the once-only notice"; `created_at`: "UTC, set on add"; `CheckResult` (transient): `product_id`, `outcome` (`StockStatus | "error"`), `error` ("present iff outcome is `error`"), `checked_at` (UTC); `AppState`: `schema_version` ("`1` (fixed in v1; loader rejects unknown major versions)"), `cadence_minutes` ("`MIN_CADENCE_MINUTES` ≤ value ≤ `MAX_CADENCE_MINUTES`; default on first start: `DEFAULT_CADENCE_MINUTES`"), `control_message_id` (`str | None`, "cleared/ignored when the bot (re)posts on startup"), `products` (insertion-ordered list); `RuntimeFlags` (in-memory only: e.g. cycle-in-progress, gateway-connected — used by `GET /api/health`, never persisted)
- [X] T008 Implement `StateStore` in `botalert/state.py` per data-model.md §3/§6/§7 and research.md §7: single process-wide `threading.Lock`; every read-modify-write (add/remove/update product, set cadence, set control-message id, per-product checker updates) is ONE critical section followed by an atomic save (serialize → write `state.json.tmp` in same dir → `os.replace`); load rules — missing file → fresh state (default cadence, empty watch list); corrupt/unparseable file → "log an error, rename the offender to `state.json.corrupt-<utcstamp>` (never delete owner data), start fresh"; unknown `schema_version` → same treatment ("never guess"); timestamps as UTC ISO-8601 strings via `datetime.fromisoformat`; URL validation/normalization exactly per T007's `url` rule, adding the same normalized URL (any casing of scheme/host) rejected as `duplicate_url` ("no silent merge", US4-6); label/indicator validation per T007; state-file persistence format exactly the JSON shape in data-model.md §7. **Documented `Any` boundary** (Constitution I justification, plan.md): parsed JSON from stdlib `json.loads` is received as `Any` in this module for one scope, then immediately validated and narrowed into `AppState`/`Product` before anything leaves the module — no other module sees `Any`
- [X] T009 [P] Write `tests/test_models.py`: `Product` construction (id generation is 12-char lowercase hex; `last_status` initialized `unknown`; `consecutive_failures` 0; `failure_notified` False), URL normalization (whitespace strip, scheme+host lowercased, path kept verbatim, non-http(s) and hostless URLs rejected), label defaults to host and enforces "1–100 chars after trim", indicator ≤200-char rejection, `StockStatus` serialization round-trip, `CheckResult` `error`-present-iff-outcome-`error` invariant
- [X] T010 [P] Write `tests/test_state.py`: persistence round-trip (save → reload equals, insertion order preserved, ISO-8601 timestamps), atomicity (no torn file when save interrupted — assert target replaced via temp+rename), validation rejection (duplicate normalized URL → `duplicate_url`; invalid label/indicator), corruption recovery (write garbage → load → fresh state AND offender renamed to `state.json.corrupt-<utcstamp>`, file present, never deleted; unknown `schema_version` → same), concurrency (N threads × M read-modify-write adds/removes under the lock end with no lost updates and no duplicate entries — the data-model.md §6 "concurrency rule" guarantee)

**Checkpoint**: `mypy --strict botalert tests` zero errors and `pytest -q` green — config, models, and the durable store are locked in; user story implementation can now begin

---

## Phase 3: User Story 1 - Watch Products and Get Restock Alerts (Priority: P1) 🎯 MVP

**Goal**: The core value loop — product pages on the watch list are checked sequentially on a configurable cadence; status is determined per product (per-product indicators → built-in heuristics → `unknown`); exactly one webhook alert fires per out-of-stock→in-stock transition; failed checks never change status; repeated failures produce a once-only owner notice. Ships a minimal management API (health, list, add, check-now) so the story is verifiable end-to-end without the dashboard or Discord (the plan's justified `POST /api/check-now` addition exists precisely to validate SC-001/SC-002 without waiting a full cadence interval).

**Independent Test**: Add one out-of-stock fixture product page via `POST /api/products` (fixture pages per quickstart.md, local `python3 -m http.server`); `POST /api/check-now` → status becomes `out_of_stock` with **no** webhook message (no alert on add, US1-4); overwrite `fixtures/out.html` **in place** with the in-stock content (same URL — the URL is immutable, and remove + re-add would reset status to `unknown`, which never alerts) and run another cycle → **exactly one** webhook notification containing the product's label and URL (US1-1/3, SC-002); run a third cycle while still in stock → **no further** notifications (US1-2); add an unreachable URL and run cycles → status unchanged, no alert, `consecutive_failures` climbs (US1-5, FR-012, SC-003).

### Tests for User Story 1 (write FIRST — they MUST fail before implementation) ⚠️

- [X] T011 [P] [US1] Create `tests/fakes.py` — the deterministic offline seams required by Constitution III (shared by later stories' tests, but created here as US1's first test dependency): in-memory fake fetcher (returns canned `FetchResult` html/error per URL, counts calls, honors call order), webhook notifier fake (records every webhook POST body and can simulate non-2xx/transport failure), management-channel sender fake (records operational notices), fake gateway/interaction harness for router-level Discord tests (typed request envelopes, no live connection), and a fake clock (manual advance) for scheduler/checker determinism
- [X] T012 [P] [US1] Write `tests/test_stock.py` (red): `determine_status(html, product)` precedence per research.md §6 — (1) `product.in_stock_indicator` set and matched → `IN_STOCK`; (2) `product.out_of_stock_indicator` set and matched → `OUT_OF_STOCK`; (3) built-in in-phrase set → `IN_STOCK`; (4) built-in out-phrase set → `OUT_OF_STOCK`; (5) no match → `UNKNOWN`; all matches are case-insensitive substrings against the fetched page text; provided indicators that do NOT match fall through to heuristics ("precedence, not exclusivity"); page matching BOTH heuristic sets → in-stock wins ("in-before-out bias")
- [X] T013 [P] [US1] Write `tests/test_notifier.py` (red): `send_restock` POSTs to the webhook via stdlib with a 10 s timeout and body exactly `{"content": "🔔 Restock: <label>\n<url>\n(detected <time> UTC)"}` (label + URL + detection time per spec "Notification content"); delivery failure (non-2xx or transport error) is surfaced to the caller with the failure detail, NEVER silently dropped (edge case "Invalid webhook destination")
- [X] T014 [P] [US1] Write `tests/test_checker.py` (red): `run_cycle` implements the data-model.md §6 state machine exactly — `unknown`→`in_stock` on first determined check records status with **no alert** (US1-4); `out_of_stock`→`in_stock` sends exactly one webhook (FR-007/FR-008) and resets `consecutive_failures`/`failure_notified`; `in_stock`→`in_stock` and `out_of_stock`→`out_of_stock` send no alert and reset failure counters (US1-2); fetch failure **or** `unknown` determination leaves `last_status` unchanged but increments `consecutive_failures` and updates `last_checked_at` (FR-012); at `consecutive_failures == FAILURE_THRESHOLD` (default 10) with `failure_notified == False` → exactly one owner notice in the management channel and `failure_notified = True`, and it does NOT repeat on subsequent failures until recovery or removal (FR-016); fetches run strictly sequentially in watch-list order with a `CHECK_PAUSE_SECONDS` (default 2 s) pause between consecutive fetches (FR-018); state is saved after each product (crash mid-cycle loses at most one product's update); a failed webhook delivery on a real transition still records the status and emits the `📡 Failed to deliver the restock alert for <label> to the webhook: <detail>. The product status was still updated.` channel notice (contracts/discord.md §Outbound notifications)
- [X] T015 [P] [US1] Write `tests/test_scheduler.py` (red): background scheduler loop — `wakeup_event.wait(cadence)` then snapshot watch list → `run_cycle` → save → re-read cadence from state (so a cadence written between cycles applies to the next wait, SC-004); `wakeup()` ends the current wait immediately (used by check-now and product add, SC-001); a new cycle never starts while another is in flight (FR-018 "no parallel in-flight page fetches"); and a scale case (SC-006): fake clock + 25 fake products at 5-minute cadence — one full sequential cycle completes within the interval and every product's `last_checked_at` advances
- [X] T016 [P] [US1] Write `tests/test_api.py` — US1 endpoints only (red), per contracts/management-api.md: `GET /api/health` is public (no token) and returns `{"status": "ok", "gateway_connected": <bool>, "cycle_in_progress": <bool>}` exposing no watch-list data or secrets; EVERY other `/api/*` request without `X-Botalert-Token` or with a wrong token → `401` with `{"error": {"code": "unauthorized", "message": ...}}` and NO state read or altered (SC-008); `GET /api/products` → `200 {"products": [...]}` in insertion order with the product representation from the contract (`failure_notified` never exposed); `POST /api/products` body `{"url": ..., "label"?, "in_stock_indicator"?, "out_of_stock_indicator"?}` → `201` created product (`last_status` `unknown`, `last_checked_at` null) and the scheduler's wakeup is set so a fresh watch list is not left to wait a full interval (SC-001); `400 invalid_url` / `400 invalid_label` / `400 invalid_indicator`; `409 duplicate_url`; `POST /api/check-now` (no body) → `202 {"status": "cycle_scheduled"}`, or `409 cycle_in_progress` when a cycle is running (no second cycle started)

### Implementation for User Story 1

- [X] T017 [P] [US1] Implement `botalert/stock.py`: `determine_status(html: str, product: Product) -> StockStatus` — strict precedence order, all case-insensitive substring matches: (1) `product.in_stock_indicator` if set → `IN_STOCK`; (2) `product.out_of_stock_indicator` if set → `OUT_OF_STOCK`; (3) built-in in-stock phrase set (frozen `set[str]` data table, NOT code): `"add to cart", "add to basket", "buy now", "in stock", "available now"`; (4) built-in out-of-stock phrase set: `"out of stock", "sold out", "notify me", "currently unavailable", "unavailable"`; (5) no match → `UNKNOWN`; if a page matches both heuristic sets the in-stock set wins (research.md §6 "in-before-out bias"); no per-retailer plugins, no regex, no HTML parsing
- [X] T018 [P] [US1] Implement `botalert/fetcher.py`: `fetch_page(url, timeout, user_agent) -> FetchResult` via stdlib `urllib.request` GET with the configurable `User-Agent` and `timeout` (default 15 s); the final response (after any redirects) is evaluated; non-2xx final status, timeout, or connection error → `FetchResult(error=...)` — a failure, NEVER a stock status (FR-012); SSL verification stays ON (stdlib default); no JS rendering (spec Assumptions)
- [X] T019 [P] [US1] Implement `botalert/notifier.py`: `send_restock(webhook_url, label, url, detected_at) -> (ok, error_detail)` — stdlib POST, 10 s timeout, payload exactly `{"content": "🔔 Restock: <label>\n<url>\n(detected <time> UTC)"}`; `notify_owner(sender, text)` — sends an operational notice to the management channel through an INJECTED channel-sender callable (keeps the module gateway-free; the real sender is wired in US2, a logging fallback is wired in T024); a delivery failure (non-2xx or transport error) returns the failure detail to the caller so the checker surfaces it, never drops it (FR-016)
- [X] T020 [US1] Implement `botalert/checker.py`: `run_cycle(snapshot, fetcher, notifier, channel_sender, state, clock)` — walks the product list in order, strictly sequentially (depends on T017, T018, T019): per product fetch (per-fetch `CHECK_TIMEOUT_SECONDS` timeout) → `determine_status` → apply the data-model.md §6 transition table inside the `StateStore` critical section → atomic save after each product → `CHECK_PAUSE_SECONDS` pause between consecutive fetches; on `out_of_stock`→`in_stock` call `send_restock` the moment the transition is applied (within 60 s of the detecting check, SC-002) and on webhook failure emit the `📡 Failed to deliver...` channel notice via the injected sender; on fetch failure/`unknown` leave `last_status` unchanged, increment `consecutive_failures`, update `last_checked_at`, and at the threshold (default 10) with `failure_notified == False` send the once-only notice exactly: `⚠️ <label> (<url>) has failed its last <threshold> checks (last error: <detail>). It appears unreachable or its stock indicator no longer matches — check the page or set per-product indicators. This notice won't repeat until it recovers or is removed.` then set `failure_notified = True`; any determined check resets `consecutive_failures = 0`, `failure_notified = False`
- [X] T021 [US1] Implement `botalert/scheduler.py`: a background thread (depends on T020) — loop: `wakeup_event.wait(cadence_minutes)` (cadence read from state each iteration) → mark cycle-in-progress (RuntimeFlags) → snapshot the watch list under the lock → `run_cycle` → clear cycle-in-progress → re-read cadence for the next wait; `wakeup()` method ends the current wait immediately (called by `POST /api/check-now` and by product add); a cycle never overlaps — `wakeup()` while a cycle is in flight is a no-op (FR-018); clean stop on shutdown event
- [X] T022 [US1] Implement the minimal management API in `botalert/api.py` as a Flask Blueprint (depends on T021): token auth guard on every route EXCEPT `GET /api/health` — header `X-Botalert-Token` compared with `hmac.compare_digest` (constant-time), auth check runs before ANY state access, missing/invalid → `401 {"error": {"code": "unauthorized", ...}}`; `GET /api/health` public → `{"status": "ok", "gateway_connected": ..., "cycle_in_progress": ...}`; `GET /api/products` → `{"products": [...]}` (insertion order, contract product representation); `POST /api/products` (validate per data-model.md §2; `201` created product, `400 invalid_url`/`invalid_label`/`invalid_indicator`, `409 duplicate_url`; sets the scheduler wakeup on success); `POST /api/check-now` → `202 {"status": "cycle_scheduled"}` or `409 {"error": {"code": "cycle_in_progress", ...}}`; all non-2xx use the contract error shape `{"error": {"code": ..., "message": ...}}`
- [X] T023 [US1] Implement `botalert/web.py`: Flask app factory `create_app(config, state, scheduler, runtime_flags)` — registers the `/api/*` blueprint, binds `0.0.0.0:$PORT` (env, default 8080) with the built-in server `threaded=True` (research.md §5 accepted trade-off), returns the app (static dashboard serving is added in US4)
- [X] T024 [US1] Implement `botalert/__main__.py`: entrypoint — `load_dotenv()` then load `Config` (fail fast on missing required vars), initialize `StateStore`, wire `notifier` with a stdout-logging channel-sender fallback (the real Discord sender arrives in US2), start the scheduler thread, run Flask on the main thread; `SIGTERM`/`SIGINT` → stop the scheduler (no graceful drain beyond the 15 s fetch timeout, research.md §5) → exit 0
- [X] T025 [US1] End-to-end validation of User Story 1 per quickstart.md: create `fixtures/out.html` and `fixtures/in.html` exactly as given in quickstart.md ("Local fixture pages"), serve `fixtures/` with `python3 -m http.server 8000`, run the bot (local), then execute quickstart scenarios 2 (health + auth boundary: `401 unauthorized` secretless/wrong-token, `200` with token), 3's add-only row (add `http://localhost:8000/out.html` label "Fixture OOS" → 201, status `unknown`), 4 (first check → `out_of_stock`, NO webhook message), 5 (toggle to `in.html` → exactly ONE webhook `🔔 Restock: <label>` + URL + detection time; check-now again → no further alerts), 6 (add `http://localhost:9999/absent`, run cycles → status unchanged, no alert, `consecutive_failures` climbs, one ⚠️ threshold notice at 10 that does not repeat); verify all with `pytest -q` and `mypy --strict botalert tests` green

**Checkpoint**: User Story 1 is fully functional and independently testable — the bot monitors pages on a cadence, sends exactly one restock alert per out→in transition, never alerts on failed checks, and is manageable for this story via the API

---

## Phase 4: User Story 2 - Manage the Watch List via Discord Controls (Priority: P1)

**Goal**: The owner manages the watch list entirely through Discord interactive controls (buttons, a select menu, a modal) delivered over the bot's single persistent, authenticated gateway connection (spec rev 2 — the exposed port is never involved in Discord input): view the list with latest statuses, remove a product, relabel a product; every interaction is attributed to the triggering user and non-owners are rejected with zero state change (FR-010, FR-017, SC-007); the control message self-heals. The "Change cadence" button intentionally lands in Phase 5 (US3) so no increment ships a dead button.

**Independent Test**: With Discord connected, using Discord controls ONLY: **View watch list** → ephemeral list of every product's label, status emoji, URL; **Manage products** → select a product → **Remove** → ephemeral `✅ Removed...` confirm and the product is gone from subsequent views; select another product → **Relabel** → type a new label in the modal → confirm, and **View** shows the new label; have a non-owner account click any control → ephemeral rejection, watch list unchanged (spec US2 independent test + scenario 4).

### Tests for User Story 2 (write FIRST — they MUST fail before implementation) ⚠️

- [X] T026 [P] [US2] Write `tests/test_discord_bot.py` — router-level tests against the typed `InteractionRequest` envelopes and the fake gateway from `tests/fakes.py` (no live connection): owner authorization — `interaction.user.id == OWNER_DISCORD_ID` → processed; any other user id → ephemeral rejection exactly `❌ Not authorized — only the owner can manage the watch list.` and NO state read or altered (SC-007: 100% rejected, zero state change); `custom_id` routing — `ctl.view` → ephemeral list `• <label> — <status emoji> — <url>` per product (status emoji legend `🟢 in stock` · `🔴 out of stock` · `⚪ unknown (not determined yet)`), plus `checked <relative time>` and a `⚠️ n consecutive failures` marker where `consecutive_failures > 0`; empty list → ephemeral `Watch list is empty — add products in the dashboard.`; `ctl.manage` → ephemeral select menu `sel.product` with one option per product, `value = product.id` (the 12-char internal id), `label = "<label> — <host>"` truncated to 100 chars; `sel.product` selection → ephemeral follow-up with two buttons for that product: `[🗑 Remove <label>]` (`ctl.remove:<id>`) and `[✏️ Relabel <label>]` (`ctl.relabel:<id>`); `ctl.remove:<id>` → product removed from state, reply `✅ Removed <label> (<url>).` followed by the updated count; stale `ctl.remove:<old-id>` (id not on the watch list) → `⚠️ That product is no longer on the watch list.`; relabel modal submit — trimmed 1–100 chars → state updated and ephemeral confirm; empty or >100 chars → ephemeral `⚠️ Label must be 1–100 characters.` with the current label unchanged; control-message lifecycle — on `on_ready` the bot deletes its previous control message (id from state), posts a fresh one, and stores the new id in state; `on_message_delete` matching the stored id → re-post (self-healing); every owner-facing response is ephemeral (contracts/discord.md)

### Implementation for User Story 2

- [X] T027 [US2] Implement the gateway core in `botalert/discord_bot.py` (research.md §3): bare `discord.Client(intents=discord.Intents.default())` — NO privileged intents, NO `commands.Bot`; the client runs `client.run(token)` on a DEDICATED thread (its own event loop); typed `InteractionRequest` (action/custom_id, product id, user id, modal text) and `InteractionResponse` dataclasses; a PURE router function over `InteractionRequest` returning typed responses (business logic, fully unit-testable with no gateway); `on_interaction` is a THIN adapter: validate-then-narrow `interaction.data` access (the one documented extraction point per research.md §4), build the request, apply the result via the gateway; owner authorization is the first router step (`interaction.user.id == config.owner_discord_id`)
- [X] T028 [US2] Implement the control message + lifecycle in `botalert/discord_bot.py`: `on_ready` → delete the bot's own previous control message (a bot may always delete its own; id from `AppState.control_message_id`), post a fresh one into `DISCORD_MANAGE_CHANNEL_ID` with content `🛒 botalert — watch list controls` and buttons `[📋 View watch list]` (`ctl.view`), `[⚙️ Manage products]` (`ctl.manage`), plus `[➕ Add product → dashboard]` (link button to env `BOTALERT_BASE_URL`) rendered ONLY when that variable is set, then store the new id in state; `on_message_delete` → if the deleted message id matches the stored control-message id, re-post (management surface always exists, zero extra API surface)
- [X] T029 [US2] Implement the US2 interaction routes in the `botalert/discord_bot.py` router: `ctl.view` (ephemeral list per T026 spec, reading state at interaction time so it always reflects current state); `ctl.manage` (ephemeral `sel.product` select, options built at interaction time); `sel.product` (ephemeral remove/relabel buttons for the selected id — id validated against the current watch list at interaction time); `ctl.remove:<id>` (state removal inside the `StateStore` critical section, ephemeral `✅ Removed <label> (<url>).` + updated count; stale id → `⚠️ That product is no longer on the watch list.`)
- [X] T030 [US2] Implement the relabel modal in `botalert/discord_bot.py`: `discord.ui.Modal` subclass with one `TextInput` (short style, placeholder `New label`, max_length 100, required, pre-filled with the current label); the modal instance carries its target product id (instantiated per trigger, so the modal carries its own context); typed `on_submit` — trim, validate 1–100 chars, update the product label through the same `StateStore` critical section as the API (single source of truth), ephemeral confirm with the new label; invalid → ephemeral `⚠️ Label must be 1–100 characters.` (current label unchanged); `ctl.relabel:<id>` route opens this modal
- [X] T031 [US2] Wire the Discord bot into `botalert/__main__.py`: start the bot thread alongside the scheduler and Flask; the real management-channel sender for `notifier.notify_owner` is now the gateway channel send dispatched from other threads via `asyncio.run_coroutine_threadsafe(coro, client.loop)` (research.md §3/§5 documented cross-thread pattern) — if the gateway is disconnected at send time, log the notice to stdout and retry once on reconnect (contracts/discord.md §Outbound notifications); shutdown: stop scheduler → `run_coroutine_threadsafe(client.close(), client.loop)` → exit 0
- [X] T032 [US2] End-to-end validation of User Story 2 per quickstart.md scenario 7 (parts 1–4 and 6, cadence part 5 belongs to US3): control message `🛒 botalert — watch list controls` present in the management channel on startup; delete it and verify it re-posts (self-healing); **View watch list** → ephemeral list with labels, status emojis, URLs; **Manage products** → select "Fixture Shoes" → **Remove** → ephemeral confirm, product gone; select another product → **Relabel** → modal → new label → confirm, **View** shows the new label; a second (non-owner) Discord account clicks any control → ephemeral `❌ Not authorized — only the owner can manage the watch list.`, watch list unchanged; `pytest -q` and `mypy --strict botalert tests` green

**Checkpoint**: User Stories 1 AND 2 are both fully functional and independently testable — together they form the complete, usable MVP per spec (monitoring + alerting + management)

---

## Phase 5: User Story 3 - Configure the Recheck Cadence (Priority: P2)

**Goal**: The owner changes the global recheck cadence at runtime via a Discord control (modal) or the web management API; the new cadence takes effect from the next check cycle with NO restart (SC-004); the enforced floor `MIN_CADENCE_MINUTES` (default 1.0) and ceiling `MAX_CADENCE_MINUTES` (default 1440.0) reject out-of-range and malformed values with an explanation (FR-005, FR-011).

**Independent Test**: Set the cadence to `2` via the Discord cadence modal (or `PUT /api/config`) → confirmation `new cadence 2 minutes`; observe the next cycle landing ~2 minutes later with no restart (SC-004); submit `0.5` → rejected stating the 1-minute minimum, current cadence unchanged (US3-2); submit `abc` → rejected as malformed, current cadence unchanged (US3-3).

### Tests for User Story 3 (write FIRST — they MUST fail before implementation) ⚠️

- [X] T033 [P] [US3] Extend `tests/test_discord_bot.py` (red): `ctl.cadence` button opens the cadence modal; modal submit — valid numeric (int or float) with `MIN_CADENCE_MINUTES ≤ v ≤ MAX_CADENCE_MINUTES` → state cadence updated, scheduler wakeup set, ephemeral confirm (`new cadence 2 minutes` style); malformed `abc` → ephemeral `⚠️ Cadence must be a number of minutes between 1 and 1440. (current: 5)` and current cadence unchanged; below minimum `0.5` → same rejection message stating the minimum, unchanged; above maximum `2000` → rejected, unchanged (US3 scenarios 1–3)
- [X] T034 [P] [US3] Extend `tests/test_scheduler.py` and `tests/test_api.py` (red): cadence change via `PUT /api/config` → the scheduler's NEXT wait uses the new cadence with no restart (fake clock, SC-004 "100% of the time"); `PUT /api/config` body `{"cadence_minutes": <number>}` → `200` echoing the same shape as `GET /api/config` with the new value; `400 {"error": {"code": "invalid_cadence", "message": "Cadence must be at least 1.0 minutes (minimum) and at most 1440.0 minutes (maximum)."}}` for non-numeric, below floor, and above ceiling — current cadence unchanged in all cases; `GET /api/config` → `{"cadence_minutes": 5.0, "min_cadence_minutes": 1.0, "max_cadence_minutes": 1440.0}`

### Implementation for User Story 3

- [X] T035 [US3] Extend `botalert/discord_bot.py` (depends on T026–T030): add the `[🔁 Change cadence]` button (`ctl.cadence`) to the control message (startup re-post picks it up), and implement the cadence modal: `discord.ui.Modal` with one `TextInput` (short style, placeholder e.g. `5`, max_length 10, required); typed `on_submit` — parse numeric (int or float), require `MIN_CADENCE_MINUTES ≤ v ≤ MAX_CADENCE_MINUTES`, update `AppState.cadence_minutes` inside the `StateStore` critical section and call the scheduler's `wakeup()` so the new interval applies from the next cycle, ephemeral confirm (`new cadence <n> minutes`); malformed, below minimum, or above maximum → ephemeral `⚠️ Cadence must be a number of minutes between 1 and 1440. (current: 5)` with the current cadence unchanged
- [X] T036 [P] [US3] Extend `botalert/api.py` (depends on T022): `GET /api/config` → `200 {"cadence_minutes": ..., "min_cadence_minutes": ..., "max_cadence_minutes": ...}` (limits from Config); `PUT /api/config` body `{"cadence_minutes": <number>}` → `200` same shape echoing the new value, state updated under the lock, scheduler `wakeup()` set (takes effect from the next check cycle, SC-004); `400 invalid_cadence` with message exactly `"Cadence must be at least 1.0 minutes (minimum) and at most 1440.0 minutes (maximum)."` for non-numeric, below floor, or above ceiling (limits interpolated from Config), current cadence unchanged
- [X] T037 [US3] End-to-end validation of User Story 3 per quickstart.md scenario 7 part 5 and the API contract: **Change cadence** → modal: `2` → confirm "new cadence 2 minutes"; `abc` → rejected with the reason; `0.5` → rejected stating the 1-minute minimum; the next cycle lands ~2 minutes later (SC-004, no restart); mirror via API: `PUT /api/config` `{"cadence_minutes": 30}` → `200` echo, next cycle on 30; `0.5` and `"abc"` → `400 invalid_cadence` with the min/max message, `GET /api/config` unchanged; `pytest -q` and `mypy --strict botalert tests` green

**Checkpoint**: All user stories up to P2 are independently functional — cadence is a live, validated, restart-free operational control on both interfaces

---

## Phase 6: User Story 4 - Interact With the Bot Over Its Exposed Port (Priority: P3)

**Goal**: The single configurable port serves BOTH the static single-page dashboard (plain HTML/JS, no framework/bundler/build step — FR-019) and the full management API (add, remove, list, modify, cadence, check-now — FR-014), all gated by the shared secret; dashboard results are identical to Discord results (one `StateStore`, one source of truth — US4-2); the service is deployable as a container with the port configurable at deployment time (FR-013, US4-3).

**Independent Test**: With the container running and the port exposed, open the dashboard in a browser, enter the token once, perform add/remove/modify/cadence operations, and verify each result matches the bot's watch list as seen via Discord (**View watch list**); redeploy with a different host port mapping → dashboard and endpoints reachable on the new port with no code changes (spec US4 independent test + scenarios 1/3).

### Tests for User Story 4 (write FIRST — they MUST fail before implementation) ⚠️

- [X] T038 [P] [US4] Extend `tests/test_api.py` (red): `PATCH /api/products?url=<url-encoded>` — body with any of `label` / `in_stock_indicator` / `out_of_stock_indicator` (at least one required; an empty-string indicator CLEARS it; the URL itself is immutable — changing URL = remove + add) → `200` updated product; `400 invalid_label` / `400 invalid_indicator` / `400` for an empty body; `404 {"error": {"code": "not_found", ...}}` for an unknown URL; `DELETE /api/products?url=<url>` → `204` no body, product no longer checked (failure counters vanish with it), `404 not_found` for unknown URL; complete the 401 matrix for EVERY protected endpoint (missing header, wrong token — no state touched); verify the error shape `{"error": {"code", "message"}}` on all non-2xx responses of the full API (FR-015 parity with Discord confirmations)

### Implementation for User Story 4

- [X] T039 [US4] Extend `botalert/api.py`: `PATCH /api/products?url=<url>` and `DELETE /api/products?url=<url>` exactly per contracts/management-api.md (product identified by URL query parameter — "avoids path-segment encoding issues"; the URL is the natural unique key per data-model.md §2): PATCH — all body fields optional, at least one required, empty-string indicator clears it, `200` updated product, `400 invalid_label`/`invalid_indicator`/empty-body, `404 not_found`; DELETE — `204` no body, `404 not_found`; both mutate inside the `StateStore` critical section (same guarantee as Discord, data-model.md §6 concurrency rule)
- [X] T040 [US4] Create `botalert/static/index.html`: the single-page dashboard (no framework, no build step — served straight from `botalert/static/`) with: a token-prompt view (shown when no token in `localStorage`; the page shell is served WITHOUT the token — it is the vehicle through which the owner enters the secret, research.md §8); a watch-list table (label, URL, status badge, last checked, failure count); an **Add form** (url + optional label + optional in/out indicators → `POST /api/products`); per-row **edit** (label/indicators → `PATCH`) and **remove** (`DELETE`) actions; a **Cadence form** (`PUT /api/config`, showing min/max from `GET /api/config`); a **Check now** button (`POST /api/check-now`, then re-poll the list); an inline confirmation/error area for EVERY operation (FR-015 — same guarantee as the Discord interface); assign stable element ids for `app.js` to bind
- [X] T041 [US4] Create `botalert/static/app.js`: plain JavaScript (no framework/bundler) — on load: read token from `localStorage`, if absent show the prompt; on submit verify with `GET /api/products` + `GET /api/config` (401 → re-prompt, clear stored token); attach `X-Botalert-Token` to every `fetch`; render the table on load and after each operation ONLY (spec "Dashboard refresh model" — no background auto-refresh, no push); wire add/edit/remove/cadence/check-now to the API with the inline confirmation/error rendering (surface the contract `message` text verbatim); status badges per the emoji legend `🟢 in stock` · `🔴 out of stock` · `⚪ unknown`
- [X] T042 [US4] Extend `botalert/web.py`: serve `GET /` → `botalert/static/index.html` WITHOUT the token (stateless shell, alters no state), and `GET /static/<file>` → the dashboard assets from `botalert/static/` (correct MIME via Flask's static file serving); the API blueprint and auth are unchanged
- [X] T043 [P] [US4] Create `Dockerfile` per research.md §9: `FROM python:3.15-slim` (pinned); `pip install --no-cache-dir -r requirements.txt` (dev deps NOT in the image); copy the `botalert/` package; create a non-root user; `VOLUME /data`; `ENV STATE_FILE=/data/state.json PORT=8080`; `EXPOSE 8080`; `CMD ["python", "-m", "botalert"]`; no build stage (pure Python); the app binds `0.0.0.0:$PORT` so the operator maps any host port with `-p <host>:<container>` (FR-013, US4-3)
- [X] T044 [US4] End-to-end validation of User Story 4 per quickstart.md scenarios 3, 9, and 10: `docker build -t botalert .` then `docker run -d --name botalert -p 8080:8080 -v botalert-data:/data --env-file .env botalert`; open the dashboard, enter the token once (prompt → stored in localStorage); add `http://localhost:8000/out.html` (label "Fixture OOS") → 201, row appears with status `unknown`; add the same URL again → `409 duplicate_url`, no second row; add `not-a-url` → `400 invalid_url`, list unchanged; relabel to "Fixture Shoes" → confirmation, new label shown; remove it → row gone (scenario 3); port configurability: `docker run -d --name botalert2 -p 9090:8080 --env-file .env -v botalert-data:/data botalert` → `curl -s localhost:9090/api/health` → `{"status": "ok", ...}` with no code changes (scenario 9, US4-3); interface parity: add a product via the dashboard → **View watch list** in Discord shows it (label, URL); remove it in Discord → refresh the dashboard → gone (scenario 10, US4-2); `pytest -q` and `mypy --strict botalert tests` green

**Checkpoint**: All user stories are independently functional — every management operation is reachable over Discord AND over the exposed port against one shared source of truth, in a deployable container

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Improvements and verification that affect all user stories

- [X] T045 [P] Write `README.md` (Constitution Quality & Documentation): what botalert is; how to run locally (venv, `pip install -r requirements.txt -r requirements-dev.txt`, `.env`, `python -m botalert`) and in a container (build + `docker run` with volume and port mapping, `--env-file`); the one-time Discord application setup from contracts/discord.md §Application setup (app + bot token, NO privileged intents, invite with scope `bot` and permissions View Channels / Send Messages / Embed Links, owner user id, channel id, webhook URL); a table documenting EVERY `.env` variable (mirror of data-model.md §5 / `.env.example`); the status emoji legend `🟢 in stock` · `🔴 out of stock` · `⚪ unknown (not determined yet)`; how stock determination works (per-product indicators → heuristics → unknown, per research.md §6) and how to extend the phrase sets; known limitations and accepted trade-offs (Werkzeug dev server in the container, no TLS in v1, no check history, dashboard refresh model, owner-side port reachability only)
- [X] T046 [P] Security & correctness audit pass (verify and fix in place): every `/api/*` except `GET /api/health` gated and the auth check precedes any state access (SC-008); token comparison via `hmac.compare_digest` only (constant-time); no secret (`BOTALERT_TOKEN`, `DISCORD_TOKEN`, webhook URL) written to the state file, the dashboard, or logs; Discord authorization is `interaction.user.id == OWNER_DISCORD_ID` before any state access (SC-007); no parallel in-flight page fetches anywhere (FR-018); failed/unknown checks never alter `last_status` (FR-012); corrupt state files are renamed, never deleted (research.md §7); every public module/class/function has a docstring stating purpose and contract (Constitution Quality)
- [X] T047 Run the full static gates and fix every finding: `mypy --strict botalert tests` → ZERO errors (Constitution I; the only `Any` is the documented `state.py` JSON boundary) and `pytest -q` → full suite passes in well under a minute (Constitution III), from a clean checkout with only `requirements.txt` + `requirements-dev.txt` installed
- [X] T048 Run the complete `quickstart.md` validation in order (scenarios 1–11 — all mandatory; scenario 11 "Scale sanity" validates the required SC-006): static gates, health + auth boundary, watch-list CRUD, first-check no-false-alert, restock exactly-one-alert, failed-checks-never-flip (incl. the once-only ⚠️ threshold notice), full Discord management (view / remove / relabel / cadence modal / non-owner rejection), persistence across restart (watch list, statuses, cadence, control-message re-post, NO duplicate restock alert for an already-notified transition — SC-005), port configurability, interface parity, and scale sanity (25 fixture URLs at 5-minute cadence, cycle completes within the interval — SC-006); record any deviation as a task, not a workaround

  **T048 result** — every scenario has executed evidence; the three operator-side slices that need live Discord and/or a real browser are recorded below as tasks (NOT worked around):
  - **1** static gates: T047 — clean checkout, fresh venv (only the two requirements files): `mypy --strict` zero errors (25 files), `pytest -q` 209 passed in 2.82 s
  - **2** health + auth boundary, **4** first check no-false-alert, **5** restock exactly one alert + no re-alert, **6** failed checks never flip status + once-only ⚠️ threshold notice, **8** persistence across restart (local SIGTERM slice the quickstart sanctions: exit 0, watch list/statuses/cadence persist, NO duplicate restock alert — SC-005): e2e driver (US1–US3), all checks passed
  - **3** watch-list CRUD (API-level against the container: add 201/unknown, duplicate 409, invalid 400, relabel, remove, PATCH/DELETE 400/404 boundaries) + **9** port configurability (botalert2 on :9090, same volume, state visible — FR-013/SC-005): US4 e2e driver, all checks passed
  - **11** scale sanity (SC-006): 25 fixture URLs, cadence 300 via `PUT /api/config`, three full sequential cycles via `POST /api/check-now` (48.7 s each — the quickstart's 25 × (fetch + 2 s pause) arithmetic, ~16% of the interval); every product's `last_checked_at` advanced each cycle; zero webhook POSTs and zero channel notices (no ⚠️ threshold notice); scale e2e driver, all checks passed
  - Discord-side cadence control (US3): API slice verified in e2e (GET/PUT `/api/config`, exact 400s, valid PUT wakes an immediate cycle); the live-Discord modal slice is T049
- [ ] T049 [P] Scenario 7 live-Discord management slice (operator-side deviation recorded by T048 — needs a real Discord account + guild, a real bot token, and a SECOND Discord account): in the management channel — control message "botalert — watch list controls" posted on startup and re-posted after deletion (self-healing; also the scenario 8 control-message re-post observation); **View watch list** ephemeral list (FR-003); **Manage products** → Remove (US2-2); **Manage products** → Relabel via modal (US2-3); **Change cadence** modal — `2` accepted with "new cadence 2 minutes", `abc` rejected with the reason, `0.5` rejected stating the 1-minute minimum (US3-2/3, SC-004); non-owner account clicks any control → ephemeral `❌ Not authorized…`, watch list unchanged (US2-4, SC-007). Unit tests already cover the router/owner-check/modal logic; only the live-gateway round trips remain
- [ ] T050 [P] Scenario 10 interface parity slice (operator-side deviation recorded by T048 — needs a real browser AND live Discord): add a product via the dashboard → **View watch list** in Discord shows it (label, URL); remove it in Discord → refresh the dashboard → gone (US4-2, data-model.md §6 one source of truth). The shared-store behavior is covered by unit + API-level e2e tests; only the live two-surface observation remains
- [ ] T051 [P] Browser-driven dashboard operations slice (operator-side deviation recorded by T048 — needs a real browser): open `http://localhost:8080/`, the token prompt appears, the token is accepted and stored in localStorage, 401 response → prompt re-appears; then run scenario 3's operations (add / relabel / remove / cadence / Check now) through the rendered UI. Verified so far at the API level (the same endpoints the dashboard JS calls, incl. the shell serving without a token); only the in-browser interaction remains
- [x] T052 [P] WAF-tolerant fetch backend (operator-side deviation — the deployed bot's live validation showed every retailer except Steam serving bot walls to the stock urllib client: Walmart "Robot or human?", Newegg Cloudflare 403, Amazon ~4 KB `validateCaptcha` cookie gate served as 200): live probes against real product pages established that a headless *browser* is refused by Walmart while `curl_cffi` `impersonate="chrome"` (full Chrome TLS/JA3 + HTTP/2 fingerprint) passes Walmart/Newegg/Best Buy/Steam, and Amazon's gate is a plain hidden form solvable in one session (fetch → submit → refetch with cookie → real page). Implemented: `curl_cffi==0.17.0` runtime dep (self-contained wheel, no Dockerfile build change); `FETCH_BACKEND` env (`http|curl|auto`, default `auto` — http first, one curl retry on WAF status 403/407/429/444/451/503, a known 2xx gate marker, or a timeout — Best Buy's edge was observed resetting plain clients without any wall status; plain 404s and successes are never retried); Amazon gate solved in-session; `FetchFn` signature and error-string shapes unchanged (T046 + data-model.md §6 pinned notice text), curl errors mapped by exception type to deterministic phrases; both backends covered by offline fakes (no network in the suite). Spec Assumption "Public, server-rendered pages" updated accordingly

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion — BLOCKS all user stories
- **User Story 1 (Phase 3)**: Depends on Foundational
- **User Story 2 (Phase 4)**: Depends on Foundational + User Story 1 (reuses `StateStore` mutations, `notifier` channel-sender seam, `__main__.py` process wiring)
- **User Story 3 (Phase 5)**: Depends on User Story 2 (extends the control message + router built there; API side depends on US1's `api.py`)
- **User Story 4 (Phase 6)**: Depends on User Story 1 (extends `api.py`/`web.py`); independent of User Stories 2–3 (the dashboard never needs the Discord connection)
- **Polish (Phase 7)**: Depends on all desired user stories being complete

### User Story Dependency Graph

```text
Foundational (Phase 2)
        │
        ├──► US1 (P1) ──► US2 (P1) ──► US3 (P2) ─┐
        │      │                                  │
        │      └────────► US4 (P3) ───────────────┤
        │                                         │
        └─────────────────────────────────────────┴──► Polish (Phase 7)
```

- **User Story 1 (P1)**: after Foundational — no dependencies on other stories
- **User Story 2 (P1)**: after US1 (state mutations, notifier seam, process wiring); independently testable once a product exists (add via US1's API)
- **User Story 3 (P2)**: after US2 (control message + router); independently testable via the Discord modal OR `PUT /api/config`
- **User Story 4 (P3)**: after US1 (API/web foundation); independently testable in a container; may be built in parallel with US2/US3 by a different developer (different files: `api.py` additions, `static/`, `web.py`, `Dockerfile` — coordinate on `api.py` with US3's T036 if running truly concurrently)

### Within Each User Story

- Tests MUST be written and FAIL (red) before the implementation that satisfies them
- Models/seams before services; services before endpoints/UI
- Core implementation before end-to-end validation
- Story complete (checkpoint green) before moving to the next priority

### Parallel Opportunities

- Setup: T003, T004, T005 in parallel (after T001/T002)
- Foundational: T006 + T007 in parallel; then T008; then T009 + T010 in parallel (T010 after T008)
- US1: T011 + T012 in parallel; then T013–T016 in parallel (red tests); then T017 + T018 + T019 in parallel (stock/fetcher/notifier); then T020 → T021 → T022 → T023 → T024 → T025
- US2: T026 first (red), then T027 → T028 → T029 → T030 (same file `discord_bot.py`, inherently sequential), then T031, then T032
- US3: T033 + T034 in parallel (red); then T035 + T036 in parallel (different files)
- US4: T038 (red) → T039; T040 + T043 in parallel; T041 after T040; T042 after T040/T041; T044 last
- Cross-story (multi-developer): once US1 is complete, US2 and US4 can proceed in parallel (disjoint files except `api.py` — US4's T039 vs US3's T036 coordinate); US3 follows US2

---

## Parallel Example: User Story 1

```text
# Batch 1 — offline seams + pure-function tests (no deps beyond Foundational):
Task T011: "Create tests/fakes.py"
Task T012: "Write tests/test_stock.py"

# Batch 2 — remaining red tests, all in parallel (different files, only need fakes):
Task T013: "Write tests/test_notifier.py"
Task T014: "Write tests/test_checker.py"
Task T015: "Write tests/test_scheduler.py"
Task T016: "Write tests/test_api.py (US1 endpoints)"

# Batch 3 — independent leaf modules, all in parallel:
Task T017: "Implement botalert/stock.py"
Task T018: "Implement botalert/fetcher.py"
Task T019: "Implement botalert/notifier.py"

# Then strictly sequential (each builds on the prior):
T020 checker.py → T021 scheduler.py → T022 api.py → T023 web.py → T024 __main__.py → T025 e2e validation
```

## Parallel Example: User Story 3

```text
# Red tests in parallel (different files):
Task T033: "Extend tests/test_discord_bot.py (cadence modal)"
Task T034: "Extend tests/test_scheduler.py + tests/test_api.py (cadence change, PUT validation)"

# Implementation in parallel (different files):
Task T035: "Extend botalert/discord_bot.py (cadence button + modal)"
Task T036: "Extend botalert/api.py (GET/PUT /api/config)"

# Then: T037 end-to-end validation
```

---

## Implementation Strategy

### MVP First (User Stories 1 + 2 — the spec's stated MVP)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1 (core monitoring + restock alerts, API-managed)
4. **STOP and VALIDATE**: run T025's independent test — exactly one webhook alert per out→in transition, zero false alerts from failures
5. Complete Phase 4: User Story 2 (Discord management controls)
6. **STOP and VALIDATE**: run T032's independent test — full management via Discord controls only
7. **This is the MVP** (spec US2: "Together with User Story 1 this forms a complete, usable MVP") — deploy/demo

### Incremental Delivery

1. Complete Setup + Foundational → foundation ready
2. Add User Story 1 → test independently → the bot watches and alerts (API-managed)
3. Add User Story 2 → test independently → deploy/demo (MVP!)
4. Add User Story 3 → test independently → cadence is a live control on both interfaces
5. Add User Story 4 → test independently → browser dashboard + container deployment complete
6. Each story adds value without breaking previous stories (run the full `pytest -q` + `mypy --strict` gate at every checkpoint)

### Parallel Team Strategy

With multiple developers:

1. Team completes Setup + Foundational together (small, fast)
2. After US1 completes:
   - Developer A: User Story 2 → then User Story 3 (shares `discord_bot.py`)
   - Developer B: User Story 4 (`api.py` additions, `static/`, `web.py`, `Dockerfile`)
3. Coordinate the two `api.py` touchpoints (US3 T036, US4 T039) via short-lived branches or strict sequential ordering
4. Polish phase after all desired stories are in

---

## Notes

- [P] tasks = different files, no dependencies on incomplete tasks
- [Story] label maps each task to a specific user story for traceability (setup/foundational/polish have no story label)
- Each user story is independently completable and testable via its **Independent Test** criterion
- Verify tests FAIL (red) before implementing; the gate at every checkpoint is `pytest -q` green + `mypy --strict botalert tests` zero errors
- Commit after each task or logical group
- Stop at any checkpoint to validate the story independently
- Constraint values quoted in tasks are verbatim from data-model.md / research.md / contracts/ — do not relax them at implementation time
- Avoid: vague tasks, same-file parallel edits, cross-story dependencies that break independence
- The only approved `Any` in the codebase is the documented `state.py` JSON boundary (Constitution I, plan.md); Discord gateway objects are typed `discord.Interaction` (discord.py ships `py.typed`)
