import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientSession, WSMsgType, web

from custom_components.yandex_station.core.cache import CacheManager
from custom_components.yandex_station.core.policy import EntryPolicy, TokenState
from custom_components.yandex_station.core.yandex_glagol import YandexGlagol
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar


class FakeWS:
    def __init__(self, script, timeout=None):
        self.script = list(script)
        self.closed = False
        self.sent = []
        self.timeout = timeout

    async def receive(self):
        if self.timeout is not None:
            raise self.timeout
        if not self.script:
            # reader ends like a server-side close of the transport
            await asyncio.sleep(3600)
        return self.script.pop(0)

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


def ws_text(payload):
    class Msg:
        type = WSMsgType.TEXT
        data = payload
    return Msg()


def ws_close(code=1000, reason=""):
    class Msg:
        type = WSMsgType.CLOSE
        data = code
        extra = reason
    return Msg()


def make_glagol(ws, token="synthetic-candidate", quasar=None):
    session = MagicMock()
    session.ws_connect = AsyncMock(return_value=ws)
    device = {
        "id": "cloud-1",
        "name": "Станция",
        "host": "127.0.0.1",
        "port": 1905,
        "quasar_info": {"device_id": "glagol-1", "platform": "yandexstation"},
    }
    glagol = YandexGlagol(session, device, token=token, quasar=quasar)
    glagol.update_handler = handler = MagicMock()
    return glagol, session, handler


def test_text_state_accepts_token_and_malformed_frames_are_ignored():
    async def scenario():
        ws = FakeWS([
            ws_text("{not json"),
            ws_text("[1, 2]"),
            ws_text(json.dumps({"state": {"aliceState": "IDLE", "volume": 0.3}})),
        ])
        glagol, session, handler = make_glagol(ws)
        glagol.url = "wss://127.0.0.1:1905"
        task = asyncio.create_task(glagol._connect(0))
        for _ in range(50):
            if handler.call_count >= 1:
                break
            await asyncio.sleep(0.01)
        assert glagol.token_state is TokenState.ACCEPTED
        handler.assert_called_once_with({"state": {"aliceState": "IDLE", "volume": 0.3}})
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # malformed frames never reset or reject the token
        assert glagol.device_token == "synthetic-candidate"

def test_invalid_token_close_invalidates_and_refetches_once():
    async def scenario():
        cache = MagicMock(spec=CacheManager)
        cache.async_invalidate_device_token = AsyncMock(return_value=True)
        session = MagicMock()
        token_resp = AsyncMock()
        token_resp.text = AsyncMock(
            return_value='{"status":"ok","token":"synthetic-candidate-2"}'
        )
        session.request_glagol = AsyncMock(return_value=token_resp)
        quasar = YandexQuasar(session, cache_manager=cache)
        quasar.owned_glagol_ids = {"glagol-1"}
        quasar.device_tokens = {"glagol-1": {"yandexstation": "synthetic-candidate"}}

        ws = FakeWS([ws_close(4000, "Invalid token")])
        glagol, _, handler = make_glagol(ws, quasar=quasar)
        glagol.url = "wss://127.0.0.1:1905"
        await glagol._connect(0)
        await glagol.stop()

        assert glagol.token_state is TokenState.CANDIDATE
        assert glagol.device_token == "synthetic-candidate-2"
        # Exactly one single-flight fetch after a confirmed token rejection
        assert session.request_glagol.call_count == 1
        # Only the matching RAM tuple is replaced with the new candidate
        assert quasar.device_tokens == {
            "glagol-1": {"yandexstation": "synthetic-candidate-2"}
        }
        cache.async_invalidate_device_token.assert_awaited_once_with(
            "glagol-1", "yandexstation", "synthetic-candidate"
        )
        handler.assert_called_with(None)

    asyncio.run(scenario())


def test_other_close_codes_keep_token_and_reconnect_with_backoff():
    async def scenario():
        for code, reason in ((4000, ""), (4001, "Invalid token"), (1000, "")):
            ws = FakeWS([ws_close(code, reason)])
            glagol, session, handler = make_glagol(ws)
            glagol.url = "wss://127.0.0.1:1905"
            await glagol._connect(5)  # 30*4=120s backoff runs as a background task

            assert glagol._connect_task is not None
            assert not glagol._connect_task.done()
            assert glagol.device_token == "synthetic-candidate"
            assert glagol.token_state is not TokenState.REJECTED
            assert glagol.ws is None
            await glagol.stop()
            assert glagol._connect_task is None

    asyncio.run(scenario())


def test_missing_token_waits_without_cloud_fetch():
    async def scenario():
        ws = FakeWS([])
        glagol, session, handler = make_glagol(ws, token=None)
        glagol.url = "wss://127.0.0.1:1905"
        await glagol._connect(0)
        session.ws_connect.assert_not_awaited()
        session.request_glagol.assert_not_called()
        assert glagol.token_state is TokenState.MISSING

    asyncio.run(scenario())


def test_stop_cancels_tasks_closes_socket_and_resets_consumer():
    async def scenario():
        ws = FakeWS([])  # reader hangs until cancelled
        glagol, session, handler = make_glagol(ws)
        await glagol.start_or_restart()
        task = glagol._connect_task
        await asyncio.sleep(0.05)
        await glagol.stop()
        assert task.cancelled() or task.done()
        assert ws.closed
        assert glagol._stopped and glagol.url is None
        handler.assert_called_with(None)
        # Restarting after stop() is not allowed
        await glagol.start_or_restart()
        assert glagol._connect_task is None

    asyncio.run(scenario())


def test_waiters_are_instance_isolated():
    async def scenario():
        ws1, ws2 = FakeWS([]), FakeWS([])
        g1, _, _ = make_glagol(ws1)
        g2, _, _ = make_glagol(ws2)
        future = g1.loop.create_future()
        g1.waiters["req-1"] = future
        assert g2.waiters == {}
        await g1.stop()
        assert future.cancelled()

    asyncio.run(scenario())


def test_quasar_token_fetch_is_single_flight():
    async def scenario():
        quasar = YandexQuasar(MagicMock(), policy=EntryPolicy())
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_fetch(device):
            started.set()
            await release.wait()
            return "token-once"

        quasar._async_fetch_device_token = slow_fetch
        device = {
            "quasar_info": {"device_id": "glagol-1", "platform": "yandexstation"}
        }
        first = quasar.async_fetch_device_token(device)
        await started.wait()
        second = quasar.async_fetch_device_token(device)
        assert first is second
        release.set()
        assert await first == "token-once"
        assert await second == "token-once"
        assert quasar._token_tasks == {}

    asyncio.run(scenario())


def test_discovery_port_change_reconnects_to_new_loopback_peer():
    async def scenario():
        events = {}
        connections = []

        async def peer(request):
            port = request.transport.get_extra_info("sockname")[1]
            connections.append(port)
            events[port].set()
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"state": {"aliceState": "IDLE"}})
            async for _ in ws:
                pass
            return ws

        app = web.Application()
        app.router.add_route("GET", "/", peer)
        runner = web.AppRunner(app)
        await runner.setup()
        first = web.TCPSite(runner, "127.0.0.1", 0)
        second = web.TCPSite(runner, "127.0.0.1", 0)
        await first.start()
        await second.start()
        first_port = first._server.sockets[0].getsockname()[1]
        second_port = second._server.sockets[0].getsockname()[1]
        events[first_port] = asyncio.Event()
        events[second_port] = asyncio.Event()
        device = {
            "id": "synthetic-cloud", "name": "Synthetic",
            "host": "127.0.0.1", "port": first_port,
            "quasar_info": {
                "device_id": "synthetic-glagol", "platform": "yandexstation",
            },
        }
        try:
            async with ClientSession() as client:
                class LocalSession:
                    async def ws_connect(self, url, **kwargs):
                        kwargs.pop("role")
                        return await client.ws_connect(
                            url.replace("wss://", "ws://"), **kwargs
                        )
                glagol = YandexGlagol(
                    LocalSession(), device, token="synthetic-candidate"
                )
                try:
                    await glagol.start_or_restart()
                    await asyncio.wait_for(events[first_port].wait(), 3)
                    device["port"] = second_port
                    await glagol.start_or_restart()
                    await asyncio.wait_for(events[second_port].wait(), 3)
                    assert connections == [first_port, second_port]
                finally:
                    await glagol.stop()
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
