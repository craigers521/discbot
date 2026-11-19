# Contract: Discord Interface (Feature 001)

**Branch**: `001-stock-checker-bot` | **Date**: 2026-11-18 | **Companion**: [management-api.md](./management-api.md), [../data-model.md](../data-model.md)

Per spec rev 2, ALL Discord-originated input arrives over the bot's single persistent, authenticated gateway connection (`discord.py` 2.7.1, standard mode, bare `discord.Client`). The exposed port is never involved in Discord input and does not need to be reachable from Discord's servers (FR-017).

## Application setup (operator, one-time)

1. Discord Developer Portal → New Application → create a **Bot** (token = `DISCORD_TOKEN`).
2. **No privileged intents required**: components (buttons, select, modals) are delivered without `message_content`/`members`; the bot uses only default intents. (botalert never reads message text.)
3. Invite the bot to the owner's server with scope `bot` and permissions: **View Channels, Send Messages, Embed Links** (the management channel).
4. Owner's Discord user id → `OWNER_DISCORD_ID`; management channel id → `DISCORD_MANAGE_CHANNEL_ID`.
5. Restock webhook (channel → Webhooks) URL → `BOTALERT_WEBHOOK_URL`.

## Authorization (FR-010, SC-007)

Every management interaction is checked: `interaction.user.id == OWNER_DISCORD_ID`.

- **Owner**: processed, then an ephemeral confirmation.
- **Non-owner**: ephemeral rejection — `❌ Not authorized — only the owner can manage the watch list.` No state is read for or altered by the interaction (SC-007: 100% rejected, zero state change).

Every management response — success or error — carries a clear confirmation or reason (FR-015). All owner-facing responses are **ephemeral**.

## The control message (the management surface)

Posted by the bot into the management channel on startup, and self-healing thereafter:

- **On startup** (`on_ready`): delete the bot's previous control message (a bot may always delete its own), post a fresh one, store its id in state (`control_message_id`).
- **If deleted while running**: `on_message_delete` matches the stored id and re-posts — the management surface always exists.
- **Content**:

  > **🛒 botalert — watch list controls**
  > `[📋 View watch list]` `[⚙️ Manage products]` `[🔁 Change cadence]` `[➕ Add product → dashboard]`

  The "Add product" button links to the dashboard URL (env `BOTALERT_BASE_URL`, where free-text URL entry lives per FR-001) and is rendered only when that variable is set.

### Buttons & select menu

| custom_id | Label / role | Behavior (owner) |
|---|---|---|
| `ctl.view` | 📋 View watch list | Ephemeral reply listing every product: `• <label> — <status emoji> — <url>` (plus `checked <relative time>` and a `⚠️ n consecutive failures` marker where `consecutive_failures > 0`). Empty list → ephemeral "Watch list is empty — add products in the dashboard." |
| `ctl.manage` | ⚙️ Manage products | Ephemeral reply with a **select menu** (`sel.product`), one option per product: `value = product.id`, `label = "<label> — <host>"` (truncated to 100 chars). |
| `sel.product` | (select) product picker | On selection, ephemeral follow-up with two buttons for that product: `[🗑 Remove <label>]` (`ctl.remove:<id>`) and `[✏️ Relabel <label>]` (`ctl.relabel:<id>`). |
| `ctl.remove:<id>` | remove one product | Removes immediately, replies: `✅ Removed <label> (<url>).` followed by the updated count. (US2-2.) |
| `ctl.relabel:<id>` | relabel one product | Opens the **relabel modal** (below). |
| `ctl.cadence` | 🔁 Change cadence | Opens the **cadence modal** (below). |

`<id>` is the product's 12-char internal id (data-model §2); ids are validated against the current watch list at interaction time — a stale `ctl.remove:<old-id>` from a deleted message replies `⚠️ That product is no longer on the watch list.` (404-equivalent, FR-015).

## Modals (free-form entry in Discord)

| Modal | Trigger | Field | Validation on submit |
|---|---|---|---|
| Relabel | `ctl.relabel:<id>` | `TextInput` (short, placeholder "New label", max 100, required, pre-filled with current label) | trim, 1–100 chars → `PATCH`-equivalent update. Invalid → ephemeral `⚠️ Label must be 1–100 characters.` (current label unchanged). |
| Cadence | `ctl.cadence` | `TextInput` (short, placeholder e.g. "5", max 10, required) | numeric (int or float) and `MIN_CADENCE_MINUTES ≤ v ≤ MAX_CADENCE_MINUTES` → state update + scheduler wakeup. Invalid (malformed **or** below minimum **or** above maximum) → ephemeral `⚠️ Cadence must be a number of minutes between 1 and 1440. (current: 5)` — current cadence unchanged. (US3 scenarios 1–3.) |

Modal submits are handled by the modal's own typed `on_submit` (the modal instance carries its target product id); no custom_id routing needed for modals.

## Interaction flow summary (sequences)

```text
view:      [btn ctl.view]                 → ephemeral list
manage:    [btn ctl.manage]               → ephemeral select
           [select sel.product → id]      → ephemeral [remove] [relabel]
remove:    [btn ctl.remove:<id>]          → state mutation → ephemeral confirm
relabel:   [btn ctl.relabel:<id>]         → modal → typed on_submit → state mutation → ephemeral confirm
cadence:   [btn ctl.cadence]              → modal → typed on_submit → state mutation → ephemeral confirm
non-owner: [any management interaction]   → ephemeral rejection, no state access
```

All mutations go through the same `StateStore` critical sections as the API (data-model §6) — a single source of truth shared by both interfaces (US2/US4 consistency).

## Outbound notifications (bot → owner)

### 1. Restock alert → **webhook** (env `BOTALERT_WEBHOOK_URL`)

Posted by the checker thread (stdlib POST, 10 s timeout) the moment a determined out→in transition is applied — within 60 s of the detecting check (SC-002), exactly one per transition (FR-008; dedup state = persisted last-known status).

```json
{ "content": "🔔 Restock: Example Sneakers\nhttps://shop.example.com/item-42\n(detected 12:00:00 UTC)" }
```

Label + URL + detection time (spec assumption "Notification content").

**Delivery failure** (non-2xx or transport error): surfaced to the owner, never silently dropped (edge case "Invalid webhook destination") — a message is sent to the management channel (below), and the failed alert is re-attributed to the product's `consecutive_failures`-style surfacing only via that channel notice; the transition itself is still recorded (the product is now `in_stock`; no silent re-alert later — the owner sees the failure notice and can act).

### 2. Operational notices → **management channel** (plain, non-ephemeral)

| Trigger | Message |
|---|---|
| Product hits `FAILURE_THRESHOLD` (10) consecutive failed/unknown checks, first time | `⚠️ <label> (<url>) has failed its last 10 checks (last error: <detail>). It appears unreachable or its stock indicator no longer matches — check the page or set per-product indicators. This notice won't repeat until it recovers or is removed.` (FR-016, edge cases: repeated failures / retailer layout change) |
| Webhook delivery failure (above) | `📡 Failed to deliver the restock alert for <label> to the webhook: <detail>. The product status was still updated.` (FR-016) |

Channel messages sent from non-gateway threads (the scheduler) are dispatched via `asyncio.run_coroutine_threadsafe` into the bot's loop (research §3/§5). If the gateway is disconnected at that moment, the notice is logged to stdout and the bot retries it on reconnect (one retry — the persistent record of *what* failed is also in the owner-visible state via the next `ctl.view` failure marker).

### 3. Status emoji legend

`🟢 in stock` · `🔴 out of stock` · `⚪ unknown (not determined yet)`
