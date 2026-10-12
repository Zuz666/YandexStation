import asyncio
import importlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
from aiohttp import web

from custom_components.yandex_station.core.lifecycle import (
    LifecycleManager,
    StableStreamDispatcher,
    StreamContext,
)


def test_stream_context_and_key_stability():
    hass = MagicMock()
    context = StreamContext(hass=hass, hass_url="http://127.0.0.1:8123")
    initial_key = context.key
    assert initial_key and len(initial_key) >= 16

    # Generate a signed token using context.key
    token = jwt.encode({"url": "http://stream.mp3", "exp": 9999999999}, context.key, "HS256")

    # Change the implementation behind context
    impl1 = MagicMock()
    impl2 = MagicMock()
    context.implementation = impl1
    assert context.key == initial_key

    context.implementation = impl2
    assert context.key == initial_key

    # Token must still be decodable with context.key
    decoded = jwt.decode(token, context.key, algorithms=["HS256"])
    assert decoded["url"] == "http://stream.mp3"


def test_dispatcher_admission_and_drain():
    async def scenario():
        hass = MagicMock()
        context = StreamContext(hass=hass, hass_url="http://127.0.0.1:8123")
        impl = MagicMock()
        impl.get = AsyncMock(return_value=web.Response(text="version 1"))
        context.implementation = impl

        dispatcher = StableStreamDispatcher(context)

        # 1. Normal request passes to implementation
        req = MagicMock()
        resp = await dispatcher.get(req, "mock_token", "mp3")
        assert resp.text == "version 1"
        impl.get.assert_called_once()

        # 2. When admission is closed (during hot-reload quiesce), returns 503
        context.admission_open = False
        resp503 = await dispatcher.get(req, "mock_token", "mp3")
        assert resp503.status == 503

        # 3. Active requests counter and drain
        context.admission_open = True
        assert context.active_requests == 0

        # Simulate in-flight request
        assert context.acquire_request() is True
        assert context.active_requests == 1
        assert context.drain_event.is_set() is False

        # Release in-flight request
        context.release_request()
        assert context.active_requests == 0
        assert context.drain_event.is_set() is True

    asyncio.run(scenario())


def test_implementation_swap_preserves_routing():
    async def scenario():
        hass = MagicMock()
        context = StreamContext(hass=hass, hass_url="http://127.0.0.1:8123")

        class Impl1:
            async def get(self, request, token, ext):
                return web.Response(text="impl 1 output")

            async def head(self, request, token, ext):
                return web.Response(status=200)

        class Impl2:
            async def get(self, request, token, ext):
                return web.Response(text="impl 2 updated output")

            async def head(self, request, token, ext):
                return web.Response(status=204)

        context.implementation = Impl1()
        dispatcher = StableStreamDispatcher(context)

        req = MagicMock()
        r1 = await dispatcher.get(req, "tok", "mp3")
        assert r1.text == "impl 1 output"

        # Hot-swap implementation
        context.implementation = Impl2()

        r2 = await dispatcher.get(req, "tok", "mp3")
        assert r2.text == "impl 2 updated output"

    asyncio.run(scenario())


def test_lifecycle_manager_opt_in():
    async def scenario():
        hass = MagicMock()
        mgr = LifecycleManager()

        # Without enable_code_reload=True, hot reload must be refused
        result = await mgr.async_hot_reload(hass, enable_code_reload=False)
        assert result.get("error") == "hot_reload_disabled"



def test_hot_reload_two_entries_success():
    async def scenario():
        hass = MagicMock()
        entry1 = MagicMock(entry_id="e1", disabled_by=None)
        entry1.state = "loaded"
        entry2 = MagicMock(entry_id="e2", disabled_by=None)
        entry2.state = "loaded"
        hass.config_entries.async_entries.return_value = [entry1, entry2]
        hass.config_entries.async_unload = AsyncMock(return_value=True)
        hass.config_entries.async_setup = AsyncMock(return_value=True)

        context = StreamContext(hass=hass, hass_url="http://127.0.0.1:8123")
        mgr = LifecycleManager(context=context)
        with patch("importlib.reload", side_effect=lambda m: m):
            res = await mgr.async_hot_reload(hass, enable_code_reload=True)
        assert res.get("success") is True
        assert res.get("setup_results") == {"e1": True, "e2": True}
        assert hass.config_entries.async_unload.call_count == 2
        assert hass.config_entries.async_setup.call_count == 2
        assert context.admission_open is True

    asyncio.run(scenario())


def test_hot_reload_failed_unload_rolls_back():
    async def scenario():
        hass = MagicMock()
        entry1 = MagicMock(entry_id="e1", disabled_by=None)
        entry1.state = "loaded"
        entry2 = MagicMock(entry_id="e2", disabled_by=None)
        entry2.state = "loaded"
        hass.config_entries.async_entries.return_value = [entry1, entry2]

        # e1 unloads ok, e2 fails to unload!
        async def mock_unload(eid):
            if eid == "e1":
                return True
            return False

        hass.config_entries.async_unload = AsyncMock(side_effect=mock_unload)
        hass.config_entries.async_setup = AsyncMock(return_value=True)

        context = StreamContext(hass=hass, hass_url="http://127.0.0.1:8123")
        mgr = LifecycleManager(context=context)

        res = await mgr.async_hot_reload(hass, enable_code_reload=True)
        assert res.get("error") == "unload_failed"
        # Entry 1 must be rolled back (re-setup) with old code!
        hass.config_entries.async_setup.assert_called_once_with("e1")
        # Admission must be re-opened for existing working runtime
        assert context.admission_open is True

    asyncio.run(scenario())


def test_hot_reload_failed_rebind_leaves_http_503():
    async def scenario():
        hass = MagicMock()
        entry1 = MagicMock(entry_id="e1", disabled_by=None)
        entry1.state = "loaded"
        hass.config_entries.async_entries.return_value = [entry1]
        hass.config_entries.async_unload = AsyncMock(return_value=True)
        hass.config_entries.async_setup = AsyncMock(return_value=True)

        context = StreamContext(hass=hass, hass_url="http://127.0.0.1:8123")
        mgr = LifecycleManager(context=context)

        orig_reload = importlib.reload
        stream_mod = sys.modules.get("custom_components.yandex_station.core.stream")
        orig_impl = getattr(stream_mod, "StreamImplementation", None)
        try:
            def mock_reload(m):
                res = orig_reload(m)
                if getattr(m, "__name__", "") == "custom_components.yandex_station.core.stream":
                    if hasattr(res, "StreamImplementation"):
                        delattr(res, "StreamImplementation")
                return res

            with patch("importlib.reload", side_effect=mock_reload):
                res = await mgr.async_hot_reload(hass, enable_code_reload=True)
                assert res.get("error") == "rebind_failed"
                # HTTP admission must remain closed (HTTP 503) per RFC §7.4
                assert context.admission_open is False

                dispatcher = StableStreamDispatcher(context)
                resp = await dispatcher.get(MagicMock(), "tok", "mp3")
                assert resp.status == 503
        finally:
            if stream_mod and orig_impl:
                setattr(stream_mod, "StreamImplementation", orig_impl)

    asyncio.run(scenario())
