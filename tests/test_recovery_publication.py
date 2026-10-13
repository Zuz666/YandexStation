import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.yandex_station.core.cache import CacheManager, StationSnapshot
from custom_components.yandex_station.core.const import DOMAIN
from custom_components.yandex_station.core.lifecycle import get_lifecycle
from custom_components.yandex_station.core.policy import EntryPolicy, DevicesState
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
from .test_cache import config_entry
from .test_ownership import ownership_runtime


def device_catalog(state="online", name="Станция Зал"):
    return {
        "id": "cloud-1",
        "name": name,
        "type": "devices.types.smart_speaker.yandex.station",
        "state": state,
        "capabilities": [{
            "type": "devices.capabilities.on_off",
            "state": {"instance": "on", "value": True},
        }],
        "properties": [],
        "quasar_info": {"device_id": "glagol-1", "platform": "yandexstation"},
    }


def devices_response(*devices):
    resp = AsyncMock()
    resp.json.return_value = {
        "status": "ok",
        "households": [{"name": "Home", "all": list(devices)}],
    }
    return resp


def snapshot_backed_quasar(devices):
    session = MagicMock()
    cache = MagicMock()
    cache.snapshot = StationSnapshot(
        entry_id="entry-1", account_login="login", devices=[dict(d) for d in devices],
    )
    cache.entry_id = "entry-1"
    cache.async_save_devices = AsyncMock()
    quasar = YandexQuasar(session, cache_manager=cache)
    quasar._restore_from_snapshot(cache.snapshot)
    return quasar, session, cache


def lifecycle_hass():
    hass = MagicMock(spec=HomeAssistant)
    hass.data = {DOMAIN: {}}
    entry = config_entry("entry-1", "login")
    entry.disabled_by = None
    entry.source = "user"
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    reloads = []

    async def reload(eid):
        reloads.append(eid)
        return True

    hass.config_entries.async_reload = reload
    hass.async_create_task = lambda coro: asyncio.create_task(coro)
    lifecycle = get_lifecycle(hass)
    return hass, lifecycle, reloads


def test_unchanged_fingerprint_updates_in_place_without_reload():
    async def scenario():
        quasar, session, cache = snapshot_backed_quasar([device_catalog()])
        old = quasar.devices[0]
        old["entity"], old["host"], old["scenario_id"] = object(), "10.0.0.5", "scen-1"

        # Same membership fingerprint, updated device name and state
        session.get = AsyncMock(return_value=devices_response(
            device_catalog(state="offline", name="Новое имя"),
        ))
        hass, lifecycle, reloads = lifecycle_hass()
        cache.hass = hass

        applied = await quasar._sync_cloud_devices()

        assert applied is True
        assert reloads == []
        assert quasar.devices[0] is old
        assert old["name"] == "Новое имя"
        assert old["state"] == "offline"
        assert old["entity"] is not None and old["host"] == "10.0.0.5"
        assert old["scenario_id"] == "scen-1"
        assert quasar.fresh_device_ids == {"cloud-1"}
        cache.async_save_devices.assert_awaited_once()

    asyncio.run(scenario())


def test_changed_fingerprint_saves_first_and_requests_single_ordinary_reload():
    async def scenario():
        quasar, session, cache = snapshot_backed_quasar([device_catalog()])
        old = quasar.devices[0]
        old["entity"] = sentinel = object()
        session.get = AsyncMock(return_value=devices_response(
            device_catalog(), device_catalog().copy() | {"id": "cloud-2"},
        ))
        hass, lifecycle, reloads = lifecycle_hass()
        cache.hass = hass

        requester = asyncio.create_task(quasar._sync_cloud_devices())
        applied = await requester
        # reload dispatches only after the requesting task returned
        assert reloads == []
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert reloads == ["entry-1"]

        assert applied is False
        # Snapshot is saved before anything else; active consumers are not replaced
        saved = cache.async_save_devices.await_args[0][0]
        assert [d["id"] for d in saved] == ["cloud-1", "cloud-2"]
        assert len(quasar.devices) == 1 and quasar.devices[0] is old
        assert old["entity"] is sentinel
        await lifecycle.async_stop()

    asyncio.run(scenario())


def test_recovery_fetches_missing_tokens_once_and_reconnects_owner():
    async def scenario():
        quasar, session, cache = snapshot_backed_quasar([device_catalog()])
        quasar.owned_glagol_ids = {"glagol-1"}
        entity = MagicMock()
        entity.hass = True
        entity.init_local_mode = AsyncMock()
        quasar.devices[0]["entity"] = entity

        token_resp = AsyncMock()
        token_resp.text = AsyncMock(
            return_value='{"status":"ok","token":"fetched-candidate"}'
        )
        session.request_glagol = AsyncMock(return_value=token_resp)
        async def fake_get(url, **kwargs):
            if "scenarios" in url:
                # Scenario catalog is unavailable: reconciliation is skipped
                raise RuntimeError("scenarios unavailable")
            return devices_response(device_catalog())

        session.get = AsyncMock(side_effect=fake_get)
        cache.async_save_device_token = AsyncMock()

        await quasar._async_recover_after_publication()

        assert quasar.device_tokens == {"glagol-1": {"yandexstation": "fetched-candidate"}}
        cache.async_save_device_token.assert_awaited_once_with(
            "glagol-1", "yandexstation", "fetched-candidate"
        )
        entity.init_local_mode.assert_awaited_once()

        # Second pass does not repeat the fetch when token is already known
        await quasar._async_recover_after_publication()
        assert session.request_glagol.call_count == 1

    asyncio.run(scenario())


def test_cached_restore_keeps_cloud_entities_unavailable_until_event():
    async def scenario():
        from custom_components.yandex_station.core.entity import YandexEntity

        quasar = YandexQuasar(None)
        quasar.readiness.inventory_source = "cache"
        quasar.readiness.devices_state = DevicesState.READY
        device = device_catalog(state="online")
        entity = YandexEntity(quasar, device)

        # Restored cached state does not mark cloud entity as online
        assert entity.available is False
        assert quasar.fresh_device_ids == set()

        entity.on_update({
            "id": "cloud-1", "state": "online",
            "capabilities": device["capabilities"],
        })
        assert entity.available is True
        assert quasar.fresh_device_ids == {"cloud-1"}

    asyncio.run(scenario())

def test_changed_membership_with_real_store_requests_one_reload_and_keeps_consumers(tmp_path):
    async def scenario():
        import json as json_mod
        from pathlib import Path

        async with ownership_runtime(tmp_path) as (hass, entries, client, lifecycle):
            entry = entries["a"]
            cache = CacheManager(
                hass, entry.entry_id, account_login=entry.unique_id,
                account_uid=entry.data["account_uid"],
            )
            await cache.async_save_devices([device_catalog()])
            quasar = YandexQuasar(MagicMock(), policy=EntryPolicy(), cache_manager=cache)
            quasar.session.account_uid = entry.data["account_uid"]
            await quasar.init()  # Restore from a real Store instance
            old = quasar.devices[0]
            old["entity"] = sentinel = object()

            quasar.session.get = AsyncMock(return_value=devices_response(
                device_catalog(), device_catalog().copy() | {"id": "cloud-2"},
            ))
            reloads = []

            async def reload(eid):
                reloads.append(eid)
                return True

            with patch.object(hass.config_entries, "async_reload", side_effect=reload):
                requester = asyncio.create_task(quasar._sync_cloud_devices())
                applied = await requester
                await hass.async_block_till_done()

            assert applied is False
            assert reloads == [entry.entry_id]
            # Active consumers are preserved: dict identity and entity survive sync
            assert len(quasar.devices) == 1 and quasar.devices[0] is old
            assert old["entity"] is sentinel
            # Updated snapshot is durably written to the real Store on disk
            on_disk = json_mod.loads(
                Path(cache.path).read_text(encoding="utf-8")
            )["data"]["devices"]
            assert [d["id"] for d in on_disk] == ["cloud-1", "cloud-2"]

            # Admission is closed: reload is not re-requested and catalog stays untouched
            lifecycle.context.admission_open = False
            quasar.session.get = AsyncMock(return_value=devices_response(
                device_catalog(), device_catalog().copy() | {"id": "cloud-2"},
                device_catalog().copy() | {"id": "cloud-3"},
            ))
            with patch.object(hass.config_entries, "async_reload", side_effect=reload):
                requester = asyncio.create_task(quasar._sync_cloud_devices())
                applied = await requester
                await hass.async_block_till_done()
            assert applied is False and reloads == [entry.entry_id]
            assert quasar.devices[0] is old and old["entity"] is sentinel
            on_disk = json_mod.loads(
                Path(cache.path).read_text(encoding="utf-8")
            )["data"]["devices"]
            assert [d["id"] for d in on_disk] == ["cloud-1", "cloud-2", "cloud-3"]
            await cache.async_close()

    asyncio.run(scenario())
