"""Unit tests for botalert.state (task T010).

Covers persistence round-trip, atomicity, validation rejection,
corruption recovery (rename, never delete), and the data-model.md §6
concurrency rule (N threads × M read-modify-write, no lost updates).
"""

from __future__ import annotations

import glob
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from botalert.models import CheckResult, DuplicateUrlError, StockStatus, ValidationError
from botalert.state import StateStore
from tests.helpers import make_config


@pytest.fixture()
def state_path(tmp_path: Path) -> str:
    return str(tmp_path / "state.json")


def make_store(path: str) -> StateStore:
    return StateStore(path, make_config())


def write_file(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


class TestFreshLoad:
    def test_missing_file_starts_fresh(self, state_path: str) -> None:
        store = make_store(state_path)
        assert store.get_products() == []
        assert store.get_cadence() == pytest.approx(5.0)
        assert store.get_control_message_id() is None


class TestPersistenceRoundTrip:
    def test_save_reload_round_trip_preserves_order_and_fields(
        self, state_path: str
    ) -> None:
        store = make_store(state_path)
        first = store.add_product("https://a.example.com/one", label="One")
        second = store.add_product(
            "https://b.example.com/two",
            label="Two",
            in_stock_indicator="Buy Now",
            out_of_stock_indicator="Notify Me",
        )
        store.set_cadence(7.5)
        store.set_control_message_id("1234567890")

        reloaded = make_store(state_path)
        products = reloaded.get_products()
        assert [p.id for p in products] == [first.id, second.id]  # insertion order
        assert products[0].url == first.url
        assert products[0].label == "One"
        assert products[0].created_at == first.created_at  # ISO-8601 preserved
        assert products[1].in_stock_indicator == "Buy Now"
        assert products[1].out_of_stock_indicator == "Notify Me"
        assert reloaded.get_cadence() == pytest.approx(7.5)
        assert reloaded.get_control_message_id() == "1234567890"

    def test_file_shape_matches_data_model_section_7(self, state_path: str) -> None:
        make_store(state_path).add_product("https://a.example.com/one")
        with open(state_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        assert data["schema_version"] == 1
        assert data["cadence_minutes"] == pytest.approx(5.0)
        assert data["control_message_id"] is None
        assert set(data["products"][0]) == {
            "id",
            "url",
            "label",
            "in_stock_indicator",
            "out_of_stock_indicator",
            "last_status",
            "last_checked_at",
            "consecutive_failures",
            "failure_notified",
            "created_at",
        }


class TestAtomicity:
    def test_no_tmp_file_left_after_save(self, state_path: str) -> None:
        store = make_store(state_path)
        store.add_product("https://a.example.com/one")
        assert not os.path.exists(state_path + ".tmp")

    def test_torn_save_never_corrupts_target(
        self, state_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = make_store(state_path)
        store.add_product("https://a.example.com/one")
        with open(state_path, "r", encoding="utf-8") as handle:
            original = handle.read()

        def failing_replace(src: str, dst: str) -> None:
            raise OSError("simulated crash before replace")

        # The target is only ever replaced via os.replace: if that fails,
        # the previous file content must survive intact (no torn write).
        monkeypatch.setattr("botalert.state.os.replace", failing_replace)

        with pytest.raises(OSError):
            store.add_product("https://b.example.com/two")
        with open(state_path, "r", encoding="utf-8") as handle:
            assert handle.read() == original

    def test_readers_return_copies_not_internals(self, state_path: str) -> None:
        store = make_store(state_path)
        created = store.add_product("https://a.example.com/one")
        snapshot = store.get_products()[0]
        snapshot.label = "MUTATED"
        snapshot.consecutive_failures = 99
        assert store.get_products()[0].label == "a.example.com"
        assert store.get_products()[0].consecutive_failures == 0
        assert created.label == "a.example.com"


class TestValidationRejection:
    def test_duplicate_normalized_url_rejected(self, state_path: str) -> None:
        store = make_store(state_path)
        store.add_product("https://Shop.Example.com/Item")
        # Any casing of scheme/host → same normalized URL (path kept verbatim).
        with pytest.raises(DuplicateUrlError):
            store.add_product("HTTPS://shop.example.com/Item")
        # A different path is a different product — no silent merge either way.
        added = store.add_product("https://shop.example.com/item")
        assert added.label == "shop.example.com"

    def test_invalid_fields_rejected(self, state_path: str) -> None:
        store = make_store(state_path)
        with pytest.raises(ValidationError):
            store.add_product("not a url")
        with pytest.raises(ValidationError):
            store.add_product("https://a.example.com/x", label="")
        with pytest.raises(ValidationError):
            store.add_product(
                "https://a.example.com/x", in_stock_indicator="i" * 201
            )

    def test_set_cadence_out_of_range_rejected(self, state_path: str) -> None:
        store = make_store(state_path)
        with pytest.raises(ValidationError):
            store.set_cadence(0.5)
        with pytest.raises(ValidationError):
            store.set_cadence(1441)
        assert store.get_cadence() == pytest.approx(5.0)

    def test_modify_unknown_url_returns_none(self, state_path: str) -> None:
        store = make_store(state_path)
        assert store.modify_product("https://missing.example.com/x") is None
        assert store.remove_product("https://missing.example.com/x") is None

    def test_modify_is_atomic_when_a_field_fails(self, state_path: str) -> None:
        """A valid field alongside an invalid one applies NOTHING (the
        update is one critical section; data-model.md §6)."""
        store = make_store(state_path)
        store.add_product(
            "https://a.example.com/x",
            label="Original",
            in_stock_indicator="Old",
        )
        with pytest.raises(ValidationError):
            store.modify_product(
                "https://a.example.com/x",
                label="Changed",
                in_stock_indicator="i" * 201,
            )
        stored = store.get_product("https://a.example.com/x")
        assert stored is not None
        assert stored.label == "Original"  # valid field not partially applied
        assert stored.in_stock_indicator == "Old"


class TestCorruptionRecovery:
    def test_garbage_file_renamed_and_fresh_state(self, state_path: str) -> None:
        write_file(state_path, "{{{{ not json at all")
        store = make_store(state_path)
        assert store.get_products() == []
        assert store.get_cadence() == pytest.approx(5.0)
        offenders = glob.glob(state_path + ".corrupt-*")
        assert len(offenders) == 1
        with open(offenders[0], "r", encoding="utf-8") as handle:
            assert handle.read() == "{{{{ not json at all"
        assert not os.path.exists(state_path)  # renamed, never deleted

    def test_unknown_schema_version_renamed_and_fresh_state(self, state_path: str) -> None:
        write_file(state_path, json.dumps({"schema_version": 99, "products": []}))
        store = make_store(state_path)
        assert store.get_products() == []
        offenders = glob.glob(state_path + ".corrupt-*")
        assert len(offenders) == 1
        assert not os.path.exists(state_path)

    def test_stale_cadence_out_of_range_renamed_and_fresh_state(
        self, state_path: str
    ) -> None:
        write_file(
            state_path,
            json.dumps({"schema_version": 1, "cadence_minutes": 0.1, "products": []}),
        )
        store = make_store(state_path)
        # Never guess: out-of-range persisted cadence → fresh default.
        assert store.get_cadence() == pytest.approx(5.0)
        assert len(glob.glob(state_path + ".corrupt-*")) == 1


class TestApplyCheckStateMachine:
    def _store(self, state_path: str) -> StateStore:
        store = make_store(state_path)
        store.add_product("https://a.example.com/one", label="One")
        return store

    def test_first_determined_check_no_restock(self, state_path: str) -> None:
        store = self._store(state_path)
        pid = store.get_products()[0].id
        effect = store.apply_check(
            CheckResult(pid, StockStatus.IN_STOCK, None, datetime.now(timezone.utc))
        )
        assert effect is not None
        assert effect.restock_transition is False
        assert store.get_products()[0].last_status is StockStatus.IN_STOCK

    def test_out_to_in_is_restock_and_resets_counters(self, state_path: str) -> None:
        store = self._store(state_path)
        pid = store.get_products()[0].id
        now = datetime.now(timezone.utc)
        store.apply_check(CheckResult(pid, StockStatus.OUT_OF_STOCK, None, now))
        # Simulate a failed check (counters climb), then the restock.
        store.apply_check(CheckResult(pid, "error", "HTTP 500", now))
        assert store.get_products()[0].consecutive_failures == 1
        effect = store.apply_check(
            CheckResult(pid, StockStatus.IN_STOCK, None, now)
        )
        assert effect is not None and effect.restock_transition is True
        product = store.get_products()[0]
        assert product.last_status is StockStatus.IN_STOCK
        assert product.consecutive_failures == 0
        assert product.failure_notified is False

    def test_failed_check_increments_and_threshold_notice_once_only(
        self, state_path: str
    ) -> None:
        store = make_store(state_path)
        store.add_product("https://a.example.com/one", label="One")
        pid = store.get_products()[0].id
        now = datetime.now(timezone.utc)
        effects = [
            store.apply_check(CheckResult(pid, "error", "boom", now)) for _ in range(12)
        ]
        assert effects is not None
        notices = [e for e in effects if e is not None and e.threshold_notice]
        assert len(notices) == 1  # exactly one, at the 10th
        assert notices[0].consecutive_failures == 10
        assert notices[0].failure_threshold == 10
        assert store.get_products()[0].failure_notified is True
        assert store.get_products()[0].consecutive_failures == 12
        # Recovery resets both counters.
        store.apply_check(CheckResult(pid, StockStatus.IN_STOCK, None, now))
        assert store.get_products()[0].failure_notified is False

    def test_unknown_determination_is_a_failure(self, state_path: str) -> None:
        store = self._store(state_path)
        pid = store.get_products()[0].id
        store.apply_check(
            CheckResult(pid, StockStatus.OUT_OF_STOCK, None, datetime.now(timezone.utc))
        )
        store.apply_check(
            CheckResult(pid, StockStatus.UNKNOWN, None, datetime.now(timezone.utc))
        )
        product = store.get_products()[0]
        assert product.last_status is StockStatus.OUT_OF_STOCK  # unchanged
        assert product.consecutive_failures == 1

    def test_removed_product_applies_to_none(self, state_path: str) -> None:
        store = self._store(state_path)
        pid = store.get_products()[0].id
        store.remove_product("https://a.example.com/one")
        effect = store.apply_check(
            CheckResult(pid, StockStatus.IN_STOCK, None, datetime.now(timezone.utc))
        )
        assert effect is None


class TestConcurrency:
    def test_no_lost_updates_under_parallel_read_modify_write(
        self, state_path: str
    ) -> None:
        store = make_store(state_path)
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker(thread_id: int) -> None:
            try:
                barrier.wait()
                for i in range(25):
                    store.add_product(f"https://t{thread_id}.example.com/p{i}")
            except BaseException as exc:  # pragma: no cover - failure capture
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        products = store.get_products()
        assert len(products) == 200
        urls = [p.url for p in products]
        assert len(set(urls)) == 200  # no duplicates, no lost updates

    def test_mixed_add_and_remove_consistent(self, state_path: str) -> None:
        store = make_store(state_path)
        for i in range(20):
            store.add_product(f"https://seed.example.com/p{i}")

        errors: list[BaseException] = []

        def adder(thread_id: int) -> None:
            try:
                for i in range(10):
                    store.add_product(f"https://a{thread_id}.example.com/p{i}")
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        def remover(thread_id: int) -> None:
            try:
                # Disjoint slices: the four removers remove all 20 seeds.
                for i in range(5):
                    store.remove_product(
                        f"https://seed.example.com/p{thread_id * 5 + i}"
                    )
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = (
            [threading.Thread(target=adder, args=(n,)) for n in range(4)]
            + [threading.Thread(target=remover, args=(n,)) for n in range(4)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        products = store.get_products()
        urls = [p.url for p in products]
        assert len(set(urls)) == len(urls)
        # All 20 seeds removed, 40 added: 4 adders x 10, no lost updates.
        assert len(products) == 40
