import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.yandex_station.core.policy import AuthState, CsrfState, SessionConfig
from custom_components.yandex_station.core.yandex_session import YandexSession


def test_cookie_probe_fallback():
    async def scenario():
        client = MagicMock()
        cfg = SessionConfig(
            cookie_storage_endpoints=[
                "https://quasar.yandex.ru/get_account_config",
                "https://yandex.ru/iot?storage=1",
            ]
        )
        session = YandexSession(client, x_token="mock_x_token", config=cfg)

        # Mock responses: 1st endpoint fails with 502, 2nd succeeds with storage json
        resp1 = MagicMock()
        resp1.status = 502
        resp1.ok = False

        resp2 = MagicMock()
        resp2.status = 200
        resp2.ok = True
        resp2.text = AsyncMock(return_value='{"status":"ok","storage":{"user":{"id":"uid_123"}}}')
        resp2.json = AsyncMock(return_value={"status": "ok", "storage": {"user": {"id": "uid_123"}}})

        async def mock_get(url, **kwargs):
            if "get_account_config" in url:
                return resp1
            if "storage=1" in url:
                return resp2
            return resp1

        session._get = AsyncMock(side_effect=mock_get)

        ok = await session.refresh_cookies()
        assert ok is True
        assert session.readiness.auth_state == AuthState.VALID
        # Check that both endpoints were queried in sequence
        assert session._get.call_count == 2

    asyncio.run(scenario())


def test_cookie_probe_all_5xx_does_not_reauth():
    async def scenario():
        client = MagicMock()
        session = YandexSession(client, x_token="mock_x_token")

        resp = MagicMock()
        resp.status = 504
        resp.ok = False
        session._get = AsyncMock(return_value=resp)
        session.login_token = AsyncMock()

        # When all probes fail with 5xx, do not clear credentials or force login_token
        ok = await session.refresh_cookies()
        assert ok is False
        assert session.readiness.auth_state == AuthState.UNKNOWN
        session.login_token.assert_not_called()

    asyncio.run(scenario())


def test_single_flight_csrf():
    async def scenario():
        client = MagicMock()
        session = YandexSession(client)

        csrf_resp = MagicMock()
        csrf_resp.status = 200
        csrf_resp.text = AsyncMock(return_value='<html><script>"csrfToken2":"secret_csrf_123"</script></html>')

        fetch_count = 0

        async def mock_get(url, **kwargs):
            nonlocal fetch_count
            fetch_count += 1
            await asyncio.sleep(0.01)
            return csrf_resp

        session._get = AsyncMock(side_effect=mock_get)
        # Launch 5 concurrent ensure_csrf calls
        tokens = await asyncio.gather(*(session.ensure_csrf() for _ in range(5)))
        assert all(t == "secret_csrf_123" for t in tokens)
        assert session.csrf_token == "secret_csrf_123"
        assert session.readiness.csrf_state == CsrfState.CANDIDATE
        # Single-flight check: only 1 network fetch for CSRF
        assert fetch_count == 1

    asyncio.run(scenario())


def test_post_does_not_retry_on_server_error():
    async def scenario():
        client = MagicMock()
        session = YandexSession(client)
        session.ensure_csrf = AsyncMock(return_value="mock_csrf")

        resp_error = MagicMock()
        resp_error.status = 500
        resp_error.ok = False

        call_count = 0

        async def mock_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            return resp_error

        session._request = AsyncMock(side_effect=mock_request)

        try:
            await session.request("post", "https://iot.quasar.yandex.ru/m/v3/user/devices", retry=2)
        except Exception:
            pass

        assert call_count == 1

    asyncio.run(scenario())


def test_csrf_rejection_retries_once():
    async def scenario():
        client = MagicMock()
        session = YandexSession(client)
        session.csrf_token = "old_token"

        resp_csrf_403 = MagicMock()
        resp_csrf_403.status = 403
        resp_csrf_403.text = AsyncMock(return_value='{"status":"error","message":"bad_csrf_token"}')

        resp_ok = MagicMock()
        resp_ok.status = 200

        call_count = 0

        async def mock_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return resp_csrf_403
            return resp_ok

        session._request = AsyncMock(side_effect=mock_request)
        session.ensure_csrf = AsyncMock(return_value="new_token")

        res = await session.request("post", "https://iot.quasar.yandex.ru/m/v3/user/devices", retry=1)
        assert res.status == 200
        assert call_count == 2

    asyncio.run(scenario())


def test_generic_403_does_not_clear_csrf_or_retry():
    async def scenario():
        client = MagicMock()
        session = YandexSession(client)
        session.csrf_token = "valid_csrf_token"

        resp_forbidden = MagicMock()
        resp_forbidden.status = 403
        resp_forbidden.text = AsyncMock(return_value='{"status":"error","message":"permission_denied"}')

        call_count = 0

        async def mock_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            return resp_forbidden

        session._request = AsyncMock(side_effect=mock_request)

        try:
            await session.request("post", "https://rpc.alice.yandex.ru/action", retry=2)
        except Exception:
            pass

        assert call_count == 1
        assert session.csrf_token == "valid_csrf_token"

    asyncio.run(scenario())
