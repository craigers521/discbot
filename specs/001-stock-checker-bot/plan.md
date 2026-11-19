# Implementation Plan: Stock Checker Bot

**Branch**: `001-stock-checker-bot` | **Date**: 2026-11-18 (updated for spec rev 2) | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-stock-checker-bot/spec.md`

## Summary

botalert is a single-container Python service that watches product-page URLs for restocks. A sequential background scheduler fetches each monitored page on a configurable cadence; when a product transitions out-of-stock → in-stock, a notification is posted to a Discord webhook. The owner manages the watch list through two interfaces that share one source of truth: (1) Discord interactive controls (buttons, a select menu, modals) — the bot maintains a **single persistent, authenticated Discord gateway connection** (discord.py's standard mode, per spec rev 2), posts a control message into the management channel, and ALL Discord-originated interactions arrive over that gateway — no inbound HTTP from Discord, no request-signature verification, and the exposed port does not need to be reachable from Discord's servers; and (2) a single static HTML/JS dashboard served by Flask on a single configurable port, gated by a shared-secret token (reachable from the owner's own devices only). All state (watch list, last-known statuses, cadence, control-message id) persists in a single JSON file on a volume and survives restarts.

## Technical Context

**Language/Version**: Python 3.15 (current stable 3.15.2, released 2026-10-09; base image `python:3.15-slim`; minimum supported 3.13) — confirmed against current stable by Phase 0 research ([research.md](./research.md) §2)

**Primary Dependencies** (all approved per Constitution Principle IV — see [research.md](./research.md) §1 for justifications):
- `flask==3.1.3` — web framework for the dashboard + management API (user-specified)
- `discord.py==2.7.1` — Discord module in its **standard persistent-gateway mode** (`discord.Client` + `run(token)` on a dedicated thread): interactive components (buttons, select menu, modals), authenticated posting to the management channel, and per-interaction user attribution for owner authorization (user-specified "Discord module"; matches the mode of the user-provided sample bot)
- `python-dotenv==1.2.4` — loads credentials/config from a `.env` file (user-specified; matches sample bot)
- Standard library for everything else: `urllib.request` (page fetching, webhook POSTs), `hmac` (constant-time shared-secret comparison), `threading`/`threading.Event` (scheduler + single-writer state), `json`/`os` (durable state store), `uuid` (short product ids for Discord UI references)

**Storage**: Single JSON file on a volume (path via `STATE_FILE`, default `state.json`; container sets `/data/state.json`), atomic writes (temp file + `os.replace`), every read-modify-write guarded by a process-wide `threading.Lock`. No database.

**Testing**: pytest (unit, deterministic, all network I/O — page fetch, Discord gateway, webhook — behind injectable seams; Discord routing tested at the router level against typed request envelopes, no live connection) + mypy `--strict` as the type-check gate (Constitution I & III). All three runtime deps ship `py.typed` (or are stdlib), so the strict gate covers the whole package.

**Target Platform**: Linux container (canonical); also runs directly with CPython for local dev

**Project Type**: web-service (Flask app: static dashboard + REST management API on one configurable port; plus a persistent-gateway Discord client and an in-process sequential checker thread)

**Performance Goals**: 25 products at 5-minute cadence, strictly sequential fetches (SC-006); full check cycle completes within one cadence interval under normal network conditions (per-fetch timeout 15 s, 2 s pause between fetches → worst-case-normal ≈ 25 × 7 s ≈ 175 s < 300 s); restock notification delivered within 60 s of the check that detected the transition (SC-002); management API responses < 200 ms (in-process state access)

**Constraints**:
- Single process, single configurable port serving dashboard + management API; the port does NOT need to be reachable from Discord's servers (FR-014, FR-020)
- Discord input arrives exclusively over the single persistent authenticated gateway connection; every interaction is attributed to the triggering Discord user for owner authorization (FR-017, rev 2)
- No parallel page fetches; sequential cycle with short pause (FR-018)
- Failed or unrecognized checks never change last-known status (FR-012)
- Dashboard is one static HTML page + plain JS; no framework/bundler/build step (FR-019)
- Light image: slim base, 3 pinned runtime deps, no build tools

**Scale/Scope**: 1 owner, 1 management channel, 1 webhook destination, ~25 products (design headroom to ~50), single host/container

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|---|---|---|
| I. Strict Type Safety | ✅ PASS (with documented justification) | mypy `--strict` on the whole package (config `python_version = 3.13` to enforce the minimum-supported floor). Zero untyped escape hatches **except** the state-file JSON boundary: the durable store is parsed by stdlib `json.loads`, whose API is not generic, so inbound JSON is received as `Any` in `state.py` for one scope, then immediately validated and narrowed into typed dataclasses before any other module sees it. This is the documented justification required by Principle I for `Any`. (Rev 2 removed the previous inbound-Discord-JSON boundary: discord.py ships `py.typed` and delivers fully typed `Interaction` objects over the gateway.) |
| II. Modular Architecture | ✅ PASS | Small single-purpose modules with one-directional, acyclic dependencies (see Project Structure): `config → (stdlib)`, `models → (stdlib)`, `state → models`, `stock → models`, `fetcher → (stdlib)`, `notifier → (stdlib)`, `checker → models,stock,fetcher,notifier,state`, `scheduler → checker,state`, `discord_bot → config,state,models,notifier`, `api → state,config,models`, `web → api`. Every capability (fetching, status determination, notifications, Discord I/O, scheduling) lands in its own module. |
| III. Unit Testing with pytest | ✅ PASS | Unit tests for status determination (indicator precedence, heuristics, unknown fallback), state store (persistence/atomicity/validation/corruption/concurrency), checker (transitions, no alert on add, failures don't change status, threshold notices), scheduler (cadence pickup, wakeup, no overlap), Discord routing (owner auth, custom_id routing, modal submits, control-message lifecycle) against a fake gateway, notifier (payload, failure surfacing), and every API route. All network I/O behind injectable seams (`tests/fakes.py`) so tests are deterministic and offline; full suite well under a minute. |
| IV. Zero Unapproved Third-Party Dependencies | ✅ PASS (3 runtime deps approved, all user-directed) | Approved with recorded justification in [research.md](./research.md) §1: `flask==3.1.3` (user-specified framework), `discord.py==2.7.1` (user-specified Discord module, standard gateway mode), `python-dotenv==1.2.4` (user-specified env-file credential handling). Dev-only: `pytest==9.2.0` + `mypy==2.4.1` (mandated by Principles I & III). All pinned. Page fetching, webhook POSTs, token comparison, and the scheduler use the standard library. |
| Tech & Deployment: Python / Simplicity / Container | ✅ PASS (2 justifications) | Python ✓. Dockerfile with pinned `python:3.15-slim` base ✓. Simplicity justifications: (1) `POST /api/check-now` endpoint added beyond the spec's five operations — justified as the only practical way to validate SC-001/SC-002 end-to-end without waiting a full cadence interval; (2) cadence + label changes in Discord use modals (text input) rather than preset selects — justified because US3 acceptance scenarios require rejecting malformed and below-minimum cadence input, which presumes free-form entry. |
| Quality & Documentation | ✅ PASS (tasks-phase obligation) | Full docstrings on all public API; README with local + container run instructions, Discord app setup, and `.env.example` with every variable documented; docs updated in the same change. Carried into tasks.md. |

**Gate result: PASS** — no unjustified violations; Complexity Tracking not required (no violations to justify).

**Phase 1 re-check: PASS** — post-design review of data-model.md, contracts/, and quickstart.md introduced no new dependencies, no new `Any` boundaries, and no new modules beyond the recorded structure. The two simplicity justifications and the single documented `Any` boundary remain as recorded above.

## Project Structure

### Documentation (this feature)

```text
specs/001-stock-checker-bot/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
│   ├── management-api.md
│   └── discord.md
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
botalert/
├── __init__.py
├── __main__.py          # entrypoint: load config, init state, start bot thread + scheduler thread, run Flask
├── config.py            # typed Config dataclass; reads .env / environment (all env vars documented)
├── models.py            # typed dataclasses: Product, StockStatus enum, CheckResult, AppState, RuntimeFlags
├── state.py             # StateStore: load/save (atomic), lock-guarded read-modify-write API, validation, corruption recovery
├── stock.py             # determine_status(html, product) → in_stock|out_of_stock|unknown; per-product indicators take precedence over heuristic phrase sets
├── fetcher.py           # fetch_page(url, timeout, user_agent) via urllib; FetchResult(html | status | error)
├── notifier.py          # send_restock (webhook POST via urllib) + notify_owner (channel message via injected sender); delivery-failure surfacing
├── checker.py           # run_cycle(snapshot, fetcher, notifier, state) — sequential loop, pauses, transitions, failure counters, threshold notices
├── scheduler.py         # background thread: Event wait (cadence or explicit wakeup) → cycle; picks up cadence changes; no overlapping cycles
├── discord_bot.py       # persistent gateway client: on_ready (re)posts control message, on_interaction router (custom_id), modals (relabel/cadence), owner auth, outbound channel messages (cross-thread)
├── api.py               # Flask Blueprint /api/*: token auth guard, watchlist CRUD, cadence, check-now, health
├── web.py               # Flask app factory; serves static dashboard; wires blueprint
└── static/
    ├── index.html       # single-page dashboard (no framework)
    └── app.js           # token prompt (localStorage), API calls, watchlist table, add/edit forms, cadence form

tests/
├── test_models.py
├── test_state.py        # persistence, atomicity, validation, corruption recovery, concurrency (lock)
├── test_stock.py        # heuristics, per-product indicator precedence, unknown fallback
├── test_checker.py      # transitions, no alert on add, failures don't change status, threshold notice once-only
├── test_scheduler.py    # cadence pickup after change, wakeup via check-now, no overlap
├── test_discord_bot.py  # owner auth, custom_id routing, modal submits, control-message lifecycle (fake gateway)
├── test_notifier.py     # webhook payload shape, delivery-failure surfacing
├── test_api.py          # every endpoint: auth 401s, CRUD, cadence floor/ceiling, duplicate URL, invalid URL, cycle-in-progress
└── fakes.py             # in-memory fetcher/notifier/gateway fakes + fake clock for deterministic tests

Dockerfile
requirements.txt         # pinned runtime deps (flask, discord.py, python-dotenv)
requirements-dev.txt     # pinned dev deps (pytest, mypy)
mypy.ini                 # strict = true, python_version = 3.13 (enforces minimum-supported floor)
.env.example
README.md
```

**Structure Decision**: Single Python package at the repository root (no `src/` layer, no `backend/`+`frontend/` split) — the project is one small service; a `src/` layout or separate frontend package would add path indirection without isolating any real boundary (Principle II: modules, not directories, are the unit). The static dashboard lives in `botalert/static/` because it is served by the package itself and is not a separately built artifact (FR-019). Rev-2 note: the pre-rev-2 modules `interactions.py` (HTTP interaction endpoint + signature verification) and `discord_client.py` (HTTP-only outbound) are superseded by the single `discord_bot.py` gateway module. Tests mirror module names; `tests/fakes.py` centralizes the network seams required by Principle III.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

No constitution violations — not applicable. (The two simplicity justifications and the one `Any` boundary documented in the Constitution Check are recorded there and in research.md §1/§4.)
