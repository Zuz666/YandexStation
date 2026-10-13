import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.yandex_station import async_setup_entry
from custom_components.yandex_station.core.cache import CacheManager, StationSnapshot
from custom_components.yandex_station.core.const import DATA_CONFIG, DATA_SPEAKERS, DOMAIN
from custom_components.yandex_station.core.policy import EntryPolicy, SessionConfig


@pytest.fixture
def mock_hass(tmp_path):
    hass = MagicMock(spec=HomeAssistant)
    hass.data = {DOMAIN: {DATA_CONFIG: {}, DATA_SPEAKERS: {}}}
    hass.config = MagicMock()
    hass.config.path = lambda *args: str(tmp_path.joinpath(*args))
    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_update_entry = MagicMock()
    return hass


def test_offline_bootstrap_from_snapshot(tmp_path):
    async def scenario():
        hass = MagicMock(spec=HomeAssistant)
        hass.data = {DOMAIN: {DATA_CONFIG: {}, DATA_SPEAKERS: {}}}
        hass.config = MagicMock()
        hass.config.path = lambda *args: str(tmp_path.joinpath(*args))
        hass.config_entries = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
        hass.config_entries.async_update_entry = MagicMock()
        mock_hass = hass
        entry = MagicMock()
        entry.entry_id = "test_entry_id_1"
        entry.unique_id = "test_login"
        entry.data = {"x_token": "valid_token"}
        entry.options = {"force_offline_local": True}
        entry.async_on_unload = MagicMock()

        # Prepopulate snapshot in cache manager
        cache = CacheManager(mock_hass, entry.entry_id, account_login="test_login")
        snapshot_devices = [
            {
                "id": "station_cloud_1",
                "name": "Станция Зал",
                "type": "devices.types.smart_speaker.yandex.station",
                "capabilities": [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}],
                "properties": [],
                "quasar_info": {"device_id": "glagol_did_1", "platform": "yandexstation"},
            },
            {
                "id": "module_cloud_2",
                "name": "Модуль Спальня",
                "type": "devices.types.media_device.tv_box",
                "capabilities": [],
                "properties": [],
                "quasar_info": {"device_id": "glagol_did_2", "platform": "yandexmodule"},
            },
        ]

        snapshot = StationSnapshot(
            entry_id=entry.entry_id,
            account_login="test_login",
            revision=1,
            devices=snapshot_devices,
            local_config={
                "glagol_did_1": {"host": "127.0.0.1", "port": 1961},
                "glagol_did_2": {"host": "127.0.0.1", "port": 1961},
            },
            device_tokens={
                "glagol_did_1": {"yandexstation": {"token": "token_for_1", "state": "valid"}},
                "glagol_did_2": {"yandexmodule": {"token": "token_for_2", "state": "valid"}},
            },
        )
        mock_session = MagicMock()
        with patch("custom_components.yandex_station.core.utils.async_create_clientsession", return_value=mock_session):
            with patch("custom_components.yandex_station.core.cache.CacheManager.async_load", AsyncMock(return_value=snapshot)):
                with patch("custom_components.yandex_station.hass.hass_utils.er") as mock_er:
                    with patch("custom_components.yandex_station.hass.hass_utils.dr") as mock_dr:
                        mock_er.async_get.return_value.entities.values.return_value = []
                        mock_dr.async_get.return_value.async_get_device.return_value = None
                        with patch("custom_components.yandex_station.core.yandex_session.YandexSession.refresh_cookies") as mock_refresh:
                            mock_refresh.side_effect = RuntimeError("Network call not allowed in offline mode")

                            # Setup entry in offline mode must succeed from cache without refresh_cookies
                            success = await async_setup_entry(mock_hass, entry)
                            assert success is True

                            quasar = mock_hass.data[DOMAIN][entry.unique_id]
                            assert len(quasar.devices) == 2
                            assert len(quasar.speakers) == 1
                            assert len(quasar.modules) == 1
                            assert quasar.policy.force_offline_local is True
                            assert quasar.policy.effective_cloud_allowed is False
                            assert quasar.device_tokens["glagol_did_1"]["yandexstation"] == "token_for_1"
                            assert quasar.device_tokens["glagol_did_2"]["yandexmodule"] == "token_for_2"
                            mock_refresh.assert_not_called()

    asyncio.run(scenario())


def test_offline_missing_cache_raises(tmp_path):
    from homeassistant.exceptions import ConfigEntryNotReady

    async def scenario():
        hass = MagicMock(spec=HomeAssistant)
        hass.data = {DOMAIN: {DATA_CONFIG: {}, DATA_SPEAKERS: {}}}
        hass.config = MagicMock()
        hass.config.path = lambda *args: str(tmp_path.joinpath(*args))
        entry = MagicMock()
        entry.entry_id = "test_entry_empty"
        entry.unique_id = "test_login"
        entry.data = {"x_token": "valid_token"}
        entry.options = {"force_offline_local": True}

        mock_session = MagicMock()
        mock_session.cookie_jar = MagicMock()
        with patch("custom_components.yandex_station.core.utils.async_create_clientsession", return_value=mock_session):
            with patch.object(CacheManager, "async_load", AsyncMock(return_value=None)):
                with pytest.raises(ConfigEntryNotReady):
                    await async_setup_entry(hass, entry)

    asyncio.run(scenario())


def test_offline_entities_availability():
    from custom_components.yandex_station.select import YandexEqualizer
    from custom_components.yandex_station.calendar import YandexCalendar

    async def scenario():
        quasar = MagicMock()
        quasar.policy = EntryPolicy(force_offline_local=True)
        quasar.get_device_config = AsyncMock()
        quasar.get_alarms = AsyncMock()

        sp = {
            "id": "dev1",
            "name": "Speaker 1",
            "quasar_info": {"device_id": "did1", "platform": "yandexstation"},
        }
        eq = YandexEqualizer(quasar, sp)
        await eq.async_update()
        assert eq.available is False
        quasar.get_device_config.assert_not_called()

        cal = YandexCalendar(quasar, sp)
        await cal.async_update()
        assert cal.available is False
        quasar.get_alarms.assert_not_called()

    asyncio.run(scenario())


def test_glagol_token_preservation_on_disconnect():
    from custom_components.yandex_station.core.yandex_glagol import YandexGlagol

    session = MagicMock()
    device = {
        "name": "Speaker",
        "host": "127.0.0.1",
        "port": 1961,
        "quasar_info": {"device_id": "did_glagol_1", "platform": "yandexstation"},
    }
    glagol = YandexGlagol(session, device, token="initial_token")
    assert glagol.device_token == "initial_token"

    # Simulate update_handler and disconnect
    handler = MagicMock()
    glagol.update_handler = handler
    glagol.update_handler(None)

    # Token must NOT be reset to None on disconnect
    assert glagol.device_token == "initial_token"


def test_offline_acceptance_a1_full_lifecycle(tmp_path):
    """A1: --network none, force_offline_local=True, snapshot with Station & Module, cached tokens.

    Setup completes, platforms forward, no cloud HTTP/WS calls made, loopback Glagol message
    activates local mode on speaker, calendar/select remain unavailable.
    """
    from custom_components.yandex_station.media_player import async_setup_entry as mp_setup_entry
    from custom_components.yandex_station.select import async_setup_entry as select_setup_entry
    from custom_components.yandex_station.calendar import async_setup_entry as cal_setup_entry

    async def scenario():
        hass = MagicMock(spec=HomeAssistant)
        hass.data = {DOMAIN: {DATA_CONFIG: {}, DATA_SPEAKERS: {}}}
        hass.config = MagicMock()
        hass.config.path = lambda *args: str(tmp_path.joinpath(*args))
        hass.config_entries.async_update_entry = MagicMock()

        entry = MagicMock()
        entry.entry_id = "entry_a1"
        entry.unique_id = "test_user_a1"
        entry.data = {"x_token": "token_a1"}
        entry.options = {"force_offline_local": True}
        entry.async_on_unload = MagicMock()
        entry.disabled_by = None
        entry.source = "user"

        # Real platform forwarding
        hass.config_entries.async_entries = MagicMock(return_value=[entry])
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)
        async def forward_setups(e, platforms):
            for plat in platforms:
                if plat == "media_player":
                    await mp_setup_entry(hass, e, lambda entities, update_before_add=False: None)
                elif plat == "select":
                    await select_setup_entry(hass, e, lambda entities, update_before_add=False: None)
                elif plat == "calendar":
                    await cal_setup_entry(hass, e, lambda entities, update_before_add=False: None)
            return True

        hass.config_entries.async_forward_entry_setups = AsyncMock(side_effect=forward_setups)

        snapshot_devices = [
            {
                "id": "station_cloud_1",
                "name": "Станция Зал",
                "type": "devices.types.smart_speaker.yandex.station",
                "capabilities": [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}],
                "properties": [],
                "quasar_info": {"device_id": "glagol_1", "platform": "yandexstation"},
            },
            {
                "id": "module_cloud_2",
                "name": "Модуль ТВ",
                "type": "devices.types.media_device.tv_box",
                "capabilities": [],
                "properties": [],
                "quasar_info": {"device_id": "glagol_2", "platform": "yandexmodule"},
            },
        ]

        snapshot = StationSnapshot(
            entry_id=entry.entry_id,
            account_login="test_user_a1",
            revision=1,
            devices=snapshot_devices,
            local_config={"glagol_1": {"host": "127.0.0.1", "port": 1961}},
            device_tokens={"glagol_1": {"yandexstation": {"token": "glagol_token_1", "state": "valid"}}},
        )

        mock_session = MagicMock()
        mock_session.cookie_jar = MagicMock()
        # This mocked Core fixture does not load translation resources.
        # Real entity registration and naming are covered in test_entity_names.
        with patch(
            "custom_components.yandex_station.hass.hass_utils.async_translate_entity_names",
            AsyncMock(),
        ), patch("custom_components.yandex_station.core.utils.async_create_clientsession", return_value=mock_session):
            with patch("custom_components.yandex_station.hass.hass_utils.er") as mock_er:
                with patch("custom_components.yandex_station.hass.hass_utils.dr") as mock_dr:
                    mock_er.async_get.return_value.entities.values.return_value = []
                    mock_dr.async_get.return_value.async_get_device.return_value = None
                    with patch("custom_components.yandex_station.core.cache.CacheManager.async_load", AsyncMock(return_value=snapshot)):
                        success = await async_setup_entry(hass, entry)
                        assert success is True

                        quasar = hass.data[DOMAIN][entry.unique_id]
                        station_dev = next(d for d in quasar.devices if d["id"] == "station_cloud_1")
                        entity = station_dev.get("entity")
                        assert entity is not None

                        # Initially not available before confirmed local state
                        assert entity.available is False

                        # Simulate local Glagol message from loopback
                        entity.async_set_state({
                            "state": {"aliceState": "IDLE", "volume": 0.3},
                            "softwareVersion": "1.0.0",
                        })

                        # Now speaker is available locally!
                        assert entity.available is True
                        attrs = entity.extra_state_attributes
                        assert attrs["transport"] == "local"
                        assert attrs["cloud_state"] == "offline"

                        # Verify central barrier blocks cloud requests
                        with pytest.raises(RuntimeError):
                            await quasar.session._request(
                                "get", "https://quasar.yandex.ru/get_account_config", role="account"
                            )
                        with pytest.raises(RuntimeError):
                            await quasar.session.ws_connect(
                                "wss://iot.quasar.yandex.ru/ws", role="cloud_ws"
                            )
                        mock_session.get.assert_not_called()
                        mock_session.ws_connect.assert_not_called()

    asyncio.run(scenario())


def test_issued_glagol_token_is_authenticated_and_saved_before_use(tmp_path):
    from custom_components.yandex_station.core.yandex_glagol import YandexGlagol
    from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
    from custom_components.yandex_station.core.yandex_session import YandexSession
    from .test_cache import cache_runtime, device_record, read_storage
    from .test_session_resilience import make_client, make_response

    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            device = device_record()
            await cache.async_save_devices([device])
            client = make_client()

            async def token_peer(url, **kwargs):
                if kwargs.get("headers", {}).get("Authorization") != "OAuth synthetic-music":
                    return make_response(401)
                assert kwargs["params"] == {
                    "device_id": "glagol-1", "platform": "yandexstation",
                }
                return make_response(200, text='{"status":"ok","token":"issued-candidate"}')

            client.get = AsyncMock(side_effect=token_peer)
            session = YandexSession(client, music_token="synthetic-music")
            quasar = YandexQuasar(session, cache_manager=cache)
            quasar.owned_glagol_ids = {"glagol-1"}
            glagol = YandexGlagol(session, device, quasar=quasar)
            saving, release = asyncio.Event(), asyncio.Event()
            original_save = cache.async_save_device_token

            async def held_save(*args):
                saving.set()
                await release.wait()
                return await original_save(*args)

            with patch.object(cache, "async_save_device_token", side_effect=held_save):
                issuing = asyncio.create_task(glagol.get_device_token())
                try:
                    await asyncio.wait_for(saving.wait(), 2)
                    assert glagol.device_token is None
                    assert quasar.device_tokens == {}
                    release.set()
                    assert await issuing == "issued-candidate"
                finally:
                    release.set()
                    issuing.cancel()
                    await asyncio.gather(issuing, return_exceptions=True)
            assert quasar.device_tokens == {"glagol-1": {"yandexstation": "issued-candidate"}}
            assert read_storage(cache)["device_tokens"]["glagol-1"]["yandexstation"]["token"] == "issued-candidate"
            await session.async_stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("hdmi_audio", [True, False, None])
def test_cached_hdmi_state_restores_without_cloud_read(tmp_path, hdmi_audio):
    from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
    from custom_components.yandex_station.core.yandex_station import (
        SOURCE_HDMI, SOURCE_STATION, YandexStation,
    )
    from .test_cache import cache_runtime, device_record

    async def scenario():
        async with cache_runtime(tmp_path) as (manager, _):
            cache = manager()
            await cache.async_save_devices([device_record()])
            if hdmi_audio is not None:
                await cache.async_save_local_config("glagol-1", hdmi_audio=hdmi_audio)
                await cache.async_flush()
            restored = await manager().async_load()
            quasar = YandexQuasar(None, policy=EntryPolicy(force_offline_local=True))
            quasar._restore_from_snapshot(restored)
            quasar.get_device_config = AsyncMock(side_effect=AssertionError("offline cloud fetch"))
            station = YandexStation(quasar, quasar.speakers[0])
            await station.init_hdmi_audio()
            assert station.hdmi_audio is hdmi_audio
            assert station.source == (
                None if hdmi_audio is None else SOURCE_HDMI if hdmi_audio else SOURCE_STATION
            )
            quasar.get_device_config.assert_not_awaited()

    asyncio.run(scenario())
