import asyncio
import json
from unittest.mock import AsyncMock, patch

from homeassistant import loader
from homeassistant.components import system_health as core_health

from custom_components.yandex_station import system_health
from custom_components.yandex_station.core.cache import CacheManager
from custom_components.yandex_station.core.const import DOMAIN
from .test_ownership import ownership_runtime, prepare_account, restore_account


def test_health_separates_accounts_without_network_or_cache_loading(tmp_path):
    async def scenario():
        async with ownership_runtime(tmp_path, disabled=("b",)) as (
            hass, entries, client, lifecycle
        ):
            if hasattr(loader, "async_setup"):
                loader.async_setup(hass)
            hass.config.language = "en"
            hass.config_entries.async_update_entry(entries["a"], title="Alpha")
            hass.config_entries.async_update_entry(entries["b"], title="Beta")
            await prepare_account(hass, entries["a"])
            await restore_account(hass, entries["a"], client)
            hass.data.setdefault(core_health.DOMAIN, {})
            registration = core_health.SystemHealthRegistration(hass, DOMAIN)
            system_health.async_register(hass, registration)
            with (
                patch.object(CacheManager, "async_load", new=AsyncMock(
                    side_effect=AssertionError("System Health must not load or migrate a cache")
                )) as load,
                patch.object(client, "get", new=AsyncMock(
                    side_effect=AssertionError("System Health must not contact the cloud")
                )) as get,
            ):
                result = await core_health.get_integration_info(hass, registration)
            info = result["info"]
            assert info["accounts"] == 2
            alpha = {
                key.removeprefix("1. Alpha / "): value
                for key, value in info.items() if key.startswith("1. Alpha / ")
            }
            beta = {
                key.removeprefix("2. Beta / "): value
                for key, value in info.items() if key.startswith("2. Beta / ")
            }
            assert alpha["Runtime loaded"] is True
            assert beta["Runtime loaded"] is False
            assert beta["Entry state"] == "disabled"
            assert alpha["Cloud readiness"] == "offline"
            assert alpha["Cloud WebSocket state"] == "disabled"
            assert alpha["Cache state"] == "cache_ready"
            assert beta["Cache state"] == "unknown"
            assert alpha["Cached local token candidates"] == 1
            assert alpha["Local connections with an accepted token"] == 0
            load.assert_not_awaited()
            get.assert_not_awaited()
            serialized = json.dumps(result)
            for secret in ("login-a", "login-b", "uid-a", "uid-b", "shared-local",
                           "cloud-entry-a", "candidate-entry-a", "trigger-entry-a"):
                assert secret not in serialized

    asyncio.run(scenario())


def test_health_keeps_distinct_rows_for_duplicate_account_titles(tmp_path):
    async def scenario():
        async with ownership_runtime(tmp_path, disabled=("b",)) as (
            hass, entries, _, _
        ):
            if hasattr(loader, "async_setup"):
                loader.async_setup(hass)
            hass.config.language = "en"
            for entry in entries.values():
                hass.config_entries.async_update_entry(entry, title="Shared name")
            hass.data.setdefault(core_health.DOMAIN, {})
            registration = core_health.SystemHealthRegistration(hass, DOMAIN)
            system_health.async_register(hass, registration)
            result = await core_health.get_integration_info(hass, registration)
            info = result["info"]
            assert info["accounts"] == 2
            assert info["1. Shared name / Entry state"] == "not_loaded"
            assert info["2. Shared name / Entry state"] == "disabled"

    asyncio.run(scenario())
