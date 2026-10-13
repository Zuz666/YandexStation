import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.yandex_station.config_flow import OptionsFlowHandler, YandexStationFlowHandler
from custom_components.yandex_station.core.cache import CacheManager, StationSnapshot
from custom_components.yandex_station.core.const import DOMAIN
from custom_components.yandex_station.core.yandex_session import LoginResponse


def test_options_flow_opens_when_quasar_missing(tmp_path):
    async def scenario():
        hass = MagicMock(spec=HomeAssistant)
        hass.data = {DOMAIN: {}}
        hass.config = MagicMock()
        hass.config.path = lambda *args: str(tmp_path.joinpath(*args))

        entry = MagicMock()
        entry.entry_id = "entry_1"
        entry.unique_id = "test_user"
        entry.options = {"include": ["offline_dev_1"]}

        # Prepare snapshot
        snapshot = StationSnapshot(
            entry_id="entry_1",
            account_login="test_user",
            devices=[{
                "id": "cached_dev_1",
                "name": "Колонка",
                "house_name": "Дом",
                "capabilities": [],
                "properties": [],
                "quasar_info": {"device_id": "did1", "platform": "yandexstation"},
            }],
        )

        with patch("custom_components.yandex_station.core.cache.CacheManager.async_load", AsyncMock(return_value=snapshot)):
            flow = OptionsFlowHandler(entry)
            flow.hass = hass

            # Step init should display menu with options
            menu = await flow.async_step_init()
            assert menu["type"] == "menu"
            assert menu["step_id"] == "init"
            assert "settings" in menu["menu_options"]
            assert "devices" in menu["menu_options"]
            assert "reauth" in menu["menu_options"]

            # Step devices should display form with cached devices + preserved offline devices
            result = await flow.async_step_devices()
            assert result["type"] == "form"
            assert result["step_id"] == "devices"
    asyncio.run(scenario())


def test_options_flow_commit_and_reload():
    async def scenario():
        hass = MagicMock(spec=HomeAssistant)
        hass.data = {DOMAIN: {}}
        hass.config_entries = MagicMock()
        hass.config_entries.async_update_entry = MagicMock()
        hass.config_entries.async_reload = AsyncMock()
        created_tasks = []
        def mock_create_task(coro):
            created_tasks.append(coro)
            coro.close()
            return MagicMock()
        hass.async_create_task = mock_create_task
        entry = MagicMock()
        entry.entry_id = "entry_1"
        entry.unique_id = "test_user"
        entry.options = {"force_offline_local": False, "enable_scenarios": True}

        flow = OptionsFlowHandler(entry)
        flow.hass = hass

        user_input = {
            "force_offline_local": True,
            "enable_scenarios": False,
        }

        result = await flow.async_step_settings(user_input)
        assert result["type"] == "create_entry"

        # Verify synchronous update before reload
        hass.config_entries.async_update_entry.assert_called_once()
        call_options = hass.config_entries.async_update_entry.call_args[1]["options"]
        assert call_options["force_offline_local"] is True
        assert call_options["enable_scenarios"] is False

        # Verify reload task was created
        assert len(created_tasks) == 1

    asyncio.run(scenario())
def test_options_flow_reauth_trigger():
    async def scenario():
        hass = MagicMock(spec=HomeAssistant)
        entry = MagicMock()
        entry.async_start_reauth = MagicMock()

        flow = OptionsFlowHandler(entry)
        flow.hass = hass

        # 1. Shows confirmation form with warning
        form = await flow.async_step_reauth()
        assert form["type"] == "form"
        assert form["step_id"] == "reauth"

        # 2. Cancel / unconfirmed returns to menu
        menu = await flow.async_step_reauth({"confirm_reauth": False})
        assert menu["type"] == "menu"

        # 3. Confirmed triggers start_reauth and aborts with reauth_started
        res = await flow.async_step_reauth({"confirm_reauth": True})
        assert res["type"] == "abort"
        assert res["reason"] == "reauth_started"
        entry.async_start_reauth.assert_called_once_with(hass)

    asyncio.run(scenario())



def test_reauth_same_account_updates_and_reloads():
    async def scenario():
        hass = MagicMock(spec=HomeAssistant)
        hass.data = {DOMAIN: {}}
        hass.config_entries = MagicMock()
        hass.config_entries.async_update_entry = MagicMock()
        hass.config_entries.async_reload = AsyncMock()
        created_tasks = []
        def mock_create_task(coro):
            created_tasks.append(coro)
            coro.close()
            return MagicMock()
        hass.async_create_task = mock_create_task
        reauth_entry = MagicMock()
        reauth_entry.entry_id = "entry_1"
        reauth_entry.unique_id = "alice_login"

        flow = YandexStationFlowHandler()
        flow.hass = hass
        flow.context = {"entry_id": "entry_1"}
        flow._reauth_entry = reauth_entry

        # Mock login response with same account
        resp = LoginResponse({
            "status": "ok",
            "display_login": "alice_login",
            "x_token": "new_token_123",
        })

        result = await flow._check_yandex_response(resp)
        assert result["type"] == "abort"
        assert result["reason"] == "reauth_successful"
        hass.config_entries.async_update_entry.assert_called_once_with(
            reauth_entry, data={"x_token": "new_token_123"}
        )
        assert len(created_tasks) == 1

    asyncio.run(scenario())


def test_reauth_different_account_aborts():
    async def scenario():
        hass = MagicMock(spec=HomeAssistant)
        hass.data = {DOMAIN: {}}

        reauth_entry = MagicMock()
        reauth_entry.entry_id = "entry_1"
        reauth_entry.unique_id = "alice_login"

        flow = YandexStationFlowHandler()
        flow.hass = hass
        flow.context = {"entry_id": "entry_1"}
        flow._reauth_entry = reauth_entry

        # Mock login response with different account
        resp = LoginResponse({
            "status": "ok",
            "display_login": "bob_login",
            "x_token": "bobs_token",
        })

        result = await flow._check_yandex_response(resp)
        assert result["type"] == "abort"
        assert result["reason"] == "account_mismatch"

    asyncio.run(scenario())
