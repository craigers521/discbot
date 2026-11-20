"""Discord gateway layer: control surface, interaction router, notices.

Implements the contracts/discord.md behavior on top of discord.py 2.7.x:

- one persistent control message in the management channel (posted on
  ready, prior message deleted, self-healed when deleted);
- an owner-only interaction router (:class:`Router`) over typed
  request/response envelopes — a pure, synchronous, fully offline core;
- thin event glue mapping Discord component interactions and modal
  submits onto the router, and router responses onto ephemeral replies;
- channel notices (restocks / check failures) sent over the gateway
  post-ready, mirrored to stdout and buffered until ``on_ready``.

No network I/O happens at import time; ``__main__`` drives the client
via ``run(token)`` on a daemon thread.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Final, Literal, Mapping, Protocol, cast, overload

import discord

from botalert.config import Config
from botalert.models import (
    Product,
    RuntimeFlags,
    StockStatus,
    ValidationError,
    host_of,
    validate_label,
)
from botalert.state import UNSET, StateStore, UnsetType

logger = logging.getLogger(__name__)

# Verbatim contract strings (contracts/discord.md) — do not reword.
CONTROL_CONTENT = "🛒 botalert — watch list controls"
NOT_AUTHORIZED = "❌ Not authorized — only the owner can manage the watch list."
EMPTY_LIST = "Watch list is empty — add products in the dashboard."
STALE_PRODUCT = "⚠️ That product is no longer on the watch list."
BAD_LABEL = "⚠️ Label must be 1–100 characters."
UNKNOWN_CONTROL = "⚠️ Unknown control — nothing was changed."
# {min}/{max} come from the config limits, {current} from the live store.
BAD_CADENCE = (
    "⚠️ Cadence must be a number of minutes between {min} and {max}. "
    "(current: {current})"
)
CADENCE_CONFIRM = "✅ new cadence {value} minutes"

_STATUS_EMOJI: Final[Mapping[StockStatus, str]] = {
    StockStatus.IN_STOCK: "🟢",
    StockStatus.OUT_OF_STOCK: "🔴",
    StockStatus.UNKNOWN: "⚪",
}


def _relative_ago(now: datetime, then: datetime) -> str:
    """Bucketed age for the watch-list view line (``45s``/``2m``/``5h``/``3d``)."""
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# ------------------------------------------------------------------ clock


class RouterClock(Protocol):
    """Wall-clock seam for the router (tests use FakeClock)."""

    def now(self) -> datetime: ...


class _UtcNowClock:
    """Production clock: UTC wall time."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------- envelopes


@dataclass(frozen=True)
class InteractionRequest:
    """One owner-facing control interaction, normalized for the router."""

    user_id: int
    action: str
    selected_value: str | None = None
    modal_text: str | None = None


@dataclass(frozen=True)
class ButtonSpec:
    """A reply button: a callback (custom_id) or a link (url)."""

    label: str
    custom_id: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class SelectOptionSpec:
    """One dropdown option (value is the product id)."""

    value: str
    label: str


@dataclass(frozen=True)
class SelectSpec:
    """A dropdown of watch-list products."""

    custom_id: str
    placeholder: str
    options: tuple[SelectOptionSpec, ...] = ()


@dataclass(frozen=True)
class ModalFieldSpec:
    """One text-input field of a reply modal."""

    custom_id: str
    label: str
    placeholder: str
    default: str | None
    max_length: int
    required: bool


@dataclass(frozen=True)
class ModalSpec:
    """A reply modal (US2: relabel; US3: cadence reuses this shape)."""

    custom_id: str
    title: str
    fields: tuple[ModalFieldSpec, ...]


@dataclass(frozen=True)
class InteractionResponse:
    """What the router wants the client to send back (always ephemeral)."""

    kind: Literal["message", "modal"]
    content: str | None = None
    ephemeral: bool = True
    buttons: tuple[ButtonSpec, ...] = ()
    select: SelectSpec | None = None
    modal: ModalSpec | None = None


# -------------------------------------------------------------- store port


class StorePort(Protocol):
    """The slice of StateStore the router touches.

    Structural, so tests can route against a SpyStore; StateStore
    satisfies it directly.
    """

    def get_products(self) -> list[Product]: ...

    def remove_product(self, url: str) -> Product | None: ...

    def modify_product(
        self,
        url: str,
        label: str | UnsetType = UNSET,
        in_stock_indicator: str | UnsetType = UNSET,
        out_of_stock_indicator: str | UnsetType = UNSET,
    ) -> Product | None: ...

    def get_cadence(self) -> float: ...

    def set_cadence(self, minutes: float) -> None: ...

    def get_control_message_id(self) -> str | None: ...

    def set_control_message_id(self, message_id: str | None) -> None: ...


# --------------------------------------------------- cadence helpers


def _format_minutes(value: float) -> str:
    """Render minutes without a spurious decimal point (5.0 -> ``"5"``)."""
    if value == int(value):
        return str(int(value))
    return str(value)


_DECIMAL_MINUTES = re.compile(r"\d+(\.\d+)?")


def _parse_cadence_minutes(raw: str) -> float | None:
    """Strictly parse a plain, non-negative decimal number of minutes.

    Only a bare decimal is accepted (``"2"``, ``"2.5"``) — no exponent
    (``"1e2"``), no separators (``"5,5"``), no surrounding garbage. The
    pattern also guarantees the result is finite. Returns ``None`` when
    unparseable.
    """
    text = raw.strip()
    if not _DECIMAL_MINUTES.fullmatch(text):
        return None
    return float(text)


# ------------------------------------------------------------------ router


class Router:
    """Pure synchronous owner-only router: request envelope -> response.

    Authorization is the FIRST step (SC-007): a non-owner gets the exact
    rejection string with zero store access. Product ids from buttons,
    dropdowns and modals are validated against the current watch list at
    interaction time — stale ids get the stale-product notice.
    """

    def __init__(
        self,
        config: Config,
        store: StorePort,
        clock: RouterClock,
        wakeup: Callable[[], None] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._clock = clock
        # Scheduler wakeup hook (SC-004): a valid cadence change ends the
        # current wait so the new interval applies from the next cycle.
        # None (tests without a scheduler) → no-op.
        self._wakeup = wakeup if wakeup is not None else (lambda: None)

    def route(self, request: InteractionRequest) -> InteractionResponse:
        if str(request.user_id) != self._config.owner_discord_id:
            return InteractionResponse(kind="message", content=NOT_AUTHORIZED)
        action = request.action
        if action == "ctl.view":
            return self._route_view()
        if action == "ctl.manage":
            return self._route_manage()
        if action == "sel.product":
            return self._route_select(request.selected_value)
        if action.startswith("ctl.remove:"):
            return self._route_remove(action.split(":", 1)[1])
        if action.startswith("ctl.relabel:"):
            return self._route_relabel_button(action.split(":", 1)[1])
        if action.startswith("modal.relabel:"):
            return self._route_relabel_submit(action.split(":", 1)[1], request.modal_text)
        if action == "ctl.cadence":
            return self._route_cadence_button()
        if action == "modal.cadence":
            return self._route_cadence_submit(request.modal_text)
        return InteractionResponse(kind="message", content=UNKNOWN_CONTROL)

    # ------------------------------------------------------------ handlers

    def _route_view(self) -> InteractionResponse:
        products = self._store.get_products()
        if not products:
            return InteractionResponse(kind="message", content=EMPTY_LIST)
        now = self._clock.now()
        lines = [self._view_line(product, now) for product in products]
        return InteractionResponse(kind="message", content="\n".join(lines))

    def _route_manage(self) -> InteractionResponse:
        products = self._store.get_products()
        if not products:
            return InteractionResponse(kind="message", content=EMPTY_LIST)
        options = tuple(
            SelectOptionSpec(
                value=product.id,
                label=f"{product.label} — {host_of(product.url)}"[:100],
            )
            for product in products
        )
        select = SelectSpec(
            custom_id="sel.product",
            placeholder="Select a product",
            options=options,
        )
        return InteractionResponse(kind="message", content=None, select=select)

    def _route_select(self, selected_value: str | None) -> InteractionResponse:
        product = self._find_by_id(selected_value)
        if product is None:
            return InteractionResponse(kind="message", content=STALE_PRODUCT)
        buttons = (
            ButtonSpec(
                label=f"🗑 Remove {product.label}",
                custom_id=f"ctl.remove:{product.id}",
            ),
            ButtonSpec(
                label=f"✏️ Relabel {product.label}",
                custom_id=f"ctl.relabel:{product.id}",
            ),
        )
        return InteractionResponse(kind="message", content=None, buttons=buttons)

    def _route_remove(self, product_id: str) -> InteractionResponse:
        product = self._find_by_id(product_id)
        if product is None:
            return InteractionResponse(kind="message", content=STALE_PRODUCT)
        removed = self._store.remove_product(product.url) or product
        remaining = len(self._store.get_products())
        noun = "product" if remaining == 1 else "products"
        return InteractionResponse(
            kind="message",
            content=(
                f"✅ Removed {removed.label} ({removed.url}).\n"
                f"Watch list now has {remaining} {noun}."
            ),
        )

    def _route_relabel_button(self, product_id: str) -> InteractionResponse:
        product = self._find_by_id(product_id)
        if product is None:
            return InteractionResponse(kind="message", content=STALE_PRODUCT)
        modal = ModalSpec(
            custom_id=f"modal.relabel:{product.id}",
            title="Relabel product",
            fields=(
                ModalFieldSpec(
                    custom_id="relabel.label",
                    label="New label",
                    placeholder="New label",
                    default=product.label,
                    max_length=100,
                    required=True,
                ),
            ),
        )
        return InteractionResponse(kind="modal", modal=modal)

    def _route_relabel_submit(
        self, product_id: str, modal_text: str | None
    ) -> InteractionResponse:
        product = self._find_by_id(product_id)
        if product is None:
            return InteractionResponse(kind="message", content=STALE_PRODUCT)
        try:
            # Reuses models' single label constraint (trim + 1–100 chars).
            new_label = validate_label(modal_text or "")
        except ValidationError:
            return InteractionResponse(kind="message", content=BAD_LABEL)
        modified = self._store.modify_product(product.url, label=new_label)
        if modified is None:
            return InteractionResponse(kind="message", content=STALE_PRODUCT)
        return InteractionResponse(
            kind="message",
            content=f"✅ Relabeled {product.label} → {modified.label}.",
        )

    def _route_cadence_button(self) -> InteractionResponse:
        modal = ModalSpec(
            custom_id="modal.cadence",
            title="Change cadence",
            fields=(
                ModalFieldSpec(
                    custom_id="cadence.minutes",
                    label="Minutes",
                    placeholder="5",
                    default=_format_minutes(self._store.get_cadence()),
                    max_length=10,
                    required=True,
                ),
            ),
        )
        return InteractionResponse(kind="modal", modal=modal)

    def _route_cadence_submit(self, modal_text: str | None) -> InteractionResponse:
        value = _parse_cadence_minutes(modal_text or "")
        if (
            value is None
            or value < self._config.min_cadence_minutes
            or value > self._config.max_cadence_minutes
        ):
            # Store untouched, nothing to reschedule (SC-004).
            return InteractionResponse(
                kind="message",
                content=BAD_CADENCE.format(
                    min=_format_minutes(self._config.min_cadence_minutes),
                    max=_format_minutes(self._config.max_cadence_minutes),
                    current=_format_minutes(self._store.get_cadence()),
                ),
            )
        self._store.set_cadence(value)
        self._wakeup()
        return InteractionResponse(
            kind="message",
            content=CADENCE_CONFIRM.format(value=_format_minutes(value)),
        )

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _view_line(product: Product, now: datetime) -> str:
        line = f"• {product.label} — {_STATUS_EMOJI[product.last_status]} — {product.url}"
        if product.last_checked_at is not None:
            line += f" checked {_relative_ago(now, product.last_checked_at)} ago"
        if product.consecutive_failures > 0:
            line += f" ⚠️ {product.consecutive_failures} consecutive failures"
        return line

    def _find_by_id(self, product_id: str | None) -> Product | None:
        if product_id is None:
            return None
        for product in self._store.get_products():
            if product.id == product_id:
                return product
        return None


# ------------------------------------------------- ports (offline seams)


class UserPort(Protocol):
    @property
    def id(self) -> int: ...


class InteractionResponsePort(Protocol):
    """The reply surface: ephemeral messages (optionally with a view)."""

    @overload
    async def send_message(
        self, *, content: str | None, ephemeral: bool
    ) -> object: ...

    @overload
    async def send_message(
        self, *, content: str | None, ephemeral: bool, view: discord.ui.View
    ) -> object: ...

    async def send_modal(self, modal: discord.ui.Modal, /) -> object: ...


class InteractionPort(Protocol):
    """A component interaction or a modal submit, whichever the glue sees."""

    type: discord.InteractionType

    @property
    def data(self) -> object: ...

    @property
    def user(self) -> UserPort: ...

    @property
    def response(self) -> InteractionResponsePort: ...


class MessagePort(Protocol):
    @property
    def message_id(self) -> int: ...

    async def delete(self) -> None: ...


class ChannelPort(Protocol):
    """The management channel, as seen by the client (offline-seamable)."""

    async def send(self, content: str, view: discord.ui.View | None) -> MessagePort: ...

    async def get_message(self, message_id: int) -> MessagePort | None: ...


# ------------------------------------------------------------ adapters


class DiscordMessage:
    """ChannelPort message backed by a discord.Message."""

    def __init__(self, message: discord.Message) -> None:
        self._message = message
        self.message_id: int = message.id

    async def delete(self) -> None:
        await self._message.delete()


class DiscordChannel:
    """ChannelPort adapter over discord.abc.Messageable."""

    def __init__(self, channel: discord.abc.Messageable) -> None:
        self._channel = channel

    async def send(self, content: str, view: discord.ui.View | None) -> MessagePort:
        if view is None:
            message = await self._channel.send(content)
        else:
            message = await self._channel.send(content, view=view)
        return DiscordMessage(message)

    async def get_message(self, message_id: int) -> MessagePort | None:
        try:
            message = await self._channel.fetch_message(message_id)
        except discord.NotFound:
            return None
        return DiscordMessage(message)


# ------------------------------------------------------------------ client


class BotalertClient(discord.Client):
    """discord.py client hosting the control surface and channel notices.

    The interaction brain is :class:`Router`; this class is glue — it
    adapts Discord events to request envelopes and router responses to
    ephemeral replies, owns the control-message lifecycle, and delivers
    channel notices (gateway when connected; stdout fallback plus a
    buffer flushed exactly once by ``on_ready`` otherwise).
    """

    def __init__(
        self,
        config: Config,
        store: StateStore,
        flags: RuntimeFlags,
        channel: ChannelPort | None = None,
        wakeup: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(intents=discord.Intents.default())
        self._config = config
        self._store = store
        self._flags = flags
        self._channel = channel
        self._router = Router(config, store, _UtcNowClock(), wakeup=wakeup)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._buffered_notices: list[str] = []

    # ------------------------------------------------------ lifecycle

    async def on_ready(self) -> None:
        # discord.Client.loop is a sentinel until the gateway connects;
        # capture the running loop for cross-thread notice dispatch.
        self._loop = asyncio.get_running_loop()
        if self._channel is None:
            self._channel = self._resolve_manage_channel()
        await self._post_control_message()
        self._flags.gateway_connected = True
        if self._buffered_notices:
            # Flush exactly once: swap the buffer first so notices that
            # arrive during the flush are not re-posted by a re-run.
            pending, self._buffered_notices = self._buffered_notices, []
            for notice in pending:
                try:
                    await self._send_channel_message(notice)
                except Exception:
                    logger.exception(
                        "botalert: failed to flush buffered channel notice"
                    )

    def on_disconnect(self) -> None:
        self._flags.gateway_connected = False

    def stop(self) -> None:
        """Schedule the client's async close on the bot loop (from __main__)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.close(), loop)
        except RuntimeError:
            pass

    def _resolve_manage_channel(self) -> ChannelPort | None:
        try:
            channel_id = int(self._config.discord_manage_channel_id)
        except ValueError:
            logger.warning(
                "botalert: DISCORD_MANAGE_CHANNEL_ID %r is not an int",
                self._config.discord_manage_channel_id,
            )
            return None
        fetched = self.get_channel(channel_id)
        if isinstance(fetched, discord.abc.Messageable):
            return DiscordChannel(fetched)
        return None

    # ------------------------------------------------- control message

    async def _post_control_message(self) -> None:
        channel = self._channel
        if channel is None:
            return
        stored = self._store.get_control_message_id()
        if stored is not None:
            try:
                prior_id = int(stored)
            except ValueError:
                prior_id = None
            if prior_id is not None:
                await self._delete_prior_control_message(prior_id)
        message = await channel.send(CONTROL_CONTENT, self._control_view())
        self._store.set_control_message_id(str(message.message_id))

    async def _delete_prior_control_message(self, message_id: int) -> None:
        channel = self._channel
        if channel is None:
            return
        try:
            prior = await channel.get_message(message_id)
        except (ValueError, discord.HTTPException):
            return  # a missing prior message is tolerated
        if prior is not None:
            try:
                await prior.delete()
            except discord.HTTPException:
                pass  # it vanished between fetch and delete

    def on_message_delete(self, message: discord.Message) -> None:
        """Self-heal: re-post the control message if it was deleted.

        Snowflake ids are globally unique, so no channel-identity check.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.create_task(self._on_control_message_maybe_deleted(message.id))

    async def _on_control_message_maybe_deleted(self, message_id: int) -> None:
        stored = self._store.get_control_message_id()
        if stored is None:
            return
        try:
            stored_id = int(stored)
        except ValueError:
            return
        if stored_id != message_id:
            return
        await self._post_control_message()

    def _control_view(self) -> discord.ui.View:
        buttons: list[ButtonSpec] = [
            ButtonSpec(label="📋 View watch list", custom_id="ctl.view"),
            ButtonSpec(label="⚙️ Manage products", custom_id="ctl.manage"),
            ButtonSpec(label="🔁 Change cadence", custom_id="ctl.cadence"),
        ]
        if self._config.base_url is not None:
            # Link button last: the dashboard is the "add" path (US1).
            buttons.append(
                ButtonSpec(label="➕ Add product → dashboard", url=self._config.base_url)
            )
        view = self._build_view(
            InteractionResponse(kind="message", buttons=tuple(buttons))
        )
        if view is None:
            # Unreachable (buttons above are always present); keep total.
            return discord.ui.View(timeout=None)
        return view

    # ---------------------------------------------------- channel notices

    def enqueue_notice(self, text: str) -> None:
        """Deliver an operational notice to the management channel.

        Pre-ready (or channel unknown): mirror to stdout and buffer; the
        buffer is flushed exactly once by ``on_ready``. Post-ready:
        dispatch to the gateway on the bot loop.
        """
        if (
            self._loop is None
            or self._channel is None
            or not self._flags.gateway_connected
        ):
            print(f"[botalert channel notice] {text}", flush=True)
            self._buffered_notices.append(text)
            return
        asyncio.run_coroutine_threadsafe(self._send_channel_message(text), self._loop)

    async def _send_channel_message(self, text: str) -> None:
        channel = self._channel
        if channel is None:
            return
        await channel.send(text, None)

    # ------------------------------------------------------ interaction

    async def on_interaction(self, interaction: InteractionPort) -> None:
        # Modal submits (type 5) are relayed by SpecModal.on_submit;
        # this handler only acts on component interactions (type 3).
        if interaction.type is not discord.InteractionType.component:
            return
        data = cast(Mapping[str, object], interaction.data)
        raw_custom_id = data.get("custom_id")
        action = raw_custom_id if isinstance(raw_custom_id, str) else ""
        selected_value: str | None = None
        raw_values = data.get("values")
        if isinstance(raw_values, list):
            for value in raw_values:
                if isinstance(value, str):
                    selected_value = value
                    break
        request = InteractionRequest(
            user_id=interaction.user.id,
            action=action,
            selected_value=selected_value,
        )
        response = self._router.route(request)
        await self._apply_response(interaction, response)

    async def handle_modal_submit(
        self, interaction: InteractionPort, action: str, text: str
    ) -> None:
        request = InteractionRequest(
            user_id=interaction.user.id,
            action=action,
            modal_text=text,
        )
        response = self._router.route(request)
        await self._apply_response(interaction, response)

    async def _apply_response(
        self, interaction: InteractionPort, response: InteractionResponse
    ) -> None:
        if response.kind == "modal" and response.modal is not None:
            await interaction.response.send_modal(self._build_modal(response.modal))
            return
        view = self._build_view(response)
        if view is None:
            await interaction.response.send_message(
                content=response.content, ephemeral=response.ephemeral
            )
        else:
            await interaction.response.send_message(
                content=response.content, ephemeral=response.ephemeral, view=view
            )

    # -------------------------------------------------------- ui builders

    def _build_view(self, response: InteractionResponse) -> discord.ui.View | None:
        if not response.buttons and response.select is None:
            return None
        view = discord.ui.View(timeout=None)
        for button in response.buttons:
            if button.url is not None:
                view.add_item(discord.ui.Button(label=button.label, url=button.url))
            else:
                view.add_item(
                    discord.ui.Button(label=button.label, custom_id=button.custom_id)
                )
        if response.select is not None:
            options = [
                discord.SelectOption(value=option.value, label=option.label)
                for option in response.select.options
            ]
            view.add_item(
                discord.ui.Select(
                    placeholder=response.select.placeholder,
                    custom_id=response.select.custom_id,
                    options=options,
                )
            )
        return view

    def _build_modal(self, spec: ModalSpec) -> SpecModal:
        return SpecModal(client=self, spec=spec)


# ------------------------------------------------------------------ modal


class SpecModal(discord.ui.Modal):
    """Modal built from a ModalSpec; relays its submission to the client.

    Named generically (not RelabelModal) because the US3 cadence modal
    reuses the same build/relay path.
    """

    def __init__(self, *, client: BotalertClient, spec: ModalSpec) -> None:
        super().__init__(title=spec.title, custom_id=spec.custom_id)
        self._client = client
        self._custom_id = spec.custom_id
        for field in spec.fields:
            self.add_item(
                discord.ui.TextInput(
                    label=field.label,
                    style=discord.TextStyle.short,
                    custom_id=field.custom_id,
                    placeholder=field.placeholder,
                    default=field.default,
                    required=field.required,
                    max_length=field.max_length,
                )
            )

    async def on_submit(self, interaction: InteractionPort, /) -> None:
        values = [
            item.value
            for item in self.children
            if isinstance(item, discord.ui.TextInput)
            and item.value is not None
        ]
        text = values[0] if values else ""
        await self._client.handle_modal_submit(interaction, self._custom_id, text)
