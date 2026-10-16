import asyncio
from types import SimpleNamespace
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


def test_send_command_dispatches_without_entity_service_fields_helper(
    tmp_path, monkeypatch
):
    async def scenario():
        async with core_flow(tmp_path) as (hass, _, lifecycle):
            from homeassistant.components import group
            # The minimum Core resolves entity targets through its component cache.
            hass.data["components"] = {"group": group}
            hass.http = MagicMock()
            assert await async_setup(hass, {
                DOMAIN: {"local_updates": False, "enable_code_reload": False}
            })

            sent = []

            async def fake_send(payload):
                sent.append(payload)
                return {"status": "ok"}

            speaker = {
                "entity": SimpleNamespace(
                    entity_id="media_player.synthetic_station",
                    glagol=SimpleNamespace(send=fake_send),
                ),
            }
            hass.data[DOMAIN]["synthetic-login"] = SimpleNamespace(
                owned_speakers=[speaker], owned_modules=[],
            )

            call_data = {
                "entity_id": "media_player.synthetic_station",
                "command": "sendText",
                "text": "привет",
            }

            async def dispatch():
                if hasattr(ha_core, "SupportsResponse"):
                    result = await hass.services.async_call(
                        DOMAIN, "send_command", call_data,
                        blocking=True, return_response=True,
                    )
                    assert result == {"status": "ok"}
                else:
                    await hass.services.async_call(
                        DOMAIN, "send_command", call_data, blocking=True
                    )

            # native helper present: real dispatch strips targeting fields
            await dispatch()
            assert sent == [{"command": "sendText", "text": "привет"}]

            # minimum core (HA 2023.2) lacks the helper: the fallback must
            # reproduce the same payload, not just import
            from homeassistant.helpers import service as ha_service
            monkeypatch.delattr(ha_service, "remove_entity_service_fields", raising=False)
            sent.clear()
            await dispatch()
            assert sent == [{"command": "sendText", "text": "привет"}]

    asyncio.run(scenario())
