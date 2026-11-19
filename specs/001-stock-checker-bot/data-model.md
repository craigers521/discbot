# Data Model: Stock Checker Bot (Feature 001)

**Branch**: `001-stock-checker-bot` | **Date**: 2026-11-18 | **Source**: [spec.md](./spec.md) Key Entities + Requirements

Entities map 1:1 to the spec's Key Entities section. Python types per the plan's Project Structure (`models.py`, `state.py`). All timestamps are UTC.

---

## 1. Entity overview

| Entity | Spec name | Where it lives | Persisted? |
|---|---|---|---|
| `Product` | Monitored Product | `AppState.products` (in the state file) | ✅ |
| `AppState` | Watch List (+ configuration) | single JSON document | ✅ |
| `CheckSchedule` | Check Schedule | `AppState.cadence_minutes` + config constants | cadence ✅ / limits ❌ (env) |
| `NotificationDestination` | Notification Destination | `Config` (env only) | ❌ |
| `CheckResult` | Check Result | transient (one check of one product) | ❌ |
| `Owner` | Owner | `Config.owner_discord_id` (env only) | ❌ |

Relationships: `AppState 1 — N Product` (insertion-ordered list; the **normalized URL is the unique natural key**). `Product 1 — 1 CheckResult` transiently, per cycle. No other relationships (no history, per spec).

---

## 2. `Product` (Monitored Product)

| Field | Type | Validation | Source |
|---|---|---|---|
| `id` | `str` | 12-char lowercase hex (`uuid4().hex[:12]`), generated on add; immutable | internal — reference key for Discord UI (select options are limited to 100 chars; URLs may exceed that) |
| `url` | `str` | Absolute `http://` or `https://` URL with a non-empty host; whitespace-stripped; scheme+host normalized to lowercase (path kept verbatim). The unique key. | FR-001, US4-5 |
| `label` | `str` | 1–100 chars after trim; optional on add — defaults to the URL's host (e.g. `shop.example.com`) | FR-001, FR-004 |
| `in_stock_indicator` | `str \| None` | ≤200 chars; case-insensitive substring matched against fetched page text; takes precedence over heuristics when set (see research §6) | FR-006 |
| `out_of_stock_indicator` | `str \| None` | same rules | FR-006 |
| `last_status` | `StockStatus` | one of `in_stock \| out_of_stock \| unknown`; initialized `unknown` on add | FR-003, FR-006 |
| `last_checked_at` | `datetime \| None` | UTC; `None` until the first completed check (a *failed* check updates this too — it was still attempted; see transition table note) | Key Entities |
| `consecutive_failures` | `int` | ≥0; incremented on fetch failure **or** `unknown` determination; reset to 0 on a determined `in_stock`/`out_of_stock` | edge cases (repeated failures) |
| `failure_notified` | `bool` | True once the threshold notice has been sent; reset when the product recovers or is removed; gates the once-only notice | FR-016, edge cases |
| `created_at` | `datetime` | UTC, set on add | Key Entities |

**Uniqueness**: one product per normalized URL (spec assumption "One product per URL"). Adding a URL already present (any casing of scheme/host) is rejected as `duplicate_url` — no silent merge (US4-6).

## 3. `AppState` (Watch List + configuration)

| Field | Type | Validation |
|---|---|---|
| `schema_version` | `int` | `1` (fixed in v1; loader rejects unknown major versions) |
| `cadence_minutes` | `float` | `MIN_CADENCE_MINUTES` (env, default 1.0) ≤ value ≤ `MAX_CADENCE_MINUTES` (env, default 1440.0); default on first start: `DEFAULT_CADENCE_MINUTES` (env, default 5.0) |
| `control_message_id` | `str \| None` | Discord snowflake id (string) of the current control message; `None` until the bot posts it; cleared/ignored when the bot (re)posts on startup | FR-017 support, research §3 |
| `products` | `list[Product]` | insertion-ordered; ≤50 entries recommended (design headroom; SC-006 sized for 25) |

## 4. `CheckResult` (transient — never persisted)

| Field | Type | Notes |
|---|---|---|
| `product_id` | `str` | which product was checked |
| `outcome` | `StockStatus \| "error"` | determined status, or `error` when the fetch failed |
| `error` | `str \| None` | failure detail (timeout, HTTP status, connection error) — present iff outcome is `error` |
| `checked_at` | `datetime` | UTC |

Produced by `fetcher` + `stock.determine_status`; consumed by `checker.run_cycle` which applies the transition rules below. Not stored (spec: no check history in v1).

## 5. Configuration (`Config`, env-only — never persisted)

| Env var | Required | Default | Maps to |
|---|---|---|---|
| `DISCORD_TOKEN` | ✅ | — | gateway auth (bot token) |
| `OWNER_DISCORD_ID` | ✅ | — | `Owner` identity (FR-010) |
| `DISCORD_MANAGE_CHANNEL_ID` | ✅ | — | control message + operational notices channel |
| `BOTALERT_WEBHOOK_URL` | ✅ | — | `NotificationDestination` (restock alerts) |
| `BOTALERT_TOKEN` | ✅ | — | shared secret for dashboard/API (FR-020) |
| `PORT` | ❌ | `8080` | exposed port (FR-013, FR-014) |
| `BOTALERT_BASE_URL` | ❌ | — | dashboard URL shown by the "Add product" Discord button |
| `STATE_FILE` | ❌ | `state.json` (container: `/data/state.json`) | durable store path (FR-009) |
| `CHECK_TIMEOUT_SECONDS` | ❌ | `15` | per-fetch timeout (edge case: very large/slow pages) |
| `CHECK_PAUSE_SECONDS` | ❌ | `2` | pause between consecutive fetches (FR-018) |
| `FAILURE_THRESHOLD` | ❌ | `10` | consecutive-failure notice threshold (FR-016) |
| `DEFAULT_CADENCE_MINUTES` | ❌ | `5` | initial cadence (assumption: sensible defaults) |
| `MIN_CADENCE_MINUTES` | ❌ | `1` | enforced floor (FR-011) |
| `MAX_CADENCE_MINUTES` | ❌ | `1440` | enforced ceiling (symmetry with floor; 24 h) |
| `USER_AGENT` | ❌ | `botalert/0.1 (personal stock checker)` | fetch identity (politeness assumption) |

Startup with a missing/empty required var → fail fast with a named-variable error (no silent defaults for secrets or ids).

## 6. Status state machine (per product)

States: `unknown` → `in_stock` ⇄ `out_of_stock`. Transitions are applied **only** by the checker after a successful, determined check (FR-012: failed fetches and `unknown` determinations change nothing).

| From | Event | To | Side effects |
|---|---|---|---|
| `unknown` (newly added) | first determined check = `in_stock` | `in_stock` | **no alert** (assumption: no immediate alert on add; US1-4) |
| `unknown` (newly added) | first determined check = `out_of_stock` | `out_of_stock` | none |
| `in_stock` | check = `out_of_stock` | `out_of_stock` | none (sold-out alerts out of scope) |
| `out_of_stock` | check = `in_stock` | `in_stock` | **restock alert via webhook** — exactly one per transition (FR-007, FR-008); resets `consecutive_failures`/`failure_notified` |
| `in_stock` | check = `in_stock` | `in_stock` | **no alert** (US1-2); resets failure counters |
| `out_of_stock` | check = `out_of_stock` | `out_of_stock` | no alert; resets failure counters |
| any | fetch failure **or** `unknown` determination | **unchanged** | `consecutive_failures += 1`; `last_checked_at` updated; at `consecutive_failures == FAILURE_THRESHOLD` and `failure_notified == False` → once-only owner notice in the management channel + `failure_notified = True` |
| any | next determined check (any) | per rows above | `consecutive_failures = 0`, `failure_notified = False` (product "recovered") |

Alert deduplication across restarts (SC-005 / edge case "container restart mid-cycle"): the alert decision derives purely from the persisted `(last_status, new_status)` pair — a transition already alerted is, after save, `in_stock → in_stock`, so a restart can never re-emit it. There is no separate "notified" flag for restocks; last-known status *is* the dedup state.

**Concurrency rule**: every read-modify-write of `AppState` (add/remove/modify/cadence from either interface, and per-product updates from the checker) happens inside one `StateStore` lock critical section, followed by an atomic save (research §7). This is what guarantees "no lost updates, no duplicate entries" under simultaneous dashboard + Discord use.

## 7. Persistence format (state file, schema v1)

```json
{
  "schema_version": 1,
  "cadence_minutes": 5.0,
  "control_message_id": null,
  "products": [
    {
      "id": "a1b2c3d4e5f6",
      "url": "https://shop.example.com/item-42",
      "label": "Example Sneakers",
      "in_stock_indicator": null,
      "out_of_stock_indicator": null,
      "last_status": "out_of_stock",
      "last_checked_at": "2026-11-18T12:00:00+00:00",
      "consecutive_failures": 0,
      "failure_notified": false,
      "created_at": "2026-11-18T11:50:00+00:00"
    }
  ]
}
```

Loader rules (research §7): missing file → fresh state; corrupt file → rename to `state.json.corrupt-<utcstamp>`, log, start fresh; unknown `schema_version` → same treatment (never guess). Writers: atomic `temp + os.replace`, under the lock, after every mutation.
