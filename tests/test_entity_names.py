import asyncio
import importlib
import logging
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from homeassistant import loader
from homeassistant.helpers import entity_registry
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.yandex_station.core.const import DOMAIN
from custom_components.yandex_station.core.policy import EntryPolicy
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
from .test_cache import device_record
from .test_ownership import ownership_runtime


async def verify_entity_names(tmp_path, language):
    async with ownership_runtime(tmp_path) as (hass, entries, client, lifecycle):
        if hasattr(loader, "async_setup"):
            loader.async_setup(hass)
        hass.config.language = language
        entry = entries["a"]
        device = device_record("cloud-speaker", "local-speaker", "yandexstation")
        device["name"] = "User speaker"
        quasar = YandexQuasar(MagicMock(), policy=EntryPolicy(
            force_offline_local=True, enable_diagnostic_sensors=True,
        ))
        quasar.devices = [device]
        quasar.owned_glagol_ids = {"local-speaker"}
        hass.data[DOMAIN][entry.unique_id] = quasar
        registry = entity_registry.async_get(hass)
        names = {
            "select": {"local-speaker_equalizer": ("Equalizer", "Эквалайзер")},
            "calendar": {"local-speaker_calendar": ("Alarms", "Будильники")},
            "camera": {"local-speaker_lyrics": ("Lyrics", "Текст")},
            "sensor": {
                "entry-a_cloud_health": ("Cloud connection status", "Статус связи с облаком"),
                "entry-a_inventory_source": ("Catalog source", "Источник каталога"),
                "entry-a_local-speaker_alice_state": ("Alice state", "Состояние Алисы"),
                "entry-a_local-speaker_transport": ("Control transport", "Транспорт управления"),
            },
            "binary_sensor": {
                "entry-a_local-speaker_local_connection": ("Local connection", "Локальное подключение"),
            },
        }
        try:
            importlib.import_module(f"custom_components.{DOMAIN}.conversation")
        except ImportError:
            pass
        else:
            names["conversation"] = {"local-speaker_conversation": ("Alice", "Алиса")}
        platforms = []
        try:
            for domain, entities in names.items():
                for unique_id in entities:
                    registry.async_get_or_create(
                        domain, DOMAIN, unique_id, config_entry=entry
                    )
                module = importlib.import_module(f"custom_components.{DOMAIN}.{domain}")
                platform = EntityPlatform(
                    hass=hass, logger=logging.getLogger(__name__), domain=domain,
                    platform_name=DOMAIN, platform=module,
                    scan_interval=timedelta(minutes=5), entity_namespace=None,
                )
                platforms.append(platform)
                assert await platform.async_setup_entry(entry)
                await hass.async_block_till_done()
                for unique_id, expected in entities.items():
                    entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
                    registered = registry.async_get(entity_id)
                    expected_name = expected[language == "ru"]
                    assert registered.original_name == expected_name, (domain, registered)
                    assert registered.unique_id == unique_id
                    state = hass.states.get(entity_id)
                    assert state is not None, entity_id
                    assert expected_name in state.attributes["friendly_name"]
            equalizer_id = registry.async_get_entity_id("select", DOMAIN, "local-speaker_equalizer")
            registry.async_update_entity(equalizer_id, name="My custom equalizer")
            await hass.async_block_till_done()
            await platforms[0].async_reset()
            assert await platforms[0].async_setup_entry(entry)
            await hass.async_block_till_done()
            registered = registry.async_get(equalizer_id)
            assert registered.name == "My custom equalizer"
            assert registered.unique_id == "local-speaker_equalizer"
            assert hass.states.get(equalizer_id).attributes["friendly_name"] == "My custom equalizer"
            print("Entity names verified:", language, "translated names and custom-name preservation PASS")
        finally:
            for platform in platforms:
                await platform.async_reset()


@pytest.mark.parametrize("language", ["en", "ru"])
def test_translated_names_preserve_entity_identity_and_custom_names(tmp_path, language):
    asyncio.run(verify_entity_names(tmp_path, language))
