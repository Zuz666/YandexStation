"""Real HA publication tests, isolated from the legacy suite's asyncio overrides."""
import asyncio
from datetime import timedelta
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import AsyncMock, patch


def test_source_discovery(scenario):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), scenario],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ != "__main__":
    import pytest

    test_source_discovery = pytest.mark.parametrize(
        "scenario", ["discovery", "configured", "hdmi", "unload"]
    )(test_source_discovery)


async def exercise(scenario, config):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from homeassistant import bootstrap, loader
    from homeassistant.components.media_player import MediaPlayerEntity, MediaPlayerEntityFeature
    from homeassistant.config_entries import ConfigEntries
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.entity_component import EntityComponent
    from homeassistant.helpers.entity_platform import EntityPlatform
    from custom_components.yandex_station.core.const import DATA_CONFIG, DOMAIN
    from custom_components.yandex_station.core.yandex_glagol import YandexGlagol
    from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
    from custom_components.yandex_station.core.yandex_station import YandexStation

    class Player(MediaPlayerEntity):
        _attr_should_poll = False
        _attr_state = "off"
        _attr_supported_features = MediaPlayerEntityFeature.PLAY_MEDIA

        def __init__(self, name):
            self._attr_name = self._attr_unique_id = name

    hass = HomeAssistant(config)
    hass.config.skip_pip = True
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await loader.async_get_custom_components(hass)
    await bootstrap.async_load_base_functionality(hass)
    logger = logging.getLogger(__name__)
    EntityComponent(logger, "media_player", hass)
    settings = {"media_players": {"media_player.receiver": "HDMI"}} if scenario in (
        "configured", "hdmi"
    ) else {}
    hass.data[DOMAIN] = {DATA_CONFIG: settings}
    platforms = [EntityPlatform(hass=hass, logger=logger, domain="media_player",
        platform_name=name, platform=None, scan_interval=timedelta(hours=1),
        entity_namespace=None) for name in (DOMAIN, "synthetic")]
    quasar = YandexQuasar(None)
    quasar.get_device_config = AsyncMock(side_effect=RuntimeError("HDMI not ready"))
    quasar.set_device_config = AsyncMock()
    station = YandexStation(quasar, {
        "id": "station", "name": "Station", "capabilities": [],
        "host": "127.0.0.1", "port": 1905,
        "quasar_info": {"device_id": "station", "platform": (
            "yandexstation_2" if scenario in ("hdmi", "unload") else "yandexmini_2"
        )},
    })

    async def add_player(name):
        await platforms[1].async_add_entities([Player(name)])
        await hass.async_block_till_done()

    with patch.object(YandexGlagol, "start_or_restart", new=AsyncMock()):
        try:
            await platforms[0].async_add_entities([station])
            station.async_set_state({"state": {"aliceState": "IDLE", "volume": 0.3}})
            registry = er.async_get(hass)
            identity = registry.async_get(station.entity_id).id
            if scenario == "discovery":
                assert not station.source_list
                assert registry.async_get(station.entity_id).original_device_class is None
                entry = registry.async_get_or_create(
                    "media_player", "synthetic", "Later", suggested_object_id="later"
                )
                hass.states.async_set(entry.entity_id, "unavailable", {
                    "restored": True, "supported_features": MediaPlayerEntityFeature.PLAY_MEDIA,
                })
                await hass.async_block_till_done()
                await add_player("Later")
                assert station.source_list == ["Станция", "Later"]
                assert registry.async_get(station.entity_id).original_device_class == "tv"
                await station.async_select_source("Later")
                registry.async_update_entity(station.entity_id,
                    new_entity_id="media_player.renamed_station")
                await hass.async_block_till_done()
                await add_player("Second")
                state = hass.states.get("media_player.renamed_station")
                assert state.attributes["source_list"] == ["Станция", "Later", "Second"]
                assert state.attributes["source"] == "Later"
                assert state.attributes["device_class"] == "tv"
                assert registry.async_get(state.entity_id).id == identity
                await station.async_remove()
                sources = list(station.source_list)
                await add_player("After removal")
                assert station.source_list == sources
            elif scenario == "unload":
                entered, release = asyncio.Event(), asyncio.Event()

                async def delayed_config(device):
                    entered.set()
                    await release.wait()
                    return {"hdmiAudio": False}, 1

                quasar.get_device_config.side_effect = delayed_config
                initialization = hass.async_create_task(station.init_local_mode())
                await entered.wait()
                await station.async_remove()
                release.set()
                await initialization
                sources = list(station.source_list or [])
                await add_player("After removal")
                assert station.source_list == sources
            else:
                await add_player("Receiver")
                assert station.source_list == ["Станция", "HDMI"]
                assert registry.async_get(station.entity_id).original_device_class == "tv"
                if scenario == "hdmi":
                    await station.async_select_source("HDMI")
                    quasar.get_device_config.side_effect = None
                    quasar.get_device_config.return_value = ({"hdmiAudio": False}, 1)
                    await station.init_local_mode()
                    assert station.source == "HDMI"
                    await station.async_select_source("Станция")
                    await station.async_select_source("HDMI")
                    assert station.hdmi_audio is False
                    assert quasar.set_device_config.await_count == 0
            print(f"{scenario}: passed")
        finally:
            for platform in platforms:
                await platform.async_reset()
            await hass.async_stop()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as config:
        asyncio.run(exercise(sys.argv[1], config))
