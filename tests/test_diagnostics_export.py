import asyncio
import json
from unittest.mock import MagicMock

from homeassistant.helpers import device_registry

from custom_components.yandex_station.core.cache import CacheManager
from custom_components.yandex_station.core.const import DOMAIN
from custom_components.yandex_station.core.policy import AuthState, DevicesState, EntryPolicy
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
from custom_components.yandex_station.diagnostics import (
    async_get_config_entry_diagnostics, async_get_device_diagnostics,
)
from .test_cache import device_record
from .test_options_and_reauth import core_flow


def test_diagnostics_account_device_and_missing_runtime_are_json_safe(tmp_path):
    async def scenario():
        async with core_flow(tmp_path, options={"enable_diagnostic_sensors": True}) as (
            hass, entry, lifecycle
        ):
            if setup := getattr(device_registry, "async_setup", None):
                setup(hass)
            await device_registry.async_load(hass)
            registry = device_registry.async_get(hass)
            account_device = registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, entry.entry_id)},
                name="ACCOUNT_SECRET_NAME",
            )
            physical = registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, "synthetic-glagol-id")},
                name="DEVICE_SECRET_NAME",
            )
            foreign = MagicMock(
                identifiers={("foreign_platform", "synthetic-glagol-id")},
                config_entries={"foreign-entry"},
            )
            cache = CacheManager(
                hass, entry.entry_id, account_login=entry.unique_id,
                account_uid=entry.data["account_uid"],
            )
            device = device_record("synthetic-cloud-id", "synthetic-glagol-id")
            device["name"] = "DEVICE_SECRET_NAME"
            device["host"] = "10.0.0.2"
            device["token"] = "SYNTHETIC_TOKEN_SECRET"
            device["updates_url"] = "https://example.invalid/path?SYNTHETIC_QUERY_SECRET"
            device["capabilities"][0]["state"]["value"] = "SYNTHETIC_COMMAND_SECRET"
            await cache.async_save_devices([device])
            await cache.async_save_scenario_marker(
                device["id"], "SYNTHETIC_TRIGGER_SECRET", "pending_unknown"
            )
            runtime = YandexQuasar(
                MagicMock(), policy=EntryPolicy(enable_diagnostic_sensors=True),
                cache_manager=cache,
            )
            runtime.devices = [device]
            runtime.readiness.auth_state = AuthState.VALID
            runtime.readiness.devices_state = DevicesState.READY
            lifecycle.runtimes[entry.entry_id] = runtime
            lifecycle.last_reload_result = {
                "status": "partial", "reason": "entry_setup_failed",
                "entries": {"SYNTHETIC_ACCOUNT_SECRET": "setup_retry"},
                "applied_modules": ["SYNTHETIC_MODULE_SECRET"],
                "timings_ms": {"drain_ms": 1200, "unexpected": "SYNTHETIC_VALUE_SECRET"},
            }
            hass.data["system_log"] = MagicMock(records={
                ("SYNTHETIC_LOG_SECRET",): MagicMock(
                    to_dict=lambda: {"message": "SYNTHETIC_LOG_SECRET"}
                ),
            })
            try:
                account = await async_get_config_entry_diagnostics(hass, entry)
                assert account["readiness"]["auth_state"] == "valid"
                assert account["cache"]["status"] == "cache_ready"
                assert account["scenario_journal"]["pending_unknown"] == 1
                assert account["devices"][0]["scenario_journal_state"] == "pending_unknown"
                assert account["devices"][0]["kind"] == "smart_speaker"
                assert account["reload"] == {
                    "status": "partial", "reason": "entry_setup_failed",
                    "entry_state_counts": {
                        "loaded": 0, "setup_retry": 1, "setup_error": 0,
                        "not_loaded": 0, "membership_changed": 0,
                    },
                    "timings_ms": {"drain_ms": 1200},
                }
                service = await async_get_device_diagnostics(hass, entry, account_device)
                assert service == account
                speaker = await async_get_device_diagnostics(hass, entry, physical)
                assert speaker["device"] == account["devices"][0]
                wrong = await async_get_device_diagnostics(hass, entry, foreign)
                assert wrong["error"] == "device_not_found"
                foreign_owner = MagicMock(
                    identifiers={(DOMAIN, "synthetic-glagol-id")},
                    config_entries={"foreign-entry"},
                )
                assert (await async_get_device_diagnostics(
                    hass, entry, foreign_owner
                ))["error"] == "device_not_found"
                unknown = MagicMock(
                    identifiers={(DOMAIN, "unknown-physical")},
                    config_entries={entry.entry_id},
                )
                assert (await async_get_device_diagnostics(
                    hass, entry, unknown
                ))["error"] == "device_not_found"
                for payload in (account, speaker, service, wrong):
                    serialized = json.dumps(payload)
                    for secret in (
                        "SECRET", "synthetic-cloud-id", "synthetic-glagol-id",
                        "previous", "music", "uid-1", "10.0.0.2",
                        "SYNTHETIC_ACCOUNT_SECRET", "SYNTHETIC_MODULE_SECRET",
                        "SYNTHETIC_VALUE_SECRET",
                    ):
                        assert secret not in serialized

                lifecycle.runtimes.pop(entry.entry_id)
                no_runtime = await async_get_config_entry_diagnostics(hass, entry)
                assert not no_runtime["runtime_loaded"]
                assert no_runtime["cache"]["status"] == "cache_ready"
                assert no_runtime["cache"]["device_count"] == 1
                assert "devices" not in no_runtime
                assert (await async_get_device_diagnostics(
                    hass, entry, physical
                ))["error"] == "device_not_found"
            finally:
                lifecycle.runtimes.pop(entry.entry_id, None)
                await cache.async_close()

    asyncio.run(scenario())


def test_missing_or_corrupt_cache_reports_local_status_without_runtime(tmp_path):
    async def scenario():
        async with core_flow(tmp_path) as (hass, entry, lifecycle):
            lifecycle.setup_reasons[entry.entry_id] = "SYNTHETIC_LOG_SECRET"
            info = await async_get_config_entry_diagnostics(hass, entry)
            assert info["cache"]["status"] == "cache_missing"
            assert info["setup_reason"] == "setup_unavailable"
            assert info["scenario_journal"]["pending_unknown"] == 0
            assert "SYNTHETIC_LOG_SECRET" not in json.dumps(info)

    asyncio.run(scenario())
