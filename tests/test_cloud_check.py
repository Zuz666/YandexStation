import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp import WSMessage, WSMsgType

from custom_components.yandex_station.core.cache import CacheManager
from custom_components.yandex_station.core.policy import (
    AuthState, DevicesState, EntryPolicy, SessionConfig,
)
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
from custom_components.yandex_station.core.yandex_session import YandexSession
from .test_session_resilience import make_client, make_response
from .test_cache import device_record
from .test_options_and_reauth import core_flow


def test_cloud_check_reports_rejection_without_repairing_or_replacing_catalog():
    async def scenario():
        client = make_client()
        client.get = AsyncMock(return_value=make_response(401))
        client.post = AsyncMock(side_effect=AssertionError("No credential exchange"))
        session = YandexSession(client, x_token="SYNTHETIC_X_TOKEN")
        session.music_token = "SYNTHETIC_MUSIC_TOKEN"
        session.readiness.auth_state = AuthState.VALID
        session.readiness.devices_state = DevicesState.READY
        quasar = YandexQuasar(session, policy=EntryPolicy())
        quasar.devices = [{"id": "cached-device", "scenario_id": "cached-scenario"}]
        quasar.scenarios = [{"id": "cached-scenario"}]

        result = await quasar.async_check_cloud()

        assert result["status"] == "failed"
        assert result["reason"] == "http_error"
        assert result["http_status"] == 401
        assert session.readiness.auth_state is AuthState.VALID
        assert session.readiness.devices_state is DevicesState.READY
        assert session.x_token == "SYNTHETIC_X_TOKEN"
        assert session.music_token == "SYNTHETIC_MUSIC_TOKEN"
        assert session.csrf_token is None
        assert quasar.devices == [{"id": "cached-device", "scenario_id": "cached-scenario"}]
        assert quasar.scenarios == [{"id": "cached-scenario"}]
        assert client.get.await_count == 1
        client.post.assert_not_called()
        await quasar.async_stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    None,
    {"status": "error", "errors": ["SYNTHETIC_SECRET"]},
    {"status": "ok", "households": [{"name": "House", "all": "not a catalog"}]},
])
def test_cloud_check_rejects_http_success_without_valid_inventory(payload):
    async def scenario():
        client = make_client()
        client.get = AsyncMock(return_value=make_response(
            200, json_body=payload, has_json=payload is not None,
        ))
        quasar = YandexQuasar(YandexSession(client))
        result = await quasar.async_check_cloud()
        assert result["status"] == "failed"
        assert result["reason"] == "invalid_response"
        assert quasar.readiness.auth_state is AuthState.UNKNOWN
        assert "device_count" not in result
        assert "SYNTHETIC_SECRET" not in str(result)
        await quasar.async_stop()

    asyncio.run(scenario())


def test_cloud_check_counts_fresh_inventory_without_saving_it(tmp_path):
    async def scenario():
        async with core_flow(tmp_path) as (hass, entry, _):
            cache = CacheManager(
                hass, entry.entry_id, account_login=entry.unique_id,
                account_uid=entry.data["account_uid"],
            )
            previous = device_record("cached-device")
            await cache.async_save_devices([previous])
            client = make_client()
            response = make_response(200, has_json=True, json_body={
                "status": "ok", "households": [
                    {"name": "Synthetic home", "all": [
                        device_record("new-device-one"), device_record("new-device-two"),
                    ]},
                ],
            })
            client.get = AsyncMock(return_value=response)
            quasar = YandexQuasar(YandexSession(client), cache_manager=cache)
            quasar.devices = [previous]
            try:
                result = await quasar.async_check_cloud()
                assert result["status"] == "ok"
                assert result["device_count"] == 2
                assert quasar.readiness.auth_state is AuthState.UNKNOWN
                assert quasar.devices == [previous]
                fresh_reader = CacheManager(
                    hass, entry.entry_id, account_login=entry.unique_id,
                    account_uid=entry.data["account_uid"],
                )
                try:
                    snapshot = await fresh_reader.async_load()
                    assert [device["id"] for device in snapshot.devices] == ["cached-device"]
                finally:
                    await fresh_reader.async_close()
                response.release.assert_called_once()
            finally:
                await quasar.async_stop()

    asyncio.run(scenario())


def test_offline_cloud_check_does_not_dispatch_network_requests():
    async def scenario():
        client = make_client()
        client.get = AsyncMock(side_effect=AssertionError("Forced offline"))
        policy = EntryPolicy(force_offline_local=True)
        quasar = YandexQuasar(YandexSession(client, policy=policy))
        result = await quasar.async_check_cloud()
        assert result["status"] == "skipped"
        assert result["reason"] == "cloud_blocked_by_policy"
        client.get.assert_not_called()
        await quasar.async_stop()

    asyncio.run(scenario())


def test_cloud_check_shares_an_attempt_and_unload_cancels_it():
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def request(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        client = make_client()
        client.get = AsyncMock(side_effect=request)
        quasar = YandexQuasar(YandexSession(client))
        first = asyncio.create_task(quasar.async_check_cloud())
        await started.wait()
        second = asyncio.create_task(quasar.async_check_cloud())
        await asyncio.sleep(0)
        assert client.get.await_count == 1
        await quasar.async_stop()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert cancelled.is_set()
        assert quasar.last_cloud_check["status"] == "cancelled"
        assert quasar.cloud_connection["state"] == "stopped"

    asyncio.run(scenario())


def test_cloud_websocket_metadata_tracks_messages_and_cancellation():
    async def scenario():
        processed = asyncio.Event()

        class Socket:
            closed = False

            def __aiter__(self):
                return self.messages()

            async def messages(self):
                yield WSMessage(WSMsgType.TEXT, '{"operation":"ping"}', "")
                processed.set()
                await asyncio.Event().wait()

            async def close(self):
                self.closed = True

        socket = Socket()
        client = make_client()
        client.get = AsyncMock(return_value=make_response(200, has_json=True, json_body={
            "status": "ok", "households": [], "updates_url": "wss://push.yandex.ru/update",
        }))
        client.ws_connect = AsyncMock(return_value=socket)
        quasar = YandexQuasar(YandexSession(client))
        connection = asyncio.create_task(quasar.connect())
        try:
            await asyncio.wait_for(processed.wait(), 2)
            assert quasar.cloud_connection["state"] == "connected"
            assert quasar.cloud_connection["connected_at"] is not None
            assert quasar.cloud_connection["last_message_at"] is not None
        finally:
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)
            await quasar.async_stop()
        assert socket.closed
        assert quasar.cloud_connection["state"] == "stopped"

    asyncio.run(scenario())


def test_cloud_check_distinguishes_blocked_destination_from_invalid_response():
    async def scenario():
        client = make_client()
        client.get = AsyncMock(side_effect=AssertionError("No untrusted transport"))
        session = YandexSession(client, x_token="SYNTHETIC_X_TOKEN", config=SessionConfig(
            quasar_base_url="https://untrusted.example",
        ))
        quasar = YandexQuasar(session)
        result = await quasar.async_check_cloud()
        assert result["status"] == "failed"
        assert result["reason"] == "request_blocked_by_policy"
        assert "http_status" not in result
        client.get.assert_not_called()
        await quasar.async_stop()

    asyncio.run(scenario())
