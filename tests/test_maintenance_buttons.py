import asyncio
import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import loader
from homeassistant.auth import auth_manager_from_config
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.setup import async_setup_component

from custom_components.yandex_station import async_setup
from custom_components.yandex_station.core.const import DOMAIN
from .test_ownership import ownership_runtime, prepare_account


@asynccontextmanager
async def maintenance_runtime(tmp_path):
    async with ownership_runtime(tmp_path) as (hass, entries, _, lifecycle):
        if hasattr(loader, "async_setup"):
            loader.async_setup(hass)
        hass.config.language = "en"
        hass.http = MagicMock()
        hass.auth = await auth_manager_from_config(hass, [], [])
        admin = await hass.auth.async_create_user("Synthetic admin", group_ids=[GROUP_ID_ADMIN])
        member = await hass.auth.async_create_user("Synthetic member", group_ids=[GROUP_ID_USER])
        assert await async_setup(hass, {DOMAIN: {
            "local_updates": False, "force_offline_local": True, "enable_code_reload": True,
        }})
        # Discovery is disabled; network-none containers cannot join multicast.
        # The fixture supplies HTTP registration above and does not exercise LAN discovery.
        hass.config.components.update({DOMAIN, "http", "zeroconf"})
        assert await async_setup_component(hass, "button", {})
        forward = hass.config_entries.async_forward_entry_setups
        unload = hass.config_entries.async_unload_platforms

        async def forward_buttons(entry, platforms):
            await forward(entry, ["button"])

        async def unload_buttons(entry, platforms):
            return await unload(entry, ["button"])

        # Load real entries and the real Core button platform; unrelated physical
        # platforms are outside this account-maintenance fixture.
        with (
            patch.object(hass.config_entries, "async_forward_entry_setups", new=forward_buttons),
            patch.object(hass.config_entries, "async_unload_platforms", new=unload_buttons),
        ):
            for entry in entries.values():
                await prepare_account(hass, entry, devices=[])
                assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            try:
                yield hass, entries, lifecycle, admin, member
            finally:
                for entry in entries.values():
                    await hass.config_entries.async_unload(entry.entry_id)


def test_account_button_preserves_admin_boundary_and_other_account(tmp_path):
    async def scenario():
        async with maintenance_runtime(tmp_path) as (hass, entries, lifecycle, admin, member):
            registry = entity_registry.async_get(hass)
            entry = entries["a"]
            button_id = registry.async_get_entity_id(
                "button", DOMAIN, f"{entry.entry_id}_reload_account"
            )
            original = registry.async_get(button_id)
            device = device_registry.async_get(hass).async_get(original.device_id)
            assert device.identifiers == {(DOMAIN, entry.entry_id)}
            assert device.configuration_url == "homeassistant://config/repairs?dialog=system-health"
            registry.async_update_entity(button_id, name="User maintenance label")
            await hass.async_block_till_done()
            before_a = lifecycle.runtimes[entry.entry_id]
            before_b = lifecycle.runtimes[entries["b"].entry_id]
            assert member.permissions.check_entity(button_id, "control")
            with pytest.raises(Unauthorized):
                await hass.services.async_call(
                    "button", "press", {"entity_id": button_id}, blocking=True,
                    context=Context(user_id=member.id),
                )
            assert lifecycle.runtimes[entry.entry_id] is before_a
            assert not before_a.stopping
            await asyncio.wait_for(hass.services.async_call(
                "button", "press", {"entity_id": button_id}, blocking=True,
                context=Context(user_id=admin.id),
            ), 5)
            await asyncio.wait_for(hass.async_block_till_done(), 10)
            assert lifecycle.runtimes[entry.entry_id] is not before_a
            assert before_a.stopping
            assert lifecycle.runtimes[entries["b"].entry_id] is before_b
            assert not before_b.stopping
            current = registry.async_get(button_id)
            assert current.device_id == original.device_id
            assert current.unique_id == original.unique_id
            assert current.name == "User maintenance label"
            assert hass.states.get(button_id).attributes["friendly_name"] == "User maintenance label"

    asyncio.run(scenario())


def test_cloud_button_respects_offline_policy_and_account_isolation(tmp_path):
    async def scenario():
        async with maintenance_runtime(tmp_path) as (hass, entries, lifecycle, _, member):
            entry = entries["a"]
            runtime = lifecycle.runtimes[entry.entry_id]
            button_id = entity_registry.async_get(hass).async_get_entity_id(
                "button", DOMAIN, f"{entry.entry_id}_check_cloud"
            )
            with patch.object(runtime.session._session, "get", new=AsyncMock(
                side_effect=AssertionError("Offline button must not send HTTP")
            )) as get:
                with pytest.raises(HomeAssistantError, match="cloud_blocked_by_policy"):
                    await hass.services.async_call(
                        "button", "press", {"entity_id": button_id}, blocking=True,
                        context=Context(user_id=member.id),
                    )
                get.assert_not_called()
            assert runtime.last_cloud_check["status"] == "skipped"
            assert lifecycle.runtimes[entries["b"].entry_id].last_cloud_check is None

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["hot_reload_disabled", "flow_in_progress"])
def test_code_button_reports_admission_refusals(tmp_path, reason):
    async def scenario():
        async with maintenance_runtime(tmp_path) as (hass, entries, lifecycle, admin, _):
            flow = None
            if reason == "hot_reload_disabled":
                hass.data[DOMAIN]["config"]["enable_code_reload"] = False
            else:
                from custom_components.yandex_station import config_flow

                if isinstance(hass.data.get("components"), set):
                    hass.data["components"].add(DOMAIN + ".config_flow")
                flow = await hass.config_entries.options.async_init(entries["a"].entry_id)
            try:
                before = dict(lifecycle.runtimes)
                button_id = entity_registry.async_get(hass).async_get_entity_id(
                    "button", DOMAIN, f"{entries['a'].entry_id}_reload_code"
                )
                with pytest.raises(HomeAssistantError, match=reason):
                    await asyncio.wait_for(hass.services.async_call(
                        "button", "press", {"entity_id": button_id}, blocking=True,
                        context=Context(user_id=admin.id),
                    ), 2)
                assert lifecycle.runtimes == before
                assert lifecycle.generation == 0
                assert lifecycle.last_reload_result["reason"] == reason
                assert lifecycle._reload_task is None
            finally:
                if flow is not None:
                    hass.config_entries.options.async_abort(flow["flow_id"])

    asyncio.run(scenario())


async def verify_button_hot_reload(config, cancel_consumer=False):
    from homeassistant.components import system_health as core_health

    async with maintenance_runtime(config) as (hass, entries, lifecycle, admin, member):
        health = importlib.import_module(f"custom_components.{DOMAIN}.system_health")
        hass.data.setdefault(core_health.DOMAIN, {})
        registration = core_health.SystemHealthRegistration(hass, DOMAIN)
        health.async_register(hass, registration)
        callback = registration.info_callback
        before = await core_health.get_integration_info(hass, registration)
        assert before["info"]["accounts"] == 2
        old_runtimes = dict(lifecycle.runtimes)
        source = Path(health.__file__)
        assert source.resolve().is_relative_to(config.resolve())
        with source.open("a", encoding="utf-8") as file:
            file.write('''

_before_smoke_collect = _collect_info
async def _collect_info(hass):
    result = await _before_smoke_collect(hass)
    result["synthetic_revision"] = "updated"
    return result
''')
        button_id = entity_registry.async_get(hass).async_get_entity_id(
            "button", DOMAIN, f"{entries['a'].entry_id}_reload_code"
        )
        assert member.permissions.check_entity(button_id, "control")
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                "button", "press", {"entity_id": button_id}, blocking=True,
                context=Context(user_id=member.id),
            )
        assert lifecycle.generation == 0
        assert lifecycle.last_reload_result is None
        accepted = asyncio.Event()
        if cancel_consumer:
            button = next(
                entity for entity in hass.data["button"].entities if entity.entity_id == button_id
            )
            original_action = button._async_press_action
            requester = None

            async def hold_response():
                nonlocal requester
                requester = asyncio.current_task()
                await original_action()
                accepted.set()
                await asyncio.Event().wait()

            button._async_press_action = hold_response
        call = asyncio.create_task(hass.services.async_call(
            "button", "press", {"entity_id": button_id}, blocking=True,
            context=Context(user_id=admin.id),
        ))
        if cancel_consumer:
            await asyncio.wait_for(accepted.wait(), 2)
            assert lifecycle.last_reload_result["status"] == "accepted"
            assert lifecycle.generation == 0
            requester.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            await asyncio.wait_for(call, 2)
        await asyncio.wait_for(hass.async_block_till_done(), 20)
        assert lifecycle.last_reload_result["status"] == "success", lifecycle.last_reload_result
        assert lifecycle.generation == 1
        assert lifecycle.active_consumers == 0
        for entry in entries.values():
            assert lifecycle.runtimes[entry.entry_id] is not old_runtimes[entry.entry_id]
            assert old_runtimes[entry.entry_id].stopping
            entity_id = entity_registry.async_get(hass).async_get_entity_id(
                "button", DOMAIN, f"{entry.entry_id}_reload_code"
            )
            assert hass.states.get(entity_id).state != STATE_UNAVAILABLE
        assert registration.info_callback is callback
        after = await core_health.get_integration_info(hass, registration)
        assert after["info"]["accounts"] == 2
        assert after["info"]["synthetic_revision"] == "updated"
        assert after["info"]["last_reload_status"] == "success"
        print("Maintenance reload verified: native button, admin context, two accounts, fresh System Health")


@pytest.mark.parametrize("cancel_consumer", [False, True])
def test_button_hot_reload_updates_accounts_and_retained_health_callback(tmp_path, cancel_consumer):
    repository = Path(__file__).resolve().parents[1]
    shutil.copytree(
        repository / "custom_components" / DOMAIN,
        tmp_path / "custom_components" / DOMAIN,
    )
    result = subprocess.run(
        [sys.executable, "-c",
         "import asyncio; from pathlib import Path; "
         "from tests.test_maintenance_buttons import verify_button_hot_reload; "
         f"asyncio.run(verify_button_hot_reload(Path.cwd(), {cancel_consumer!r}))"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(tmp_path), str(repository)))},
        capture_output=True, text=True, timeout=50,
    )
    assert result.returncode == 0, result.stdout + result.stderr
