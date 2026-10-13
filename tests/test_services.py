import asyncio
from unittest.mock import MagicMock

import pytest
import homeassistant.core as ha_core
from homeassistant.exceptions import HomeAssistantError

from custom_components.yandex_station import async_setup
from custom_components.yandex_station.core.const import DOMAIN
from .test_options_and_reauth import core_flow


def test_admin_reload_service_refuses_without_opt_in_on_real_core(tmp_path):
    async def scenario():
        async with core_flow(tmp_path) as (hass, _, lifecycle):
            hass.http = MagicMock()
            assert await async_setup(hass, {
                DOMAIN: {"local_updates": False, "enable_code_reload": False}
            })
            assert hass.services.has_service(DOMAIN, "reload_code")
            assert lifecycle.stream_dispatcher is not None
            if hasattr(ha_core, "SupportsResponse"):
                result = await hass.services.async_call(
                    DOMAIN, "reload_code", {}, blocking=True, return_response=True
                )
                assert result["status"] == "failed"
                assert result["reason"] == "hot_reload_disabled"
            else:
                with pytest.raises(HomeAssistantError, match="hot_reload_disabled"):
                    await hass.services.async_call(
                        DOMAIN, "reload_code", {}, blocking=True
                    )
            assert lifecycle.last_reload_result["status"] == "failed"
            assert lifecycle.context.admission_open

    asyncio.run(scenario())
