import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from homeassistant.core import CoreState

from custom_components.yandex_station.core.cache import (
    SCENARIO_STATE_CONFIRMED,
    SCENARIO_STATE_PENDING,
    CacheManager,
    StationSnapshot,
)


class FakeHass:
    """Minimal HomeAssistant double for homeassistant.helpers.storage.Store."""

    def __init__(self, config_dir: str):
        self.config = SimpleNamespace(
            config_dir=config_dir,
            path=lambda *parts: os.path.join(config_dir, *parts),
        )
        self.data = {}
        self.state = CoreState.not_running
        self.loop = asyncio.get_running_loop()

    async def async_add_executor_job(self, target, *args):
        return await self.loop.run_in_executor(None, target, *args)


def device_record(device_id: str = "dev-1") -> dict:
    return {
        "id": device_id,
        "name": "Station",
        "type": "devices.types.smart_speaker",
        "item_type": "devices",
        "capabilities": [
            {
                "type": "devices.capabilities.on_off",
                "state": {"instance": "on", "value": True},
            }
        ],
        "properties": [],
        "quasar_info": {"device_id": "glagol-1", "platform": "yandexstation"},
    }


def test_snapshot_cleans_runtime_fields(tmp_path):
    async def scenario():
        hass = FakeHass(str(tmp_path))
        manager = CacheManager(hass, "entry-1")

        device = device_record()
        device.update(
            entity=object(),
            callbacks=[lambda _: _],
            update_task=asyncio.get_running_loop().create_future(),
            glagol=object(),
            online=True,
            host="192.168.1.50",
            port=1905,
        )

        revision = await manager.async_save_devices(
            [device, "broken"], account_uid="uid-1", account_login="login"
        )
        assert revision == 1

        stored = manager.snapshot.devices
        # corrupt record ignored, healthy one restored
        assert [i["id"] for i in stored] == ["dev-1"]
        for key in (
            "entity",
            "callbacks",
            "update_task",
            "glagol",
            "online",
            "host",
            "port",
        ):
            assert key not in stored[0]
        assert stored[0]["capabilities"][0]["state"] == {"instance": "on", "value": True}

        # persisted payload is plain JSON in the Store envelope
        with open(manager.path, encoding="utf-8") as file:
            persisted = json.load(file)
        assert persisted["version"] == 1
        assert persisted["key"] == "yandex_station.entry-1"
        persisted_device = persisted["data"]["devices"][0]
        for key in (
            "entity",
            "callbacks",
            "update_task",
            "glagol",
            "online",
            "host",
            "port",
        ):
            assert key not in persisted_device
        json.dumps(persisted)

    asyncio.run(scenario())


def test_from_dict_rejects_invalid_payloads():
    valid = {
        "entry_id": "entry-1",
        "account_uid": "uid-1",
        "account_login": "login",
        "revision": 3,
        "updated_at": "2026-10-01T00:00:00+00:00",
        "devices": [device_record()],
        "local_config": {"glagol-1": {"host": "192.168.1.50", "port": 1905}},
        "device_tokens": {
            "glagol-1": {
                "token": "opaque",
                "obtained_at": "2026-10-01T00:00:00+00:00",
                "state": "valid",
            }
        },
        "scenario_creation": {
            "dev-1": {
                "trigger": "алиса включи свет",
                "state": SCENARIO_STATE_PENDING,
                "updated_at": "2026-10-01T00:00:00+00:00",
            }
        },
    }
    assert StationSnapshot.from_dict(valid) is not None

    assert StationSnapshot.from_dict(None) is None
    assert StationSnapshot.from_dict("junk") is None
    assert StationSnapshot.from_dict({}) is None

    for bad in (
        {"entry_id": 1, "revision": 1, "devices": []},
        {"entry_id": "e", "revision": "1", "devices": []},
        {"entry_id": "e", "revision": True, "devices": []},
        {"entry_id": "e", "revision": -1, "devices": []},
        {"entry_id": "e", "revision": 1},
        {"entry_id": "e", "revision": 1, "devices": {}},
        {"entry_id": "e", "revision": 1, "devices": [], "account_uid": 5},
        {"entry_id": "e", "revision": 1, "devices": [], "account_login": 5},
        {"entry_id": "e", "revision": 1, "devices": [], "updated_at": 12},
        {"entry_id": "e", "revision": 1, "devices": [], "local_config": []},
        {"entry_id": "e", "revision": 1, "devices": [], "device_tokens": None},
        {"entry_id": "e", "revision": 1, "devices": [], "scenario_creation": 7},
    ):
        assert StationSnapshot.from_dict(bad) is None, bad

    # corrupt records are ignored, healthy siblings survive
    snapshot = StationSnapshot.from_dict(
        {
            "entry_id": "e",
            "revision": 2,
            "devices": [device_record("dev-ok"), {"name": "no id"}, {"id": 42}, "junk"],
            "local_config": {"ok": {"host": "h", "port": 1}, "bad": {"port": "x"}, "junk": 3},
            "device_tokens": {
                "ok": {"token": "opaque", "state": "valid"},
                "bad": {"state": "valid"},
                "junk": "x",
            },
            "scenario_creation": {
                "ok": {"trigger": "t", "state": SCENARIO_STATE_CONFIRMED},
                "bad": {"trigger": "t", "state": "definitely-not-a-state"},
                "junk": 1,
            },
        }
    )
    assert snapshot is not None
    assert [i["id"] for i in snapshot.devices] == ["dev-ok"]
    assert list(snapshot.local_config) == ["ok"]
    assert list(snapshot.device_tokens) == ["ok"]
    assert list(snapshot.scenario_creation) == ["ok"]


def write_storage(tmp_path: str, entry_id: str, envelope: dict) -> str:
    path = os.path.join(str(tmp_path), ".storage", f"yandex_station.{entry_id}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(envelope, file)
    return path


def test_async_load_rejects_version_and_account_mismatch(tmp_path):
    async def scenario():
        hass = FakeHass(str(tmp_path))
        manager = CacheManager(hass, "entry-1")
        await manager.async_save_devices([device_record()], account_uid="uid-1")

        same = await CacheManager(hass, "entry-1").async_load(account_uid="uid-1")
        assert same is not None
        assert same.account_uid == "uid-1"
        assert same.revision == 1

        # snapshot of another account is rejected
        assert await CacheManager(hass, "entry-1").async_load(account_uid="uid-2") is None
        # no confirmed account on caller side: snapshot still usable
        assert await CacheManager(hass, "entry-1").async_load() is not None

        # unsupported envelope version: rejected and never rewritten
        path = write_storage(
            tmp_path,
            "entry-2",
            {
                "version": 99,
                "minor_version": 1,
                "key": "yandex_station.entry-2",
                "data": {"entry_id": "entry-2", "revision": 1, "devices": []},
            },
        )
        assert await CacheManager(hass, "entry-2").async_load() is None
        with open(path, encoding="utf-8") as file:
            assert json.load(file)["version"] == 99

        # envelope without a version or with junk data: no crash, rejected
        write_storage(
            tmp_path,
            "entry-3",
            {"minor_version": 1, "key": "yandex_station.entry-3", "data": {}},
        )
        assert await CacheManager(hass, "entry-3").async_load() is None

        write_storage(
            tmp_path,
            "entry-4",
            {
                "version": 1,
                "minor_version": 1,
                "key": "yandex_station.entry-4",
                "data": "not-a-snapshot",
            },
        )
        assert await CacheManager(hass, "entry-4").async_load() is None

        # snapshot bound to another entry is rejected
        write_storage(
            tmp_path,
            "entry-5",
            {
                "version": 1,
                "minor_version": 1,
                "key": "yandex_station.entry-5",
                "data": {"entry_id": "someone-else", "revision": 1, "devices": []},
            },
        )
        assert await CacheManager(hass, "entry-5").async_load() is None

    asyncio.run(scenario())


def test_save_devices_increments_monotonic_revision(tmp_path):
    async def scenario():
        hass = FakeHass(str(tmp_path))
        manager = CacheManager(hass, "entry-1")
        assert await manager.async_load() is None

        first = await manager.async_save_devices([device_record("dev-1")])
        second = await manager.async_save_devices(
            [device_record("dev-1"), device_record("dev-2")]
        )
        assert (first, second) == (1, 2)
        assert manager.snapshot.updated_at is not None

        # new runtime continues from the durable revision
        restored = await CacheManager(hass, "entry-1").async_load()
        assert restored.revision == 2
        assert len(restored.devices) == 2

        # full inventory replaces the catalog
        third = await manager.async_save_devices([device_record("dev-1")])
        assert third == 3
        reloaded = await CacheManager(hass, "entry-1").async_load()
        assert [i["id"] for i in reloaded.devices] == ["dev-1"]

    asyncio.run(scenario())


def test_save_token_keeps_devices_and_inventory_freshness(tmp_path):
    async def scenario():
        hass = FakeHass(str(tmp_path))
        manager = CacheManager(hass, "entry-1")
        revision = await manager.async_save_devices(
            [device_record()], account_uid="uid-1"
        )
        devices_before = json.loads(json.dumps(manager.snapshot.devices))
        updated_at = manager.snapshot.updated_at

        token_revision = await manager.async_save_device_token("glagol-1", "opaque-token")
        assert token_revision > revision

        snapshot = manager.snapshot
        assert snapshot.devices == devices_before
        assert snapshot.updated_at == updated_at
        assert snapshot.device_tokens["glagol-1"]["token"] == "opaque-token"
        assert snapshot.device_tokens["glagol-1"]["state"] == "valid"
        assert snapshot.device_tokens["glagol-1"]["obtained_at"]

        invalid_revision = await manager.async_save_device_token(
            "glagol-1", "opaque-token", "invalid"
        )
        assert invalid_revision > token_revision
        assert manager.snapshot.device_tokens["glagol-1"]["state"] == "invalid"
        assert manager.snapshot.devices == devices_before
        assert manager.snapshot.updated_at == updated_at

        # token is not lost by the next full inventory save
        await manager.async_save_devices([device_record()])
        assert manager.snapshot.device_tokens["glagol-1"]["token"] == "opaque-token"

    asyncio.run(scenario())


def test_scenario_marker_and_durable_verification(tmp_path):
    async def scenario():
        hass = FakeHass(str(tmp_path))
        manager = CacheManager(hass, "entry-1")
        await manager.async_save_devices([device_record()])

        revision = await manager.async_save_scenario_marker(
            "dev-1", "алиса включи свет", SCENARIO_STATE_PENDING
        )
        with open(manager.path, encoding="utf-8") as file:
            persisted = json.load(file)
        assert persisted["data"]["revision"] == revision
        assert (
            persisted["data"]["scenario_creation"]["dev-1"]["state"]
            == SCENARIO_STATE_PENDING
        )

        # durable marker confirmed on disk, straight from the file
        assert await manager.async_verify_durable_marker(
            revision, ("dev-1", SCENARIO_STATE_PENDING)
        )
        assert not await manager.async_verify_durable_marker(
            revision - 1, ("dev-1", SCENARIO_STATE_PENDING)
        )

        confirmed = await manager.async_save_scenario_marker(
            "dev-1", "алиса включи свет", SCENARIO_STATE_CONFIRMED
        )
        assert await manager.async_verify_durable_marker(
            confirmed, ("dev-1", SCENARIO_STATE_CONFIRMED)
        )
        assert not await manager.async_verify_durable_marker(
            confirmed, ("dev-1", SCENARIO_STATE_PENDING)
        )
        # revision-only check still passes
        assert await manager.async_verify_durable_marker(confirmed)

        # marker missing on disk: POST must be forbidden
        with open(manager.path, encoding="utf-8") as file:
            envelope = json.load(file)
        del envelope["data"]["scenario_creation"]["dev-1"]
        with open(manager.path, "w", encoding="utf-8") as file:
            json.dump(envelope, file)
        assert not await manager.async_verify_durable_marker(
            confirmed, ("dev-1", SCENARIO_STATE_CONFIRMED)
        )

        # missing file: verification fails
        os.remove(manager.path)
        assert not await manager.async_verify_durable_marker(confirmed)

        with pytest.raises(ValueError):
            await manager.async_save_scenario_marker("dev-1", "trigger", "bogus")

    asyncio.run(scenario())


def test_local_config_roundtrip(tmp_path):
    async def scenario():
        hass = FakeHass(str(tmp_path))
        manager = CacheManager(hass, "entry-1")
        await manager.async_save_devices([device_record()])
        await manager.async_save_local_config("glagol-1", "192.168.1.50", 1905)

        snapshot = await CacheManager(hass, "entry-1").async_load()
        assert snapshot.local_config == {"glagol-1": {"host": "192.168.1.50", "port": 1905}}

    asyncio.run(scenario())


def test_concurrent_saves_are_serialized(tmp_path):
    async def scenario():
        hass = FakeHass(str(tmp_path))
        manager = CacheManager(hass, "entry-1")
        await manager.async_save_devices([device_record()])

        revisions = await asyncio.gather(
            *(
                manager.async_save_device_token(f"glagol-{i}", f"token-{i}")
                for i in range(10)
            )
        )
        assert sorted(revisions) == list(range(2, 12))
        assert manager.snapshot.revision == 11

        with open(manager.path, encoding="utf-8") as file:
            envelope = json.load(file)
        assert envelope["data"]["revision"] == 11
        assert len(envelope["data"]["device_tokens"]) == 10

    asyncio.run(scenario())


def test_async_remove_deletes_storage(tmp_path):
    async def scenario():
        hass = FakeHass(str(tmp_path))
        manager = CacheManager(hass, "entry-1")
        await manager.async_save_devices([device_record()])
        assert os.path.exists(manager.path)

        await manager.async_remove()
        assert not os.path.exists(manager.path)
        assert manager.snapshot is None
        assert await CacheManager(hass, "entry-1").async_load() is None

    asyncio.run(scenario())
