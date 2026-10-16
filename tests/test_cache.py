import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntries, ConfigEntry

from custom_components.yandex_station.core.cache import (
    SCENARIO_STATE_CONFIRMED,
    SCENARIO_STATE_PENDING,
    TOKEN_STATE_INVALID,
    TOKEN_STATE_VALID,
    CacheManager,
    StationSnapshot,
)
from custom_components.yandex_station.core.policy import EntryPolicy
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar


def device_record(device_id="dev-1", glagol_id="glagol-1", platform="yandexstation"):
    return {
        "id": device_id,
        "name": "Synthetic station",
        "type": "devices.types.smart_speaker.yandex.station",
        "capabilities": [{
            "type": "devices.capabilities.on_off",
            "state": {"instance": "on", "value": True},
        }],
        "properties": [],
        "quasar_info": {"device_id": glagol_id, "platform": platform},
    }


def config_entry(entry_id="entry-1", login="login"):
    values = dict(
        version=1, domain="yandex_station", title="Synthetic account", data={},
        source="user", options={}, unique_id=login, entry_id=entry_id,
    )
    try:
        return ConfigEntry(
            **values, minor_version=1, discovery_keys=MappingProxyType({}),
            subentries_data=(),
        )
    except TypeError:
        return ConfigEntry(**values)


@asynccontextmanager
async def cache_runtime(tmp_path):
    try:
        hass = HomeAssistant(str(tmp_path))
    except TypeError:
        hass = HomeAssistant()
    hass.config.config_dir = str(tmp_path)
    hass.config_entries = ConfigEntries(hass, {})
    entries = {"entry-1": config_entry()}
    managers = []

    def manager(entry_id="entry-1", login="login", uid="uid-1"):
        cache = CacheManager(hass, entry_id, account_login=login, account_uid=uid)
        managers.append(cache)
        return cache

    # Only the entry lookup is controlled. Store, executor and HA task handling are real.
    with patch.object(hass.config_entries, "async_get_entry", side_effect=entries.get):
        try:
            yield manager, entries
        finally:
            for cache in managers:
                await cache.async_close()
            await hass.async_stop()


def write_storage(tmp_path, payload, entry_id="entry-1", version=1):
    path = Path(tmp_path) / ".storage" / f"yandex_station.{entry_id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": version, "minor_version": 1,
        "key": f"yandex_station.{entry_id}", "data": payload,
    }), encoding="utf-8")
    return path


def read_storage(cache):
    return json.loads(Path(cache.path).read_text(encoding="utf-8"))["data"]


def snapshot_payload():
    return StationSnapshot(
        entry_id="entry-1", account_login="login", account_uid="uid-1",
        revision=3, updated_at="2026-10-01T00:00:00+00:00",
        devices=[device_record()],
    ).to_dict()


def test_snapshot_cleans_runtime_fields(tmp_path):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            device = device_record()
            device.update(
                entity=object(), glagol=object(), callbacks=[lambda _: None],
                update_task=asyncio.get_running_loop().create_future(),
                host="127.0.0.1", port=1905, hdmi_audio=True, online=True,
                scenario_id="runtime-only-scenario", updates_url="PRIVATE_URL",
            )
            await cache.async_save_devices([device])
            persisted = read_storage(cache)
            assert persisted["account_login"] == "login"
            assert persisted["account_uid"] == "uid-1"
            assert persisted["devices"] == [device_record()]
            assert "PRIVATE_URL" not in Path(cache.path).read_text(encoding="utf-8")

    asyncio.run(scenario())


def test_corrupt_devices_are_dropped_but_empty_module_capabilities_are_valid():
    payload = snapshot_payload()
    module = device_record("module", "module-id", "yandexmodule")
    module["type"] = "devices.types.media_device.tv_box"
    module["capabilities"] = []
    corrupt = []
    for field, value in (
        ("id", 1), ("name", None), ("type", []), ("capabilities", None),
        ("properties", {}), ("quasar_info", {"device_id": "missing-platform"}),
    ):
        record = device_record()
        record[field] = value
        corrupt.append(record)
    missing = device_record()
    del missing["capabilities"]
    payload["devices"] += [module, missing, *corrupt]
    restored = StationSnapshot.from_dict(payload)
    assert restored.devices == [device_record(), module]


@pytest.mark.parametrize("field, value", [
    ("capabilities", [None]),
    ("capabilities", ["devices.capabilities.on_off"]),
    ("capabilities", [{"type": None}]),
    ("capabilities", [{"parameters": {"instance": "on"}}]),
    ("capabilities", [{"type": "devices.capabilities.mode", "parameters": "mode"}]),
    ("capabilities", [{"type": "devices.capabilities.on_off", "state": "on"}]),
    ("properties", [None]),
    ("properties", [{"type": "devices.properties.float", "state": 5}]),
    ("capabilities", [{"type": "devices.capabilities.mode"}]),
    ("properties", [{"type": "devices.properties.float", "parameters": {}}]),
    ("properties", [{"type": "devices.capabilities.on_off"}]),
    ("properties", [{"type": "unknown", "parameters": {}}]),
])
def test_malformed_capability_records_drop_device_but_keep_healthy_neighbor(field, value):
    payload = snapshot_payload()
    malformed = device_record("broken", "broken-local")
    malformed[field] = value
    payload["devices"] = [device_record(), malformed]
    restored = StationSnapshot.from_dict(payload)
    assert restored.devices == [device_record()]


def test_cloud_inventory_with_corrupt_capability_records_keeps_last_good_snapshot(tmp_path):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            await cache.async_save_devices([device_record()])
            original = Path(cache.path).read_bytes()
            malformed = device_record("broken", "broken-local")
            malformed["capabilities"] = [None]
            with pytest.raises(ValueError, match="inventory_unavailable"):
                await cache.async_save_devices([device_record(), malformed])
            assert Path(cache.path).read_bytes() == original
            assert cache.snapshot.devices == [device_record()]

    asyncio.run(scenario())


@pytest.mark.parametrize("patch_data", [
    {"entry_id": 1}, {"revision": True}, {"revision": -1},
    {"devices": {}}, {"account_login": 1}, {"account_uid": 1},
    {"updated_at": 12},
])
def test_invalid_snapshot_envelope_is_not_usable(patch_data):
    payload = snapshot_payload()
    payload.update(patch_data)
    assert StationSnapshot.from_dict(payload) is None


@pytest.mark.parametrize("field, value", [
    ("account_login", "another-login"), ("account_uid", "another-uid"),
    ("entry_id", "another-entry"),
])
def test_account_mismatch_is_rejected_without_rewriting(tmp_path, field, value):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            payload = snapshot_payload()
            payload[field] = value
            path = write_storage(tmp_path, payload)
            original = path.read_bytes()
            cache = manager()
            assert await cache.async_load() is None
            assert cache.cache_status == "cache_invalid"
            assert cache.scenario_creation_trusted is False
            assert path.read_bytes() == original

    asyncio.run(scenario())


def test_missing_corrupt_future_and_empty_snapshots_are_distinct(tmp_path):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            assert await cache.async_load() is None
            assert cache.cache_status == "cache_missing"
            assert await cache.async_invalidate_device_token("absent", "platform", "token")
            assert cache.snapshot is None and not Path(cache.path).exists()
            path = write_storage(tmp_path, snapshot_payload(), version=99)
            original = path.read_bytes()
            future = manager()
            assert await future.async_load() is None
            assert future.cache_status == "cache_invalid"
            assert path.read_bytes() == original

            path.write_text("{broken-json", encoding="utf-8")
            for _ in range(2):
                broken = manager()
                assert await broken.async_load() is None
                assert broken.cache_status == "cache_invalid"
                assert path.read_text(encoding="utf-8") == "{broken-json"
                assert not await broken.async_invalidate_device_token("g", "p", "token")
            with pytest.raises(RuntimeError, match="cache_unavailable"):
                await broken.async_save_scenario_marker("dev-1", "trigger", SCENARIO_STATE_PENDING)
            assert path.read_text(encoding="utf-8") == "{broken-json"

            # A complete empty inventory is valid; lost creation history stays untrusted.
            await broken.async_save_devices([])
            restored = await manager().async_load()
            assert restored.devices == []
            assert restored.scenario_creation_trusted is False
            quasar = YandexQuasar(None, policy=EntryPolicy(force_offline_local=True), cache_manager=manager())
            await quasar.init()
            assert quasar.readiness.inventory_source == "cache"
            assert quasar.devices == []

    asyncio.run(scenario())


def test_legacy_snapshot_migrates_only_with_existing_immutable_entry(tmp_path):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, entries):
            legacy = snapshot_payload()
            del legacy["account_login"]
            del legacy["account_uid"]
            legacy["device_tokens"] = {"glagol-1": {
                "token": "old-opaque-candidate", "state": "valid",
                "obtained_at": "2000-01-01T00:00:00+00:00",
            }}
            write_storage(tmp_path, legacy)
            migrated = await manager(uid=None).async_load()
            assert migrated.account_login == "login"
            assert migrated.account_uid is None
            assert migrated.device_tokens["glagol-1"]["yandexstation"]["token"] == "old-opaque-candidate"
            assert (await manager(uid=None).async_load()).to_dict() == migrated.to_dict()
            quasar = YandexQuasar(None, policy=EntryPolicy(force_offline_local=True))
            quasar._restore_from_snapshot(migrated)
            assert quasar.device_tokens == {"glagol-1": {"yandexstation": "old-opaque-candidate"}}

            # Constructor metadata alone is not evidence of an existing entry for legacy data.
            write_storage(tmp_path, legacy)
            entries.clear()
            assert await manager(uid=None).async_load() is None
            entries["entry-1"] = config_entry(login=None)
            assert await manager(uid=None).async_load() is None

    asyncio.run(scenario())


def test_ambiguous_legacy_token_is_not_assigned_to_a_platform(tmp_path):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            payload = snapshot_payload()
            payload["devices"].append(device_record("dev-2", "glagol-1", "yandexstation_2"))
            payload["device_tokens"] = {"glagol-1": {"token": "ambiguous", "state": "valid"}}
            write_storage(tmp_path, payload)
            snapshot = await manager().async_load()
            assert snapshot.device_tokens == {}
            assert (await manager().async_load()).device_tokens == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("journal", [None, []] + [{
    "healthy": {
        "trigger": "known", "state": SCENARIO_STATE_CONFIRMED,
        "updated_at": "2026-10-01T00:00:00+00:00",
    },
    "damaged": {"trigger": "unknown", "state": state},
} for state in ("corrupt", [], {})])
def test_corrupt_journal_remains_untrusted_after_inventory_and_restart(tmp_path, journal):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            payload = snapshot_payload()
            payload["scenario_creation"] = journal
            expected_journal = (
                {"healthy": journal["healthy"]} if isinstance(journal, dict) else {}
            )
            write_storage(tmp_path, payload)
            cache = manager()
            snapshot = await cache.async_load()
            assert snapshot.devices == [device_record()]
            assert snapshot.scenario_creation == expected_journal
            assert cache.scenario_creation_trusted is False
            await cache.async_save_devices([device_record()])
            restored = await manager().async_load()
            assert restored.scenario_creation_trusted is False
            assert restored.devices == [device_record()]
            assert restored.scenario_creation == expected_journal

    asyncio.run(scenario())


def test_partial_cloud_inventory_cannot_replace_last_good_snapshot(tmp_path):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            await cache.async_save_devices([device_record()])
            original = Path(cache.path).read_bytes()
            for devices in ([device_record(), "broken"], [device_record(), device_record()]):
                with pytest.raises(ValueError, match="inventory_unavailable"):
                    await cache.async_save_devices(devices)
                assert Path(cache.path).read_bytes() == original
                assert cache.snapshot.devices == [device_record()]

    asyncio.run(scenario())


def test_token_compare_invalidation_preserves_neighbors_and_freshness(tmp_path):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            devices = [device_record(), device_record("dev-2", "glagol-2", "yandexmidi")]
            await cache.async_save_devices(devices)
            await cache.async_save_device_token("glagol-1", "yandexstation", "rejected")
            await cache.async_save_device_token("glagol-1", "yandexstation_2", "other-platform")
            await cache.async_save_device_token("glagol-2", "yandexmidi", "neighbor")
            before = cache.snapshot.to_dict()
            assert await cache.async_invalidate_device_token("glagol-1", "yandexstation", "rejected")
            record = cache.snapshot.device_tokens["glagol-1"]["yandexstation"]
            assert record["state"] == TOKEN_STATE_INVALID
            assert record["obtained_at"] == before["device_tokens"]["glagol-1"]["yandexstation"]["obtained_at"]
            assert cache.snapshot.updated_at == before["updated_at"]
            assert cache.snapshot.devices == before["devices"]
            assert cache.snapshot.device_tokens["glagol-2"] == before["device_tokens"]["glagol-2"]
            assert cache.snapshot.device_tokens["glagol-1"]["yandexstation_2"]["token"] == "other-platform"

            restored = await manager().async_load()
            quasar = YandexQuasar(None)
            quasar._restore_from_snapshot(restored)
            assert quasar.device_tokens.get("glagol-1", {}).get("yandexstation") is None
            assert quasar.device_tokens["glagol-2"]["yandexmidi"] == "neighbor"

            await cache.async_save_device_token("glagol-1", "yandexstation", "replacement")
            replacement = deepcopy(cache.snapshot.device_tokens["glagol-1"]["yandexstation"])
            assert await cache.async_invalidate_device_token("glagol-1", "yandexstation", "rejected")
            assert cache.snapshot.device_tokens["glagol-1"]["yandexstation"] == replacement
            assert read_storage(cache)["device_tokens"]["glagol-1"]["yandexstation"] == replacement
            assert await cache.async_invalidate_device_token("missing", "platform", "absent")

    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["invalid", "unknown-state"])
def test_only_valid_disk_token_state_restores_a_candidate(tmp_path, state):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            payload = snapshot_payload()
            payload["device_tokens"] = {"glagol-1": {"yandexstation": {
                "token": "must-not-restore", "state": state, "obtained_at": None,
            }}}
            write_storage(tmp_path, payload)
            quasar = YandexQuasar(None)
            quasar._restore_from_snapshot(await manager().async_load())
            assert not quasar.device_tokens.get("glagol-1")

    asyncio.run(scenario())


def test_failed_invalidation_keeps_ram_rejected_and_flushes_later(tmp_path, caplog):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            await cache.async_save_devices([device_record()])
            await cache.async_save_device_token("glagol-1", "yandexstation", "opaque")
            obtained_at = cache.snapshot.device_tokens["glagol-1"]["yandexstation"]["obtained_at"]
            with patch.object(cache._store, "async_save", AsyncMock(return_value=None)):
                assert not await cache.async_invalidate_device_token("glagol-1", "yandexstation", "opaque")
            assert cache.snapshot.device_tokens["glagol-1"]["yandexstation"]["state"] == TOKEN_STATE_INVALID
            assert "token_cache_write_failed" in caplog.text
            assert read_storage(cache)["device_tokens"]["glagol-1"]["yandexstation"]["state"] == TOKEN_STATE_VALID
            assert await cache.async_flush()
            restored = await manager().async_load()
            record = restored.device_tokens["glagol-1"]["yandexstation"]
            assert record["state"] == TOKEN_STATE_INVALID
            assert record["obtained_at"] == obtained_at

    asyncio.run(scenario())


def test_cancelled_token_write_finishes_before_invalidation(tmp_path):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            await cache.async_save_devices([device_record()])
            original_save = cache._store.async_save
            started, release = asyncio.Event(), asyncio.Event()

            async def delayed_save(payload):
                if payload["device_tokens"]["glagol-1"]["yandexstation"]["state"] == TOKEN_STATE_VALID:
                    started.set()
                    await release.wait()
                await original_save(payload)

            with patch.object(cache._store, "async_save", side_effect=delayed_save):
                saving = asyncio.create_task(cache.async_save_device_token("glagol-1", "yandexstation", "opaque"))
                await asyncio.wait_for(started.wait(), 2)
                saving.cancel()
                invalidating = asyncio.create_task(cache.async_invalidate_device_token("glagol-1", "yandexstation", "opaque"))
                try:
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(asyncio.shield(saving), 0.01)
                finally:
                    release.set()
                with pytest.raises(asyncio.CancelledError):
                    await saving
                assert await invalidating
            restored = await manager().async_load()
            assert restored.device_tokens["glagol-1"]["yandexstation"]["state"] == TOKEN_STATE_INVALID

    asyncio.run(scenario())


def test_durable_marker_checks_content_not_only_revision(tmp_path):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            await cache.async_save_devices([device_record()])
            await cache.async_save_device_token("glagol-1", "yandexstation", "opaque")
            assert await cache.async_save_scenario_marker(
                "dev-1", "trigger", SCENARIO_STATE_PENDING
            )
            payload = read_storage(cache)
            payload["scenario_creation"]["dev-1"]["trigger"] = "different-trigger"
            write_storage(tmp_path, payload)
            # A durable invalidation notices the tampered revision through
            # direct disk read-back, not just the revision counter
            assert not await cache.async_invalidate_device_token(
                "glagol-1", "yandexstation", "other"
            )
            with patch.object(cache._store, "async_save", AsyncMock(return_value=None)):
                assert not await cache.async_save_scenario_marker(
                    "dev-2", "new-trigger", SCENARIO_STATE_PENDING
                )

    asyncio.run(scenario())


def test_metadata_merges_coalesces_and_closes_write_admission(tmp_path):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            await cache.async_save_devices([device_record()])
            before = cache.snapshot.updated_at
            await cache.async_save_local_config("glagol-1", host="127.0.0.1", port=1905)
            await cache.async_save_local_config("glagol-1", hdmi_audio=False)
            await cache.async_save_local_config("glagol-1", host="127.0.0.2")
            await cache.async_close()
            restored = await manager().async_load()
            assert restored.local_config == {"glagol-1": {
                "host": "127.0.0.2", "port": 1905, "hdmi_audio": False,
            }}
            assert restored.revision == 2
            assert restored.updated_at == before
            with pytest.raises(RuntimeError, match="cache_closed"):
                await cache.async_save_device_token("glagol-1", "yandexstation", "late")
            with pytest.raises(RuntimeError, match="cache_closed"):
                await cache.async_save_local_config("glagol-1", hdmi_audio=True)

    asyncio.run(scenario())


def test_inventory_prunes_tokens_and_addresses_but_retains_creation_history(tmp_path):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            neighbor = device_record("dev-2", "glagol-2", "yandexmidi")
            await cache.async_save_devices([device_record(), neighbor])
            await cache.async_save_device_token("glagol-1", "yandexstation", "removed-device")
            await cache.async_save_device_token("glagol-2", "yandexmidi", "neighbor")
            await cache.async_save_local_config("glagol-1", host="127.0.0.1")
            await cache.async_save_scenario_marker("dev-1", "trigger", SCENARIO_STATE_CONFIRMED)
            await cache.async_save_devices([neighbor])
            restored = await manager().async_load()
            assert restored.devices == [neighbor]
            assert set(restored.device_tokens) == {"glagol-2"}
            assert restored.local_config == {}
            assert restored.scenario_creation["dev-1"]["state"] == SCENARIO_STATE_CONFIRMED

    asyncio.run(scenario())


def test_concurrent_inventory_token_and_marker_writes_survive_restart(tmp_path):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            await cache.async_save_devices([device_record()])
            await asyncio.gather(
                cache.async_save_devices([device_record()]),
                cache.async_save_device_token("glagol-1", "yandexstation", "candidate"),
                cache.async_save_scenario_marker("dev-1", "trigger", SCENARIO_STATE_PENDING),
            )
            restored = await manager().async_load()
            assert restored.devices == [device_record()]
            assert restored.device_tokens["glagol-1"]["yandexstation"]["token"] == "candidate"
            assert restored.scenario_creation["dev-1"]["state"] == SCENARIO_STATE_PENDING
            assert restored.revision == 4

    asyncio.run(scenario())


def test_async_remove_waits_for_metadata_and_prevents_file_resurrection(tmp_path):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            await cache.async_save_devices([device_record()])
            await cache.async_save_local_config("glagol-1", hdmi_audio=True)
            await cache.async_remove()
            assert not Path(cache.path).exists()
            assert cache.snapshot is None
            with pytest.raises(RuntimeError, match="cache_closed"):
                await cache.async_save_devices([device_record()])
            assert await manager().async_load() is None

    asyncio.run(scenario())
