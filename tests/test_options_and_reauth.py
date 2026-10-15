import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.yandex_station.config_flow import YandexStationFlowHandler
from custom_components.yandex_station.core.cache import CacheManager
from custom_components.yandex_station.core.const import DATA_CONFIG, DOMAIN
from custom_components.yandex_station.core.lifecycle import get_lifecycle
from custom_components.yandex_station.core.policy import EntryPolicy, ScenariosState
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
from custom_components.yandex_station.core.yandex_session import LoginResponse, YandexSession
from . import core_hass, seed_config_entries
from .test_cache import config_entry, device_record
from .test_scenario_recovery import wire_scenario


@asynccontextmanager
async def core_flow(tmp_path, *, options=None, disabled=False):
    hass = core_hass(tmp_path)
    hass.data.setdefault("components", set()).add("yandex_station.config_flow")
    record = config_entry("entry-1", "synthetic-login").as_dict()
    record["data"] = {"x_token": "previous", "music_token": "music", "account_uid": "uid-1"}
    record["options"] = options or {}
    if disabled:
        record["disabled_by"] = "user"
    await seed_config_entries(hass, [record])
    hass.data[DOMAIN] = {DATA_CONFIG: {}}
    entry = hass.config_entries.async_get_entry("entry-1")
    lifecycle = get_lifecycle(hass)
    try:
        yield hass, entry, lifecycle
    finally:
        await lifecycle.async_stop()
        await hass.async_stop()


async def options_step(hass, step):
    menu = await hass.config_entries.options.async_init("entry-1")
    return await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": step}
    )


def test_settings_round_trip_and_invalid_endpoint_leave_other_options(tmp_path):
    async def scenario():
        initial = {"include": ["offline-1"], "force_offline_local": False,
                   "csrf_endpoints": ["https://yandex.ru/iot"]}
        async with core_flow(tmp_path, options=initial) as (hass, entry, lifecycle):
            seen = []

            async def reload_entry(entry_id):
                seen.append((entry_id, dict(entry.options)))
                return True

            with patch.object(hass.config_entries, "async_reload", side_effect=reload_entry):
                settings = await options_step(hass, "settings")
                schema = settings["data_schema"]
                assert {"quasar_base_url", "account_base_url", "passport_base_url",
                        "mobileproxy_base_url", "glagol_base_url", "rpc_base_url",
                        "cookie_storage_endpoints", "csrf_endpoints",
                        "enable_diagnostic_sensors"} <= {
                            key.schema for key in schema.schema
                        }
                for value in (
                    "[]", "{bad", json.dumps([{"url": "https://example.invalid/probe",
                                               "validator": "not-a-validator"}]),
                    json.dumps([{"url": "http://example.invalid/probe",
                                 "validator": "status_ok"}]),
                ):
                    invalid = await hass.config_entries.options.async_configure(
                        settings["flow_id"], {"cookie_storage_endpoints": value}
                    )
                    assert invalid["errors"] == {"base": "invalid_endpoint"}
                    assert dict(entry.options) == initial

                cookies = json.dumps([{
                    "url": "https://quasar.yandex.ru/get_account_config",
                    "validator": "status_ok",
                }])
                submit = asyncio.create_task(hass.config_entries.options.async_configure(
                    settings["flow_id"], {
                        "csrf_endpoints": "", "cookie_storage_endpoints": cookies,
                        "force_offline_local": True, "enable_scenarios": False,
                    }
                ))
                result = await submit
                assert result["type"] == "create_entry"
                assert entry.options["force_offline_local"] is True
                assert entry.options["enable_scenarios"] is False
                assert "csrf_endpoints" not in entry.options
                assert entry.options["include"] == ["offline-1"]
                assert entry.options["cookie_storage_endpoints"][0]["validator"] == "status_ok"
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert seen == [(entry.entry_id, dict(entry.options))]

    asyncio.run(scenario())


def test_unchanged_options_closed_gate_and_disabled_retry(tmp_path):
    async def scenario():
        async with core_flow(tmp_path, disabled=True) as (hass, entry, lifecycle):
            settings = await options_step(hass, "settings")
            unchanged = await hass.config_entries.options.async_configure(
                settings["flow_id"], {}
            )
            assert unchanged["type"] == "create_entry"
            assert dict(entry.options) == {}
            first = await options_step(hass, "settings")
            second = await options_step(hass, "settings")
            first_done = await hass.config_entries.options.async_configure(
                first["flow_id"], {}
            )
            assert first_done["type"] == "create_entry"
            second_done = await hass.config_entries.options.async_configure(
                second["flow_id"], {"force_offline_local": True}
            )
            assert second_done["type"] == "create_entry"
            assert entry.options["force_offline_local"] is True
            hass.config_entries.async_update_entry(entry, options={})
            retry = await options_step(hass, "recovery")
            assert "retry_setup" in retry["menu_options"]
            assert retry["description_placeholders"]["cache_reason"] == "cache_missing"
            form = await hass.config_entries.options.async_configure(
                retry["flow_id"], {"next_step_id": "retry_setup"}
            )
            declined = await hass.config_entries.options.async_configure(
                form["flow_id"], {"confirm_retry": True}
            )
            assert declined["errors"] == {"base": "entry_disabled"}
            assert dict(entry.options) == {}

        async with core_flow(tmp_path / "closed") as (hass, entry, lifecycle):
            settings = await options_step(hass, "settings")
            lifecycle.context.admission_open = False
            result = await hass.config_entries.options.async_configure(
                settings["flow_id"], {"force_offline_local": True}
            )
            assert result["errors"] == {"base": "reload_in_progress"}
            assert dict(entry.options) == {}

    asyncio.run(scenario())


def test_recovery_devices_and_retry_setup_use_local_snapshot(tmp_path):
    async def scenario():
        async with core_flow(tmp_path, options={"include": ["offline-1"]}) as (hass, entry, lifecycle):
            cache = CacheManager(hass, entry.entry_id, account_login=entry.unique_id,
                                 account_uid=entry.data["account_uid"])
            device = device_record("cloud-1")
            device["house_name"] = "Synthetic home"
            await cache.async_save_devices([device])
            await cache.async_close()
            lifecycle.setup_reasons[entry.entry_id] = "inventory_unavailable"
            recovery = await options_step(hass, "recovery")
            assert recovery["description_placeholders"]["cache_reason"] == "cache_ready"
            assert recovery["description_placeholders"]["setup_reason"] == "inventory_unavailable"
            devices = await options_step(hass, "devices")
            assert devices["data_schema"]({
                "include": ["cloud-1", "offline-1"],
            })["include"] == ["cloud-1", "offline-1"]

            calls = []
            async def reload_entry(entry_id):
                calls.append(entry_id)
                return True
            with patch.object(hass.config_entries, "async_reload", side_effect=reload_entry):
                form = await hass.config_entries.options.async_configure(
                    recovery["flow_id"], {"next_step_id": "retry_setup"}
                )
                outcome = await asyncio.create_task(hass.config_entries.options.async_configure(
                    form["flow_id"], {"confirm_retry": True}
                ))
                assert outcome["reason"] == "retry_started"
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert calls == [entry.entry_id]
                assert dict(entry.options) == {"include": ["offline-1"]}

    asyncio.run(scenario())


def test_reauth_requires_target_and_offline_blocks_every_network_stage(tmp_path):
    async def scenario():
        async with core_flow(tmp_path, options={"force_offline_local": True}) as (hass, entry, _):
            missing = YandexStationFlowHandler()
            missing.hass = hass
            missing.context = {"source": "reauth"}
            assert (await missing.async_step_reauth())["reason"] == "reauth_target_missing"

            flow = YandexStationFlowHandler()
            flow.hass = hass
            flow.context = {"source": "reauth", "entry_id": entry.entry_id}
            confirm = await flow.async_step_reauth()
            assert confirm["step_id"] == "reauth_confirm"
            assert (await flow.async_step_reauth_confirm({"confirm_reauth": False}))["reason"] == "reauth_cancelled"
            await flow.async_step_reauth_confirm({"confirm_reauth": True})
            with patch.object(YandexSession, "get_qr", new_callable=AsyncMock) as qr, \
                 patch.object(YandexSession, "login_qr", new_callable=AsyncMock) as poll, \
                 patch.object(YandexSession, "login_cookies", new_callable=AsyncMock) as cookies, \
                 patch.object(YandexSession, "validate_token", new_callable=AsyncMock) as token:
                for invoke in (
                    flow.async_step_user({"method": "qr"}),
                    flow.async_step_qr(None),
                    flow.async_step_cookies({"cookies": "synthetic"}),
                    flow.async_step_token({"token": "synthetic"}),
                ):
                    result = await invoke
                    assert result["errors"] == {"base": "offline_reauth_blocked"}
                qr.assert_not_awaited()
                poll.assert_not_awaited()
                cookies.assert_not_awaited()
                token.assert_not_awaited()
            assert entry.data["x_token"] == "previous"

    asyncio.run(scenario())


def test_reauth_same_account_and_ordinary_login_preserve_entry_uid(tmp_path):
    async def scenario():
        async with core_flow(tmp_path) as (hass, entry, lifecycle):
            reloads = []
            async def reload_entry(entry_id):
                reloads.append((entry_id, dict(entry.data)))
                return True
            with patch.object(hass.config_entries, "async_reload", side_effect=reload_entry):
                flow = YandexStationFlowHandler()
                flow.hass = hass
                flow.context = {"source": "reauth", "entry_id": entry.entry_id}
                await flow.async_step_reauth()
                wrong = LoginResponse({"status": "ok", "display_login": entry.unique_id,
                                       "uid": "uid-other", "x_token": "foreign"})
                assert (await flow._check_yandex_response(wrong))["reason"] == "account_mismatch"
                assert entry.data["x_token"] == "previous"
                success = LoginResponse({"status": "ok", "display_login": entry.unique_id,
                                         "uid": "uid-1", "x_token": "fresh"})
                outcome = await asyncio.create_task(flow._check_yandex_response(success))
                assert outcome["reason"] == "reauth_successful"
                assert dict(entry.data) == {"x_token": "fresh", "music_token": "music",
                                            "account_uid": "uid-1"}
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert reloads == [(entry.entry_id, dict(entry.data))]

    asyncio.run(scenario())


def test_core_config_flow_reauth_and_existing_account_login(tmp_path):
    async def scenario():
        for source in ("reauth", "user"):
            async with core_flow(tmp_path / source) as (hass, entry, lifecycle):
                loaded = MagicMock()
                loaded.single_config_entry = False
                loaded.get_platform.return_value = YandexStationFlowHandler
                expected = LoginResponse({
                    "status": "ok", "display_login": entry.unique_id,
                    "uid": entry.data["account_uid"], "x_token": "refreshed",
                })
                reloads = []

                async def reload_entry(entry_id):
                    reloads.append((entry_id, dict(entry.data)))
                    return True
                client = MagicMock()
                client.close = AsyncMock()

                with patch("homeassistant.loader.async_get_integration",
                           new_callable=AsyncMock, return_value=loaded), \
                     patch("homeassistant.config_entries.async_process_deps_reqs",
                           new_callable=AsyncMock, create=True), \
                     patch("custom_components.yandex_station.config_flow.utils.async_create_clientsession",
                           return_value=client), \
                     patch.object(YandexSession, "validate_token",
                                  new_callable=AsyncMock, return_value=expected), \
                     patch.object(hass.config_entries, "async_reload",
                                  side_effect=reload_entry):
                    context = {"source": source}
                    if source == "reauth":
                        context["entry_id"] = entry.entry_id
                    first = await hass.config_entries.flow.async_init(DOMAIN, context=context)
                    if source == "reauth":
                        assert first["step_id"] == "reauth_confirm"
                        first = await hass.config_entries.flow.async_configure(
                            first["flow_id"], {"confirm_reauth": True}
                        )
                    assert first.get("step_id") == "user", (
                        source, first.get("type"), first.get("reason")
                    )
                    credential = await hass.config_entries.flow.async_configure(
                        first["flow_id"], {"method": "token"}
                    )
                    outcome = await asyncio.create_task(hass.config_entries.flow.async_configure(
                        credential["flow_id"], {"token": "replacement"}
                    ))
                    assert outcome["reason"] == (
                        "reauth_successful" if source == "reauth" else "account_updated"
                    )
                    assert dict(entry.data) == {
                        "x_token": "refreshed", "music_token": "music", "account_uid": "uid-1",
                    }
                    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    assert reloads == [(entry.entry_id, dict(entry.data))]

    asyncio.run(scenario())


def test_recovery_scenario_retry_binds_without_duplicate_creation(tmp_path):
    async def scenario():
        async with core_flow(tmp_path) as (hass, entry, lifecycle):
            cache = CacheManager(hass, entry.entry_id, account_login=entry.unique_id,
                                 account_uid=entry.data["account_uid"])
            device = device_record("dev_123", "did1")
            await cache.async_save_devices([device])
            await cache.async_save_scenario_marker("dev_123", "saved-trigger", "pending_unknown")
            runtime = YandexQuasar(MagicMock(), policy=EntryPolicy(), cache_manager=cache)
            runtime.devices = [device]
            runtime.owned_glagol_ids = {"did1"}
            runtime.readiness.scenarios_state = ScenariosState.READY
            runtime.load_scenarios = AsyncMock(
                side_effect=lambda: setattr(runtime, "scenarios", [wire_scenario("late")])
            )
            runtime.add_scenario = AsyncMock()
            lifecycle.runtimes[entry.entry_id] = runtime
            try:
                recovery = await options_step(hass, "recovery")
                assert runtime.load_scenarios.await_count == 0
                form = await hass.config_entries.options.async_configure(
                    recovery["flow_id"], {"next_step_id": "retry_scenario_creation"}
                )
                assert form["step_id"] == "retry_scenario_creation"
                assert runtime.load_scenarios.await_count == 0
                result = await hass.config_entries.options.async_configure(
                    form["flow_id"], {"device_id": "dev_123", "confirm_risk": True}
                )
                assert result["reason"] == "scenario_bound"
                assert device["scenario_id"] == "late"
                runtime.add_scenario.assert_not_awaited()
                assert runtime.load_scenarios.await_count == 1
                assert dict(entry.options) == {}
            finally:
                lifecycle.runtimes.pop(entry.entry_id, None)
                await cache.async_close()

    asyncio.run(scenario())


def test_reauth_timeout_and_account_mismatch_keep_credentials(tmp_path):
    async def scenario():
        async with core_flow(tmp_path) as (hass, entry, lifecycle):
            flow = YandexStationFlowHandler()
            flow.hass = hass
            flow.context = {"source": "reauth", "entry_id": entry.entry_id}
            await flow.async_step_reauth()
            await flow.async_step_reauth_confirm({"confirm_reauth": True})
            client = MagicMock()
            client.close = AsyncMock()
            with patch("custom_components.yandex_station.config_flow.utils.async_create_clientsession",
                       return_value=client), \
                 patch.object(YandexSession, "validate_token",
                              new_callable=AsyncMock, side_effect=asyncio.TimeoutError):
                with pytest.raises(asyncio.TimeoutError):
                    await flow.async_step_token({"token": "synthetic"})
            assert entry.data["x_token"] == "previous"
            assert entry.data["account_uid"] == "uid-1"
            await flow._close_flow_session()
            mismatch = LoginResponse({
                "status": "ok", "display_login": "synthetic-login",
                "uid": "other-uid", "x_token": "wrong-account",
            })
            assert (await flow._check_yandex_response(mismatch))["reason"] == "account_mismatch"
            assert entry.data["x_token"] == "previous"
            assert len(hass.config_entries.async_entries(DOMAIN)) == 1

    asyncio.run(scenario())


def test_broken_saved_endpoints_can_be_repaired_from_settings(tmp_path):
    async def scenario():
        async with core_flow(tmp_path, options={
            "cookie_storage_endpoints": [],
            "include": ["synthetic-device"],
        }) as (hass, entry, lifecycle):
            with patch.object(hass.config_entries, "async_reload", new_callable=AsyncMock,
                              return_value=True):
                settings = await options_step(hass, "settings")
                assert settings["type"] == "form"
                assert "некорректная" in settings["description_placeholders"]["current_cookie"]
                restored = await asyncio.create_task(hass.config_entries.options.async_configure(
                    settings["flow_id"], {
                        "cookie_storage_endpoints": json.dumps([{
                            "url": "https://quasar.yandex.ru/get_account_config",
                            "validator": "status_ok",
                        }]),
                    }
                ))
                assert restored["type"] == "create_entry"
                assert entry.options["include"] == ["synthetic-device"]
                assert entry.options["cookie_storage_endpoints"][0]["validator"] == "status_ok"

    asyncio.run(scenario())


def test_empty_valid_snapshot_keeps_offline_selection(tmp_path):
    async def scenario():
        async with core_flow(tmp_path, options={"include": ["unavailable-1"]}) as (
            hass, entry, lifecycle
        ):
            cache = CacheManager(hass, entry.entry_id, account_login=entry.unique_id,
                                 account_uid=entry.data["account_uid"])
            await cache.async_save_devices([])
            await cache.async_close()
            recovery = await options_step(hass, "recovery")
            assert recovery["description_placeholders"]["cache_reason"] == "cache_ready"
            devices = await options_step(hass, "devices")
            assert devices["data_schema"]({
                "include": ["unavailable-1"],
            })["include"] == ["unavailable-1"]

    asyncio.run(scenario())


def test_core_qr_poll_pending_then_success_keeps_the_same_flow(tmp_path):
    async def scenario():
        async with core_flow(tmp_path) as (hass, entry, lifecycle):
            loaded = MagicMock()
            loaded.single_config_entry = False
            loaded.get_platform.return_value = YandexStationFlowHandler
            client = MagicMock()
            client.close = AsyncMock()
            polls = []
            reloads = []

            def response(payload=None, text=""):
                result = MagicMock(status=200, ok=True)
                result.json = AsyncMock(return_value=payload)
                result.text = AsyncMock(return_value=text)
                return result

            async def get(url, **kwargs):
                if url.endswith("/pwl-yandex"):
                    return response(text='__CSRF__ = "synthetic-csrf"')
                assert "short_info/" in url
                return response({
                    "status": "ok", "display_login": entry.unique_id,
                    "uid": "uid-1",
                })

            async def post(url, **kwargs):
                if url.endswith("/password/submit"):
                    return response({"track_id": "synthetic-track"})
                if url.endswith("/magic/code"):
                    return response({"link": "https://passport.yandex.ru/synthetic-qr"})
                if url.endswith("/magic/code/status"):
                    polls.append(1)
                    if len(polls) == 1:
                        return response({"state": "otp_auth_waiting"})
                    return response({
                        "state": "otp_auth_finished", "trackId": "synthetic-track",
                    })
                if url.endswith("/sessions/get_session"):
                    return response({"status": "ok"})
                assert url.endswith("/oauth/token_by_sessionid")
                return response({"access_token": "replacement"})

            async def reload_entry(entry_id):
                reloads.append((entry_id, dict(entry.data)))
                return True

            with patch("homeassistant.loader.async_get_integration",
                       new_callable=AsyncMock, return_value=loaded), \
                 patch("homeassistant.config_entries.async_process_deps_reqs",
                       new_callable=AsyncMock, create=True), \
                 patch("custom_components.yandex_station.config_flow.utils.async_create_clientsession",
                       return_value=client), \
                 patch("custom_components.yandex_station.config_flow.generate_qr_code",
                       return_value="synthetic-qr-image"), \
                 patch.object(YandexSession, "_get", side_effect=get), \
                 patch.object(YandexSession, "_post", side_effect=post), \
                 patch.object(hass.config_entries, "async_reload",
                              side_effect=reload_entry):
                form = await hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}
                )
                form = await hass.config_entries.flow.async_configure(
                    form["flow_id"], {"confirm_reauth": True}
                )
                qr = await hass.config_entries.flow.async_configure(
                    form["flow_id"], {"method": "qr"}
                )
                assert qr["step_id"] == "qr"
                pending = await hass.config_entries.flow.async_configure(qr["flow_id"])
                assert pending["type"] == "form"
                assert pending["step_id"] == "qr"
                assert pending["flow_id"] == qr["flow_id"]
                assert pending["errors"] == {"base": "unauthorised"}
                assert pending["description_placeholders"]["qr_url"] == (
                    "https://passport.yandex.ru/synthetic-qr"
                )
                assert entry.data["x_token"] == "previous"
                assert not reloads
                success = await asyncio.create_task(
                    hass.config_entries.flow.async_configure(qr["flow_id"])
                )
                assert success["reason"] == "reauth_successful"
                assert len(polls) == 2
                assert entry.data["x_token"] == "replacement"
                assert entry.data["account_uid"] == "uid-1"
                assert len(hass.config_entries.async_entries(DOMAIN)) == 1
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert reloads == [(entry.entry_id, dict(entry.data))]
                client.close.assert_awaited_once()

    asyncio.run(scenario())
