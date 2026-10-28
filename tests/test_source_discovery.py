"""Exercise source discovery through real HA entity publication, isolated per case."""
import asyncio
from datetime import timedelta
import json
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
        "scenario", [
            "late", "partial", "features", "config_dict", "config_alias",
            "config_filtered", "config_empty", "hdmi", "hdmi_retry",
            "cleanup", "collision", "override",
            "restored", "config_hdmi_alias", "hdmi_unload",
            "rename",
            "hdmi_alias_retry",
        ]
    )(test_source_discovery)


async def exercise(scenario, config):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from homeassistant import bootstrap, loader
    import voluptuous as vol
    from homeassistant.components.media_player import (
        MediaPlayerEntity, MediaPlayerEntityFeature,
    )
    from homeassistant.config_entries import ConfigEntries
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.entity_component import EntityComponent
    from homeassistant.helpers.entity_platform import EntityPlatform
    from custom_components.yandex_station.core.const import DATA_CONFIG, DOMAIN
    from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
    from custom_components.yandex_station.core.yandex_station import YandexStation
    from custom_components.yandex_station.core.yandex_glagol import YandexGlagol

    calls = []

    class Player(MediaPlayerEntity):
        _attr_should_poll = False
        _attr_state = "off"
        _attr_supported_features = MediaPlayerEntityFeature.PLAY_MEDIA

        def __init__(self, name, uid=None):
            self._attr_name = name
            self._attr_unique_id = uid or name

        async def async_set_volume_level(self, volume_level):
            calls.append(("volume", self.entity_id, volume_level))

        async def async_media_pause(self):
            calls.append(("pause", self.entity_id))

    hass = HomeAssistant(config)
    hass.config.skip_pip = True
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await loader.async_get_custom_components(hass)
    await bootstrap.async_load_base_functionality(hass)
    logger = logging.getLogger(__name__)
    component = EntityComponent(logger, "media_player", hass)
    component.async_register_entity_service(
        "select_source", {vol.Required("source"): str}, "async_select_source"
    )
    component.async_register_entity_service(
        "volume_set", {vol.Required("volume_level"): vol.Coerce(float)},
        "async_set_volume_level",
    )
    component.async_register_entity_service("media_pause", {}, "async_media_pause")
    settings = {}
    hass.data[DOMAIN] = {DATA_CONFIG: settings}
    platforms = [EntityPlatform(hass=hass, logger=logger, domain="media_player",
        platform_name=name, platform=None, scan_interval=timedelta(hours=1),
        entity_namespace=None) for name in (DOMAIN, "synthetic")]
    quasar = YandexQuasar(None)
    quasar.get_device_config = AsyncMock(return_value=({"hdmiAudio": False}, 1))

    def make_station(uid="synthetic-station"):
        return YandexStation(quasar, {
            "id": uid, "name": uid, "capabilities": [],
            "host": "127.0.0.1", "port": 1905,
            "quasar_info": {"device_id": uid, "platform": (
                "yandexstation_2" if scenario.startswith("hdmi") else "yandexmini_2"
            )},
        })

    def frame(volume=0.3):
        return {"state": {"aliceState": "IDLE", "volume": volume, "playing": False,
            "playerState": {"hasPrev": False, "hasNext": False, "duration": 1,
                "type": "Track", "subtitle": "", "playlistType": "",
                "extra": {}, "entityInfo": {}, "id": "track", "progress": 0,
                "title": "Synthetic track"}}}

    async def add_player(name, **kwargs):
        player = Player(name, **kwargs)
        await platforms[1].async_add_entities([player])
        await hass.async_block_till_done()
        return player

    async def select(source):
        await hass.services.async_call("media_player", "select_source",
            {"entity_id": station.entity_id, "source": source}, blocking=True)

    station = make_station()
    # Replace only network I/O, never enumeration, events, state or registry.
    with (
        patch.object(YandexGlagol, "start_or_restart", new=AsyncMock()),
        patch.object(YandexGlagol, "send", new=AsyncMock()),
    ):
        try:
            first = None
            if scenario in ("partial", "collision"):
                first = await add_player("First")
            if scenario == "config_dict":
                settings["media_players"] = {"media_player.configured": "Configured"}
            elif scenario == "config_alias":
                settings["media_players"] = [
                    {"entity_id": "media_player.configured", "name": "Quiet",
                     "sync_volume": False, "quality": "nq", "codecs": "mp3"},
                    {"entity_id": "media_player.configured", "name": "Loud",
                     "sync_volume": "{{ volume_level / 2 }}",
                     "media_content_type": "url", "speaker_id": [station.entity_id]},
                ]
            elif scenario == "config_filtered":
                settings["media_players"] = [
                    {"entity_id": "media_player.configured", "name": "Excluded",
                     "speaker_id": ["media_player.other_station"]},
                ]
            elif scenario == "config_empty":
                settings["media_players"] = []
            elif scenario == "config_hdmi_alias":
                settings["media_players"] = {"media_player.receiver": "HDMI"}
            elif scenario == "hdmi_alias_retry":
                settings["media_players"] = {"media_player.receiver": "HDMI"}
                quasar.get_device_config.side_effect = [
                    RuntimeError("Synthetic HDMI not ready"), ({"hdmiAudio": False}, 1)
                ]
            elif scenario == "hdmi_unload":
                quasar.get_device_config.side_effect = RuntimeError("Synthetic not ready")
            elif scenario == "hdmi_retry":
                quasar.get_device_config.side_effect = [
                    RuntimeError("Synthetic HDMI not ready"), ({"hdmiAudio": True}, 1)
                ]

            await platforms[0].async_add_entities([station])
            station.async_set_state(frame())
            registry = er.async_get(hass)
            identity = registry.async_get(station.entity_id)
            if scenario == "hdmi_unload":
                entered, release = asyncio.Event(), asyncio.Event()

                async def delayed_config(device):
                    entered.set()
                    await release.wait()
                    return {"hdmiAudio": True}, 1

                quasar.get_device_config.side_effect = delayed_config
                initialization = hass.async_create_task(station.init_local_mode())
                await entered.wait()
                await station.async_remove()
                release.set()
                await initialization
                removed_sources = list(station.source_list or [])
                await add_player("After removal")
                assert station.source_list == removed_sources
                print(json.dumps({"scenario": scenario, "removed": True}))
                return
            if scenario == "hdmi_alias_retry":
                receiver = await add_player("Receiver")
                await select("HDMI")
                await station.init_local_mode()
                station.async_set_state(frame(0.5))
                await hass.async_block_till_done()
                assert station.source == "HDMI"
                assert ("volume", receiver.entity_id, 0.5) in calls
                quasar.get_device_config.side_effect = None
                quasar.set_device_config = AsyncMock()
                await select("Станция")
                await select("HDMI")
                await hass.async_block_till_done()
                assert quasar.set_device_config.await_count == 0
                assert station.hdmi_audio is False
                print(json.dumps({"scenario": scenario, "source": station.source}))
                return
            if scenario == "override":
                registry.async_update_entity(station.entity_id,
                    device_class="speaker", name="User station")
                await hass.async_block_till_done()
            if first:
                await select("First")
            if scenario == "hdmi":
                quasar.set_device_config = AsyncMock()
                await select("HDMI")
            if scenario == "restored":
                entry = registry.async_get_or_create(
                    "media_player", "synthetic", "later", suggested_object_id="later"
                )
                hass.states.async_set(entry.entity_id, "unavailable", {
                    "restored": True,
                    "supported_features": MediaPlayerEntityFeature.PLAY_MEDIA,
                })
                await hass.async_block_till_done()
                assert "Later" not in (station.source_list or [])
            later = await add_player(
                "First" if scenario == "collision" else "Later", uid="later"
            ) if scenario != "features" else Player("Later")
            if scenario == "features":
                later._attr_supported_features = MediaPlayerEntityFeature(0)
                await platforms[1].async_add_entities([later])
                await hass.async_block_till_done()
                assert "Later" not in (station.source_list or [])
                later._attr_supported_features = MediaPlayerEntityFeature.PLAY_MEDIA
                later._attr_available = False
                later.async_write_ha_state()
                await hass.async_block_till_done()
            state = hass.states.get(station.entity_id)
            sources = state.attributes.get("source_list", [])
            if scenario == "config_dict":
                assert sources == ["Станция", "Configured"]
            elif scenario == "config_hdmi_alias":
                assert sources == ["Станция", "HDMI"]
                assert registry.async_get(station.entity_id).original_device_class == "tv"
            elif scenario == "config_alias":
                assert sources == ["Станция", "Quiet", "Loud"]
                configured = await add_player("Configured")
                assert configured.entity_id == "media_player.configured"
                await select("Quiet")
                station.async_set_state(frame(0.4))
                await hass.async_block_till_done()
                assert not any(call[0] == "volume" for call in calls)
                await select("Loud")
                station.async_set_state(frame(0.6))
                await hass.async_block_till_done()
                assert ("volume", configured.entity_id, 0.3) in calls
            elif scenario == "config_filtered":
                assert sources == []
                assert registry.async_get(station.entity_id).original_device_class is None
            elif scenario in ("hdmi", "hdmi_retry"):
                if scenario == "hdmi_retry":
                    await select("Later")
                    await station.init_local_mode()
                    station.async_write_ha_state()
                    sources = station.source_list
                assert sources == ["Станция", "HDMI", "Later"]
                assert station.source == ("HDMI" if scenario == "hdmi" else "Later")
                assert station.device_class == "tv"
            else:
                assert sources == (["Станция", "First"] if scenario == "collision"
                    else ["Станция", "First", "Later"] if scenario == "partial"
                    else ["Станция", "Later"]), state
                assert registry.async_get(station.entity_id).original_device_class == "tv"
                assert registry.async_get(station.entity_id).id == identity.id
                assert calls == [], calls
                if first:
                    assert station.source == "First"
                    station.async_set_state(frame(0.5))
                    await hass.async_block_till_done()
                    assert ("volume", first.entity_id, 0.5) in calls
                    assert not any(call[1] == later.entity_id for call in calls)
                if scenario == "override":
                    current = registry.async_get(station.entity_id)
                    assert current.device_class == "speaker" and current.name == "User station"
                    assert hass.states.get(station.entity_id).attributes["device_class"] == "speaker"
                if scenario == "cleanup":
                    second = make_station("second-station")
                    await platforms[0].async_add_entities([second])
                    second.async_set_state(frame())
                    old_sources = list(station.source_list)
                    await station.async_remove()
                    await add_player("After removal")
                    assert station.source_list == old_sources
                    assert second.source_list == ["Станция", "Later", "After removal"]
                    replacement = make_station()
                    await platforms[0].async_add_entities([replacement])
                    replacement.async_set_state(frame())
                    await add_player("After reload")
                    assert replacement.source_list == [
                        "Станция", "Later", "After removal", "After reload",
                    ]
                elif scenario == "rename":
                    registry.async_update_entity(station.entity_id,
                        new_entity_id="media_player.renamed_station")
                    await hass.async_block_till_done()
                    await add_player("After rename")
                    renamed = hass.states.get("media_player.renamed_station")
                    assert renamed.attributes["source_list"] == [
                        "Станция", "Later", "After rename",
                    ]
                    assert registry.async_get(renamed.entity_id).id == identity.id
                else:
                    if not first:
                        later._attr_available = True
                        later.async_write_ha_state()
                        await select("Later")
                        station.async_set_state(frame(0.7))
                        await hass.async_block_till_done()
                        assert ("volume", later.entity_id, 0.7) in calls
                    for _ in range(3):
                        later.async_write_ha_state()
                    await hass.async_block_till_done()
                    assert station.source_list == sources
            print(json.dumps({"scenario": scenario, "sources": sources,
                              "originalClass": registry.async_get(station.entity_id).original_device_class}))
        finally:
            for platform in platforms:
                await platform.async_reset()
            await hass.async_stop()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as config:
        asyncio.run(exercise(sys.argv[1], config))
