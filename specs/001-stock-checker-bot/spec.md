# Feature Specification: Stock Checker Bot

**Feature Branch**: `001-stock-checker-bot`

**Created**: 2026-11-17

**Status**: Draft

**Input**: User description: "i want to develop a stock checker bot. The purpose of the bot is to check for available stock at various online retailers and notify the user that a restock has occurred. It should allow a user to create and modify a list of product pages to check in the form of URLs/web addresses to the product page for each retailer. The cadence that the bot rechecks for available stock should be configurable. Interacting with the bot to manage monitored pages should happen via discord. Notifications will be sent as webhooks to discord. I will provide context/examples of a sample discord bot project during the plan phase. Lastly the bot will be deployed via a docker container, and should allow for configurable port to be exposed to allow for user interaction."

## Clarifications

### Session 2026-11-18

- Q: How should the bot receive user input from Discord — interactive webhook POSTs to the exposed port, or a persistent always-on gateway/websocket connection? → A: Webhook-driven: the bot posts interactive control messages (buttons/selects) into a Discord channel; interactions arrive as HTTP POSTs to the bot's endpoint on the exposed port. No persistent Discord connection in v1 (may be added later if a use case is uncovered) — *superseded by the rev-2 session below: the persistent gateway connection was adopted.* Free-text entry (product URLs) happens in the web dashboard.
- Q: Should state persist across container restarts in v1, and via what kind of store? → A: Minimal durable store: the watch list, last-known statuses, and cadence are persisted in a small durable store (e.g., a single file on a volume) and survive restarts; check history or anything heavier is out of scope for v1.
- Q: How should checks across monitored product pages be executed when a cadence tick fires? → A: Single sequential scheduler loop: on each cadence tick the bot walks the watch list in order and fetches each product page one at a time, with a short pause between consecutive requests; no parallel in-flight fetches, no external trigger.
- Q: How should the web management dashboard be built? → A: A single static HTML page with plain JavaScript that calls the HTTP API; no JavaScript framework, bundler, or build step.
- Q: What access control should protect the dashboard and management API on the exposed port, which must be reachable from Discord's servers? → A: Single shared secret: one token (supplied at deployment, entered once in the dashboard) gates the dashboard and all management API operations; Discord interaction POSTs are authenticated separately via Discord's built-in request signatures — *signature-verification half superseded by rev 2: Discord input no longer arrives as HTTP POSTs (see below).*

### Session 2026-11-18 (rev 2)

- Q: Was the "webhook-driven, no persistent Discord connection" decision (clarification #1) a hard requirement, or only an advocacy for simplicity? If the Discord Python module (discord.py) enables a persistent gateway connection simply and sensibly, may the design pivot to it? → A: It was only an advocacy for simplicity — not a hard rule. **Pivot to the persistent gateway connection** (discord.py's standard mode, matching the user-provided sample bot): Discord-originated input (buttons, select menus, modals) arrives over the authenticated gateway; the exposed port no longer needs to be publicly reachable from Discord (it serves only the dashboard and management API, to the owner); no application public client secret or request-signature verification is required. Rev 2 amends FR-014, FR-017, FR-020, User Story 2, User Story 4 (scenario 3 removed), SC-008, and the affected assumptions below.

### Session 2026-11-19 (rev 3)

- Q: Requirements-quality review (checklists/review.md, 40 items) — which items need spec-level statements or explicit scope decisions, and which can be accepted at contract/data-model level? → A: Resolved with the owner item by item. **13 spec amendments applied** (each states at requirements level what plan/data-model/contracts already implement — no design changes): FR-001 (valid-address definition, CHK025), FR-004 (URL immutability, CHK024), FR-011 (cadence maximum + US3 scenario 4, CHK023), FR-020 (gate-boundary definition, CHK003), SC-009 (detection latency, CHK021), edge cases (no-retry clause on "Invalid webhook destination" (CHK008); new: Missing deployment credentials (CHK005), Corrupt durable state at startup (CHK016), Gateway disconnect (CHK018), Management channel unavailable (CHK019), Bot malfunction visibility (CHK034)), assumptions (No availability targets in v1 (CHK033), External platform dependencies (CHK037)). **4 acceptance decisions** (no spec change, recorded in checklists/review.md): non-owner rejections are ephemeral to the invoking user (CHK002); secret-rotation recovery is the contract's 401→re-prompt flow (CHK004); persist-before-send ordering is the data-model/contract mechanism satisfying SC-002/SC-005/the edge cases (CHK012); the URL normalization algorithm is a data-model design detail (CHK040). All 40 review items resolved.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Watch Products and Get Restock Alerts (Priority: P1)

The owner adds product page URLs to the bot's watch list. The bot rechecks each page on a configurable cadence, tracks whether each product is in or out of stock, and the moment a product goes from unavailable to available, the bot sends a notification to the owner's Discord channel via webhook, including the product's name and its page link.

**Why this priority**: This is the core value of the product — the reason the bot exists. Without monitoring plus alerting, nothing else matters; every other capability (management, cadence, the exposed port) exists in service of this flow.

**Independent Test**: Add one product page that is currently out of stock, wait for the product to be restocked, and verify that exactly one Discord webhook notification arrives containing the product's name and URL.

**Acceptance Scenarios**:

1. **Given** a product page is out of stock and is on the watch list, **when** a scheduled check determines the product is in stock, **then** the bot sends a webhook notification to the configured Discord channel containing the product's label and its URL.
2. **Given** a product is in stock on one check, **when** subsequent checks also determine it is in stock, **then** no further notifications are sent.
3. **Given** a product is added to the watch list while currently out of stock, **when** the product later becomes available, **then** the owner is notified.
4. **Given** a product is added to the watch list while already in stock, **when** the first check runs, **then** no restock notification is sent (alerts fire only on an unavailable-to-available transition).
5. **Given** a check of a product page fails (unreachable, blocked, or timed out), **when** the check completes, **then** no restock alert is generated from that check and the product's last known status is retained.

---

### User Story 2 - Manage the Watch List via Discord Controls (Priority: P1)

The owner interacts with the bot in Discord through interactive controls (buttons and select menus) posted by the bot in a Discord channel: view the current watch list with each product's latest status, remove a product, modify an existing entry (for example, relabeling a product), and change the recheck cadence. Adding a new product page — which requires free-text URL entry — happens in the web dashboard (User Story 4). Discord interactions arrive at the bot over its authenticated gateway connection (rev-2 pivot); the exposed port is not involved in Discord input.

**Why this priority**: Management is the only way the owner controls what gets monitored. Together with User Story 1 this forms a complete, usable MVP; without it the bot could only watch a single hardcoded product.

**Independent Test**: Using Discord controls only, view the watch list, modify one entry, and remove another, verifying the bot's confirmation and the resulting list after each step.

**Acceptance Scenarios**:

1. **Given** the owner requests the watch list via a Discord control, **when** the bot processes the interaction, **then** it responds with every monitored product's label, URL, and latest determined stock status.
2. **Given** the owner selects a monitored product and chooses to remove it, **when** the bot processes the interaction, **then** the bot confirms and the product is no longer checked.
3. **Given** the owner modifies an existing product's label via a Discord control, **when** the bot processes the interaction, **then** the bot confirms and the new label is used in subsequent lists and notifications.
4. **Given** a non-authorized Discord user triggers a management control, **when** the bot processes the interaction, **then** the action is rejected and the watch list is unchanged.

---

### User Story 3 - Configure the Recheck Cadence (Priority: P2)

The owner changes how often the bot rechecks product pages (for example, from every 5 minutes to every 30 minutes) using a Discord control (or the web dashboard), and the new cadence takes effect without restarting the bot. The bot enforces a minimum cadence so it does not place excessive load on retailers.

**Why this priority**: The cadence is an explicitly requested operational control (the trade-off between alert speed and load on retailers), but the bot remains fully useful with a sensible default cadence until it is changed.

**Independent Test**: Set the cadence to a different value via a Discord control (or the web dashboard) and observe that subsequent checks occur on the new cadence.

**Acceptance Scenarios**:

1. **Given** the bot is running with a default cadence, **when** the owner sets a new cadence via a Discord control or the web dashboard, **then** the bot confirms and all subsequent checks follow the new cadence.
2. **Given** the owner sets a cadence below the allowed minimum, **when** the bot processes the request, **then** the bot rejects it and explains the minimum.
3. **Given** the owner sets a cadence using a malformed value (for example, non-numeric input), **when** the bot processes the request, **then** the bot rejects it and the current cadence is unchanged.
4. **Given** the owner sets a cadence above the allowed maximum, **when** the bot processes the request, **then** the bot rejects it and explains the maximum.

---

### User Story 4 - Interact With the Bot Over Its Exposed Port (Priority: P3)

The bot is deployed as a container with a single configurable port. That port serves both a lightweight web-based management dashboard (view the watch list and latest statuses, add/remove/modify products, change the cadence) and an HTTP API.

**Why this priority**: An explicitly requested deployment requirement that also provides a browser-based alternative to Discord for day-to-day management; Discord remains the primary channel, so this does not block the core value of the bot.

**Independent Test**: With the container running and the port exposed, open the dashboard in a browser, perform a management operation (such as adding or removing a product), and verify the result matches the bot's watch list as seen via Discord.

**Acceptance Scenarios**:

1. **Given** the container is deployed with a configured port, **when** the user connects to that port, **then** both the web dashboard and the bot's HTTP endpoints are reachable over that single port.
2. **Given** a management operation is performed via the web dashboard, **when** it completes, **then** the result is identical to performing the same operation via Discord (the watch list has a single source of truth shared by all interfaces).
3. **Given** the container is redeployed with a different configured port, **when** the container starts, **then** the dashboard and endpoints are reachable on the new port with no code changes.
4. **Given** the owner submits a valid product URL (with an optional label) through the dashboard's add-product form, **when** the bot processes it, **then** the bot confirms the addition and the product is checked according to the configured cadence.
5. **Given** the owner submits a URL that is not a valid product page address, **when** the bot processes it, **then** the bot rejects it with a clear error and the watch list is unchanged.
6. **Given** the owner submits a URL that is already on the watch list, **when** the bot processes it, **then** the bot reports that the product is already monitored and no duplicate entry is created.

---

### Edge Cases

- **Blocked or protected pages**: What happens when a retailer returns an error page, an anti-bot challenge, or a rate-limit response instead of product content? The check is marked as failed, and the bot MUST NOT interpret such a response as either "in stock" or "out of stock".
- **Retailer layout change**: How does the system handle a product page whose stock indicator no longer matches what the bot expects? The check is marked "unknown" rather than flipping the status, and the owner is informed after repeated failures for the same product.
- **Repeated check failures**: If a product fails to be checked a set number of times in a row (for example, 10 consecutive failures), the owner is notified once that the product appears unreachable; the bot does not repeat this warning on every subsequent failure until the product recovers or is removed.
- **Restock between checks**: The bot detects restocks on the next scheduled check; an alert arrives within one cadence interval of the actual restock, and finer granularity is out of scope.
- **Multiple products restocking at once**: Each product's restock produces its own separate notification; notifications are not deduplicated or batched.
- **Container restart mid-cycle**: The watch list, last-known statuses, and configured cadence survive a restart; no restock notification is re-sent after a restart for a transition that was already notified.
- **Invalid webhook destination**: If the webhook destination is invalid or has been revoked, failed notification delivery is surfaced to the owner rather than silently dropped. A failed delivery is surfaced once as an operational notice and is not retried as a restock notification: the exactly-once guarantee (FR-008) applies to transitions, and the product's status is still updated.
- **Concurrent interface use**: What happens if the owner makes changes via the dashboard and via Discord at the same time? The bot MUST end with a consistent watch list — no lost updates and no duplicate entries.
- **Very large or slow product pages**: A check that exceeds a timeout is treated as a failure, not as a status change.
- **Missing deployment credentials**: If any required deployment credential (Discord bot token, owner id, management channel id, webhook URL, shared secret) is missing, empty, or malformed at startup, the bot MUST fail fast with an error identifying the missing credential and MUST NOT start with silent defaults.
- **Corrupt durable state at startup**: If the durable store cannot be read at startup, the bot MUST start with a fresh state and MUST preserve the unreadable store (for example, move it aside under a new name) so the owner can inspect or restore it; starting fresh MUST NOT overwrite or delete it.
- **Gateway disconnect**: If the Discord gateway connection is lost, the bot MUST re-establish it automatically and re-post the management control message; operational notices that could not be delivered while disconnected MUST be retried on reconnect (at least once) rather than dropped.
- **Management channel unavailable**: If the management channel is deleted or the bot loses access to it, the bot MUST keep checking products and sending restock alerts (the core flow does not depend on the channel); control-message and notice failures are logged, and the control message is re-posted when the channel becomes available again.
- **Bot malfunction visibility**: If the Discord gateway or the management channel is unavailable, the bot MUST continue checking products and sending restock alerts, and MUST expose a public health indicator over the exposed port (exposing no watch-list data or secrets) reporting at minimum whether the Discord gateway is connected and whether a check cycle is in progress.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST allow the owner to add a product page to the watch list by providing the product's URL — a valid product page address is an absolute `http://` or `https://` URL with a non-empty host — with an optional short label, via the web dashboard (free-text URL entry is available only through the dashboard).
- **FR-002**: System MUST allow the owner to remove a product from the watch list via Discord controls or the web dashboard.
- **FR-003**: System MUST allow the owner to view the watch list via Discord controls or the web dashboard, including each product's label, URL, and latest determined stock status.
- **FR-004**: System MUST allow the owner to modify an existing watch list entry (at minimum, its label) via Discord controls or the web dashboard. The product page URL is immutable: changing which page is tracked is a remove-and-add operation, and a re-added product starts from an undetermined status.
- **FR-005**: System MUST check each monitored product page on a configurable cadence, and the owner MUST be able to change the cadence at runtime via Discord controls or the web dashboard without restarting the system.
- **FR-006**: System MUST determine each product's stock status as one of "in stock", "out of stock", or "unknown", based on the fetched product page content. Built-in heuristics for common stock phrases MUST apply by default; per-product configurable indicators (supplied by the owner when adding the product, for example the text or element that indicates in-stock vs. out-of-stock) MUST take precedence over the heuristics for that product when provided. If neither heuristics nor per-product indicators match the fetched page, the result MUST be "unknown".
- **FR-007**: System MUST send a notification to the configured Discord webhook destination when a monitored product transitions from "out of stock" to "in stock", and the notification MUST include the product's label and URL.
- **FR-008**: System MUST send at most one restock notification per out-of-stock-to-in-stock transition and MUST NOT re-notify while a product remains in stock.
- **FR-009**: System MUST persist the watch list, each product's last known stock status, and the configured cadence in a minimal durable store (for example, a single file on a volume mounted into the container), and resume all of them automatically after a restart without user intervention. Storing check history or any more elaborate persistence mechanism is out of scope for v1.
- **FR-010**: System MUST restrict all watch-list and configuration management to an authorized owner user; commands from any other user MUST be rejected.
- **FR-011**: System MUST enforce a minimum check cadence (to avoid excessive load on retailers) and reject cadence values below it with an explanation. The system MUST also enforce a maximum check cadence (a sanity upper bound, assumed 24 hours) and reject cadence values above it with an explanation.
- **FR-012**: System MUST treat failed page fetches (network errors, timeouts, blocked responses, unrecognizable page content) as non-status-changing events: a failed check MUST NOT produce a restock alert and MUST NOT alter the product's last known status.
- **FR-013**: System MUST be deployable as a container image, with the exposed port number configurable at deployment time.
- **FR-014**: System MUST expose a single configurable network port (settable at deployment time) that serves both a lightweight web-based management dashboard and an HTTP API. The dashboard and the API MUST expose all management operations (add, remove, list, modify, cadence).
- **FR-015**: System MUST provide a clear confirmation or error (with a reason) for every management command or operation, whether issued via Discord or via the port interface.
- **FR-016**: System MUST inform the owner (through the management channel) when a monitored product fails to be checked repeatedly (a consecutive-failure threshold) and when the configured webhook destination fails to accept notifications.
- **FR-017**: System MUST maintain a single authenticated Discord gateway connection (persistent, per the Discord Python module's standard mode) through which ALL Discord-originated user input — buttons, select menus, modals — arrives; the exposed port MUST NOT be required to receive Discord input. Each interaction MUST be attributed to the Discord user who triggered it, for owner authorization.
- **FR-018**: System MUST execute checks as a single sequential loop per cadence cycle: on each tick it walks the watch list in order and fetches each product page one at a time, with a short pause between consecutive requests. System MUST NOT run parallel in-flight page fetches, and a full check cycle over the entire watch list MUST complete within one cadence interval under normal network conditions.
- **FR-019**: The web dashboard MUST be delivered as a static front end (a single HTML page with plain JavaScript that calls the HTTP API) served from the same exposed port. It MUST NOT require a JavaScript framework, bundler, or build step, either at runtime or in the container image build.
- **FR-020**: All dashboard and management API operations on the exposed port MUST be gated by a single shared secret (a token supplied at deployment time, for example via an environment variable); requests without a valid secret MUST be rejected without altering state. "Operations" means the management operations enumerated in FR-014; the stateless static dashboard shell (which contains no watch-list data and alters no state) and any public health indicator that exposes no watch-list data or secrets are not operations. Discord-originated interactions arrive over the gateway connection, authenticated by the bot token rather than the shared secret.

### Key Entities

- **Monitored Product**: A product page under watch. Key attributes: product page URL, owner-supplied label, last known stock status, last checked time, consecutive check-failure count, creation time.
- **Watch List**: The collection of monitored products the owner has registered; the single source of truth shared by all interfaces (Discord and the exposed port), persisted in the minimal durable store.
- **Check Schedule**: The cadence (interval) at which product pages are rechecked, plus the enforced minimum interval.
- **Notification Destination**: The Discord webhook destination to which restock alerts are delivered.
- **Check Result**: The outcome of a single check of a single product: determined status (in stock / out of stock / unknown), timestamp, and error detail when the check failed.
- **Owner**: The single authorized Discord identity permitted to manage the watch list and configuration.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A product added via the web dashboard is checked at least once within one cadence interval of its addition.
- **SC-002**: 100% of detected out-of-stock-to-in-stock transitions result in a Discord webhook notification, delivered within 1 minute of the check that detected the transition.
- **SC-003**: Zero false restock notifications are produced by failed checks (blocked pages, errors, timeouts) across a representative test period.
- **SC-004**: A cadence change made via Discord takes effect on the next check cycle, with no restart required, 100% of the time.
- **SC-005**: After a container restart, 100% of the watch list resumes monitoring automatically, with no lost last-known statuses and no duplicate restock notifications.
- **SC-006**: The bot reliably checks a watch list of 25 products at a 5-minute cadence with no missed or overdue checks, and the sequential check cycle completes within one cadence interval, under normal network conditions.
- **SC-007**: 100% of management commands issued by non-owner Discord users are rejected without altering any state.
- **SC-008**: 100% of dashboard or management API requests that do not present a valid shared secret are rejected without altering any state.
- **SC-009**: A restock is detected on the next scheduled check after it occurs — detection latency is at most one cadence interval from the actual restock.

## Assumptions

- **Single owner.** The bot is a personal tool: exactly one Discord user (the owner) can manage the watch list and configuration. Multi-user access and role management are out of scope for v1.
- **Public, server-rendered pages.** Product pages are publicly accessible without login, and the stock status is present in the initial page content. Products whose stock status requires JavaScript rendering, an authenticated session, or that actively block automated access are out of scope for v1.
- **Restock means unavailable-to-available.** Only out-of-stock-to-in-stock transitions trigger notifications. Sold-out alerts (in-to-out), price-change alerts, and low-stock alerts ("only N left") are out of scope for v1.
- **No immediate alert on add.** Adding a product that is already in stock does not produce a notification; the first alert fires on the first unavailable-to-available transition observed while the product is monitored.
- **Global cadence in v1.** A single cadence applies to all monitored products. Per-product cadence overrides are out of scope for v1.
- **Secrets via environment.** The Discord bot token (gateway auth), the dashboard/API shared secret, and the notification destination webhook URL (restock alerts only — control messages are posted over the gateway per rev 2, and no interaction POSTs arrive at the port) are supplied at deployment time (for example, via environment variables); everything else (watch list, cadence, labels) is managed at runtime through the interfaces.
- **Sensible defaults.** The bot ships with a default check cadence (assumed 5 minutes) and a minimum cadence floor (assumed 1 minute) that the owner can change at runtime.
- **Polite fetching.** The bot applies a timeout to page fetches, fetches pages strictly sequentially (one at a time, with a short pause between consecutive requests within a cycle), and does not issue more requests to a retailer than the configured cadence implies.
- **One product per URL.** A watch list entry tracks one product page; multiple variants (sizes, colors) on the same page are treated as a single entry.
- **Notification content.** Restock notifications include the product's label, URL, and the time the restock was detected; no further content (price, images, retailer branding) is required in v1.
- **Stock-detection hybrid (clarified).** Per-product indicators take precedence over built-in heuristics; when neither matches a fetched page, the result is "unknown" and is treated like a failed determination — no change to the last-known status, and repeated unknown results trigger the repeated-failure notification.
- **Single-port interface (clarified, rev 2).** The web dashboard and the HTTP API are served over the same configurable port; no separate port for the API or for health checks is required in v1. Discord input does NOT use the exposed port — it arrives over the bot's outbound gateway connection — so the port only needs to be reachable from the owner's own devices (no public inbound endpoint is required).
- **No check history in v1.** The durable store holds only the current watch list state and configuration; per-check result history is out of scope for v1.
- **Dashboard refresh model.** The dashboard reflects current state on load and after each operation it performs; real-time push or background auto-refresh of the dashboard is out of scope for v1.
- **No availability targets in v1.** The bot is a single personal container: no uptime/availability target, watchdog, or self-healing beyond the gateway-reconnect and channel-unavailable edge cases is required in v1. If the container stops, the owner restarts it; state continuity across restarts is covered by FR-009/SC-005.
- **External platform dependencies.** Discord (gateway and webhook service) is an external dependency; its temporary unavailability MUST degrade the bot per the edge cases (Gateway disconnect, Management channel unavailable, Invalid webhook destination) rather than halt the core check-and-alert flow.
- **Scaffolding resolved at plan time.** The Discord bot scaffolding was resolved during the plan phase from the user's direction and the provided sample bot: Flask web layer for the dashboard and management API; discord.py (2.x) in its standard persistent-gateway mode with interactive components (buttons, select menus, modals); credentials and deployment configuration supplied at deployment time via a `.env` file (python-dotenv).
