import asyncio
import socket
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.yandex_station.core.lifecycle import get_lifecycle
from custom_components.yandex_station.core.policy import EntryPolicy
from custom_components.yandex_station.core.stream import StreamImplementation, get_content_type


def config_entry():
    fields = dict(version=1, domain="yandex_station", title="Synthetic", data={},
                  source="user", options={}, unique_id="synthetic", entry_id="entry-1")
    try:
        return ConfigEntry(**fields, minor_version=1, discovery_keys=MappingProxyType({}),
                           subentries_data=())
    except TypeError:
        return ConfigEntry(**fields)


def test_numeric_ip_like_host_cannot_bypass_actual_offline_connector(tmp_path):
    async def scenario():
        try:
            hass = HomeAssistant(str(tmp_path))
        except TypeError:
            hass = HomeAssistant()
        hass.config.config_dir = str(tmp_path)
        hass.config_entries = ConfigEntries(hass, {})
        lifecycle = get_lifecycle(hass)
        context = lifecycle.context
        context.hass_url = "http://127.0.0.1:8123"
        lifecycle.runtimes["entry-1"] = SimpleNamespace(
            policy=EntryPolicy(force_offline_local=True), stopping=False
        )
        context.implementation = StreamImplementation(context)
        attempted = []

        def intercept_connect(sock, address):
            attempted.append(address)
            raise OSError("synthetic connector interception")

        with patch.object(hass.config_entries, "async_get_entry", return_value=config_entry()):
            try:
                # The real TCPConnector is constructed and used if URL admission
                # fails: its bypass of AbstractResolver must never reach socket.connect.
                with patch.object(socket.socket, "connect", intercept_connect):
                    for host in ("010.000.000.001", "0x08000001", "134217729"):
                        try:
                            await get_content_type(
                                context, "entry-1", f"http://{host}:19001/track.mp3"
                            )
                        except ValueError as error:
                            assert str(error) == "media_destination_blocked"
                        else:
                            assert False, f"numeric host accepted; socket attempts: {attempted}"
                assert attempted == [], f"socket connection attempted: {attempted}"
            finally:
                await context.implementation.async_close()
                await hass.async_stop()

    asyncio.run(scenario())
