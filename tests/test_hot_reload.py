import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import homeassistant.core as ha_core
import pytest
from aiohttp import ClientSession, web
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry
from homeassistant.components.http import HomeAssistantHTTP
from homeassistant.components.http.const import KEY_HASS


SOURCE = Path(__file__).resolve().parents[1] / "custom_components" / "yandex_station"


def _run_isolated(tmp_path, mode, expected_reason=None):
    target = tmp_path / "custom_components" / "yandex_station"
    shutil.copytree(SOURCE, target)
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), mode,
         *([expected_reason] if expected_reason else [])],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True, text=True, timeout=50,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reload behavior verified" in result.stdout


@pytest.mark.parametrize("mode", [
    "success", "drain", "drain_head_service", "parallel_reload", "cancel_waiter",
    "cancel_during_import", "entries_success", "entries_setup_retry",
    "entries_retry_mix", "entries_disabled", "entries_mixed",
    "entries_discovery_reconnect",
])
def test_full_reload_changes_real_http_and_core_service(tmp_path, mode):
    _run_isolated(tmp_path, mode)


@pytest.mark.parametrize("mode, reason", [
    ("syntax", "source_preflight_failed"),
    ("binding", "top_level_binding_removed"),
    ("anchor", "stable_contract_changed"),
    ("module_set", "module_set_changed"),
    ("registration", "registration_or_storage_schema_changed"),
    ("mid_import", "import_failed"),
    ("rebind", "rebind_failed"),
    ("changed_during_drain", "source_changed_during_reload"),
    ("drain_timeout", "drain_timeout"),
    ("entries_rollback", "source_changed_during_reload"),
    ("entries_unload_fail", "unload_failed"),
    ("options_flow", "flow_in_progress"),
    ("unsupported", "unsupported_runtime"),
    ("state_during_drain", "membership_changed"),
    ("flow_during_drain", "membership_changed"),
    ("discovery_stop_fail", "discovery_stop_failed"),
    ("entries_second_unload_fail", "unload_failed"),
])
def test_reload_refusal_and_failure_preserve_honest_http_state(tmp_path, mode, reason):
    _run_isolated(tmp_path, mode, reason)


async def _run_case(config: Path, mode: str, expected_reason=None):
    # subprocess cwd and PYTHONPATH point only to this disposable integration copy
    from custom_components.yandex_station import async_setup
    from custom_components.yandex_station.core.const import DATA_CONFIG, DOMAIN
    from custom_components.yandex_station.core.lifecycle import get_lifecycle
    from custom_components.yandex_station.core.policy import EntryPolicy, Readiness
    from custom_components.yandex_station.core.stream import get_url
    from custom_components.yandex_station.core.yandex_quasar import YandexQuasar

    try:
        hass = HomeAssistant(str(config))
    except TypeError:
        hass = HomeAssistant()
    hass.config.config_dir = str(config)
    if mode.startswith("entries_") or mode in {"options_flow", "flow_during_drain"}:
        from homeassistant import loader
        if hasattr(loader, "async_setup"):
            loader.async_setup(hass)
    await issue_registry.async_load(hass)
    hass.config_entries = ConfigEntries(hass, {})
    values = dict(version=1, domain=DOMAIN, title="Synthetic account", data={},
                  source="user", options={}, unique_id="synthetic", entry_id="entry-1")
    try:
        entry = ConfigEntry(**values, minor_version=1,
                            discovery_keys=MappingProxyType({}), subentries_data=())
    except TypeError:
        entry = ConfigEntry(**values)
    records = [entry.as_dict()]
    if mode in {"entries_retry_mix", "entries_disabled"}:
        secondary = {**entry.as_dict(), "entry_id": "entry-secondary",
                     "unique_id": "synthetic-secondary"}
        if mode == "entries_disabled":
            secondary["disabled_by"] = "user"
        records.append(secondary)
    if mode in {"entries_mixed", "entries_second_unload_fail"}:
        records.append({
            **entry.as_dict(), "entry_id": "entry-second-loaded",
            "unique_id": "synthetic-second-loaded",
        })
    if mode == "entries_mixed":
        for other_id in ("entry-retry", "entry-disabled", "entry-intentionally-unloaded"):
            record = {
                **entry.as_dict(), "entry_id": other_id,
                "unique_id": f"synthetic-{other_id}",
            }
            if other_id == "entry-disabled":
                record["disabled_by"] = "user"
            records.append(record)
    await hass.config_entries._store.async_save({"entries": records})
    await hass.config_entries.async_initialize()
    if mode == "entries_discovery_reconnect":
        from homeassistant.helpers import device_registry, entity_registry
        if setup_registry := getattr(device_registry, "async_setup", None):
            setup_registry(hass)
        await device_registry.async_load(hass)
        await entity_registry.async_load(hass)
    entry = hass.config_entries.async_get_entry("entry-1")
    hass.http = HomeAssistantHTTP(
        hass, None, None, None, ["127.0.0.1"], 0, [], "intermediate"
    )
    hass.http.app[KEY_HASS] = hass
    assert await async_setup(hass, {DOMAIN: {
        "local_updates": False, "force_offline_local": True,
        "enable_code_reload": True,
    }})
    lifecycle = get_lifecycle(hass)
    class Runtime:
        policy = EntryPolicy(force_offline_local=True)
        stopping = False
        owned_speakers = []
        owned_modules = []
        readiness = Readiness()
        async def async_stop(self):
            self.stopping = True
    lifecycle.runtimes[entry.entry_id] = Runtime()
    quasar = YandexQuasar(MagicMock(), policy=EntryPolicy())
    quasar.devices = [{
        "id": "ordinary-id", "name": "Synthetic", "type": "devices.types.other",
        "capabilities": [], "properties": [{
            "type": "devices.properties.float", "retrievable": True,
            "parameters": {"instance": "temperature"},
            "state": {"instance": "temperature", "value": 23},
        }],
    }]
    hass.data[DOMAIN][entry.unique_id] = quasar
    hass.data[DOMAIN][DATA_CONFIG]["include"] = ["ordinary-id"]
    if mode.startswith("entries_"):
        from custom_components.yandex_station.core.cache import CacheManager
        cache = CacheManager(hass, entry.entry_id, account_login=entry.unique_id)
        if mode == "entries_discovery_reconnect":
            await cache.async_save_devices([{
                "id": "synthetic-station", "name": "Synthetic station",
                "type": "devices.types.smart_speaker.yandex.station",
                "capabilities": [{
                    "type": "devices.capabilities.on_off",
                    "state": {"instance": "on", "value": True},
                }],
                "properties": [],
                "quasar_info": {
                    "device_id": "synthetic-glagol", "platform": "yandexstation",
                },
            }])
            await cache.async_save_device_token(
                "synthetic-glagol", "yandexstation", "synthetic-candidate"
            )
        else:
            await cache.async_save_devices([])
        await cache.async_close()
        hass.config.components.add(DOMAIN)
        from homeassistant.helpers import aiohttp_client
        patch.object(
            aiohttp_client, "async_create_clientsession", return_value=MagicMock()
        ).start()
        hass.config_entries.async_unload_platforms = AsyncMock(
            return_value=mode != "entries_unload_fail"
        )
        hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED, entry.state
        print("Core loaded entry state:", entry.state)
        if mode == "entries_retry_mix":
            await hass.config_entries.async_setup("entry-secondary")
            assert hass.config_entries.async_get_entry(
                "entry-secondary"
            ).state is ConfigEntryState.SETUP_RETRY
        if mode in {"entries_mixed", "entries_second_unload_fail"}:
            other = hass.config_entries.async_get_entry("entry-second-loaded")
            second_cache = CacheManager(
                hass, other.entry_id, account_login=other.unique_id
            )
            await second_cache.async_save_devices([])
            await second_cache.async_close()
            assert await hass.config_entries.async_setup(other.entry_id)
            assert other.state is ConfigEntryState.LOADED
            if mode == "entries_mixed":
                await hass.config_entries.async_setup("entry-retry")
                assert hass.config_entries.async_get_entry(
                    "entry-retry"
                ).state is ConfigEntryState.SETUP_RETRY
    if mode == "entries_discovery_reconnect":
        # Only the transport/browser are synthetic; Core setup, discovery callback,
        # station generation and the two loopback WebSocket peers are real.
        class SyntheticZeroconf:
            port = None

            def get_service_info(self, service_type, name):
                return SimpleNamespace(
                    addresses=[b"\x7f\x00\x00\x01"], port=self.port,
                    properties={
                        b"deviceId": b"synthetic-glagol",
                        b"platform": b"yandexstation",
                    },
                )

        class SyntheticBrowser:
            def __init__(self, zeroconf, service_type, handlers):
                self.zeroconf = zeroconf
                self.handler = handlers[0]
                self.cancelled = False

            def emit(self, port):
                self.zeroconf.port = port
                self.handler(self.zeroconf, "_yandexio._tcp.local.", "synthetic", None)

            def cancel(self):
                self.cancelled = True

        fake_zeroconf = SyntheticZeroconf()
        patch("zeroconf.ServiceBrowser", SyntheticBrowser).start()
        patch(
            "homeassistant.components.zeroconf.async_get_instance",
            new=AsyncMock(return_value=fake_zeroconf),
        ).start()
        hass.data[DOMAIN][DATA_CONFIG]["local_updates"] = True
    hold = asyncio.Event()
    blocking_media = mode in {
        "drain", "drain_head_service", "parallel_reload", "cancel_waiter",
        "changed_during_drain", "drain_timeout", "state_during_drain",
        "flow_during_drain",
    }
    async def media(request):
        if blocking_media and request.path == "/audio.mp3":
            await hold.wait()
        if request.path == "/list.m3u8":
            return web.Response(
                text="#EXTM3U\n#EXTINF:1,\n/segment.ts\n",
                content_type="application/vnd.apple.mpegurl",
            )
        body = {
            "/audio.mp3": b"original-audio",
            "/alternate-v2.mp3": b"v2",
            "/alternate-v3.mp3": b"v3",
            "/segment.ts": b"segment-after-reload",
        }[request.path]
        headers = {"Accept-Ranges": "bytes"}
        if requested := request.headers.get("Range"):
            assert requested.startswith("bytes=")
            start, end = (int(value) for value in requested[6:].split("-", 1))
            headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
            return web.Response(
                status=206, body=body[start:end + 1], headers=headers,
                content_type="audio/mpeg",
            )
        return web.Response(body=body, headers=headers, content_type="audio/mpeg")
    app = hass.http.app
    for media_path in (
        "/audio.mp3", "/alternate-v2.mp3", "/alternate-v3.mp3",
        "/list.m3u8", "/segment.ts",
    ):
        app.router.add_route("*", media_path, media)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    context = lifecycle.context
    context.hass_url = f"http://127.0.0.1:{port}"
    signed = get_url(context, entry.entry_id, context.hass_url + "/audio.mp3")
    try:
        async with ClientSession() as client:
            if blocking_media:
                active = asyncio.create_task(_get_body(client, signed))
                for _ in range(200):
                    if context.active_requests:
                        break
                    await asyncio.sleep(.01)
                assert context.active_requests == 1
                if mode == "drain_head_service":
                    head_task = asyncio.create_task(_get_head(client, signed))
                    for _ in range(200):
                        if context.active_requests == 2:
                            break
                        await asyncio.sleep(.01)
                    assert context.active_requests == 2
                    service_hold = asyncio.Event()
                    async def old_service(call):
                        await service_hold.wait()
                        hass.states.async_set("sensor.synthetic_reload_generation", "old")
                    lifecycle.bind_service("tts.yandex_station_say", old_service)
                    service_task = asyncio.create_task(hass.services.async_call(
                        "tts", "yandex_station_say", {}, blocking=True,
                        **({} if hasattr(ha_core, "SupportsResponse") else {"limit": None}),
                    ))
                    for _ in range(200):
                        if lifecycle.active_consumers:
                            break
                        await asyncio.sleep(.01)
                    assert lifecycle.active_consumers == 1
            else:
                assert await _get_body(client, signed) == b"original-audio"
                async with client.head(signed) as response:
                    assert response.status == 200 and await response.read() == b""
            if mode == "entries_success":
                from custom_components.yandex_station.sensor import YandexCustomSensor
                old_entity = YandexCustomSensor(
                    hass.data[DOMAIN][entry.unique_id], quasar.devices[0],
                    quasar.devices[0]["properties"][0],
                )
            if mode == "drain":
                start_late = asyncio.Event()
                async with lifecycle.consumer(entry.entry_id):
                    async def delayed_consumer():
                        await start_late.wait()
                        try:
                            async with lifecycle.consumer(entry.entry_id):
                                return False
                        except HomeAssistantError:
                            return True
                    delayed_task = asyncio.create_task(delayed_consumer())
            root = config / "custom_components" / "yandex_station"
            if mode in {"success", "drain", "drain_head_service", "parallel_reload",
                        "cancel_waiter", "cancel_during_import", "entries_success",
                        "entries_rollback",
                        "entries_setup_retry", "entries_retry_mix", "entries_disabled",
                        "entries_mixed", "entries_discovery_reconnect",
                        "state_during_drain", "flow_during_drain"}:
                _change_behavior(root, "v2")
            elif mode == "syntax":
                _append(root / "core" / "stream.py", "\ndef broken(:\n")
            elif mode == "binding":
                path = root / "core" / "const.py"
                path.write_text(path.read_text().replace(
                    'CONF_MEDIA_PLAYERS = "media_players"\n', ""
                ))
            elif mode == "anchor":
                _append(root / "core" / "lifecycle.py", "\n# changed anchor\n")
            elif mode == "module_set":
                (root / "core" / "extra_module.py").write_text("VALUE = 1\n")
            elif mode == "registration":
                path = root / "__init__.py"
                path.write_text(path.read_text().replace(
                    'hass, DOMAIN, "reload_code", lifecycle.reload_service_handler()',
                    'hass, DOMAIN, "changed_service", lifecycle.reload_service_handler()',
                ))
            elif mode == "mid_import":
                _append(root / "core" / "image.py", "\nraise RuntimeError('synthetic import failure')\n")
            elif mode == "rebind":
                _append(root / "core" / "stream.py", "\ndel StreamImplementation\n")
            elif mode == "changed_during_drain":
                _change_behavior(root, "v2")
            if mode == "entries_rollback":
                async def unload_and_change(*args):
                    _append(root / "core" / "stream.py", "\n# edited during Core unload\n")
                    return True
                hass.config_entries.async_unload_platforms.side_effect = unload_and_change
            if mode == "entries_setup_retry":
                from custom_components.yandex_station.core.cache import CacheManager
                async def unload_and_remove_cache(*args):
                    cache = CacheManager(
                        hass, entry.entry_id, account_login=entry.unique_id
                    )
                    await cache.async_remove()
                    return True
                hass.config_entries.async_unload_platforms.side_effect = unload_and_remove_cache
            if mode == "entries_second_unload_fail":
                async def fail_second_unload(owned_entry, platforms):
                    return owned_entry.entry_id != "entry-second-loaded"
                hass.config_entries.async_unload_platforms.side_effect = fail_second_unload
            if mode == "options_flow":
                from custom_components.yandex_station import config_flow
                if isinstance(hass.data.get("components"), set):
                    hass.data["components"].add(DOMAIN + ".config_flow")
                hass.config.components.update({"http", "zeroconf", DOMAIN})
                flow = await hass.config_entries.options.async_init(entry.entry_id)
                assert flow["flow_id"]
            elif mode == "unsupported":
                from custom_components.yandex_station.core import lifecycle as anchor
                anchor.HA_VERSION = "unsupported-synthetic-version"
            if mode == "discovery_stop_fail":
                class FailingDiscovery:
                    def stop(self):
                        raise RuntimeError("synthetic discovery stop error")
                lifecycle.discovery_listener = FailingDiscovery()
            if mode == "cancel_during_import":
                import builtins
                import threading
                builtins.hot_reload_entered = threading.Event()
                builtins.hot_reload_release = threading.Event()
                _append(root / "core" / "image.py", '''
import builtins
builtins.hot_reload_entered.set()
if not builtins.hot_reload_release.wait(10):
    raise RuntimeError("import barrier timed out")
''')

            if mode in {
                "drain", "drain_head_service", "parallel_reload", "cancel_waiter",
                "changed_during_drain", "state_during_drain", "flow_during_drain",
            }:
                task = asyncio.create_task(_reload(hass, lifecycle, DOMAIN))
                for _ in range(200):
                    if not context.admission_open:
                        break
                    await asyncio.sleep(.01)
                assert not context.admission_open
                async with client.get(signed) as denied:
                    assert denied.status == 503
                if mode == "parallel_reload":
                    concurrent = await _reload(hass, lifecycle, DOMAIN)
                    assert concurrent == {
                        "status": "failed", "reason": "reload_already_in_progress",
                    }, concurrent
                if mode in {"drain", "drain_head_service"}:
                    if mode == "drain":
                        start_late.set()
                        assert await delayed_task
                    try:
                        await hass.services.async_call(
                            "tts", "yandex_station_say", {}, blocking=True
                        )
                    except HomeAssistantError:
                        pass
                    else:
                        raise AssertionError("service bypassed the closed reload gate")
                if mode == "changed_during_drain":
                    _append(root / "core" / "stream.py", "\n# edited after preflight\n")
                if mode == "state_during_drain":
                    set_state = getattr(entry, "_async_set_state", None) or entry.async_set_state
                    set_state(hass, ConfigEntryState.SETUP_RETRY, None)
                if mode == "flow_during_drain":
                    from custom_components.yandex_station import config_flow
                    if isinstance(hass.data.get("components"), set):
                        hass.data["components"].add(DOMAIN + ".config_flow")
                    hass.config.components.update({"http", "zeroconf", DOMAIN})
                    flow = await hass.config_entries.options.async_init(entry.entry_id)
                    assert flow["flow_id"]
                if mode == "cancel_waiter":
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                hold.set()
                assert await active == b"original-audio"
                if mode == "drain_head_service":
                    assert await head_task == (200, b"")
                    assert not context.admission_open
                    service_hold.set()
                    await service_task
                    assert hass.states.get("sensor.synthetic_reload_generation").state == "old"
                if mode != "cancel_waiter":
                    result = await task
                else:
                    for _ in range(200):
                        if lifecycle.last_reload_result is not None:
                            break
                        await asyncio.sleep(.01)
                    result = lifecycle.last_reload_result
            elif mode == "drain_timeout":
                result = await _reload(hass, lifecycle, DOMAIN)
                assert result["reason"] == "drain_timeout"
                hold.set()
                assert await active == b"original-audio"
            elif mode == "cancel_during_import":
                task = asyncio.create_task(_reload(hass, lifecycle, DOMAIN))
                for _ in range(400):
                    if builtins.hot_reload_entered.is_set():
                        break
                    await asyncio.sleep(.01)
                assert builtins.hot_reload_entered.is_set()
                async with client.get(signed) as denied:
                    assert denied.status == 503
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                assert lifecycle._reload_task is not None
                builtins.hot_reload_release.set()
                for _ in range(400):
                    if lifecycle.last_reload_result is not None:
                        break
                    await asyncio.sleep(.01)
                result = lifecycle.last_reload_result
            else:
                result = await _reload(hass, lifecycle, DOMAIN)
            assert result is not None
            expected = expected_reason
            if expected:
                assert result["reason"] == expected, result
                if expected in {"import_failed", "rebind_failed", "unload_failed"}:
                    assert result["status"] == "restart_required", (result, entry.state)
                    async with client.get(signed) as denied:
                        assert denied.status == 503
                    if expected == "unload_failed":
                        assert entry.state in {
                            ConfigEntryState.FAILED_UNLOAD, ConfigEntryState.LOADED,
                        }
                        if mode == "entries_second_unload_fail":
                            assert result["rollback"] == {entry.entry_id: True}, result
                            assert entry.state is ConfigEntryState.LOADED
                            assert hass.config_entries.async_get_entry(
                                "entry-second-loaded"
                            ).state in {
                                ConfigEntryState.LOADED, ConfigEntryState.FAILED_UNLOAD,
                            }
                else:
                    assert context.admission_open
                    assert await _get_body(client, signed) == b"original-audio"
            else:
                if mode in {"entries_setup_retry", "entries_retry_mix", "entries_mixed"}:
                    assert result["status"] == "partial", result
                    retry_id = {
                        "entries_setup_retry": entry.entry_id,
                        "entries_retry_mix": "entry-secondary",
                        "entries_mixed": "entry-retry",
                    }[mode]
                    assert result["entries"][retry_id] == "setup_retry", result
                    assert hass.config_entries.async_get_entry(
                        retry_id
                    ).state is ConfigEntryState.SETUP_RETRY
                    if mode == "entries_setup_retry":
                        async with client.get(signed) as denied:
                            assert denied.status == 403
                        async with client.head(signed) as denied:
                            assert denied.status == 403
                    else:
                        assert entry.state is ConfigEntryState.LOADED
                        assert result["entries"][entry.entry_id] == "loaded"
                        assert await _get_body(client, signed) == b"v2"
                        if mode == "entries_mixed":
                            assert result["entries"] == {
                                entry.entry_id: "loaded",
                                "entry-second-loaded": "loaded",
                                "entry-retry": "setup_retry",
                            }, result
                            for other_id in (
                                "entry-disabled", "entry-intentionally-unloaded",
                            ):
                                secondary = hass.config_entries.async_get_entry(other_id)
                                assert secondary.state is ConfigEntryState.NOT_LOADED
                            assert hass.config_entries.async_get_entry(
                                "entry-disabled"
                            ).disabled_by is not None
                else:
                    assert result["status"] == "success", result
                    if mode.startswith("entries_"):
                        assert entry.state is ConfigEntryState.LOADED, entry.state
                        if mode == "entries_success":
                            with pytest.raises(HomeAssistantError, match="stale_entity_generation"):
                                await old_entity.async_update()
                    if mode == "entries_disabled":
                        disabled = hass.config_entries.async_get_entry("entry-secondary")
                        assert disabled.disabled_by is not None
                        assert disabled.state is ConfigEntryState.NOT_LOADED
                        assert result["entries"] == {entry.entry_id: "loaded"}
                    assert await _get_body(client, signed) == b"v2"
                    from custom_components.yandex_station.core.stream import get_url as new_get_url
                    fresh_signed = new_get_url(
                        context, entry.entry_id, context.hass_url + "/audio.mp3"
                    )
                    assert await _get_body(client, fresh_signed) == b"v2"
                    async with client.head(signed) as response:
                        assert response.status == 200 and await response.read() == b""
                        assert response.headers["Content-Length"] == "2"
                    async with client.get(
                        signed, headers={"Range": "bytes=0-1"}
                    ) as response:
                        assert response.status == 206
                        assert response.headers["Content-Range"] == "bytes 0-1/2"
                        assert await response.read() == b"v2"
                    playlist_url = new_get_url(
                        context, entry.entry_id, context.hass_url + "/list.m3u8"
                    )
                    async with client.get(playlist_url) as playlist_response:
                        assert playlist_response.status == 200
                        playlist = await playlist_response.text()
                    segment = next(
                        line for line in playlist.splitlines()
                        if line.startswith(context.hass_url + "/api/yandex_station/")
                    )
                    assert await _get_body(client, segment) == b"segment-after-reload"
                    expired_claims = {
                        "url": context.hass_url + "/audio.mp3",
                        "entry_id": entry.entry_id, "lan_only": True, "exp": 1,
                    }
                    expired = (
                        context.hass_url + "/api/yandex_station/"
                        + jwt.encode(expired_claims, context.key, algorithm="HS256")
                        + ".mp3"
                    )
                    async with client.get(expired) as denied:
                        assert denied.status == 403
                    async with client.get(
                        context.hass_url + "/api/yandex_station/invalid.invalid.invalid.mp3"
                    ) as denied:
                        assert denied.status == 403
                if mode == "entries_discovery_reconnect":
                    from custom_components.yandex_station.core.yandex_station import YandexStation
                    active_quasar = lifecycle.runtimes[entry.entry_id]
                    assert active_quasar.owned_glagol_ids == {"synthetic-glagol"}
                    device = active_quasar.speakers[0]
                    station = YandexStation(active_quasar, device)
                    station.hass = hass
                    hass.data["entity_components"] = {
                        "media_player": SimpleNamespace(entities=[]),
                    }
                    station.async_write_ha_state = MagicMock()
                    device["entity"] = station
                    browser = lifecycle.discovery_listener.browser
                    assert isinstance(browser, SyntheticBrowser)
                    connections = []
                    received = [asyncio.Event(), asyncio.Event()]

                    async def peer(request):
                        connections.append(request.transport.get_extra_info("sockname")[1])
                        received[len(connections) - 1].set()
                        ws = web.WebSocketResponse()
                        await ws.prepare(request)
                        async for _ in ws:
                            pass
                        return ws

                    peer_app = web.Application()
                    peer_app.router.add_get("/", peer)
                    peer_runner = web.AppRunner(peer_app)
                    await peer_runner.setup()
                    try:
                        sites = [
                            web.TCPSite(peer_runner, "127.0.0.1", 0)
                            for _ in range(2)
                        ]
                        for peer_site in sites:
                            await peer_site.start()
                        ports = [
                            peer_site._server.sockets[0].getsockname()[1]
                            for peer_site in sites
                        ]
                        async def local_ws(url, **kwargs):
                            kwargs.pop("role")
                            return await client.ws_connect(
                                url.replace("wss://", "ws://"), **kwargs
                            )
                        active_quasar.session.ws_connect = local_ws
                        for index, peer_port in enumerate(ports):
                            browser.emit(peer_port)
                            await asyncio.wait_for(received[index].wait(), 3)
                            assert device["port"] == peer_port
                        assert connections == ports
                        assert station.glagol is not None
                        request_started = asyncio.Event()
                        async def pending_token_request(*args, **kwargs):
                            request_started.set()
                            await asyncio.Event().wait()
                        active_quasar.policy.force_offline_local = False
                        active_quasar.session.request_glagol = pending_token_request
                        pending_token = active_quasar.async_fetch_device_token(device)
                        await asyncio.wait_for(request_started.wait(), 2)
                        old_glagol = station.glagol
                        service_keys = set(lifecycle.service_targets)
                        route_count = len(list(hass.http.app.router.routes()))
                        _change_behavior(root, "v3")
                        repeated = await _reload(hass, lifecycle, DOMAIN)
                        assert repeated["status"] == "success", repeated
                        assert old_glagol._stopped and old_glagol.ws is None
                        assert pending_token.cancelled()
                        assert not active_quasar._token_tasks
                        assert browser.cancelled
                        assert isinstance(lifecycle.discovery_listener.browser, SyntheticBrowser)
                        assert lifecycle.discovery_listener.browser is not browser
                        assert set(lifecycle.service_targets) == service_keys
                        assert len(list(hass.http.app.router.routes())) == route_count
                        assert await _get_body(client, signed) == b"v3"
                    finally:
                        if station.glagol is not None:
                            await station.glagol.stop()
                        await peer_runner.cleanup()
                if mode == "drain_head_service":
                    await hass.services.async_call(
                        "tts", "yandex_station_say", {}, blocking=True
                    )
                    assert hass.states.get("sensor.synthetic_reload_generation").state == "v2"
                if mode == "success":
                    await hass.services.async_call(
                        "tts", "yandex_station_say", {}, blocking=True
                    )
                    assert hass.states.get("sensor.synthetic_reload_generation").state == "v2"
                    from custom_components.yandex_station.sensor import async_setup_entry
                    sensors = []
                    await async_setup_entry(hass, entry, sensors.extend)
                    assert any(sensor.native_value == 23 for sensor in sensors)
                    _change_behavior(root, "v3")
                    second = await _reload(hass, lifecycle, DOMAIN)
                    assert second["status"] == "success", second
                    assert await _get_body(client, signed) == b"v3"
                    assert lifecycle.stream_dispatcher.context is context
            print("reload behavior verified", mode, result["status"])
    finally:
        hold.set()
        if mode == "drain_head_service" and "service_hold" in locals():
            service_hold.set()
        if mode == "cancel_during_import":
            if release := getattr(sys.modules["builtins"], "hot_reload_release", None):
                release.set()
        await runner.cleanup()
        lifecycle.runtimes.pop(entry.entry_id, None)
        await lifecycle.async_stop()
        await hass.async_stop()
        patch.stopall()


async def _get_body(client, url):
    async with client.get(url) as response:
        assert response.status == 200
        return await response.read()

async def _get_head(client, url):
    async with client.head(url) as response:
        return response.status, await response.read()


async def _reload(hass, lifecycle, domain):
    if hasattr(ha_core, "SupportsResponse"):
        return await hass.services.async_call(
            domain, "reload_code", {}, blocking=True, return_response=True
        )
    try:
        await hass.services.async_call(domain, "reload_code", {}, blocking=True, limit=None)
    except HomeAssistantError:
        pass
    return lifecycle.last_reload_result


def _append(path, content):
    with path.open("a", encoding="utf-8") as file:
        file.write(content)


def _change_behavior(root, version):
    _append(root / "core" / "stream.py", f'''
+class StreamImplementation(StreamImplementation):
+    async def _request(self, method, entry_id, url, offline, headers=None):
+        if url.endswith("/audio.mp3"):
+            url = url[:-len("/audio.mp3")] + "/alternate-{version}.mp3"
+        return await super()._request(method, entry_id, url, offline, headers)
+'''.replace("\n+", "\n"))
    _append(root / "__init__.py", f'''
+_previous_rebind_{version} = async_rebind_runtime
+async def async_rebind_runtime(hass, _original=_previous_rebind_{version}):
+    await _original(hass)
+    lifecycle = get_lifecycle(hass)
+    async def generation_service(call):
+        hass.states.async_set("sensor.synthetic_reload_generation", "{version}")
+    lifecycle.bind_service("tts.yandex_station_say", generation_service)
+'''.replace("\n+", "\n"))
    if version == "v2":
        _append(root / "hass" / "hass_utils.py", '''
+_previous_build_include = build_include_config
+def build_include_config(device):
+    if device.get("id") == "ordinary-id":
+        return {"properties": ["temperature"]}
+    return _previous_build_include(device)
+'''.replace("\n+", "\n"))


if __name__ == "__main__":
    asyncio.run(_run_case(Path.cwd(), sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
