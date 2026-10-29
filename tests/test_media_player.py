import asyncio
import json
from contextlib import nullcontext
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.exceptions import HomeAssistantError

from custom_components.yandex_station.media_player import YandexMediaPlayer
from custom_components.yandex_station.core.utils import YandexDebug
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
from custom_components.yandex_station.core.yandex_glagol import YandexGlagol
from custom_components.yandex_station.core.const import DATA_CONFIG, DOMAIN
from custom_components.yandex_station.core.policy import TokenState
from custom_components.yandex_station.core.yandex_station import (
    SOURCE_HDMI,
    SOURCE_STATION,
    YandexStation,
)
from .test_glagol_lifecycle import FakeWS, ws_text
from .test_cache import cache_runtime
from . import false, null, true, update_ha_state


def test_tv_rf():
    device = {
        "id": "xxx",
        "name": "Зал Телевизор",
        "type": "devices.types.media_device.tv",
        "icon_url": "https://avatars.mds.yandex.net/get-iot/icons-devices-devices.types.media_device.tv.svg/orig",
        "capabilities": [
            {
                "retrievable": false,
                "type": "devices.capabilities.on_off",
                "state": null,
                "parameters": {"split": false},
            },
            {
                "retrievable": false,
                "type": "devices.capabilities.range",
                "state": null,
                "parameters": {
                    "instance": "volume",
                    "name": "громкость",
                    "unit": "",
                    "random_access": false,
                    "looped": false,
                },
            },
            {
                "retrievable": false,
                "type": "devices.capabilities.range",
                "state": null,
                "parameters": {
                    "instance": "channel",
                    "name": "канал",
                    "unit": "",
                    "random_access": true,
                    "looped": true,
                },
            },
            {
                "retrievable": false,
                "type": "devices.capabilities.mode",
                "state": null,
                "parameters": {
                    "instance": "input_source",
                    "name": "источник сигнала",
                    "modes": [
                        {"value": "one", "name": "Один"},
                        {"value": "two", "name": "Два"},
                    ],
                },
            },
            {
                "retrievable": false,
                "type": "devices.capabilities.toggle",
                "state": null,
                "parameters": {"instance": "mute", "name": "без звука"},
            },
            {
                "retrievable": false,
                "type": "devices.capabilities.toggle",
                "state": null,
                "parameters": {"instance": "pause", "name": "пауза"},
            },
        ],
        "properties": [],
        "item_type": "device",
        "skill_id": "T",
        "room_name": "Зал",
        "state": "online",
        "created": "2020-11-29T06:30:20Z",
        "parameters": {
            "device_info": {"manufacturer": "Неизвестно", "model": "Неизвестно"}
        },
    }

    state = update_ha_state(YandexMediaPlayer, device, config={})
    assert state.state == "idle"
    assert state.attributes == {
        "assumed_state": True,
        "device_class": "tv",
        "friendly_name": "Зал Телевизор",
        "icon": "mdi:television-classic",
        "source_list": ["Один", "Два"],
        "supported_features": (
            MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
            | MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.VOLUME_STEP
            | MediaPlayerEntityFeature.SELECT_SOURCE
            | MediaPlayerEntityFeature.PLAY
        ),
    }


def test_tv_lg():
    device = {
        "id": "xxx",
        "name": "Телевизор",
        "names": ["Телевизор"],
        "type": "devices.types.media_device.tv",
        "icon_url": "https://avatars.mds.yandex.net/get-iot/icons-devices-devices.types.media_device.tv.svg/orig",
        "state": "online",
        "groups": [],
        "room": "Комната",
        "capabilities": [
            {
                "retrievable": true,
                "type": "devices.capabilities.on_off",
                "state": {"instance": "on", "value": true},
                "parameters": {"split": false},
            },
            {
                "retrievable": false,
                "type": "devices.capabilities.range",
                "state": null,
                "parameters": {
                    "instance": "volume",
                    "name": "громкость",
                    "unit": "",
                    "random_access": true,
                    "looped": false,
                    "range": {"min": -100, "max": 100, "precision": 1},
                },
            },
            {
                "retrievable": false,
                "type": "devices.capabilities.range",
                "state": null,
                "parameters": {
                    "instance": "channel",
                    "name": "канал",
                    "unit": "",
                    "random_access": true,
                    "looped": false,
                },
            },
            {
                "retrievable": false,
                "type": "devices.capabilities.toggle",
                "state": null,
                "parameters": {"instance": "mute", "name": "без звука"},
            },
            {
                "retrievable": false,
                "type": "devices.capabilities.toggle",
                "state": null,
                "parameters": {"instance": "pause", "name": "пауза"},
            },
        ],
        "properties": [],
        "skill_id": "xxx",
        "external_id": "xxx_TV",
        "favorite": false,
    }

    state = update_ha_state(YandexMediaPlayer, device, config={})
    assert state.state == "on"
    assert state.attributes == {
        "device_class": "tv",
        "friendly_name": "Телевизор",
        "icon": "mdi:television-classic",
        "supported_features": (
            MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
            | MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.VOLUME_STEP
            | MediaPlayerEntityFeature.PLAY
        ),
    }


@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize(
    ("media_type", "media_id", "source"),
    [
        ("application/unknown", "PRIVATE_PHRASE", "text"),
        (
            "audio/unknown:PRIVATE_SKILL",
            "custom://user:PRIVATE_PASSWORD@example.invalid/asset.bin?token=PRIVATE_QUERY",
            "url",
        ),
    ],
)
def test_unsupported_media_keeps_safe_diagnostic_context(
    local, media_type, media_id, source, caplog
):
    async def scenario():
        station = YandexStation(
            YandexQuasar(MagicMock()),
            {
                "id": "cloud-device",
                "name": "Synthetic station",
                "capabilities": [],
                "quasar_info": {
                    "device_id": "glagol-device",
                    "platform": "yandexstation",
                },
            },
        )
        station.entity_id = "media_player.synthetic_station"
        if local:
            station.local_state = {"playerState": {"isPlaying": False}}
        expectation = (
            pytest.raises(HomeAssistantError)
            if local and source == "url"
            else nullcontext()
        )
        with caplog.at_level(logging.DEBUG), expectation:
            await station.async_play_media(media_type, media_id, extra={})

        messages = [record.getMessage() for record in caplog.records]
        warning = next(
            record for record in caplog.records if record.levelno == logging.WARNING
        )
        message = warning.getMessage()
        assert f"transport={'local' if local else 'cloud'}" in message
        assert f"media_type={media_type.partition(':')[0]}" in message
        assert f"source={source}" in message
        assert station.entity_id in message
        for secret in ("PRIVATE_PHRASE", "PRIVATE_PASSWORD", "PRIVATE_QUERY", "PRIVATE_SKILL"):
            assert all(secret not in message for message in messages)

        # The integration's debug page must render parameterized log fields too.
        handler = object.__new__(YandexDebug)
        handler.text = ""
        handler.handle(warning)
        assert f"source={source}" in handler.text
        assert f"media_type={media_type.partition(':')[0]}" in handler.text
        assert "%s" not in handler.text

    asyncio.run(scenario())


def local_station_device(**extra):
    device = {
        "id": "cloud-device",
        "name": "Synthetic station",
        "type": "devices.types.smart_speaker.yandex.station",
        "capabilities": [],
        "host": "127.0.0.1",
        "port": 1905,
        "quasar_info": {"device_id": "glagol-device", "platform": "yandexstation"},
    }
    device.update(extra)
    return device


def quiet_hass():
    """hass stub whose media_player enumeration returns [] without exceptions."""
    component = MagicMock()
    component.entities = []
    hass = MagicMock()
    hass.data = {
        DOMAIN: {DATA_CONFIG: {}},
        "entity_components": {"media_player": component},
    }
    return hass


def test_restart_media_command_reconnects_local_session():
    async def scenario():
        station = YandexStation(YandexQuasar(MagicMock()), local_station_device())
        station.entity_id = "media_player.synthetic_station"
        station.local_state = {"aliceState": "IDLE", "volume": 0.3}

        first, second = FakeWS([]), FakeWS([])
        session = MagicMock()
        session.ws_connect = AsyncMock(side_effect=[first, second])
        station.glagol = glagol = YandexGlagol(
            session, station.device, token="synthetic-local"
        )
        await glagol.start_or_restart()
        await asyncio.sleep(0.05)
        assert session.ws_connect.await_count == 1
        reader = glagol._connect_task

        await station.async_play_media("restart", "0.01", extra={})

        # the restart command must not leave the local client terminal-stopped
        assert glagol._stopped is False
        assert first.closed
        assert reader.done()
        await asyncio.sleep(0.1)
        assert session.ws_connect.await_count == 2
        assert glagol.ws is second

        # unload semantics stay terminal after a restart-driven reconnect
        await glagol.stop()
        assert glagol._stopped is True
        assert glagol._connect_task is None
        await glagol.start_or_restart()
        assert glagol._connect_task is None

    asyncio.run(scenario())


def test_hdmi_switch_preserves_external_sync_sources():
    async def scenario():
        quasar = YandexQuasar(MagicMock())
        station = YandexStation(quasar, local_station_device(hdmi_audio=True))
        station.async_write_ha_state = MagicMock()
        # external players enumerated by YandexStation.init_local_mode
        station.sync_sources = {
            "Kodi": {"entity_id": "media_player.kodi", "name": "Kodi"}
        }
        station._attr_source_list = [SOURCE_STATION, SOURCE_HDMI, "Kodi"]
        station._attr_source = SOURCE_HDMI

        quasar.get_device_config = AsyncMock(return_value=({"hdmiAudio": True}, 1))
        quasar.set_device_config = AsyncMock()

        # switching the built-in source keeps external players listed
        await station.async_select_source(SOURCE_STATION)
        assert station.source == SOURCE_STATION
        assert station.source_list == [SOURCE_STATION, SOURCE_HDMI, "Kodi"]

        # Selecting an external source does not change the known HDMI setting.
        station._attr_source = "Kodi"
        await station.sync_hdmi_audio()
        assert station.hdmi_audio is False
        assert station.source_list == [SOURCE_STATION, SOURCE_HDMI, "Kodi"]
        assert station.source == "Kodi"

    asyncio.run(scenario())


def test_initial_hdmi_discovery_with_external_sources():
    async def scenario():
        quasar = YandexQuasar(MagicMock())
        station = YandexStation(quasar, local_station_device())
        station.async_write_ha_state = MagicMock()
        # external sources are enumerated before the HDMI config is known
        station.sync_sources = {
            "Kodi": {"entity_id": "media_player.kodi", "name": "Kodi"}
        }
        station._attr_source_list = [SOURCE_STATION, "Kodi"]
        station._attr_source = "Kodi"
        quasar.get_device_config = AsyncMock(return_value=({"hdmiAudio": True}, 1))

        await station.init_hdmi_audio()

        quasar.get_device_config.assert_awaited_once()
        assert station.hdmi_audio is True
        assert station.source_list == [SOURCE_STATION, SOURCE_HDMI, "Kodi"]
        assert station.source == "Kodi"

        # a cached HDMI state is not refetched
        cached = YandexStation(quasar, local_station_device(hdmi_audio=False))
        cached.async_write_ha_state = MagicMock()
        await cached.init_hdmi_audio()
        quasar.get_device_config.assert_awaited_once()

    asyncio.run(scenario())


def test_hdmi_refresh_task_is_tracked_deduplicated_and_awaited_on_removal():
    async def scenario():
        quasar = YandexQuasar(MagicMock())
        quasar.owned_glagol_ids = {"glagol-device"}
        station = YandexStation(quasar, local_station_device())
        station.hass = quiet_hass()
        station.async_write_ha_state = MagicMock()
        calls = []
        release = asyncio.Event()

        async def blocked_config(device):
            calls.append(device)
            await release.wait()
            return {"hdmiAudio": True}, 1

        quasar.get_device_config = AsyncMock(side_effect=blocked_config)

        await station.init_local_mode()
        task = station._hdmi_refresh_task
        assert task is not None and not task.done()
        await asyncio.sleep(0.05)
        assert len(calls) == 1

        # repeated init must not spawn a second concurrent HDMI refresh
        await station.init_local_mode()
        await asyncio.sleep(0.05)
        assert len(calls) == 1
        assert station._hdmi_refresh_task is task

        # unload observes the cancellation before returning
        await station.async_will_remove_from_hass()
        assert task.done()
        assert station._hdmi_refresh_task is None

    asyncio.run(scenario())


def test_init_local_mode_adopts_fetched_token_into_existing_empty_glagol():
    async def scenario():
        ws = FakeWS([
            ws_text(json.dumps({"state": {"aliceState": "IDLE", "volume": 0.3}}))
        ])
        session = MagicMock()
        session.ws_connect = AsyncMock(return_value=ws)
        quasar = YandexQuasar(session)
        quasar.owned_glagol_ids = {"glagol-device"}
        quasar.get_device_config = AsyncMock(return_value=({}, 1))
        station = YandexStation(quasar, local_station_device())
        station.entity_id = "media_player.synthetic_station"
        station.hass = quiet_hass()
        station.async_write_ha_state = MagicMock()

        # setup while the token is still missing: the empty client only waits
        await station.init_local_mode()
        assert station.glagol.device_token is None
        await asyncio.sleep(0.05)
        assert session.ws_connect.await_count == 0

        # post-publication recovery saved the fetched token, then re-inits
        quasar.device_tokens["glagol-device"] = {"yandexstation": "adopted-token"}
        await station.init_local_mode()

        assert station.glagol.device_token == "adopted-token"
        await asyncio.sleep(0.1)
        assert session.ws_connect.await_count == 1
        assert station.glagol.ws is ws

        # a live accepted connection keeps its own token on later re-inits
        for _ in range(100):
            if station.glagol.token_state is TokenState.ACCEPTED:
                break
            await asyncio.sleep(0.01)
        assert station.glagol.token_state is TokenState.ACCEPTED
        quasar.device_tokens["glagol-device"] = {"yandexstation": "other-token"}
        await station.init_local_mode()
        assert station.glagol.device_token == "adopted-token"
        assert session.ws_connect.await_count == 1

        await station.glagol.stop()

    asyncio.run(scenario())


def test_failed_hdmi_refresh_does_not_abort_entity_teardown():
    async def scenario():
        cache = MagicMock()
        cache.async_save_local_config = AsyncMock(side_effect=RuntimeError("cache_closed"))
        quasar = YandexQuasar(MagicMock(), cache_manager=cache)
        quasar.get_device_config = AsyncMock(return_value=({"hdmiAudio": True}, 1))
        station = YandexStation(quasar, local_station_device())
        station.glagol = YandexGlagol(MagicMock(), station.device)
        station._hdmi_refresh_task = asyncio.create_task(station.init_hdmi_audio())
        await asyncio.sleep(0)
        await station.async_will_remove_from_hass()
        assert station._hdmi_refresh_task is None
        assert station.glagol._stopped

    asyncio.run(scenario())


def test_domain_shutdown_stops_guarded_hdmi_before_closing_its_cache(tmp_path):
    async def scenario():
        from custom_components.yandex_station.core.lifecycle import get_lifecycle

        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            session = MagicMock()
            session.async_stop = AsyncMock()
            quasar = YandexQuasar(session, cache_manager=cache)
            device = local_station_device()
            quasar.devices = [device]
            await cache.async_save_devices([device])
            station = YandexStation(quasar, device)
            device["entity"] = station
            lifecycle = get_lifecycle(cache.hass)
            lifecycle.runtimes[cache.entry_id] = quasar
            started, release = asyncio.Event(), asyncio.Event()

            async def configuration(_):
                started.set()
                await release.wait()
                return {"hdmiAudio": True}, 1

            quasar.get_device_config = configuration
            station._hdmi_refresh_task = task = asyncio.create_task(station.init_hdmi_audio())
            await started.wait()
            try:
                await lifecycle.async_stop()
                assert task.cancelled()
                release.set()
                await asyncio.sleep(0)
                assert station.hdmi_audio is None
                assert "hdmi_audio" not in device
                assert cache.snapshot.local_config == {}
                restored = await manager().async_load()
                assert restored.local_config == {}
                await station.async_will_remove_from_hass()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_local_initialization_cannot_schedule_hdmi_after_entity_removal():
    async def scenario():
        quasar = YandexQuasar(MagicMock())
        quasar.owned_glagol_ids = {"glagol-device"}
        quasar.device_tokens = {"glagol-device": {"yandexstation": "replacement"}}
        quasar.get_device_config = AsyncMock(return_value=({"hdmiAudio": True}, 1))
        station = YandexStation(quasar, local_station_device())
        station.hass = quiet_hass()
        station.async_write_ha_state = MagicMock()
        station.glagol = YandexGlagol(MagicMock(), station.device)
        station.glagol.url = "wss://127.0.0.1:1905"
        cancelling, release = asyncio.Event(), asyncio.Event()

        async def old_reader():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await release.wait()
                raise

        station.glagol._connect_task = asyncio.create_task(old_reader())
        await asyncio.sleep(0)
        initialization = asyncio.create_task(station.init_local_mode())
        await asyncio.wait_for(cancelling.wait(), 1)
        removal = asyncio.create_task(station.async_will_remove_from_hass())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(initialization, removal), 1)
        await asyncio.sleep(0)
        assert station.hdmi_audio is None
        assert station._hdmi_refresh_task is None
        quasar.get_device_config.assert_not_awaited()

    asyncio.run(scenario())


def test_hdmi_projection_and_device_change_before_store_without_stale_reapply(tmp_path):
    async def scenario():
        from datetime import timedelta
        from homeassistant import bootstrap, loader
        from homeassistant.helpers.entity_component import EntityComponent
        from homeassistant.helpers.entity_platform import EntityPlatform

        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            hass = cache.hass
            hass.config.skip_pip = True
            if hasattr(loader, "async_setup"):
                loader.async_setup(hass)
            await loader.async_get_custom_components(hass)
            if hasattr(bootstrap, "async_load_base_functionality"):
                await bootstrap.async_load_base_functionality(hass)
            else:
                from homeassistant.helpers import (
                    area_registry, device_registry, entity_registry, issue_registry,
                )
                await area_registry.async_load(hass)
                await device_registry.async_load(hass)
                await entity_registry.async_load(hass)
                await issue_registry.async_load(hass)
            hass.data[DOMAIN] = {DATA_CONFIG: {}}
            logger = logging.getLogger(__name__)
            EntityComponent(logger, "media_player", hass)
            platform = EntityPlatform(
                hass=hass, logger=logger, domain="media_player",
                platform_name=DOMAIN, platform=None,
                scan_interval=timedelta(hours=1), entity_namespace=None,
            )
            device = local_station_device()
            await cache.async_save_devices([device])
            quasar = YandexQuasar(MagicMock(), cache_manager=cache)
            quasar.devices = [device]
            quasar.owned_glagol_ids = {"glagol-device"}
            quasar.get_device_config = AsyncMock(return_value=({"hdmiAudio": True}, 1))
            changed = asyncio.Event()

            async def set_config(*_):
                changed.set()

            quasar.set_device_config = set_config
            station = YandexStation(quasar, device)
            # Register a real entity before publishing. Without an address,
            # adding it does not start unrelated local/cloud discovery.
            host = device.pop("host")
            await platform.async_add_entities([station])
            device["host"] = host
            assert hass.states.get(station.entity_id) is not None
            station.async_set_state({"state": {"aliceState": "IDLE", "volume": 0.3}})
            first_started, second_started = asyncio.Event(), asyncio.Event()
            release_first, release_second = asyncio.Event(), asyncio.Event()
            original_save = cache._store.async_save

            async def delayed_save(payload):
                enabled = payload["local_config"]["glagol-device"]["hdmi_audio"]
                (first_started if enabled else second_started).set()
                await (release_first if enabled else release_second).wait()
                await original_save(payload)

            tasks = []
            with patch.object(cache._store, "async_save", side_effect=delayed_save):
                try:
                    tasks.append(asyncio.create_task(station.init_hdmi_audio()))
                    await asyncio.wait_for(first_started.wait(), 2)
                    assert station.hdmi_audio is True
                    assert device["hdmi_audio"] is True
                    assert station.source == SOURCE_HDMI
                    assert station.device_class == "tv"

                    tasks.append(asyncio.create_task(
                        station.async_select_source(SOURCE_STATION)
                    ))
                    await asyncio.wait_for(changed.wait(), 2)
                    # The second accepted cloud transition applies even while
                    # the first Store write still holds the cache lock.
                    assert station.hdmi_audio is False
                    assert device["hdmi_audio"] is False
                    assert station.source == SOURCE_STATION
                    published = cache.hass.states.get(station.entity_id)
                    assert published.attributes["source"] == SOURCE_STATION
                    assert published.attributes["source_list"] == [
                        SOURCE_STATION, SOURCE_HDMI,
                    ]

                    release_first.set()
                    await asyncio.wait_for(tasks[0], 2)
                    await asyncio.wait_for(second_started.wait(), 2)
                    # Resuming the older refresh must not restore its True.
                    assert station.hdmi_audio is False
                    assert device["hdmi_audio"] is False
                    assert station.source == SOURCE_STATION
                finally:
                    release_first.set()
                    release_second.set()
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    await platform.async_reset()
                    for result in results:
                        if isinstance(result, BaseException):
                            raise result
            restored = await manager().async_load()
            assert restored.local_config["glagol-device"]["hdmi_audio"] is False

    asyncio.run(scenario())


def test_hdmi_accepted_store_write_finishes_during_domain_shutdown(tmp_path):
    async def scenario():
        from custom_components.yandex_station.core.lifecycle import get_lifecycle

        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            device = local_station_device()
            await cache.async_save_devices([device])
            session = MagicMock()
            session.async_stop = AsyncMock()
            quasar = YandexQuasar(session, cache_manager=cache)
            quasar.devices = [device]
            quasar.get_device_config = AsyncMock(return_value=({"hdmiAudio": True}, 1))
            station = YandexStation(quasar, device)
            device["entity"] = station
            lifecycle = get_lifecycle(cache.hass)
            lifecycle.runtimes[cache.entry_id] = quasar
            started, release = asyncio.Event(), asyncio.Event()
            original_save = cache._store.async_save

            async def delayed_save(payload):
                started.set()
                await release.wait()
                await original_save(payload)

            stopping = None
            with patch.object(cache._store, "async_save", side_effect=delayed_save):
                station._hdmi_refresh_task = refresh = asyncio.create_task(
                    station.init_hdmi_audio()
                )
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    stopping = asyncio.create_task(lifecycle.async_stop())
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(asyncio.shield(stopping), 0.01)
                    assert not refresh.done()
                    assert station.hdmi_audio is True
                    assert device["hdmi_audio"] is True
                    assert station.source == SOURCE_HDMI
                finally:
                    release.set()
                    await asyncio.gather(
                        refresh, *([stopping] if stopping else []),
                        return_exceptions=True,
                    )
            assert refresh.cancelled()
            assert stopping is not None and stopping.exception() is None
            assert station.hdmi_audio is True
            assert device["hdmi_audio"] is True
            assert station.source == SOURCE_HDMI
            restored = await manager().async_load()
            assert restored.local_config["glagol-device"]["hdmi_audio"] is True

    asyncio.run(scenario())


def test_hdmi_store_error_keeps_known_state_flushes_and_restores_without_save(tmp_path):
    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            device = local_station_device(hdmi_audio=True)
            await cache.async_save_devices([device])
            await cache.async_save_local_config("glagol-device", hdmi_audio=True)
            quasar = YandexQuasar(MagicMock(), cache_manager=cache)
            quasar.devices = [device]
            quasar.get_device_config = AsyncMock(return_value=({"hdmiAudio": True}, 1))
            quasar.set_device_config = AsyncMock()
            station = YandexStation(quasar, device)
            station.async_write_ha_state = MagicMock()

            with patch.object(cache._store, "async_save", side_effect=OSError("disk")):
                await station.async_select_source(SOURCE_STATION)
            assert station.hdmi_audio is False
            assert device["hdmi_audio"] is False
            assert station.source == SOURCE_STATION
            before_flush = await manager().async_load()
            assert before_flush.local_config["glagol-device"]["hdmi_audio"] is True

            assert await cache.async_flush()
            restored_cache = manager()
            restored = await restored_cache.async_load()
            assert restored.local_config["glagol-device"]["hdmi_audio"] is False

            # Projecting the restored bool is not another observation or write.
            cached_quasar = YandexQuasar(MagicMock(), cache_manager=restored_cache)
            cached_quasar.get_device_config = AsyncMock(
                side_effect=AssertionError("cached HDMI must not be fetched")
            )
            with patch.object(restored_cache._store, "async_save") as saving:
                cached = YandexStation(cached_quasar, local_station_device(
                    **restored.local_config["glagol-device"]
                ))
                await cached.init_hdmi_audio()
                assert cached.hdmi_audio is False
                assert cached.source == SOURCE_STATION
                assert cached.source_list == [SOURCE_STATION, SOURCE_HDMI]
                assert cached.device_class == "tv"
                saving.assert_not_awaited()
            cached_quasar.get_device_config.assert_not_awaited()

    asyncio.run(scenario())
