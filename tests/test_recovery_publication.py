import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.yandex_station.core.cache import (
    CacheManager, StationSnapshot, TOKEN_STATE_INVALID, TOKEN_STATE_VALID,
)
from custom_components.yandex_station.core.const import DOMAIN
from custom_components.yandex_station.core.lifecycle import get_lifecycle
from custom_components.yandex_station.core.policy import (
    EntryPolicy, DevicesState, Readiness,
)
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


def snapshot_backed_quasar(devices, policy=None, readiness=None):
    session = MagicMock()
    cache = MagicMock()
    cache.snapshot = StationSnapshot(
        entry_id="entry-1", account_login="login", devices=[dict(d) for d in devices],
    )
    cache.entry_id = "entry-1"
    cache.async_save_devices = AsyncMock()
    cache.async_close = AsyncMock()
    quasar = YandexQuasar(session, policy=policy, readiness=readiness, cache_manager=cache)
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
        async def fake_get(*, path, role):
            if "scenarios" in path:
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
            restored_timestamp = quasar.readiness.last_snapshot_at
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
            persisted = json_mod.loads(Path(cache.path).read_text(encoding="utf-8"))["data"]
            assert quasar.readiness.last_snapshot_at == persisted["updated_at"]
            assert quasar.readiness.last_snapshot_at != restored_timestamp

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


def token_response(token):
    resp = AsyncMock()
    resp.text = AsyncMock(return_value=f'{{"status":"ok","token":"{token}"}}')
    return resp


def entity_stub():
    entity = MagicMock()
    entity.hass = True
    entity.init_local_mode = AsyncMock()
    return entity


def test_recovery_refetches_token_after_persisted_rejection_restored_empty():
    """Finding 6: outer-map membership must not hide a missing (id, platform)."""
    async def scenario():
        quasar, session, cache = snapshot_backed_quasar(
            [device_catalog()], policy=EntryPolicy(), readiness=Readiness(),
        )
        # Persisted rejection on disk: restore keeps the glagol id with an
        # empty platform map because only VALID tokens are restored.
        cache.snapshot.device_tokens = {
            "glagol-1": {"yandexstation": {"token": "rejected", "state": TOKEN_STATE_INVALID}},
        }
        quasar._restore_from_snapshot(cache.snapshot)
        assert quasar.device_tokens == {"glagol-1": {}}
        quasar.owned_glagol_ids = {"glagol-1"}
        quasar.devices[0]["entity"] = entity_stub()

        session.request_glagol = AsyncMock(return_value=token_response("fresh-candidate"))

        async def fake_get(*, path, role):
            if "scenarios" in path:
                raise RuntimeError("scenarios unavailable")
            return devices_response(device_catalog())

        session.get = AsyncMock(side_effect=fake_get)
        cache.async_save_device_token = AsyncMock()

        await quasar._async_recover_after_publication()

        session.request_glagol.assert_awaited_once()
        cache.async_save_device_token.assert_awaited_once_with(
            "glagol-1", "yandexstation", "fresh-candidate"
        )
        assert quasar.device_tokens == {"glagol-1": {"yandexstation": "fresh-candidate"}}

    asyncio.run(scenario())


def test_recovery_survives_inventory_outage_and_completes_without_reload():
    """Finding 7: an inventory outage must not end independent recovery."""
    async def scenario():
        from custom_components.yandex_station.core.policy import RecoveryBackoff
        from custom_components.yandex_station.core.yandex_session import CloudRequestError

        quasar, session, cache = snapshot_backed_quasar(
            [device_catalog()], policy=EntryPolicy(), readiness=Readiness(),
        )
        quasar.owned_glagol_ids = {"glagol-1"}
        quasar.devices[0]["entity"] = entity_stub()

        inventory_attempts = []

        async def fake_get(*, path, role):
            if "user/devices" in path:
                inventory_attempts.append(path)
                if len(inventory_attempts) == 1:
                    raise CloudRequestError("quasar", 503)
                return devices_response(device_catalog())
            if "scenarios" in path:
                raise RuntimeError("scenarios unavailable")
            raise AssertionError(path)

        session.get = AsyncMock(side_effect=fake_get)
        session.request_glagol = AsyncMock(return_value=token_response("fetched-candidate"))
        cache.async_save_device_token = AsyncMock()

        original = RecoveryBackoff.next_delay
        RecoveryBackoff.next_delay = lambda self, retry_after=None: 0
        try:
            await asyncio.wait_for(
                quasar._async_recover_after_publication(), 5
            )
        finally:
            RecoveryBackoff.next_delay = original

        # The outage was retried, not treated as terminal
        assert len(inventory_attempts) == 2
        # Independent recovery still ran: worker armed and token fetched
        assert quasar._scenario_worker_task is not None
        assert quasar.device_tokens == {"glagol-1": {"yandexstation": "fetched-candidate"}}
        assert quasar._recovery_task is None or quasar._recovery_task.done()
        quasar.stopping = True
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_token_recovery_does_not_wait_for_inventory_availability():
    async def scenario():
        from custom_components.yandex_station.core.policy import RecoveryBackoff
        from custom_components.yandex_station.core.yandex_session import CloudRequestError

        quasar, session, cache = snapshot_backed_quasar(
            [device_catalog()],
            policy=EntryPolicy(enable_scenarios=False, cloud_updates=False),
            readiness=Readiness(),
        )
        quasar.owned_glagol_ids = {"glagol-1"}
        recovered = asyncio.Event()
        session.get = AsyncMock(side_effect=CloudRequestError("quasar", 503))

        async def fetch(*, path, role, **kwargs):
            recovered.set()
            return token_response("independent-candidate")

        session.request_glagol = AsyncMock(side_effect=fetch)
        cache.async_save_device_token = AsyncMock()
        with patch.object(RecoveryBackoff, "next_delay", return_value=0.01):
            quasar.start()
            task = quasar._recovery_task
            try:
                await asyncio.wait_for(recovered.wait(), 0.5)
                await asyncio.sleep(0)
                assert quasar.device_tokens == {
                    "glagol-1": {"yandexstation": "independent-candidate"}
                }
                assert quasar.readiness.inventory_source == "cache"
            finally:
                quasar.stopping = True
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_start_rearms_completed_post_publication_recovery():
    async def scenario():
        quasar, _, _ = snapshot_backed_quasar(
            [], policy=EntryPolicy(
                use_cached_devices=True, enable_scenarios=False, cloud_updates=False,
            ), readiness=Readiness(),
        )
        quasar.start()
        first = quasar._recovery_task
        await first
        quasar.start()
        second = quasar._recovery_task
        try:
            assert second is not first
            await second
        finally:
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)

    asyncio.run(scenario())


def test_recovery_continues_remaining_speakers_after_token_fetch_failure():
    """Finding 7: one speaker's token failure must not abort its peers."""
    async def scenario():
        second = device_catalog(name="Станция Кухня")
        second["id"] = "cloud-2"
        second["quasar_info"] = {"device_id": "glagol-2", "platform": "yandexstation"}
        quasar, session, cache = snapshot_backed_quasar(
            [device_catalog(), second],
            policy=EntryPolicy(use_cached_devices=True),
            readiness=Readiness(),
        )
        quasar.owned_glagol_ids = {"glagol-1", "glagol-2"}
        entities = [entity_stub(), entity_stub()]
        quasar.devices[0]["entity"] = entities[0]
        quasar.devices[1]["entity"] = entities[1]

        attempts = []

        async def fetch(*, path, role, **kwargs):
            did = kwargs["params"]["device_id"]
            attempts.append(did)
            if len(attempts) == 1:
                raise RuntimeError("device_token_unavailable")
            return token_response("candidate-" + did)

        session.request_glagol = AsyncMock(side_effect=fetch)
        cache.async_save_device_token = AsyncMock()

        with patch(
            "custom_components.yandex_station.core.yandex_quasar.RecoveryBackoff.next_delay",
            return_value=0,
        ):
            await asyncio.wait_for(quasar._async_recover_after_publication(), 2)

        assert attempts == ["glagol-1", "glagol-2", "glagol-1"]
        assert quasar.device_tokens == {
            did: {"yandexstation": "candidate-" + did} for did in ("glagol-1", "glagol-2")
        }

    asyncio.run(scenario())


def test_cached_token_missing_hdmi_config_fetched_after_publication():
    """Station F1 interplay: a cached token must not skip local setup."""
    async def scenario():
        from custom_components.yandex_station.core.yandex_station import (
            YandexStationBase,
        )
        from .test_glagol_lifecycle import FakeWS, ws_text

        quasar, session, cache = snapshot_backed_quasar(
            [device_catalog()], policy=EntryPolicy(), readiness=Readiness(),
        )
        # Cached valid token for the exact (device_id, platform) pair
        cache.snapshot.device_tokens = {
            "glagol-1": {"yandexstation": {
                "token": "cached-token", "state": TOKEN_STATE_VALID,
            }},
        }
        quasar._restore_from_snapshot(cache.snapshot)
        quasar.owned_glagol_ids = {"glagol-1"}
        device = quasar.devices[0]
        device["host"] = "127.0.0.1"
        device["port"] = 1905
        entity = YandexStationBase(quasar, device)
        entity.hass = MagicMock()
        entity.async_write_ha_state = MagicMock()
        device["entity"] = entity
        assert entity.hdmi_audio is None  # HDMI configuration absent

        config = AsyncMock()
        config.json = AsyncMock(return_value={
            "status": "ok",
            "quasar_config": {"hdmiAudio": True},
            "quasar_config_version": "1",
        })

        async def fake_get(*, path, role):
            if "configuration" in path:
                return config
            if "scenarios" in path:
                raise RuntimeError("scenarios unavailable")
            return devices_response(device_catalog())

        session.get = AsyncMock(side_effect=fake_get)
        cache.async_save_local_config = AsyncMock()
        session.request_glagol = AsyncMock()
        session.ws_connect = AsyncMock(return_value=FakeWS([
            ws_text('{"state":{"volume":0.3,"aliceState":"IDLE"}}'),
        ]))

        try:
            await asyncio.wait_for(quasar._async_recover_after_publication(), 2)
            await asyncio.wait_for(entity._hdmi_refresh_task, 2)
            session.request_glagol.assert_not_called()
            assert entity.hdmi_audio is True
            assert device["hdmi_audio"] is True
            cache.async_save_local_config.assert_awaited_once_with(
                "glagol-1", hdmi_audio=True
            )
        finally:
            quasar.stopping = True
            await entity.glagol.stop()
            if quasar._scenario_worker_task:
                quasar._scenario_worker_task.cancel()
                await asyncio.gather(quasar._scenario_worker_task, return_exceptions=True)

    asyncio.run(scenario())

def test_fetched_token_reaches_existing_empty_glagol_client():
    """Finding 5: recovery must hand the fetched token to the live consumer.

    Real YandexStationBase and YandexGlagol against a scripted local peer;
    init_local_mode is never mocked.
    """
    async def scenario():
        from custom_components.yandex_station.core.policy import TokenState
        from custom_components.yandex_station.core.yandex_station import YandexStationBase
        from .test_glagol_lifecycle import FakeWS, ws_text

        session = MagicMock()
        session.ws_connect = AsyncMock(side_effect=lambda *, url, role, **kwargs: FakeWS([
            ws_text('{"state":{"volume":0.3,"playing":false}}'),
        ]))
        cache = MagicMock()
        cache.snapshot = StationSnapshot(entry_id="entry-1", account_login="login")
        quasar = YandexQuasar(
            session, policy=EntryPolicy(use_cached_devices=True, enable_scenarios=False),
            readiness=Readiness(), cache_manager=cache,
        )
        quasar.owned_glagol_ids = {"glagol-1"}
        device = device_catalog()
        device["host"] = "127.0.0.1"
        device["port"] = 1905
        device["hdmi_audio"] = False
        quasar.devices = [device]

        entity = YandexStationBase(quasar, device)
        entity.hass = MagicMock()
        entity.async_write_ha_state = MagicMock()
        device["entity"] = entity

        # Startup with no known token: the real client is created empty and waits
        await entity.init_local_mode()
        assert entity.glagol is not None
        assert entity.glagol.device_token is None
        for _ in range(5):
            await asyncio.sleep(0)
        assert session.ws_connect.await_count == 0

        # Post-publication recovery fetches and durably stores a fresh token
        session.request_glagol = AsyncMock(return_value=token_response("fetched-candidate"))
        cache.async_save_device_token = AsyncMock()
        await quasar._async_recover_after_publication()
        assert quasar.device_tokens == {
            "glagol-1": {"yandexstation": "fetched-candidate"}
        }

        # The peer accepts the fetched token on the already-created client
        accepted = False
        for _ in range(200):
            if entity.glagol.token_state is TokenState.ACCEPTED:
                accepted = True
                break
            await asyncio.sleep(0.01)
        assert entity.glagol.device_token == "fetched-candidate"
        assert accepted
        await entity.glagol.stop()

    asyncio.run(scenario())


def test_session_csrf_transitions_reach_readiness_sensor_end_to_end():
    """Finding 20: real session producer -> quasar signal -> real sensor.

    No mocked listener API: a real YandexSession CSRF pass must refresh a
    real YandexCloudHealthSensor subscribed through the quasar dispatcher.
    """
    async def scenario():
        import time

        from homeassistant.helpers.entity import DeviceInfo

        from custom_components.yandex_station.core.policy import SessionConfig
        from custom_components.yandex_station.core.yandex_session import (
            CSRF_FRESH_SECS, YandexSession,
        )
        from custom_components.yandex_station.sensor import YandexCloudHealthSensor
        from .test_session_resilience import csrf_page, make_client, make_response

        client = make_client(cookie_domains=())
        session = YandexSession(
            client, config=SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        )
        quasar = YandexQuasar(session)
        entry = MagicMock(entry_id="entry-1")
        sensor = YandexCloudHealthSensor(
            quasar, entry, DeviceInfo(identifiers={("yandex_station", "entry-1")})
        )
        sensor.hass = MagicMock()
        writes = []
        sensor.async_write_ha_state = lambda: writes.append(1)
        await sensor.async_added_to_hass()

        assert sensor.extra_state_attributes["csrf_state"] == "unknown"
        assert writes == []

        # Successful fetch pass: UNKNOWN -> CANDIDATE reaches the sensor
        client.get = AsyncMock(return_value=csrf_page("candidate_1"))
        assert await session.ensure_csrf() == "candidate_1"
        assert sensor.extra_state_attributes["csrf_state"] == "candidate"
        assert len(writes) == 1

        # Stale drop + failed pass: CANDIDATE -> UNAVAILABLE reaches the sensor
        session._csrf_obtained_at = time.monotonic() - (CSRF_FRESH_SECS + 60)
        client.get = AsyncMock(return_value=make_response(500))
        assert await session.ensure_csrf() is None
        assert sensor.extra_state_attributes["csrf_state"] == "unavailable"
        assert len(writes) == 2

        await quasar.async_stop()

    asyncio.run(scenario())


def test_startup_deduplicates_inventory_get_across_init_recovery_and_connect():
    async def scenario():
        from .test_glagol_lifecycle import FakeWS

        # 1. Cold startup (no snapshot): init() + start() (recovery + connect) -> 1 GET /devices
        session = MagicMock()
        session.closed = False
        session.async_stop = AsyncMock()
        resp = devices_response(device_catalog())
        resp.json.return_value["updates_url"] = "wss://push.yandex.ru/v2/subscribe"
        session.get = AsyncMock(return_value=resp)
        session.ws_connect = AsyncMock(return_value=FakeWS([]))
        quasar = YandexQuasar(
            session,
            policy=EntryPolicy(cloud_updates=True, enable_scenarios=False),
            readiness=Readiness(),
        )
        await quasar.init()
        assert session.get.await_count == 1
        quasar.start()
        await asyncio.wait_for(quasar._recovery_task, 2)
        for _ in range(20):
            if session.ws_connect.await_count >= 1:
                break
            await asyncio.sleep(0.01)
        assert session.ws_connect.await_count == 1
        assert session.get.await_count == 1
        await quasar.async_stop()

        # 2. Cached startup: init() (0 GET) + start() (recovery + connect share 1 GET)
        quasar2, session2, _ = snapshot_backed_quasar(
            [device_catalog()],
            policy=EntryPolicy(cloud_updates=True, enable_scenarios=False),
            readiness=Readiness(),
        )
        session2.closed = False
        session2.async_stop = AsyncMock()
        resp2 = devices_response(device_catalog())
        resp2.json.return_value["updates_url"] = "wss://push.yandex.ru/v2/subscribe"
        session2.get = AsyncMock(return_value=resp2)
        session2.ws_connect = AsyncMock(return_value=FakeWS([]))
        quasar2.start()
        await asyncio.wait_for(quasar2._recovery_task, 2)
        for _ in range(20):
            if session2.ws_connect.await_count >= 1:
                break
            await asyncio.sleep(0.01)
        assert session2.ws_connect.await_count == 1
        assert session2.get.await_count == 1
        await quasar2.async_stop()

        # 3. Cached startup with 429 + Retry-After: shared sync fails once -> connect() enters backoff without a 2nd GET
        from custom_components.yandex_station.core.yandex_session import CloudRequestError

        quasar3, session3, _ = snapshot_backed_quasar(
            [device_catalog()],
            policy=EntryPolicy(cloud_updates=True, enable_scenarios=False),
            readiness=Readiness(),
        )
        session3.closed = False
        session3.async_stop = AsyncMock()
        session3.retry_after = {"quasar": "120"}
        session3.get = AsyncMock(side_effect=CloudRequestError("quasar", 429))
        session3.ws_connect = AsyncMock()
        quasar3.start()
        for _ in range(20):
            if quasar3.cloud_connection["state"] == "backoff":
                break
            await asyncio.sleep(0.01)
        assert quasar3.cloud_connection["state"] == "backoff"
        assert session3.get.await_count == 1
        assert session3.ws_connect.await_count == 0
        await quasar3.async_stop()

    asyncio.run(scenario())
