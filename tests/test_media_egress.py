import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.yandex_station.camera import YandexLyrics
from custom_components.yandex_station.core import protobuf
from custom_components.yandex_station.core.lifecycle import get_lifecycle
from custom_components.yandex_station.core.policy import EntryPolicy
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
from custom_components.yandex_station.core.yandex_station import YandexStation
from .test_cache import config_entry


def test_offline_local_media_bypasses_cloud_metadata_but_not_stream_policy(tmp_path):
    async def scenario():
        try:
            hass = HomeAssistant(str(tmp_path))
        except TypeError:
            hass = HomeAssistant()
        hass.config.config_dir = str(tmp_path)
        hass.config_entries = ConfigEntries(hass, {})
        lifecycle = get_lifecycle(hass)
        lifecycle.context.hass_url = "http://127.0.0.1:8123"
        policy = EntryPolicy(force_offline_local=True)
        quasar = YandexQuasar(None, policy=policy,
                              cache_manager=SimpleNamespace(entry_id="entry-1"))
        lifecycle.runtimes["entry-1"] = quasar
        device = {"id": "cloud-id", "name": "Synthetic station", "capabilities": [
            {"type": "devices.capabilities.on_off"}], "quasar_info": {
                "device_id": "lan-id", "platform": "yandexstation"}}
        station = YandexStation(quasar, device)
        station.hass = hass
        station.local_state = {"playerState": {"isPlaying": False}}
        station.glagol = SimpleNamespace(send=AsyncMock())
        with patch.object(hass.config_entries, "async_get_entry", return_value=config_entry()):
            try:
                with patch("custom_components.yandex_station.core.utils.get_media_payload",
                           new_callable=AsyncMock) as metadata:
                    await station.async_play_media(
                        "stream.mp3", "http://127.0.0.1:8097/track.mp3", extra={})
                    payload = station.glagol.send.await_args.args[0]
                    command = json.loads(protobuf.loads(payload["data"])[2].decode())
                    signed = command["stream"]["url"]
                    token = signed.rsplit("/", 1)[1].rsplit(".", 1)[0]
                    claims = jwt.decode(token, lifecycle.context.key,
                                        algorithms=["HS256"])
                    assert claims["entry_id"] == "entry-1"
                    assert claims["lan_only"] is True
                    metadata.assert_not_awaited()
                    sent = station.glagol.send.await_count
                    with pytest.raises(HomeAssistantError, match="media_destination_blocked"):
                        await station.async_play_media(
                            "stream.mp3", "http://198.51.100.10/private.mp3", extra={})
                    assert station.glagol.send.await_count == sent
                    metadata.assert_not_awaited()

                    with patch("custom_components.yandex_station.core.yandex_station.media_source.async_resolve_media",
                               new_callable=AsyncMock) as resolver:
                        with pytest.raises(HomeAssistantError, match="offline_media_source_blocked"):
                            await station.async_play_media(
                                "audio/mp3", "media-source://tts/cloud_provider/speech", extra={})
                        resolver.assert_not_awaited()

                station._attr_media_image_url = "https://example.invalid/private.jpg"
                station._attr_media_content_id = "synthetic-track"
                camera = YandexLyrics(quasar, device)
                camera.hass = hass
                device["entity"] = station
                with patch("custom_components.yandex_station.camera.async_get_clientsession") as session:
                    assert await camera.get_cover() is None
                    session.assert_not_called()
            finally:
                await hass.async_stop()

    asyncio.run(scenario())
