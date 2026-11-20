"""Discord gateway layer tests (US2; tasks T026–T031).

Two levels, both fully offline (no live gateway connection):

1. Router level — the pure ``route()`` function over typed
   ``InteractionRequest`` envelopes: owner authorization is the FIRST
   step (SC-007: non-owner → exact rejection, zero state access),
   ``custom_id`` routing for the US2 surface (view / manage / select /
   remove / relabel), verbatim contract strings (contracts/discord.md),
   ids validated against the current watch list at interaction time.
2. Client level — ``BotalertClient`` driven by ``FakeChannel``
   (tests/fakes.py): control-message lifecycle (``on_ready`` posts +
   stores the id, deletes the prior message, tolerates a missing prior
   message; ``on_message_delete`` self-heal), channel notices (stdout
   fallback pre-ready, flush exactly once on ready, gateway send
   post-ready), and the thin ``on_interaction`` adapter glue.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
import discord.ui
import pytest

from botalert.discord_bot import (
    BotalertClient,
    ButtonSpec,
    InteractionRequest,
    InteractionResponse,
    ModalFieldSpec,
    ModalSpec,
    Router,
    SelectOptionSpec,
    SelectSpec,
)
from botalert.models import CheckResult, RuntimeFlags, StockStatus
from botalert.state import StateStore
from tests.fakes import FakeChannel, FakeClock, SpyStore
from tests.helpers import make_config

OWNER_ID = 111111111111111111  # int of make_config().owner_discord_id
NON_OWNER_ID = 999999999999999999
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_store(tmp_path: Path) -> StateStore:
    return StateStore(str(tmp_path / "state.json"), make_config())


def make_router(tmp_path: Path) -> tuple[Router, StateStore]:
    config = make_config()
    store = StateStore(str(tmp_path / "state.json"), config)
    return Router(config, store, FakeClock(start=NOW)), store


def label_of(store: StateStore, url: str) -> str:
    product = store.get_product(url)
    assert product is not None
    return product.label


def make_client(
    tmp_path: Path,
    base_url: str | None = None,
) -> tuple[BotalertClient, StateStore, FakeChannel, RuntimeFlags]:
    config = make_config(base_url=base_url)
    store = StateStore(str(tmp_path / "state.json"), config)
    flags = RuntimeFlags()
    channel = FakeChannel()
    client = BotalertClient(config, store, flags, channel=channel)
    return client, store, channel, flags


# --------------------------------------------------------------------- auth


@pytest.mark.parametrize(
    ("action", "selected_value", "modal_text"),
    [
        ("ctl.view", None, None),
        ("ctl.manage", None, None),
        ("sel.product", "deadbeef0001", None),
        ("ctl.remove:deadbeef0001", None, None),
        ("ctl.relabel:deadbeef0001", None, None),
        ("modal.relabel:deadbeef0001", None, "x"),
    ],
    ids=["view", "manage", "select", "remove", "relabel", "relabel-submit"],
)
def test_non_owner_rejected_without_state_access(
    tmp_path: Path, action: str, selected_value: str | None, modal_text: str | None
) -> None:
    config = make_config()
    store = StateStore(str(tmp_path / "state.json"), config)
    spy = SpyStore(store)
    router = Router(config, spy, FakeClock(start=NOW))
    response = router.route(
        InteractionRequest(
            user_id=NON_OWNER_ID,
            action=action,
            selected_value=selected_value,
            modal_text=modal_text,
        )
    )
    assert response.content == "❌ Not authorized — only the owner can manage the watch list."
    assert response.ephemeral is True
    assert spy.calls == []  # SC-007: no state read or altered


# ------------------------------------------------------------------ ctl.view


def test_view_empty(tmp_path: Path) -> None:
    router, _store = make_router(tmp_path)
    response = router.route(InteractionRequest(user_id=OWNER_ID, action="ctl.view"))
    assert response.content == "Watch list is empty — add products in the dashboard."
    assert response.ephemeral is True


def test_view_full_format(tmp_path: Path) -> None:
    router, store = make_router(tmp_path)
    p1 = store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    store.apply_check(CheckResult(p1.id, StockStatus.IN_STOCK, None, NOW - timedelta(minutes=2)))
    p2 = store.add_product("https://shop.example.com/boots", label="Boots")
    store.apply_check(CheckResult(p2.id, StockStatus.OUT_OF_STOCK, None, NOW - timedelta(hours=2)))
    store.add_product("https://shop.example.com/hats", label="Hats")  # never checked
    p4 = store.add_product("https://shop.example.com/gloves", label="Gloves")
    # determined out-of-stock, then 3 failed checks; last one 5 h ago
    store.apply_check(CheckResult(p4.id, StockStatus.OUT_OF_STOCK, None, NOW - timedelta(hours=5, minutes=15)))
    store.apply_check(CheckResult(p4.id, "error", "boom", NOW - timedelta(hours=5, minutes=10)))
    store.apply_check(CheckResult(p4.id, "error", "boom", NOW - timedelta(hours=5, minutes=5)))
    store.apply_check(CheckResult(p4.id, "error", "boom", NOW - timedelta(hours=5)))
    response = router.route(InteractionRequest(user_id=OWNER_ID, action="ctl.view"))
    assert response.content == (
        "• Sneakers — 🟢 — https://shop.example.com/sneakers checked 2m ago\n"
        "• Boots — 🔴 — https://shop.example.com/boots checked 2h ago\n"
        "• Hats — ⚪ — https://shop.example.com/hats\n"
        "• Gloves — 🔴 — https://shop.example.com/gloves checked 5h ago ⚠️ 3 consecutive failures"
    )


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=45), "checked 45s ago"),
        (timedelta(minutes=1), "checked 1m ago"),
        (timedelta(minutes=59), "checked 59m ago"),
        (timedelta(hours=1), "checked 1h ago"),
        (timedelta(hours=23), "checked 23h ago"),
        (timedelta(days=1), "checked 1d ago"),
        (timedelta(days=2, hours=1), "checked 2d ago"),
    ],
    ids=["s", "m1", "m", "h1", "h", "d1", "d"],
)
def test_view_relative_time_buckets(
    tmp_path: Path, delta: timedelta, expected: str
) -> None:
    router, store = make_router(tmp_path)
    p = store.add_product("https://shop.example.com/t", label="T")
    store.apply_check(CheckResult(p.id, StockStatus.IN_STOCK, None, NOW - delta))
    response = router.route(InteractionRequest(user_id=OWNER_ID, action="ctl.view"))
    assert response.content == f"• T — 🟢 — https://shop.example.com/t {expected}"


# ---------------------------------------------------------------- ctl.manage


def test_manage_select_options(tmp_path: Path) -> None:
    router, store = make_router(tmp_path)
    p1 = store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    long_label = "A" * 90  # 90 + " — " + 16-char host = 109 > 100 → truncated
    p2 = store.add_product("https://shop.example.com/boots", label=long_label)
    response = router.route(InteractionRequest(user_id=OWNER_ID, action="ctl.manage"))
    assert response.content is None
    assert response.ephemeral is True
    assert response.buttons == ()
    assert response.select is not None
    assert response.select == SelectSpec(
        custom_id="sel.product",
        placeholder="Select a product",
        options=(
            SelectOptionSpec(value=p1.id, label="Sneakers — shop.example.com"),
            SelectOptionSpec(
                value=p2.id, label=(long_label + " — shop.example.com")[:100]
            ),
        ),
    )
    assert len(response.select.options[1].label) == 100


def test_manage_empty(tmp_path: Path) -> None:
    router, _store = make_router(tmp_path)
    response = router.route(InteractionRequest(user_id=OWNER_ID, action="ctl.manage"))
    assert response.content == "Watch list is empty — add products in the dashboard."
    assert response.select is None


# ----------------------------------------------------------------- sel.product


def test_select_product_buttons(tmp_path: Path) -> None:
    router, store = make_router(tmp_path)
    p = store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    response = router.route(
        InteractionRequest(user_id=OWNER_ID, action="sel.product", selected_value=p.id)
    )
    assert response.content is None
    assert response.ephemeral is True
    assert response.select is None
    assert response.buttons == (
        ButtonSpec(label=f"🗑 Remove {p.label}", custom_id=f"ctl.remove:{p.id}"),
        ButtonSpec(label=f"✏️ Relabel {p.label}", custom_id=f"ctl.relabel:{p.id}"),
    )


def test_select_stale_id(tmp_path: Path) -> None:
    router, store = make_router(tmp_path)
    store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    response = router.route(
        InteractionRequest(user_id=OWNER_ID, action="sel.product", selected_value="deadbeef0001")
    )
    assert response.content == "⚠️ That product is no longer on the watch list."


def test_select_missing_value(tmp_path: Path) -> None:
    router, store = make_router(tmp_path)
    store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    response = router.route(InteractionRequest(user_id=OWNER_ID, action="sel.product"))
    assert response.content == "⚠️ That product is no longer on the watch list."


# ---------------------------------------------------------------- ctl.remove


def test_remove_confirms_and_persists(tmp_path: Path) -> None:
    router, store = make_router(tmp_path)
    p1 = store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    p2 = store.add_product("https://shop.example.com/boots", label="Boots")
    response = router.route(
        InteractionRequest(user_id=OWNER_ID, action=f"ctl.remove:{p1.id}")
    )
    assert response.ephemeral is True
    assert response.content == (
        f"✅ Removed {p1.label} ({p1.url}).\nWatch list now has 1 product."
    )
    assert [p.id for p in store.get_products()] == [p2.id]
    # persisted: a fresh store reading the same file sees the removal
    fresh = StateStore(str(tmp_path / "state.json"), make_config())
    assert [p.id for p in fresh.get_products()] == [p2.id]


def test_remove_grammar_zero_products(tmp_path: Path) -> None:
    router, store = make_router(tmp_path)
    p = store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    response = router.route(InteractionRequest(user_id=OWNER_ID, action=f"ctl.remove:{p.id}"))
    assert response.content == (
        f"✅ Removed {p.label} ({p.url}).\nWatch list now has 0 products."
    )


def test_remove_stale_id(tmp_path: Path) -> None:
    router, store = make_router(tmp_path)
    store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    response = router.route(
        InteractionRequest(user_id=OWNER_ID, action="ctl.remove:deadbeef0001")
    )
    assert response.content == "⚠️ That product is no longer on the watch list."
    assert len(store.get_products()) == 1  # nothing removed


# -------------------------------------------------------- ctl.relabel (modal)


def test_relabel_opens_modal(tmp_path: Path) -> None:
    router, store = make_router(tmp_path)
    p = store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    response = router.route(
        InteractionRequest(user_id=OWNER_ID, action=f"ctl.relabel:{p.id}")
    )
    assert response.kind == "modal"
    assert response.content is None
    assert response.modal == ModalSpec(
        custom_id=f"modal.relabel:{p.id}",
        title="Relabel product",
        fields=(
            ModalFieldSpec(
                custom_id="relabel.label",
                label="New label",
                placeholder="New label",
                default=p.label,
                max_length=100,
                required=True,
            ),
        ),
    )


def test_relabel_stale_id(tmp_path: Path) -> None:
    router, _store = make_router(tmp_path)
    response = router.route(
        InteractionRequest(user_id=OWNER_ID, action="ctl.relabel:deadbeef0001")
    )
    assert response.kind == "message"
    assert response.content == "⚠️ That product is no longer on the watch list."
    assert response.modal is None


# ------------------------------------------------------- modal.relabel submit


def test_relabel_submit_trims_and_persists(tmp_path: Path) -> None:
    router, store = make_router(tmp_path)
    p = store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    response = router.route(
        InteractionRequest(
            user_id=OWNER_ID,
            action=f"modal.relabel:{p.id}",
            modal_text="  Fresh Sneakers  ",
        )
    )
    assert response.kind == "message"
    assert response.ephemeral is True
    assert response.content == "✅ Relabeled Sneakers → Fresh Sneakers."
    assert label_of(store, p.url) == "Fresh Sneakers"
    fresh = StateStore(str(tmp_path / "state.json"), make_config())
    assert label_of(fresh, p.url) == "Fresh Sneakers"


@pytest.mark.parametrize("text", ["", "   ", "x" * 101], ids=["empty", "blank", "too-long"])
def test_relabel_submit_invalid_unchanged(tmp_path: Path, text: str) -> None:
    router, store = make_router(tmp_path)
    p = store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    response = router.route(
        InteractionRequest(
            user_id=OWNER_ID, action=f"modal.relabel:{p.id}", modal_text=text
        )
    )
    assert response.content == "⚠️ Label must be 1–100 characters."
    assert label_of(store, p.url) == "Sneakers"  # current label unchanged


def test_relabel_submit_boundary_100_chars(tmp_path: Path) -> None:
    router, store = make_router(tmp_path)
    p = store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    text = "y" * 100
    response = router.route(
        InteractionRequest(
            user_id=OWNER_ID, action=f"modal.relabel:{p.id}", modal_text=text
        )
    )
    assert response.content == f"✅ Relabeled Sneakers → {text}."
    assert label_of(store, p.url) == text


def test_relabel_submit_stale_id(tmp_path: Path) -> None:
    router, _store = make_router(tmp_path)
    response = router.route(
        InteractionRequest(
            user_id=OWNER_ID, action="modal.relabel:deadbeef0001", modal_text="New"
        )
    )
    assert response.content == "⚠️ That product is no longer on the watch list."


# ----------------------------------------------- client: control lifecycle


def test_on_ready_posts_control_message(tmp_path: Path) -> None:
    client, store, channel, flags = make_client(tmp_path)
    asyncio.run(client.on_ready())
    message_id, content, view = channel.last()
    assert content == "🛒 botalert — watch list controls"
    assert view is not None
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(buttons) == len(view.children)
    assert [(b.label, b.custom_id, b.url) for b in buttons] == [
        ("📋 View watch list", "ctl.view", None),
        ("⚙️ Manage products", "ctl.manage", None),
    ]
    assert store.get_control_message_id() == str(message_id)
    assert flags.gateway_connected is True


def test_on_ready_link_button_only_when_base_url_set(tmp_path: Path) -> None:
    client, store, channel, _flags = make_client(
        tmp_path, base_url="http://127.0.0.1:8080"
    )
    asyncio.run(client.on_ready())
    view = channel.last()[2]
    assert view is not None
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(buttons) == len(view.children)
    assert [(b.label, b.custom_id, b.url) for b in buttons] == [
        ("📋 View watch list", "ctl.view", None),
        ("⚙️ Manage products", "ctl.manage", None),
        ("➕ Add product → dashboard", None, "http://127.0.0.1:8080"),
    ]


def test_on_ready_deletes_previous_control_message(tmp_path: Path) -> None:
    client, store, channel, _flags = make_client(tmp_path)
    channel.seed(555)
    store.set_control_message_id("555")
    asyncio.run(client.on_ready())
    assert channel.messages[555].deleted is True
    assert len(channel.sent) == 1  # only the fresh post
    new_id = store.get_control_message_id()
    assert new_id is not None and new_id != "555"
    assert new_id == str(channel.last()[0])


def test_on_ready_tolerates_missing_previous_message(tmp_path: Path) -> None:
    client, store, channel, _flags = make_client(tmp_path)
    store.set_control_message_id("999999")  # not present in the channel
    asyncio.run(client.on_ready())  # must not raise
    assert len(channel.sent) == 1
    assert store.get_control_message_id() == str(channel.last()[0])


def test_deleted_control_message_is_reposted(tmp_path: Path) -> None:
    client, store, channel, _flags = make_client(tmp_path)
    asyncio.run(client.on_ready())
    stored_str = store.get_control_message_id()
    assert stored_str is not None
    stored = int(stored_str)
    before = len(channel.sent)
    asyncio.run(client._on_control_message_maybe_deleted(stored))
    assert len(channel.sent) == before + 1
    new_id, content, view = channel.last()
    assert content == "🛒 botalert — watch list controls"
    assert view is not None
    assert store.get_control_message_id() == str(new_id)
    assert new_id != stored


def test_unrelated_message_delete_is_ignored(tmp_path: Path) -> None:
    client, store, channel, _flags = make_client(tmp_path)
    asyncio.run(client.on_ready())
    stored_str = store.get_control_message_id()
    assert stored_str is not None
    stored = int(stored_str)
    before = len(channel.sent)
    asyncio.run(client._on_control_message_maybe_deleted(stored + 1))
    assert len(channel.sent) == before
    assert store.get_control_message_id() == str(stored)


# ------------------------------------------------- client: channel notices


def test_notice_before_ready_goes_to_stdout_and_is_flushed_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client, _store, channel, _flags = make_client(tmp_path)
    client.enqueue_notice("first check failed")
    out = capsys.readouterr().out
    assert "[botalert channel notice] first check failed" in out
    assert channel.sent == []  # nothing posted pre-ready
    asyncio.run(client.on_ready())
    notices = [content for (_id, content, view) in channel.sent if view is None]
    assert notices == ["first check failed"]
    # buffer cleared: a second on_ready must not re-post it
    before = len(channel.sent)
    asyncio.run(client.on_ready())
    notices = [content for (_id, content, view) in channel.sent[before:] if view is None]
    assert notices == []


def test_notice_after_ready_goes_through_gateway(tmp_path: Path) -> None:
    client, _store, channel, _flags = make_client(tmp_path)

    async def scenario() -> None:
        await client.on_ready()
        client.enqueue_notice("after ready")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    notices = [content for (_id, content, view) in channel.sent if view is None]
    assert notices == ["after ready"]


def test_on_disconnect_clears_flag(tmp_path: Path) -> None:
    client, _store, _channel, flags = make_client(tmp_path)
    asyncio.run(client.on_ready())
    assert flags.gateway_connected is True
    client.on_disconnect()
    assert flags.gateway_connected is False


# --------------------------------------------- client: on_interaction glue


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeInteractionResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str | None, bool, discord.ui.View | None]] = []
        self.modals: list[discord.ui.Modal] = []

    async def send_message(
        self,
        *,
        content: str | None,
        ephemeral: bool,
        view: discord.ui.View | None = None,
    ) -> object:
        self.messages.append((content, ephemeral, view))
        return None

    async def send_modal(self, modal: discord.ui.Modal, /) -> object:
        self.modals.append(modal)
        return None


class FakeInteraction:
    def __init__(
        self,
        user_id: int,
        data: dict[str, object],
        interaction_type: discord.InteractionType = discord.InteractionType.component,
    ) -> None:
        self.type = interaction_type
        self.data = data
        self.response = FakeInteractionResponse()
        self._user = FakeUser(user_id)

    @property
    def user(self) -> FakeUser:
        return self._user


def test_on_interaction_routes_component_and_applies(tmp_path: Path) -> None:
    client, store, _channel, _flags = make_client(tmp_path)
    p = store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    interaction = FakeInteraction(OWNER_ID, {"custom_id": "ctl.view"})
    asyncio.run(client.on_interaction(interaction))
    assert interaction.response.messages == [
        (f"• {p.label} — ⚪ — {p.url}", True, None)
    ]


def test_on_interaction_select_routes_selected_value(tmp_path: Path) -> None:
    client, store, _channel, _flags = make_client(tmp_path)
    p = store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    interaction = FakeInteraction(
        OWNER_ID, {"custom_id": "sel.product", "values": [p.id]}
    )
    asyncio.run(client.on_interaction(interaction))
    assert len(interaction.response.messages) == 1
    content, ephemeral, view = interaction.response.messages[0]
    assert content is None
    assert ephemeral is True
    assert view is not None
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(buttons) == len(view.children)
    assert [(b.label, b.custom_id) for b in buttons] == [
        (f"🗑 Remove {p.label}", f"ctl.remove:{p.id}"),
        (f"✏️ Relabel {p.label}", f"ctl.relabel:{p.id}"),
    ]


def test_on_interaction_modal_response_sends_modal(tmp_path: Path) -> None:
    client, store, _channel, _flags = make_client(tmp_path)
    p = store.add_product("https://shop.example.com/sneakers", label="Sneakers")
    interaction = FakeInteraction(OWNER_ID, {"custom_id": f"ctl.relabel:{p.id}"})
    asyncio.run(client.on_interaction(interaction))
    assert interaction.response.messages == []
    assert len(interaction.response.modals) == 1
    modal = interaction.response.modals[0]
    assert modal.custom_id == f"modal.relabel:{p.id}"
    assert modal.title == "Relabel product"
    assert len(modal.children) == 1
    item = modal.children[0]
    assert isinstance(item, discord.ui.TextInput)
    assert item.custom_id == "relabel.label"
    assert item.label == "New label"
    assert item.placeholder == "New label"
    assert item.default == p.label
    assert item.max_length == 100
    assert item.required is True
    assert item.style is discord.TextStyle.short


def test_on_interaction_non_owner_rejected(tmp_path: Path) -> None:
    client, _store, _channel, _flags = make_client(tmp_path)
    interaction = FakeInteraction(NON_OWNER_ID, {"custom_id": "ctl.view"})
    asyncio.run(client.on_interaction(interaction))
    assert interaction.response.messages == [
        ("❌ Not authorized — only the owner can manage the watch list.", True, None)
    ]


def test_on_interaction_ignores_non_component(tmp_path: Path) -> None:
    client, _store, _channel, _flags = make_client(tmp_path)
    interaction = FakeInteraction(
        OWNER_ID, {}, interaction_type=discord.InteractionType.modal_submit
    )
    asyncio.run(client.on_interaction(interaction))
    assert interaction.response.messages == []
    assert interaction.response.modals == []


# ------------------------------------------------- client: spec → ui build


def test_build_view_maps_specs(tmp_path: Path) -> None:
    client, _store, _channel, _flags = make_client(tmp_path)
    response = InteractionResponse(
        kind="message",
        content="x",
        buttons=(
            ButtonSpec(label="🗑 Remove A", custom_id="ctl.remove:abc123def456"),
            ButtonSpec(label="➕ Add product → dashboard", url="http://127.0.0.1:8080"),
        ),
        select=SelectSpec(
            custom_id="sel.product",
            placeholder="Select a product",
            options=(
                SelectOptionSpec(value="abc123def456", label="A — shop.example.com"),
            ),
        ),
    )
    view = client._build_view(response)
    assert view is not None
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    selects = [c for c in view.children if isinstance(c, discord.ui.Select)]
    assert [(b.label, b.custom_id, b.url) for b in buttons] == [
        ("🗑 Remove A", "ctl.remove:abc123def456", None),
        ("➕ Add product → dashboard", None, "http://127.0.0.1:8080"),
    ]
    assert len(selects) == 1
    select = selects[0]
    assert select.custom_id == "sel.product"
    assert select.placeholder == "Select a product"
    assert [(o.value, o.label) for o in select.options] == [
        ("abc123def456", "A — shop.example.com")
    ]


def test_build_view_none_when_no_components(tmp_path: Path) -> None:
    client, _store, _channel, _flags = make_client(tmp_path)
    response = InteractionResponse(kind="message", content="plain text")
    assert client._build_view(response) is None


def test_build_modal_from_spec(tmp_path: Path) -> None:
    client, _store, _channel, _flags = make_client(tmp_path)
    spec = ModalSpec(
        custom_id="modal.relabel:abc123def456",
        title="Relabel product",
        fields=(
            ModalFieldSpec(
                custom_id="relabel.label",
                label="New label",
                placeholder="New label",
                default="Sneakers",
                max_length=100,
                required=True,
            ),
        ),
    )
    modal = client._build_modal(spec)
    assert modal.custom_id == spec.custom_id
    assert modal.title == spec.title
    assert len(modal.children) == 1
    item = modal.children[0]
    assert isinstance(item, discord.ui.TextInput)
    assert item.custom_id == "relabel.label"
    assert item.label == "New label"
    assert item.placeholder == "New label"
    assert item.default == "Sneakers"
    assert item.max_length == 100
    assert item.required is True
    assert item.style is discord.TextStyle.short
