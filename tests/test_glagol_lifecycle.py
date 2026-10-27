import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from aiohttp import ClientSession, WSMsgType, web
from homeassistant import loader
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.yandex_station.core.cache import CacheManager
from custom_components.yandex_station.core.policy import EntryPolicy, TokenState
from custom_components.yandex_station.core.yandex_glagol import YandexGlagol
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
from custom_components.yandex_station.core.yandex_session import YandexSession
from custom_components.yandex_station.core.yandex_station import YandexStation

from .test_cache import device_record
from .test_options_and_reauth import core_flow
from .test_session_resilience import make_client, make_response


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


def make_glagol(ws, token="synthetic-candidate"):
    session = MagicMock()
    session.ws_connect = AsyncMock(return_value=ws)
    device = {
        "id": "cloud-1",
        "name": "Станция",
        "host": "127.0.0.1",
        "port": 1905,
        "quasar_info": {"device_id": "glagol-1", "platform": "yandexstation"},
    }
    glagol = YandexGlagol(session, device, token=token)
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



def test_token_recovery_is_logged_only_after_current_state_once_per_incident(caplog):
    async def scenario():
        caplog.set_level(
            logging.INFO, logger="custom_components.yandex_station.core.yandex_glagol"
        )
        glagol, session, _ = make_glagol(None)

        def recovered():
            return [r for r in caplog.records if r.levelno == logging.INFO]

        async def receive(*messages):
            session.ws_connect.return_value = FakeWS([*messages, ws_close()])
            await glagol._connect(0)

        state = ws_text('{"state":{"volume":0.3}}')
        await receive(state)  # A normal initial connection is not a recovery.
        assert not recovered()

        for candidate in ("synthetic-replacement-1", "synthetic-replacement-2"):
            caplog.clear()
            await receive(ws_close(4000, "Invalid token"))
            assert glagol.token_state is TokenState.REJECTED
            glagol.device_token = candidate
            await receive(ws_text("{bad"), ws_text("[]"), ws_text('{"status":"ok"}'))
            assert not recovered()  # Fetch/handshake/replies do not prove acceptance.
            await receive(state, state)
            assert glagol.token_state is TokenState.ACCEPTED
            assert len(recovered()) == 1
            assert recovered()[0].args[0] == "glagol-1"
            assert recovered()[0].args[1] >= 0
            await receive(state)  # Neither another state nor reconnect repeats INFO.
            assert len(recovered()) == 1
            assert candidate not in caplog.text
        await glagol.stop()

    asyncio.run(scenario())


def test_token_recovery_ignores_superseded_connection_and_is_instance_local(caplog):
    async def scenario():
        caplog.set_level(
            logging.INFO, logger="custom_components.yandex_station.core.yandex_glagol"
        )
        glagol, session, _ = make_glagol(FakeWS([]))
        await glagol._handle_token_rejection(glagol.device_token)
        glagol.device_token = "synthetic-replacement"
        other, _, _ = make_glagol(FakeWS([ws_text('{"state":{}}'), ws_close()]))
        await other._connect(0)

        for superseded in ("token", "socket"):
            ws = FakeWS([ws_text('{"state":{}}'), ws_close()])
            session.ws_connect.return_value = ws

            async def supersede(payload):
                if superseded == "token":
                    glagol.device_token = "synthetic-newer"
                else:
                    glagol.ws = FakeWS([])

            ws.send_json = AsyncMock(side_effect=supersede)
            await glagol._connect(0)
        assert not [r for r in caplog.records if r.levelno == logging.INFO]
        session.ws_connect.return_value = FakeWS([ws_text('{"state":{}}'), ws_close()])
        await glagol._connect(0)
        assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 1
        await glagol.stop()
        await other.stop()

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
                    async def ws_connect(self, *, url, role, **kwargs):
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


def test_overlapping_restarts_leave_one_reader_and_stop_closes_every_socket():
    async def scenario():
        closing, release = asyncio.Event(), asyncio.Event()
        sockets, readers = [], []

        class HeldClose(FakeWS):
            async def close(self):
                closing.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
                await super().close()

        async def connect(*, url, role, **kwargs):
            ws = HeldClose([]) if not sockets else FakeWS([])
            sockets.append(ws)
            readers.append(asyncio.current_task())
            return ws

        glagol, session, _ = make_glagol(None)
        session.ws_connect.side_effect = connect
        try:
            await glagol.start_or_restart()
            await asyncio.sleep(0)
            first = asyncio.create_task(glagol.restart())
            await asyncio.wait_for(closing.wait(), 1)
            second = asyncio.create_task(glagol.restart())
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 1)
            await asyncio.sleep(0)
            assert sum(not ws.closed for ws in sockets) == 1
            await glagol.stop()
            assert all(ws.closed for ws in sockets)
            assert all(task.done() for task in readers)
        finally:
            release.set()
            await glagol.stop()
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    asyncio.run(scenario())


def test_stop_invalidates_token_adoption_waiting_for_old_reader():
    async def scenario():
        cancelled, release = asyncio.Event(), asyncio.Event()

        async def old_reader():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
                raise

        glagol, session, _ = make_glagol(FakeWS([]), token=None)
        glagol.url = "wss://127.0.0.1:1905"
        glagol._connect_task = asyncio.create_task(old_reader())
        await asyncio.sleep(0)
        adoption = asyncio.create_task(glagol.adopt_token("replacement"))
        await asyncio.wait_for(cancelled.wait(), 1)
        stop = asyncio.create_task(glagol.stop())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(adoption, stop), 1)
        await asyncio.sleep(0)
        try:
            session.ws_connect.assert_not_awaited()
            assert glagol._connect_task is None
        finally:
            await glagol.stop()

    asyncio.run(scenario())


def test_terminal_stop_interrupts_long_restart_delay():
    async def scenario():
        closed = asyncio.Event()

        class NotifyingWS(FakeWS):
            async def close(self):
                await super().close()
                closed.set()

        glagol, session, _ = make_glagol(NotifyingWS([]))
        await glagol.start_or_restart()
        await asyncio.sleep(0)
        restart = asyncio.create_task(glagol.restart(delay=60))
        await asyncio.wait_for(closed.wait(), 1)
        try:
            await asyncio.wait_for(glagol.stop(), 1)
            await asyncio.wait_for(restart, 1)
            assert session.ws_connect.await_count == 1
            assert glagol._connect_task is None
        finally:
            restart.cancel()
            await asyncio.gather(restart, return_exceptions=True)
            await glagol.stop()

    asyncio.run(scenario())


class TokenPeer:
    """Controllable transport peer; no token/runtime internals are substituted."""

    def __init__(self):
        self.frames = asyncio.Queue()
        self.closed = False
        self.sent_tokens = []
        self.pinged = asyncio.Event()

    async def receive(self):
        return await self.frames.get()

    async def send_json(self, payload):
        self.sent_tokens.append(payload["conversationToken"])
        self.pinged.set()

    async def close(self):
        self.closed = True

    def state(self):
        self.frames.put_nowait(ws_text(
            '{"state":{"aliceState":"IDLE","volume":0.3}}'
        ))

    def reject(self):
        self.closed = True
        self.frames.put_nowait(ws_close(4000, "Invalid token"))


@asynccontextmanager
async def token_runtime(tmp_path, *, offline=False):
    async with core_flow(tmp_path) as (hass, entry, _):
        if hasattr(loader, "async_setup"):
            loader.async_setup(hass)
        if setup_registry := getattr(device_registry, "async_setup", None):
            setup_registry(hass)
        await device_registry.async_load(hass)
        await entity_registry.async_load(hass)
        hass.data.setdefault("entity_components", {})["media_player"] = EntityComponent(
            logging.getLogger(__name__), "media_player", hass
        )
        stores = []

        def manager():
            store = CacheManager(
                hass, entry.entry_id, account_login=entry.unique_id, account_uid="uid-1"
            )
            stores.append(store)
            return store

        cache = manager()
        device = device_record()
        await cache.async_save_devices([device])
        await cache.async_save_local_config("glagol-1", host="127.0.0.1", port=1905)
        await cache.async_save_device_token("glagol-1", "yandexstation", "cached-candidate")
        client = make_client()
        policy = EntryPolicy(
            force_offline_local=offline, use_cached_devices=True,
            cloud_updates=False, enable_scenarios=False,
        )
        session = YandexSession(client, music_token="synthetic-music", policy=policy)
        quasar = YandexQuasar(session, policy=policy, cache_manager=cache)
        quasar._restore_from_snapshot(await cache.async_load())
        quasar.owned_glagol_ids = {"glagol-1"}
        station = YandexStation(quasar, quasar.speakers[0])
        station.hdmi_audio = False  # Persisted metadata: no unrelated HDMI refresh.
        station.entity_id = "media_player.synthetic_token"
        platform = EntityPlatform(
            hass=hass, logger=logging.getLogger(__name__), domain="media_player",
            platform_name="yandex_station", platform=None,
            scan_interval=timedelta(minutes=5), entity_namespace=None,
        )
        quasar.speakers[0]["entity"] = station
        changed = asyncio.Event()
        quasar.subscribe_update("glagol-1_glagol", lambda _: changed.set())
        platform.config_entry = entry
        # Publish before the synthetic mDNS observation supplies an address.
        host = station.device.pop("host")
        await platform.async_add_entities([station])
        station.device["host"] = host
        try:
            yield station, quasar, client, cache, manager, changed
        finally:
            await quasar.async_stop()
            await platform.async_reset()
            for store in stores:
                await store.async_close()


def test_rejected_connection_publishes_disconnect_before_cloud_fetch(tmp_path):
    async def scenario():
        async with token_runtime(tmp_path) as (station, _, client, _, _, changed):
            peer = TokenPeer()
            started, release = asyncio.Event(), asyncio.Event()

            async def fetch(url, **kwargs):
                started.set()
                await release.wait()
                return make_response(200, text='{"status":"ok","token":"replacement"}')

            client.get = AsyncMock(side_effect=fetch)
            client.ws_connect = AsyncMock(return_value=peer)
            try:
                await station.init_local_mode()
                await asyncio.wait_for(peer.pinged.wait(), 2)
                peer.state()
                await asyncio.wait_for(changed.wait(), 2)
                assert station.local_state == {"aliceState": "IDLE", "volume": 0.3}
                peer.reject()
                await asyncio.wait_for(started.wait(), 2)
                assert station.local_state is None
            finally:
                release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("response", [
    '{"status":"ok","token":"cached-candidate"}',
    '{"status":"ok"}',
])
def test_unusable_replacement_does_not_restore_rejected_candidate(tmp_path, response):
    async def scenario():
        async with token_runtime(tmp_path) as (
            station, quasar, client, _, manager, changed,
        ):
            peer = TokenPeer()
            started, release = asyncio.Event(), asyncio.Event()

            async def fetch(url, **kwargs):
                started.set()
                await release.wait()
                return make_response(200, text=response)

            client.get = AsyncMock(side_effect=fetch)
            client.ws_connect = AsyncMock(return_value=peer)
            try:
                await station.init_local_mode()
                await asyncio.wait_for(peer.pinged.wait(), 2)
                peer.reject()
                await asyncio.wait_for(started.wait(), 2)
                # A recovery caller joins the same acquisition as the reader.
                waiting = asyncio.create_task(
                    quasar.async_fetch_device_token(station.device)
                )
                await asyncio.sleep(0)
                release.set()
                assert await waiting is None
                assert quasar.cached_device_token(station.device) is None
                restored = await manager().async_load()
                restored_owner = YandexQuasar(None)
                restored_owner._restore_from_snapshot(restored)
                assert restored_owner.cached_device_token(station.device) is None
                assert client.get.await_count == 1

                # Absence of a candidate is not terminal: later recovery may
                # supply a different token to this existing local client.
                replacement = TokenPeer()
                client.ws_connect.return_value = replacement
                await station.glagol.adopt_token("later-candidate")
                await asyncio.wait_for(replacement.pinged.wait(), 2)
                assert replacement.sent_tokens == ["later-candidate"]
                assert station.local_state is None
                changed.clear()
                replacement.state()
                await asyncio.wait_for(changed.wait(), 2)
                assert station.local_state["volume"] == 0.3
            finally:
                release.set()

    asyncio.run(scenario())


def test_existing_different_candidate_reconnects_without_another_fetch(tmp_path):
    async def scenario():
        async with token_runtime(tmp_path) as (
            station, quasar, client, _, manager, changed,
        ):
            old_peer, new_peer = TokenPeer(), TokenPeer()
            client.ws_connect = AsyncMock(side_effect=[old_peer, new_peer])
            client.get = AsyncMock(return_value=make_response(
                200, text='{"status":"ok","token":"already-fetched"}'
            ))
            await station.init_local_mode()
            await asyncio.wait_for(old_peer.pinged.wait(), 2)
            assert await quasar.async_fetch_device_token(station.device) == "already-fetched"
            old_peer.reject()
            await asyncio.wait_for(new_peer.pinged.wait(), 2)
            assert new_peer.sent_tokens == ["already-fetched"]
            assert client.get.await_count == 1
            restored_owner = YandexQuasar(None)
            restored_owner._restore_from_snapshot(await manager().async_load())
            assert restored_owner.cached_device_token(station.device) == "already-fetched"
            assert station.local_state is None
            changed.clear()
            new_peer.state()
            await asyncio.wait_for(changed.wait(), 2)
            assert station.transport == "local"

    asyncio.run(scenario())


@pytest.mark.parametrize("unload", [False, True])
def test_reader_cancellation_and_owner_unload_have_distinct_fetch_ownership(tmp_path, unload):
    async def scenario():
        async with token_runtime(tmp_path) as (
            station, quasar, client, _, _, _,
        ):
            peer = TokenPeer()
            started, release, cancelled = (
                asyncio.Event(), asyncio.Event(), asyncio.Event()
            )

            async def fetch(url, **kwargs):
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return make_response(200, text='{"status":"ok","token":"shared-result"}')

            client.ws_connect = AsyncMock(return_value=peer)
            client.get = AsyncMock(side_effect=fetch)
            waiting = None
            try:
                await station.init_local_mode()
                await asyncio.wait_for(peer.pinged.wait(), 2)
                peer.reject()
                await asyncio.wait_for(started.wait(), 2)
                waiting = asyncio.create_task(
                    quasar.async_fetch_device_token(station.device)
                )
                await asyncio.sleep(0)
                if unload:
                    await asyncio.wait_for(quasar.async_stop(), 2)
                    with pytest.raises(asyncio.CancelledError):
                        await waiting
                    assert cancelled.is_set()
                else:
                    await station.glagol.stop()
                    assert not cancelled.is_set()
                    release.set()
                    assert await waiting == "shared-result"
                    assert quasar.cached_device_token(station.device) == "shared-result"
                await station.glagol.start_or_restart()
                assert client.ws_connect.await_count == 1
                assert client.get.await_count == 1
                assert station.local_state is None
            finally:
                release.set()
                if waiting is not None:
                    waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("held_stage", ["response", "store_write"])
def test_fetch_started_before_rejection_cannot_resurrect_token(tmp_path, held_stage):
    async def scenario():
        async with token_runtime(tmp_path) as (
            station, quasar, client, cache, manager, changed,
        ):
            peer = TokenPeer()
            held, release = asyncio.Event(), asyncio.Event()
            original_write = cache._store.async_save

            async def fetch(url, **kwargs):
                if held_stage == "response":
                    held.set()
                    await release.wait()
                return make_response(
                    200, text='{"status":"ok","token":"cached-candidate"}'
                )

            async def write(payload):
                token = payload["device_tokens"]["glagol-1"]["yandexstation"]
                if held_stage == "store_write" and token["state"] == "valid":
                    held.set()
                    await release.wait()
                await original_write(payload)

            client.get = AsyncMock(side_effect=fetch)
            client.ws_connect = AsyncMock(return_value=peer)
            waiting = None
            with patch.object(cache._store, "async_save", side_effect=write):
                try:
                    await station.init_local_mode()
                    await asyncio.wait_for(peer.pinged.wait(), 2)
                    peer.state()
                    await asyncio.wait_for(changed.wait(), 2)
                    changed.clear()
                    waiting = asyncio.create_task(
                        quasar.async_fetch_device_token(station.device)
                    )
                    await asyncio.wait_for(held.wait(), 2)
                    peer.reject()
                    await asyncio.wait_for(changed.wait(), 2)
                    assert station.local_state is None
                    release.set()
                    assert await waiting is None
                    await quasar.async_stop()
                    restored_owner = YandexQuasar(None)
                    restored_owner._restore_from_snapshot(await manager().async_load())
                    assert restored_owner.cached_device_token(station.device) is None
                    assert client.get.await_count == 1
                finally:
                    release.set()
                    if waiting is not None:
                        waiting.cancel()
                        await asyncio.gather(waiting, return_exceptions=True)

    asyncio.run(scenario())


def test_failed_rejection_write_does_not_prevent_online_replacement(tmp_path, caplog):
    async def scenario():
        async with token_runtime(tmp_path) as (
            station, quasar, client, cache, _, _,
        ):
            old_peer, new_peer = TokenPeer(), TokenPeer()
            client.ws_connect = AsyncMock(side_effect=[old_peer, new_peer])
            client.get = AsyncMock(return_value=make_response(
                200, text='{"status":"ok","token":"after-disk-error"}'
            ))
            original_write = cache._store.async_save

            async def fail_invalid(payload):
                token = payload["device_tokens"]["glagol-1"]["yandexstation"]
                if token["state"] == "invalid":
                    raise OSError("synthetic write failure")
                await original_write(payload)

            with patch.object(cache._store, "async_save", side_effect=fail_invalid):
                await station.init_local_mode()
                await asyncio.wait_for(old_peer.pinged.wait(), 2)
                old_peer.reject()
                await asyncio.wait_for(new_peer.pinged.wait(), 2)
                assert new_peer.sent_tokens == ["after-disk-error"]
                assert quasar.cached_device_token(station.device) == "after-disk-error"
                assert client.get.await_count == 1
                assert "cache_write_failed" in caplog.text
                assert "cached-candidate" not in caplog.text
                assert "after-disk-error" not in caplog.text

    asyncio.run(scenario())


def test_offline_rejection_survives_restore_without_hidden_fetch(tmp_path):
    async def scenario():
        async with token_runtime(tmp_path, offline=True) as (
            station, quasar, client, cache, manager, changed,
        ):
            peer = TokenPeer()
            saved = asyncio.Event()
            original_write = cache._store.async_save

            async def write(payload):
                await original_write(payload)
                saved.set()

            client.ws_connect = AsyncMock(return_value=peer)
            with patch.object(cache._store, "async_save", side_effect=write):
                await station.init_local_mode()
                await asyncio.wait_for(peer.pinged.wait(), 2)
                peer.state()
                await asyncio.wait_for(changed.wait(), 2)
                assert station.available
                peer.reject()
                await asyncio.wait_for(saved.wait(), 2)
                assert station.local_state is None and not station.available
                assert station.hass.states.get(station.entity_id).state == "unavailable"
                assert quasar.cached_device_token(station.device) is None
                restored_owner = YandexQuasar(None)
                restored_owner._restore_from_snapshot(await manager().async_load())
                assert restored_owner.cached_device_token(station.device) is None
                # mDNS and repeated local init only read the remaining candidate.
                await station.init_local_mode()
                client.get.assert_not_called()
                assert client.ws_connect.await_count == 1

    asyncio.run(scenario())


def test_unaccepted_replacement_cannot_cycle_back_to_rejected_token(tmp_path):
    async def scenario():
        async with token_runtime(tmp_path) as (
            station, quasar, client, cache, manager, _,
        ):
            first, second, forbidden = TokenPeer(), TokenPeer(), TokenPeer()
            client.ws_connect = AsyncMock(side_effect=[first, second, forbidden])
            requested, release = asyncio.Event(), asyncio.Event()
            replies = iter(["unaccepted-b", "cached-candidate"])

            async def fetch(url, **kwargs):
                token = next(replies)
                if token == "cached-candidate":
                    requested.set()
                    await release.wait()
                return make_response(200, text=json.dumps({"status": "ok", "token": token}))

            client.get = AsyncMock(side_effect=fetch)
            await station.init_local_mode()
            await asyncio.wait_for(first.pinged.wait(), 2)
            first.reject()
            await asyncio.wait_for(second.pinged.wait(), 2)
            assert second.sent_tokens == ["unaccepted-b"]
            assert station.local_state is None
            second.reject()
            await asyncio.wait_for(requested.wait(), 2)
            waiting = asyncio.create_task(
                quasar.async_fetch_device_token(station.device)
            )
            await asyncio.sleep(0)
            release.set()
            assert await waiting is None
            assert quasar.cached_device_token(station.device) is None
            restored_owner = YandexQuasar(None)
            restored_owner._restore_from_snapshot(await manager().async_load())
            assert restored_owner.cached_device_token(station.device) is None
            assert client.get.await_count == 2
            assert forbidden.sent_tokens == []

    asyncio.run(scenario())


def test_unload_drains_rejection_queued_behind_cancelled_reader_write(tmp_path):
    async def scenario():
        async with token_runtime(tmp_path) as (
            station, quasar, client, cache, manager, changed,
        ):
            peer = TokenPeer()
            held, release = asyncio.Event(), asyncio.Event()
            original_write = cache._store.async_save

            async def write(payload):
                token = payload["device_tokens"]["glagol-1"]["yandexstation"]
                if token["state"] == "valid":
                    held.set()
                    await release.wait()
                await original_write(payload)

            client.get = AsyncMock(return_value=make_response(
                200, text='{"status":"ok","token":"cached-candidate"}'
            ))
            client.ws_connect = AsyncMock(return_value=peer)
            waiting = unloading = None
            with patch.object(cache._store, "async_save", side_effect=write):
                try:
                    await station.init_local_mode()
                    await asyncio.wait_for(peer.pinged.wait(), 2)
                    peer.state()
                    await asyncio.wait_for(changed.wait(), 2)
                    changed.clear()
                    waiting = asyncio.create_task(
                        quasar.async_fetch_device_token(station.device)
                    )
                    await asyncio.wait_for(held.wait(), 2)
                    peer.reject()
                    await asyncio.wait_for(changed.wait(), 2)
                    # Invalidation has not acquired the Store lock yet.
                    await asyncio.wait_for(station.glagol.stop(), 2)
                    unloading = asyncio.create_task(quasar.async_stop())
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(asyncio.shield(unloading), 0.01)
                    release.set()
                    await asyncio.wait_for(unloading, 2)
                    with pytest.raises(asyncio.CancelledError):
                        await waiting
                    restored_owner = YandexQuasar(None)
                    restored_owner._restore_from_snapshot(await manager().async_load())
                    assert restored_owner.cached_device_token(station.device) is None
                    assert client.ws_connect.await_count == 1
                finally:
                    release.set()
                    tasks = [task for task in (waiting, unloading) if task is not None]
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
