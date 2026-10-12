import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.yandex_station import async_setup
from custom_components.yandex_station.core.const import DOMAIN
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar


def test_async_setup_and_services(tmp_path):
    async def scenario():
        hass = MagicMock(spec=HomeAssistant)
        hass.data = {}
        hass.bus = MagicMock()
        hass.services = MagicMock()
        registered_services = {}
        def mock_register(domain, service, handler, **kwargs):
            registered_services[(domain, service)] = handler

        hass.services.async_register = mock_register
        hass.http = MagicMock()
        hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))

        # Mock zeroconf singleton to ensure _init_local_discovery succeeds without NameError
        mock_zc = MagicMock()
        with patch(
            "custom_components.yandex_station.core.utils.get_zeroconf_singleton",
            AsyncMock(return_value=mock_zc),
        ):
            with patch(
                "custom_components.yandex_station.core.stream.async_get_clientsession",
                return_value=MagicMock(),
            ):
                with patch(
                    "custom_components.yandex_station.core.yandex_glagol.YandexIOListener.start"
                ) as mock_listener_start:
                    config = {DOMAIN: {"local_updates": True}}
                    success = await async_setup(hass, config)
                    assert success is True
                    mock_listener_start.assert_called_once_with(mock_zc)

        # Verify services registered
        assert (DOMAIN, "send_command") in registered_services
        assert ("tts", "yandex_station_say") in registered_services

        # Set up a mock Quasar with an entity in hass.data[DOMAIN]
        session = MagicMock()
        quasar = YandexQuasar(session)
        entity = MagicMock()
        entity.entity_id = "media_player.station_hall"
        entity.glagol = MagicMock()
        entity.glagol.send = AsyncMock(return_value={"status": "ok"})

        dev = {
            "id": "cloud_id_1",
            "name": "Станция Зал",
            "quasar_info": {"device_id": "did_1", "platform": "yandexstation"},
            "capabilities": [{"type": "devices.capabilities.on_off"}],
            "entity": entity,
        }
        quasar.devices = [dev]
        hass.data[DOMAIN]["user1"] = quasar

        # Test send_command
        send_command = registered_services[(DOMAIN, "send_command")]
        call = MagicMock(spec=ServiceCall)
        call.data = {"entity_id": "media_player.station_hall", "text": "Привет"}

        with patch(
            "homeassistant.helpers.service.async_extract_entity_ids",
            AsyncMock(return_value=["media_player.station_hall"]),
        ):
            with patch(
                "homeassistant.helpers.service.remove_entity_service_fields",
                return_value={"text": "Привет"},
            ):
                resp = await send_command(call)
                assert resp == {"status": "ok"}
                entity.glagol.send.assert_called_once_with(
                    {"text": "Привет", "command": "sendText"}
                )

        # Test yandex_station_say finds station across quasar accounts
        yandex_station_say = registered_services[("tts", "yandex_station_say")]
        say_call = MagicMock(spec=ServiceCall)
        say_call.data = {"message": "Проверка речи"}
        hass.services.async_call = AsyncMock()

        await yandex_station_say(say_call)
        hass.services.async_call.assert_called_once()
        args = hass.services.async_call.call_args[0]
        assert args[0] == "media_player"
        assert args[1] == "play_media"
        assert args[2]["entity_id"] == "media_player.station_hall"
        assert args[2]["media_content_id"] == "Проверка речи"

    asyncio.run(scenario())
