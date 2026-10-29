import asyncio
import logging
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.helpers import device_registry

from custom_components.yandex_station.core.policy import EntryPolicy
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
from custom_components.yandex_station.binary_sensor import YandexStationLocalConnection
from custom_components.yandex_station.core.yandex_station import YandexStation
from .test_cache import device_record
from .test_local import _player_frame
from .test_ownership import ownership_runtime


async def verify_firmware_updates(tmp_path):
    async with ownership_runtime(tmp_path) as (hass, entries, _, _):
        entry = entries["a"]
        registry = device_registry.async_get(hass)
        registered = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={("yandex_station", "firmware-speaker")},
            name="Synthetic speaker", sw_version="old-version",
        )
        quasar = YandexQuasar(MagicMock(), policy=EntryPolicy(force_offline_local=True))
        station = YandexStation(
            quasar, device_record("cloud-speaker", "firmware-speaker")
        )
        station.hass = hass
        station.platform = SimpleNamespace(config_entry=entry)
        station.async_write_ha_state = MagicMock()
        state = {"aliceState": "IDLE", "volume": 0.3}
        station.async_set_state({"state": deepcopy(state), "softwareVersion": "old-version"})
        # A metadata-only change must not be dropped by player-state deduplication.
        station.async_set_state({"state": deepcopy(state), "softwareVersion": "new-version"})
        assert registry.async_get(registered.id).sw_version == "new-version"
        with patch.object(registry, "async_update_device", wraps=registry.async_update_device) as update:
            station.async_set_state({"state": deepcopy(state), "softwareVersion": "new-version"})
            station.async_set_state({"state": {**state, "volume": 0.5}, "softwareVersion": "new-version"})
            update.assert_not_called()
        assert station.local_state["volume"] == 0.5
        assert station.volume_level == 0.5
        print("Firmware-only update and duplicate-write suppression PASS")


def test_firmware_changes_without_player_state_change(tmp_path):
    asyncio.run(verify_firmware_updates(tmp_path))


@pytest.mark.parametrize("failure", ["missing", "exception"])
def test_registry_metadata_failure_preserves_playback_and_safe_context(tmp_path, caplog, failure):
    async def scenario():
        async with ownership_runtime(tmp_path) as (hass, entries, _, _):
            entry = entries["a"]
            registry = device_registry.async_get(hass)
            quasar = YandexQuasar(MagicMock(), policy=EntryPolicy(force_offline_local=True))
            station = YandexStation(
                quasar, device_record("cloud-speaker", "firmware-speaker")
            )
            station.hass = hass
            station.platform = SimpleNamespace(config_entry=entry)
            station.async_write_ha_state = MagicMock()
            frame = {
                "state": {"aliceState": "IDLE", "volume": 0.7},
                "softwareVersion": "new-version",
            }
            registered = None
            if failure == "exception":
                registered = registry.async_get_or_create(
                    config_entry_id=entry.entry_id,
                    identifiers={("yandex_station", "firmware-speaker")},
                    name="Synthetic speaker", sw_version="old-version",
                )
            with caplog.at_level(
                logging.DEBUG,
                logger="custom_components.yandex_station.core.yandex_station",
            ):
                if failure == "exception":
                    with patch.object(
                        registry, "async_update_device",
                        side_effect=RuntimeError("PRIVATE_TOKEN signed URL and payload"),
                    ):
                        station.async_set_state(deepcopy(frame))
                else:
                    station.async_set_state(deepcopy(frame))
            assert station.local_state == frame["state"]
            assert station.volume_level == 0.7
            metadata_logs = [
                record.getMessage() for record in caplog.records
                if "operation=update_device_info" in record.getMessage()
            ]
            expected_reason = "device_not_found" if failure == "missing" else "registry_error"
            assert any(f"reason={expected_reason}" in message for message in metadata_logs)
            if failure == "exception":
                assert any("exception=RuntimeError" in message for message in metadata_logs)
                assert registry.async_get(registered.id).sw_version == "old-version"
                # A failed metadata write must remain eligible on an identical frame.
                station.async_set_state(deepcopy(frame))
                assert registry.async_get(registered.id).sw_version == "new-version"
            assert "PRIVATE_TOKEN" not in caplog.text
            assert "signed URL and payload" not in caplog.text

    asyncio.run(scenario())


async def verify_firmware_only_diagnostics(tmp_path):
    async with ownership_runtime(tmp_path) as (hass, entries, _, _):
        entry = entries["a"]
        registry = device_registry.async_get(hass)
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={("yandex_station", "firmware-speaker")},
            name="Synthetic speaker", sw_version="old-version",
        )
        quasar = YandexQuasar(MagicMock(), policy=EntryPolicy(force_offline_local=True))
        station = YandexStation(
            quasar, device_record("cloud-speaker", "firmware-speaker")
        )
        station.hass = hass
        station.platform = SimpleNamespace(config_entry=entry)
        station.async_write_ha_state = MagicMock()

        frames, firmware = [], []
        quasar.subscribe_update("firmware-speaker_glagol", frames.append)
        quasar.subscribe_update(
            "firmware-speaker_glagol_firmware", firmware.append
        )

        sensor = YandexStationLocalConnection(quasar, entry, station.device)
        sensor.hass = hass
        sensor.async_write_ha_state = MagicMock()
        await sensor.async_added_to_hass()

        state = {"aliceState": "IDLE", "volume": 0.3}
        station.async_set_state({"state": deepcopy(state), "softwareVersion": "v1"})
        assert len(frames) == 1 and "state" in frames[0]
        assert firmware == []
        assert sensor.is_on is True
        assert sensor.extra_state_attributes["software_version"] == "v1"
        heartbeat = sensor.extra_state_attributes["last_heartbeat"]

        # identical state with identical firmware stays deduplicated
        station.async_set_state({"state": deepcopy(state), "softwareVersion": "v1"})
        assert len(frames) == 1
        assert firmware == []

        # a firmware-only change still reaches glagol subscribers, without
        # re-delivering the identical state frame
        station.async_set_state({"state": deepcopy(state), "softwareVersion": "v2"})
        assert len(frames) == 1
        assert firmware == [{"softwareVersion": "v2"}]

        # repeated identical firmware does not spam diagnostics
        station.async_set_state({"state": deepcopy(state), "softwareVersion": "v2"})
        assert len(frames) == 1
        assert firmware == [{"softwareVersion": "v2"}]

        assert sensor.is_on is True
        assert sensor.extra_state_attributes["software_version"] == "v2"
        assert sensor.extra_state_attributes["last_heartbeat"] == heartbeat

        # A rejected media heartbeat can still carry new firmware diagnostics.
        video = _player_frame(
            type="tv_show_episode", playerType="com.yandex.tv.videoplayer",
            subtitle="Три кота", title="2. Киношедевр",
        )
        station.async_set_state({**video, "softwareVersion": "v2"})
        heartbeat = sensor.extra_state_attributes["last_heartbeat"]
        background = _player_frame(type="Track", playerType="music_thin")
        background["state"]["playing"] = False
        station.async_set_state({**background, "softwareVersion": "v3"})
        assert station.media_title == "2. Киношедевр"
        assert station.media_series_title == "Три кота"
        assert sensor.is_on is True
        assert sensor.extra_state_attributes["software_version"] == "v3"
        assert sensor.extra_state_attributes["last_heartbeat"] == heartbeat


def test_firmware_only_dispatch_reaches_diagnostics_without_state_change(tmp_path):
    asyncio.run(verify_firmware_only_diagnostics(tmp_path))
