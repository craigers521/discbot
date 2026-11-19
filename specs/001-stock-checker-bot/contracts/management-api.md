# Contract: Management API & Dashboard (Feature 001)

**Branch**: `001-stock-checker-bot` | **Date**: 2026-11-18 | **Companion**: [discord.md](./discord.md), [../data-model.md](../data-model.md)

The exposed port (env `PORT`, default 8080) serves, from one Flask app:

1. `GET /` → the static dashboard shell (`botalert/static/index.html` + `app.js`)
2. `GET /static/<file>` → the dashboard assets
3. `/api/*` → the management API (this contract)

**Scope note**: This contract covers the port interface only. Discord input never uses this port (spec rev 2 — see [discord.md](./discord.md)).

## Authentication (FR-020, SC-008)

- Shared secret: env `BOTALERT_TOKEN` (supplied at deployment).
- **Every `/api/*` endpoint except `GET /api/health`** requires header `X-Botalert-Token: <token>`, compared with `hmac.compare_digest`.
- The dashboard prompts for the token once (stored in browser `localStorage`), then attaches it to every request. The static shell at `/` is served without the token: it is a stateless page containing no data and altering no state — it is the vehicle through which the owner enters the token.
- Missing/invalid token → `401` with error code `unauthorized`. The auth check runs before any state access; rejected requests alter nothing.

## Common conventions

- Content type: `application/json` for all `/api` requests/responses.
- Error shape (all non-2xx):

  ```json
  { "error": { "code": "duplicate_url", "message": "A product with this URL is already monitored." } }
  ```

- Error codes used by this API: `unauthorized` (401), `invalid_url` (400), `invalid_label` (400), `invalid_indicator` (400), `invalid_cadence` (400), `duplicate_url` (409), `not_found` (404), `cycle_in_progress` (409).
- Product identification by URL: `PATCH`/`DELETE` take the product's URL as a **query parameter** `?url=<url-encoded>` (avoids path-segment encoding issues; the URL is the natural unique key per data-model §2).
- Timestamps: UTC ISO-8601. Statuses: `in_stock` | `out_of_stock` | `unknown`.

## Product representation

```json
{
  "id": "a1b2c3d4e5f6",
  "url": "https://shop.example.com/item-42",
  "label": "Example Sneakers",
  "in_stock_indicator": null,
  "out_of_stock_indicator": null,
  "last_status": "out_of_stock",
  "last_checked_at": "2026-11-18T12:00:00+00:00",
  "consecutive_failures": 0,
  "created_at": "2026-11-18T11:50:00+00:00"
}
```

(`failure_notified` is internal and never exposed.)

## Endpoints

### `GET /api/health` — public liveness probe

- **Auth**: none.
- **200**:

  ```json
  { "status": "ok", "gateway_connected": true, "cycle_in_progress": false }
  ```

- Exposes no watch-list data, no secrets. For operator/container health checks.

### `GET /api/products` — list (FR-003)

- **200**: `{ "products": [ <product> ] }` (insertion order).
- Includes label, URL, latest determined status per FR-003.

### `POST /api/products` — add (FR-001, US4-4/5/6)

- **Body**:

  ```json
  { "url": "https://shop.example.com/item-42", "label": "Sneakers",
    "in_stock_indicator": "Add to Cart", "out_of_stock_indicator": "Out of Stock" }
  ```

  `label`, `in_stock_indicator`, `out_of_stock_indicator` optional (validation: data-model §2).
- **201**: the created `<product>` (status `unknown`, `last_checked_at` null). It will be checked on the next cycle (SC-001: at least once within one cadence interval — the cycle runs on the scheduler's existing tick, and adding a product sets the scheduler's wakeup so a fresh watch list is not left to wait a full interval).
- **400** `invalid_url` / `invalid_label` / `invalid_indicator`; **409** `duplicate_url`.

### `PATCH /api/products?url=<url>` — modify (FR-004)

- **Body** (all fields optional; at least one required): `{ "label": "...", "in_stock_indicator": "...", "out_of_stock_indicator": "..." }`. An empty-string indicator clears it. The URL itself is immutable (changing the URL = remove + add).
- **200**: the updated `<product>`.
- **400** `invalid_label` / `invalid_indicator` / (empty body); **404** `not_found`.

### `DELETE /api/products?url=<url>` — remove (FR-002)

- **204**: no body. The product is no longer checked; failure counters vanish with it (data-model §6 "recovered" rule).
- **404** `not_found`.

### `GET /api/config` — read cadence (FR-005)

- **200**: `{ "cadence_minutes": 5.0, "min_cadence_minutes": 1.0, "max_cadence_minutes": 1440.0 }`

### `PUT /api/config` — set cadence (FR-005, FR-011, US3)

- **Body**: `{ "cadence_minutes": 30 }` (number).
- **200**: same shape as `GET /api/config` (echoes the new value). Takes effect from the next check cycle with no restart (SC-004 — the write sets the scheduler's wakeup event).
- **400** `invalid_cadence` with a message stating the enforced minimum (and maximum), e.g. `"Cadence must be at least 1.0 minutes (minimum) and at most 1440.0 minutes (maximum)."` Rejected values: non-numeric, below floor, above ceiling. Current cadence is unchanged.

### `POST /api/check-now` — trigger an immediate cycle *(justified addition, plan.md Constitution Check)*

- **No body.**
- **202**: `{ "status": "cycle_scheduled" }` — a cycle will run as soon as the scheduler is free.
- **409** `cycle_in_progress` — a cycle is already running; no second one is started (FR-018).
- The client verifies results by polling `GET /api/products` (statuses / `last_checked_at`). This endpoint exists so SC-001/SC-002 are verifiable end-to-end without waiting a full cadence interval; it performs no action the scheduler would not itself perform.

## Dashboard contract (FR-019, US4)

- One static HTML page + plain JS; no framework, bundler, or build step (served straight from `botalert/static/`).
- Flow: load → if no token in `localStorage`, show token prompt → `GET /api/products` + `GET /api/config` to verify token (401 → re-prompt) → render watch-list table (label, URL, status badge, last checked, failure count) with:
  - **Add form**: url + optional label + optional indicators → `POST /api/products`
  - **Per-row actions**: edit (label/indicators → `PATCH`), remove (`DELETE`)
  - **Cadence form**: `PUT /api/config`
  - **Check now** button: `POST /api/check-now`, then re-poll the list
  - Inline confirmation/error text for every operation (FR-015 — same guarantee as the Discord interface)
- Refresh model (spec assumption): re-render on load and after each operation; no background auto-refresh, no push.
- The dashboard and Discord share the one `StateStore`, so a result produced through either is identical to the other (US4-2; data-model §6 concurrency rule).

## Response-time expectation

All endpoints are in-process state access: < 200 ms (plan, Performance Goals). `POST /api/check-now` returns immediately (202/409) — the cycle itself runs on the scheduler thread.
