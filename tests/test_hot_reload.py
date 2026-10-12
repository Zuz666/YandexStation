import asyncio
from unittest.mock import AsyncMock, MagicMock

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

    asyncio.run(scenario())
