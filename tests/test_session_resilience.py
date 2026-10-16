import asyncio
import io
import time
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import CookieJar
from yarl import URL

from custom_components.yandex_station.core.policy import (
    AuthOutcome,
    AuthState,
    CsrfState,
    EntryPolicy,
    SessionConfig,
)
from custom_components.yandex_station.core.yandex_session import (
    CSRF_FRESH_SECS,
    CloudRequestError,
    YandexSession,
)

QUASAR_URL = "https://iot.quasar.yandex.ru/m/v3/user/devices"
SCENARIO_PUT_URL = "https://iot.quasar.yandex.ru/m/v4/user/scenarios/sid-1"
RPC_URL = "https://rpc.alice.yandex.ru/action"
GLAGOL_URL = "https://quasar.yandex.net/glagol/device_list"
MUSIC_CDN_URL = "https://cdn.example.net/audio/track.mp3"
PUSH_WS_URL = "wss://push.yandex.ru/update"
LAN_WS_URL = "wss://192.168.1.50:1961/"

UID_PROBE = {"url": "https://yandex.ru/iot?storage=1", "validator": "uid_in_storage"}
STATUS_PROBE = {
    "url": "https://quasar.yandex.ru/get_account_config",
    "validator": "status_ok",
}


def make_client(cookie_domains=(".yandex.ru",)):
    """Fake transport with real cookie scoping and persistence."""
    client = MagicMock()
    client.cookie_jar = CookieJar()
    for domain in cookie_domains:
        cookie = SimpleCookie()
        cookie["Session_id"] = "synthetic"
        cookie["Session_id"]["domain"] = domain
        cookie["Session_id"]["path"] = "/"
        client.cookie_jar.update_cookies(
            cookie, response_url=URL(f"https://{domain.lstrip('.')}")
        )
    return client


def make_response(status, *, text="", json_body=None, headers=None, has_json=False):
    resp = MagicMock()
    resp.status = status
    resp.ok = 200 <= status < 300
    resp.text = AsyncMock(return_value=text)
    if has_json:
        resp.json = AsyncMock(return_value=json_body)
    else:
        resp.json = AsyncMock(side_effect=ValueError("not json"))
    resp.headers = headers or {}
    resp.read = AsyncMock(return_value=b"")
    stream = io.BytesIO(text.encode("utf-8"))
    resp.content.read = AsyncMock(side_effect=lambda n=-1: stream.read(n))
    return resp


def csrf_page(token: str):
    return make_response(200, text=f'<html><script>"csrfToken2":"{token}"</script></html>')


def captured_forbidden():
    """Captured live wire shape: 403, text/plain, body 'Forbidden\\n'."""
    resp = make_response(403, text="Forbidden\n")
    resp.headers = {"Content-Type": "text/plain; charset=utf-8"}
    return resp


def exchange_ok(passport_host="https://passport.yandex.ru"):
    return make_response(
        200,
        json_body={
            "status": "ok",
            "passport_host": passport_host,
            "track_id": "track-1",
        },
        has_json=True,
    )


def finish_redirect(location="https://passport.yandex.ru/auth/finish?uuid=1"):
    return make_response(302, headers={"Location": location})


async def prime_csrf(session, client, token):
    """Install a candidate through the real fetch path, then isolate client.get."""
    client.get = AsyncMock(return_value=csrf_page(token))
    assert await session.ensure_csrf() == token
    client.get = AsyncMock()


def get_urls(client):
    return [str(call.args[0]) for call in client.get.call_args_list]


# ---------------------------------------------------------------------------
# cookie probes and auth outcomes
# ---------------------------------------------------------------------------


def test_cookie_probe_fallback_last_good_first():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(cookie_storage_endpoints=[STATUS_PROBE, UID_PROBE])
        session = YandexSession(
            client, x_token="mock_x_token", config=cfg, account_uid="uid_123"
        )

        resp_502 = make_response(502)
        resp_storage = make_response(
            200,
            json_body={"status": "ok", "storage": {"user": {"uid": "uid_123"}}},
            has_json=True,
        )

        def route(url, **kwargs):
            if "get_account_config" in url:
                return resp_502
            return resp_storage

        client.get = AsyncMock(side_effect=route)

        assert await session.refresh_cookies() is AuthOutcome.AUTHENTICATED
        assert session.readiness.auth_state == AuthState.VALID
        # fallback: both endpoints were queried in configured order
        assert client.get.call_count == 2

        # the last successful probe is tried first on the next refresh
        client.get.reset_mock()
        client.get = AsyncMock(return_value=resp_storage)
        assert await session.refresh_cookies() is AuthOutcome.AUTHENTICATED
        assert "storage=1" in get_urls(client)[0]

    asyncio.run(scenario())


def test_cookie_probe_outage_http_errors_and_html_never_reauth():
    async def scenario():
        client = make_client()
        session = YandexSession(client, x_token="mock_x_token")
        session.login_token = AsyncMock(side_effect=AssertionError("no exchange"))

        inconclusive = [
            make_response(500),
            make_response(502),
            make_response(504),
            make_response(401),
            make_response(403),
            make_response(429),
            make_response(200, text="<html>challenge</html>"),
            make_response(200, json_body={"unrelated": True}, has_json=True),
        ]
        for resp in inconclusive:
            client.get = AsyncMock(return_value=resp)
            assert await session.refresh_cookies() is AuthOutcome.UNAVAILABLE
            assert session.readiness.auth_state is AuthState.UNKNOWN

        session.login_token.assert_not_called()

    asyncio.run(scenario())


def test_absent_cookies_exchange_with_validated_redirect_and_post_probe():
    async def scenario():
        client = make_client(cookie_domains=())
        session = YandexSession(client, x_token="xtok", config=SessionConfig())
        updates = []

        async def listener(**kwargs):
            updates.append(kwargs)

        session.add_update_listener(listener)

        client.post = AsyncMock(return_value=exchange_ok())

        def route(url, **kwargs):
            if "/auth/session" in url:
                client.cookie_jar.update_cookies(
                    SimpleCookie("Session_id=exchanged-cookie; Domain=.yandex.ru; Path=/"),
                    response_url=URL("https://passport.yandex.ru"),
                )
                return finish_redirect()
            if "storage=1" in url or "get_account_config" in url:
                assert updates == []
                return make_response(
                    200,
                    json_body={"status": "ok", "storage": {"user": {"uid": "uid_1"}}},
                    has_json=True,
                )
            return make_response(502)

        client.get = AsyncMock(side_effect=route)

        assert await session.refresh_cookies() is AuthOutcome.AUTHENTICATED
        assert session.readiness.auth_state is AuthState.VALID

        # exchange carried the consumer OAuth header
        assert client.post.call_count == 1
        sent = client.post.call_args
        assert sent.kwargs["headers"]["Ya-Consumer-Authorization"] == "OAuth xtok"

        # track_id went once to the validated passport session endpoint only
        urls = get_urls(client)
        assert "/auth/session" in urls[0]
        session_call = client.get.call_args_list[0]
        assert session_call.kwargs["params"] == {"track_id": "track-1"}
        assert sum("/auth/session" in u for u in urls) == 1

        # credentials persisted only after the authenticated post-probe
        assert updates and updates[0]["x_token"] == "xtok"
        restored = make_client(cookie_domains=())
        YandexSession(restored, cookie=updates[0]["cookie"])
        assert restored.cookie_jar.filter_cookies(
            URL("https://quasar.yandex.ru")
        )["Session_id"].value == "exchanged-cookie"

    asyncio.run(scenario())


def test_absent_cookies_without_x_token_is_unavailable():
    async def scenario():
        client = make_client(cookie_domains=())
        session = YandexSession(client, config=SessionConfig())
        client.get = AsyncMock()
        client.post = AsyncMock()

        # a missing credential is never a confirmed rejection
        assert await session.refresh_cookies() is AuthOutcome.UNAVAILABLE
        assert session.readiness.auth_state is AuthState.UNKNOWN
        client.get.assert_not_called()
        client.post.assert_not_called()

    asyncio.run(scenario())


def test_exchange_oauth_token_invalid_is_credential_rejected():
    async def scenario():
        client = make_client(cookie_domains=())
        session = YandexSession(client, x_token="xtok", config=SessionConfig())
        client.post = AsyncMock(
            return_value=make_response(
                400,
                json_body={"status": "error", "errors": ["oauth_token.invalid"]},
                has_json=True,
            )
        )
        client.get = AsyncMock()

        assert await session.refresh_cookies() is AuthOutcome.CREDENTIAL_REJECTED
        assert session.readiness.auth_state is AuthState.REAUTH_REQUIRED
        # rejected before any redirect/probe traffic
        assert client.post.call_count == 1
        assert client.get.call_count == 0

    asyncio.run(scenario())


def test_exchange_other_failures_stay_unavailable():
    async def scenario():
        failures = [
            make_response(502),
            make_response(200, text="<html>challenge</html>"),
            make_response(
                400, json_body={"status": "error", "errors": ["handle.invalid"]}, has_json=True
            ),
            make_response(
                400,
                json_body={"status": "error", "errors": ["oauth_token.expired", "x_token.invalid"]},
                has_json=True,
            ),
            # 5xx body never proves rejection even with the exact error list
            make_response(
                503,
                json_body={"status": "error", "errors": ["oauth_token.invalid"]},
                has_json=True,
            ),
        ]
        for exchange in failures:
            client = make_client(cookie_domains=())
            session = YandexSession(client, x_token="xtok", config=SessionConfig())
            client.post = AsyncMock(return_value=exchange)
            client.get = AsyncMock()

            assert await session.refresh_cookies() is AuthOutcome.UNAVAILABLE
            assert session.readiness.auth_state is AuthState.UNKNOWN
            assert session.x_token == "xtok"
            assert client.get.call_count == 0

    asyncio.run(scenario())


def test_validate_token_never_trusts_5xx_body():
    async def scenario():
        client = make_client()
        session = YandexSession(client, config=SessionConfig())
        bodies = (
            {"status": "ok", "display_login": "user"},
            {"status": "error", "errors": ["oauth_token.invalid"]},
        )
        for body in bodies:
            client.get = AsyncMock(
                return_value=make_response(502, json_body=body, has_json=True)
            )
            resp = await session.validate_token("xtok")
            assert resp.ok is False
            assert resp.oauth_token_invalid is False
            assert resp.x_token == "xtok"

    asyncio.run(scenario())


def test_exchange_success_probe_outage_keeps_credentials():
    async def scenario():
        client = make_client(cookie_domains=())
        session = YandexSession(client, x_token="xtok", config=SessionConfig())
        updates = []

        async def listener(**kwargs):
            updates.append(kwargs)

        session.add_update_listener(listener)

        client.post = AsyncMock(return_value=exchange_ok())

        def route(url, **kwargs):
            if "/auth/session" in url:
                return finish_redirect()
            return make_response(502)

        client.get = AsyncMock(side_effect=route)

        assert await session.refresh_cookies() is AuthOutcome.UNAVAILABLE
        assert session.readiness.auth_state is AuthState.UNKNOWN
        # exchange succeeded but nothing is persisted without the probe
        assert updates == []
        assert session.x_token == "xtok"

    asyncio.run(scenario())


def test_uid_mismatch_without_x_token_is_unavailable():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(cookie_storage_endpoints=[UID_PROBE])
        session = YandexSession(client, config=cfg, account_uid="123")
        client.get = AsyncMock(
            return_value=make_response(
                200, json_body={"storage": {"user": {"uid": "456"}}}, has_json=True
            )
        )
        client.post = AsyncMock()

        # a mismatch is never an account revocation, and without an x-token
        # there is nothing to restore: outcome stays unknown/unavailable
        assert await session.refresh_cookies() is AuthOutcome.UNAVAILABLE
        assert session.readiness.auth_state is AuthState.UNKNOWN
        client.post.assert_not_called()

    asyncio.run(scenario())


def test_uid_mismatch_after_exchange_stays_unknown():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(cookie_storage_endpoints=[UID_PROBE])
        session = YandexSession(client, x_token="xtok", config=cfg, account_uid="123")

        mismatch = make_response(
            200, json_body={"storage": {"user": {"uid": "456"}}}, has_json=True
        )
        client.post = AsyncMock(return_value=exchange_ok())

        def route(url, **kwargs):
            if "/auth/session" in url:
                return finish_redirect()
            return mismatch

        client.get = AsyncMock(side_effect=route)

        # the x-token restored cookies, but the account still differs:
        # never assert revocation, keep credentials, report unknown
        assert await session.refresh_cookies() is AuthOutcome.UNAVAILABLE
        assert session.readiness.auth_state is AuthState.UNKNOWN
        assert session.x_token == "xtok"

    asyncio.run(scenario())


def test_storage_envelope_gaps_are_unknown_but_empty_uid_is_absent():
    async def scenario():
        gaps = [
            {},
            {"storage": None},
            {"storage": {}},
            {"storage": {"user": {}}},
            {"storage": {"user": {"name": "u"}}},
            # the authoritative field is storage.user.uid, not id
            {"storage": {"user": {"id": "456"}}},
            {"storage": {"user": {"uid": ["456"]}}},
        ]
        for body in gaps:
            client = make_client()
            cfg = SessionConfig(cookie_storage_endpoints=[UID_PROBE])
            session = YandexSession(client, x_token="xtok", config=cfg, account_uid="123")
            client.get = AsyncMock(return_value=make_response(200, json_body=body, has_json=True))
            client.post = AsyncMock()

            assert await session.refresh_cookies() is AuthOutcome.UNAVAILABLE
            assert session.readiness.auth_state is AuthState.UNKNOWN
            client.post.assert_not_called()

        # explicit empty/null uid in a proper envelope is confirmed absence
        for uid in ("", None):
            client = make_client()
            cfg = SessionConfig(cookie_storage_endpoints=[UID_PROBE])
            session = YandexSession(client, x_token="xtok", config=cfg, account_uid="123")
            client.get = AsyncMock(
                return_value=make_response(
                    200, json_body={"storage": {"user": {"uid": uid}}}, has_json=True
                )
            )
            client.post = AsyncMock(return_value=make_response(502))

            assert await session.refresh_cookies() is AuthOutcome.UNAVAILABLE
            # absence (not outage) unlocked the single exchange attempt
            assert client.post.call_count == 1

    asyncio.run(scenario())


def test_empty_jar_exchanges_without_probe_requests():
    async def scenario():
        client = make_client(cookie_domains=())
        session = YandexSession(client, x_token="xtok", config=SessionConfig())
        client.post = AsyncMock(return_value=exchange_ok())

        def route(url, **kwargs):
            if "/auth/session" in url:
                return finish_redirect()
            if "storage=1" in url or "get_account_config" in url:
                return make_response(
                    200,
                    json_body={"status": "ok", "storage": {"user": {"uid": "uid_1"}}},
                    has_json=True,
                )
            raise AssertionError("unexpected probe before exchange")

        client.get = AsyncMock(side_effect=route)

        assert await session.refresh_cookies() is AuthOutcome.AUTHENTICATED
        urls = get_urls(client)
        # no cookie endpoint was probed before the exchange
        assert "/auth/session" in urls[0]
        assert "get_account_config" not in urls[0]

    asyncio.run(scenario())


def test_passport_redirect_chain_validated():
    async def scenario():
        # foreign passport_host never receives the track_id
        client = make_client(cookie_domains=())
        session = YandexSession(client, x_token="xtok", config=SessionConfig())
        client.post = AsyncMock(return_value=exchange_ok(passport_host="https://evil.example.com"))
        client.get = AsyncMock(side_effect=AssertionError("no egress to foreign host"))

        assert await session.refresh_cookies() is AuthOutcome.UNAVAILABLE
        assert client.get.call_count == 0

        # a foreign host offering /auth/finish never counts as success
        client2 = make_client(cookie_domains=())
        session2 = YandexSession(client2, x_token="xtok", config=SessionConfig())
        client2.post = AsyncMock(return_value=exchange_ok())

        def route2(url, **kwargs):
            if "/auth/session" in url:
                return finish_redirect(location="https://evil.example.com/auth/finish?uuid=1")
            raise AssertionError("no egress to foreign host")

        client2.get = AsyncMock(side_effect=route2)

        assert await session2.refresh_cookies() is AuthOutcome.UNAVAILABLE
        assert all("evil.example.com" not in u for u in get_urls(client2))

        # the marker inside a query is not the exact finish path
        client3 = make_client(cookie_domains=())
        session3 = YandexSession(client3, x_token="xtok", config=SessionConfig())
        client3.post = AsyncMock(return_value=exchange_ok())

        def route3(url, **kwargs):
            if "/auth/session" in url:
                return finish_redirect(location="/session/next?back=/auth/finish")
            if "/session/next" in url:
                return make_response(200)
            raise AssertionError("unexpected destination")

        client3.get = AsyncMock(side_effect=route3)

        assert await session3.refresh_cookies() is AuthOutcome.UNAVAILABLE
        assert any("/session/next" in u for u in get_urls(client3))

        # relative /auth/finish location resolves against the passport origin
        client4 = make_client(cookie_domains=())
        session4 = YandexSession(client4, x_token="xtok", config=SessionConfig())
        client4.post = AsyncMock(return_value=exchange_ok())

        def route4(url, **kwargs):
            if "/auth/session" in url:
                return finish_redirect(location="/auth/finish?uuid=1")
            if "storage=1" in url or "get_account_config" in url:
                return make_response(
                    200,
                    json_body={"status": "ok", "storage": {"user": {"uid": "uid_1"}}},
                    has_json=True,
                )
            return make_response(502)

        client4.get = AsyncMock(side_effect=route4)

        assert await session4.refresh_cookies() is AuthOutcome.AUTHENTICATED
        # the finish marker itself is not followed with another request
        assert sum("/auth/finish" in u for u in get_urls(client4)) == 0

    asyncio.run(scenario())


def test_prefixed_passport_base_finish_marker_recognized():
    async def scenario():
        # a configured passport base with a path prefix resolves the default
        # passport origin onto the prefixed mirror; the finish marker must
        # be recognized relative to that prefix
        cfg = SessionConfig(passport_base_url="https://passport.yandex.com/prefix")

        # relative finish location on the prefixed mirror base
        client = make_client(cookie_domains=())
        session = YandexSession(client, x_token="xtok", config=cfg)
        client.post = AsyncMock(return_value=exchange_ok())

        def route(url, **kwargs):
            if "/auth/session" in url:
                return finish_redirect(location="/auth/finish?uuid=1")
            if "storage=1" in url or "get_account_config" in url:
                return make_response(
                    200,
                    json_body={"status": "ok", "storage": {"user": {"uid": "uid_1"}}},
                    has_json=True,
                )
            raise AssertionError(f"unexpected destination: {url}")

        client.get = AsyncMock(side_effect=route)

        assert await session.refresh_cookies() is AuthOutcome.AUTHENTICATED
        # the finish marker itself is never followed with another request
        assert sum("/auth/finish" in u for u in get_urls(client)) == 0

        # absolute finish location on the default host is swapped onto the
        # prefixed base and recognized the same way
        client2 = make_client(cookie_domains=())
        session2 = YandexSession(client2, x_token="xtok", config=cfg)
        client2.post = AsyncMock(return_value=exchange_ok())

        def route2(url, **kwargs):
            if "/auth/session" in url:
                return finish_redirect(
                    location="https://passport.yandex.ru/auth/finish?uuid=2"
                )
            if "storage=1" in url or "get_account_config" in url:
                return make_response(
                    200,
                    json_body={"status": "ok", "storage": {"user": {"uid": "uid_1"}}},
                    has_json=True,
                )
            raise AssertionError(f"unexpected destination: {url}")

        client2.get = AsyncMock(side_effect=route2)

        assert await session2.refresh_cookies() is AuthOutcome.AUTHENTICATED
        assert sum("/auth/finish" in u for u in get_urls(client2)) == 0

        # a finish-looking path outside the configured prefix never counts
        # as the exact marker
        client3 = make_client(cookie_domains=())
        session3 = YandexSession(client3, x_token="xtok", config=cfg)
        client3.post = AsyncMock(return_value=exchange_ok())

        def route3(url, **kwargs):
            if "/auth/session" in url:
                return finish_redirect(location="/other/auth/finish?uuid=3")
            if "/other/auth/finish" in url:
                # a real finish endpoint answers without another redirect
                return make_response(200)
            raise AssertionError(f"unexpected destination: {url}")

        client3.get = AsyncMock(side_effect=route3)

        assert await session3.refresh_cookies() is AuthOutcome.UNAVAILABLE
        assert any("/other/auth/finish" in u for u in get_urls(client3))

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# CSRF candidate lifecycle
# ---------------------------------------------------------------------------


def test_single_flight_csrf_fetch():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)

        async def slow_fetch(url, **kwargs):
            await asyncio.sleep(0.01)
            return csrf_page("secret_csrf_1")

        client.get = AsyncMock(side_effect=slow_fetch)

        tokens = await asyncio.gather(*(session.ensure_csrf() for _ in range(5)))
        assert all(t == "secret_csrf_1" for t in tokens)
        assert client.get.call_count == 1
        assert session.readiness.csrf_state == CsrfState.CANDIDATE
        assert session._csrf_generation == 1
        # a fetch never changes auth state
        assert session.readiness.auth_state is AuthState.UNKNOWN

    asyncio.run(scenario())


def test_csrf_failure_cooldown_single_pass():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(
            csrf_endpoints=["https://yandex.ru/iot", "https://yandex.ru/quasar"]
        )
        session = YandexSession(client, config=cfg)
        client.get = AsyncMock(return_value=make_response(500))

        results = await asyncio.gather(*(session.ensure_csrf() for _ in range(4)))
        assert all(r is None for r in results)
        # exactly one full pass over the fallback endpoints
        assert client.get.call_count == 2
        assert session.readiness.csrf_state == CsrfState.UNAVAILABLE
        assert session._csrf_retry_at > time.monotonic()

        # during cooldown no caller triggers another pass
        assert await session.ensure_csrf() is None
        assert client.get.call_count == 2

        # the next backoff slot allows one new pass
        session._csrf_retry_at = 0.0
        client.get = AsyncMock(return_value=csrf_page("candidate_1"))
        assert await session.ensure_csrf() == "candidate_1"
        assert client.get.call_count == 1
        assert session._csrf_generation == 1

    asyncio.run(scenario())


def test_expired_candidate_cleared_before_refresh():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)
        await prime_csrf(session, client, "token_1")

        # candidate older than 24h - 5min is dropped before the fetch
        session._csrf_obtained_at = time.monotonic() - (CSRF_FRESH_SECS + 60)
        client.get = AsyncMock(return_value=make_response(500))
        assert await session.ensure_csrf() is None
        assert session.csrf_token is None
        assert session.readiness.csrf_state == CsrfState.UNAVAILABLE

        # a failed pass/cooldown never resurrects the stale candidate
        assert await session.ensure_csrf() is None
        assert session.csrf_token is None

        # a later pass may install a fresh candidate
        session._csrf_retry_at = 0.0
        client.get = AsyncMock(return_value=csrf_page("token_2"))
        assert await session.ensure_csrf() == "token_2"
        assert session._csrf_generation == 2

    asyncio.run(scenario())


def test_captured_put_403_refreshes_csrf_and_retries_once():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)

        await prime_csrf(session, client, "candidate_1")
        generation = session._csrf_generation
        ok = make_response(200, json_body={"status": "ok"}, has_json=True)
        client.get = AsyncMock(return_value=csrf_page("candidate_2"))
        client.put = AsyncMock(side_effect=[captured_forbidden(), ok])

        # Case 1: rotated token on /iot -> 1 refresh + 1 transparent retry -> 200 OK
        r = await session.put(SCENARIO_PUT_URL, role="quasar", json={"scenario": {}})
        assert r.status == 200
        assert client.get.call_count == 1
        assert client.put.call_count == 2
        assert session.csrf_token == "candidate_2"
        assert session._csrf_generation == generation + 1
        assert session.readiness.csrf_state == CsrfState.CANDIDATE
        assert session.readiness.auth_state is AuthState.UNKNOWN
        assert session._csrf_retry_at == 0.0
        assert client.put.call_args.kwargs["headers"]["x-csrf-token"] == "candidate_2"

        # Case 2: /iot returns the SAME token string -> no replay, cooldown armed
        client.get = AsyncMock(return_value=csrf_page("candidate_2"))
        client.put = AsyncMock(return_value=captured_forbidden())
        session._last_request_ts = 0.0
        with pytest.raises(CloudRequestError) as exc:
            await session.put(SCENARIO_PUT_URL, role="quasar", json={"scenario": {}})
        assert exc.value.status == 403
        assert client.get.call_count == 1
        assert client.put.call_count == 1
        assert session.csrf_token is None
        assert session._csrf_retry_at > time.monotonic()

        # Case 3: new token on /iot, but the single retry also gets 403 -> no 3rd try, cooldown armed
        session._csrf_retry_at = 0.0
        await prime_csrf(session, client, "candidate_3")
        client.get = AsyncMock(return_value=csrf_page("candidate_4"))
        client.put = AsyncMock(side_effect=[captured_forbidden(), captured_forbidden()])
        session._last_request_ts = 0.0
        with pytest.raises(CloudRequestError) as exc2:
            await session.put(SCENARIO_PUT_URL, role="quasar", json={"scenario": {}})
        assert exc2.value.status == 403
        assert client.get.call_count == 1
        assert client.put.call_count == 2
        assert session.csrf_token is None
        assert session._csrf_retry_at > time.monotonic()

        # Case 4: other plain-text 403 body or non-exact MIME -> neither refetch nor replay
        for headers, body in (
            ({"Content-Type": "text/plain; charset=utf-8"}, "Access denied\n"),
            ({"Content-Type": "text/plain; charset=utf-8"}, "Forbidden\n" + "X" * 1024),
            ({"Content-Type": "text/plain; charset=utf-8", "Content-Length": "11"}, "Forbidden\n"),
            ({"Content-Type": "text/plain-custom"}, "Forbidden\n"),
        ):
            session._csrf_retry_at = 0.0
            await prime_csrf(session, client, "candidate_5")
            client.get = AsyncMock(return_value=csrf_page("candidate_6"))
            other_403 = make_response(403, text=body, headers=headers)
            client.put = AsyncMock(return_value=other_403)
            session._last_request_ts = 0.0
            with pytest.raises(CloudRequestError) as exc3:
                await session.put(SCENARIO_PUT_URL, role="quasar", json={"scenario": {}})
            assert exc3.value.status == 403
            assert client.get.call_count == 0
            assert client.put.call_count == 1
            assert session.csrf_token is None
            assert session._csrf_retry_at > time.monotonic()

    asyncio.run(scenario())


def test_late_403_of_old_generation_keeps_same_string_candidate():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)
        await prime_csrf(session, client, "candidate_T")

        loop = asyncio.get_running_loop()
        fut_a = loop.create_future()
        fut_b = loop.create_future()
        pending = [fut_a, fut_b]

        async def dispatch(url, **kwargs):
            return await pending.pop(0)

        client.put = AsyncMock(side_effect=dispatch)

        task_a = asyncio.create_task(
            session.request("put", SCENARIO_PUT_URL, role="quasar")
        )
        for _ in range(3):
            await asyncio.sleep(0)
        assert not fut_a.done()

        session._last_request_ts = 0.0
        task_b = asyncio.create_task(
            session.request("put", SCENARIO_PUT_URL, role="quasar")
        )
        for _ in range(3):
            await asyncio.sleep(0)
        assert not fut_b.done()

        # first response: 403 invalidates generation-1; failed refresh leaves token None
        client.get = AsyncMock(return_value=make_response(500))
        fut_a.set_result(captured_forbidden())
        with pytest.raises(CloudRequestError):
            await task_a
        assert session.csrf_token is None

        # next slot: the HTML yields the SAME string -> new generation
        session._csrf_retry_at = 0.0
        client.get = AsyncMock(return_value=csrf_page("candidate_T"))
        assert await session.ensure_csrf() == "candidate_T"
        assert session._csrf_generation == 2

        # late 403 dispatched with generation 1 must not reset the new pair
        fut_b.set_result(captured_forbidden())
        with pytest.raises(CloudRequestError):
            await task_b
        assert session.csrf_token == "candidate_T"
        assert session._csrf_generation == 2
        assert session.readiness.csrf_state == CsrfState.CANDIDATE

    asyncio.run(scenario())


@pytest.mark.parametrize("retry_after", [None, "400"])
def test_late_success_cannot_cancel_invalidated_candidate_cooldown(retry_after):
    async def scenario():
        client = make_client()
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)
        await prime_csrf(session, client, "candidate_T")
        client.get = AsyncMock(return_value=make_response(500))
        replies = {index: asyncio.get_running_loop().create_future() for index in (1, 2)}
        dispatched = set()
        both_dispatched = asyncio.Event()

        async def dispatch(url, **kwargs):
            index = kwargs["json"]["request"]
            dispatched.add(index)
            if dispatched == {1, 2}:
                both_dispatched.set()
            return await replies[index]

        client.put = AsyncMock(side_effect=dispatch)
        tasks = [
            asyncio.create_task(session.put(
                SCENARIO_PUT_URL, role="quasar", json={"request": index}
            ))
            for index in (1, 2)
        ]
        try:
            await asyncio.wait_for(both_dispatched.wait(), 1)
            forbidden = captured_forbidden()
            if retry_after is not None:
                forbidden.headers["Retry-After"] = retry_after
            replies[1].set_result(forbidden)
            with pytest.raises(CloudRequestError):
                await tasks[0]
            retry_at = session._csrf_retry_at

            replies[2].set_result(make_response(200))
            assert (await tasks[1]).status == 200
            assert await session.ensure_csrf() is None
            assert session.csrf_token is None
            assert session.readiness.csrf_state == CsrfState.UNAVAILABLE
            assert session._csrf_retry_at == retry_at
            assert retry_at > time.monotonic()
            if retry_after is not None:
                assert retry_at - time.monotonic() > 350
            with pytest.raises(RuntimeError, match="csrf_unavailable"):
                await session.put(SCENARIO_PUT_URL, role="quasar", json={})
            assert client.get.await_count == (1 if retry_after is None else 0)
            assert client.put.call_count == 2
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await session.async_stop()

    asyncio.run(scenario())


def test_non_csrf_403_preserves_candidate():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)
        await prime_csrf(session, client, "candidate_1")

        # RPC never receives CSRF and its 403 never touches the cache
        client.post = AsyncMock(return_value=make_response(403))
        with pytest.raises(CloudRequestError) as rpc_error:
            await session.post(RPC_URL, role="rpc", json={})
        assert rpc_error.value.role == "rpc"
        assert client.post.call_count == 1
        assert session.csrf_token == "candidate_1"
        assert session.readiness.csrf_state == CsrfState.CANDIDATE

        # quasar GET carries no CSRF header either
        client.get = AsyncMock(return_value=make_response(403))
        with pytest.raises(CloudRequestError) as get_error:
            await session.get(QUASAR_URL, role="quasar")
        assert get_error.value.status == 403
        assert session.csrf_token == "candidate_1"

    asyncio.run(scenario())


def test_401_does_not_escalate_auth():
    async def scenario():
        client = make_client()
        session = YandexSession(client, config=SessionConfig())
        session.refresh_cookies = AsyncMock(side_effect=AssertionError("no reauth"))
        client.get = AsyncMock(return_value=make_response(401, text="Unauthorized\n"))

        with pytest.raises(CloudRequestError) as exc:
            await session.get(QUASAR_URL, role="quasar")
        assert exc.value.status == 401
        assert session.readiness.auth_state is AuthState.UNKNOWN

    asyncio.run(scenario())


def test_2xx_never_marks_auth_valid():
    async def scenario():
        client = make_client()
        session = YandexSession(client, config=SessionConfig())
        client.get = AsyncMock(return_value=make_response(200, text="<html>challenge</html>"))

        r = await session.get(QUASAR_URL, role="quasar")
        assert r.status == 200
        # auth success belongs to probes/validated inventory, not a bare 2xx
        assert session.readiness.auth_state is AuthState.UNKNOWN

    asyncio.run(scenario())


def test_mutating_quasar_request_needs_candidate_before_dispatch():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)

        client.get = AsyncMock(return_value=make_response(500))
        client.put = AsyncMock(return_value=make_response(200))

        with pytest.raises(RuntimeError, match="csrf_unavailable"):
            await session.put(QUASAR_URL, role="quasar", json={})
        client.put.assert_not_called()

        # GET proceeds without a CSRF candidate
        client.get = AsyncMock(return_value=make_response(200))
        r = await session.get(QUASAR_URL, role="quasar")
        assert r.status == 200

    asyncio.run(scenario())


def test_proactive_csrf_refresh_before_expiry():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)

        await prime_csrf(session, client, "token_1")
        # still fresh: no network
        assert await session.ensure_csrf() == "token_1"
        assert client.get.call_count == 0

        # candidate older than 24h - 5min is refreshed proactively
        session._csrf_obtained_at = time.monotonic() - (CSRF_FRESH_SECS + 60)
        client.get = AsyncMock(return_value=csrf_page("token_2"))
        assert await session.ensure_csrf() == "token_2"
        assert session._csrf_generation == 2

    asyncio.run(scenario())


def test_cancelled_caller_keeps_shared_csrf_fetch():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)

        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_fetch(url, **kwargs):
            started.set()
            await release.wait()
            return csrf_page("candidate_1")

        client.get = AsyncMock(side_effect=slow_fetch)

        caller = asyncio.create_task(session.ensure_csrf())
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        # the shared pass survived the cancelled command deadline
        assert session.csrf_token is None
        release.set()
        await session._csrf_fetch_task
        assert session.csrf_token == "candidate_1"

        client.get.reset_mock()
        assert await session.ensure_csrf() == "candidate_1"
        assert client.get.call_count == 0

    asyncio.run(scenario())


def test_async_stop_cancels_owned_csrf_fetch():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)

        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_fetch(url, **kwargs):
            started.set()
            await release.wait()
            return csrf_page("candidate_1")

        client.get = AsyncMock(side_effect=slow_fetch)
        caller = asyncio.create_task(session.ensure_csrf())
        await started.wait()

        await session.async_stop()
        assert session._csrf_fetch_task is None
        release.set()
        for _ in range(3):
            await asyncio.sleep(0)
        # the owned task never completed its install after unload
        assert session.csrf_token is None

        # teardown does not block later work on a fresh lifecycle
        with pytest.raises(asyncio.CancelledError):
            await caller

    asyncio.run(scenario())


def test_csrf_state_changes_notify_readiness_listener():
    async def scenario():
        client = make_client(cookie_domains=())
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)
        events = []
        session.add_readiness_listener(
            lambda: events.append(str(session.readiness.csrf_state))
        )

        # successful pass: UNKNOWN -> CANDIDATE is a delivered state change
        client.get = AsyncMock(return_value=csrf_page("candidate_1"))
        assert await session.ensure_csrf() == "candidate_1"
        assert events == ["candidate"]

        # a same-state refresh publishes nothing: state change, not every frame
        session._apply_csrf_candidate("candidate_1b")
        assert session.readiness.csrf_state == CsrfState.CANDIDATE
        assert events == ["candidate"]

        # stale drop then failed pass: exactly one CANDIDATE -> UNAVAILABLE
        session._csrf_obtained_at = time.monotonic() - (CSRF_FRESH_SECS + 60)
        client.get = AsyncMock(return_value=make_response(500))
        assert await session.ensure_csrf() is None
        assert events == ["candidate", "unavailable"]

        # repeated failed passes never republish the same state
        session._csrf_retry_at = 0.0
        client.get = AsyncMock(return_value=make_response(500))
        assert await session.ensure_csrf() is None
        assert events == ["candidate", "unavailable"]

        # 403 invalidation of the live candidate publishes the transition
        session._csrf_retry_at = 0.0
        client.get = AsyncMock(return_value=csrf_page("candidate_2"))
        assert await session.ensure_csrf() == "candidate_2"
        client.put = AsyncMock(return_value=captured_forbidden())
        with pytest.raises(CloudRequestError):
            await session.put(SCENARIO_PUT_URL, role="quasar", json={"scenario": {}})
        assert events == ["candidate", "unavailable", "candidate", "unavailable"]

    asyncio.run(scenario())


def test_readiness_listener_failures_and_unsubscribe_are_isolated():
    async def scenario():
        client = make_client(cookie_domains=())
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)
        events = []
        unsubscribe = session.add_readiness_listener(lambda: events.append(1))

        def broken_listener():
            raise RuntimeError("listener teardown bug")

        session.add_readiness_listener(broken_listener)

        # a broken listener never breaks the shared fetch pass
        client.get = AsyncMock(return_value=csrf_page("candidate_1"))
        assert await session.ensure_csrf() == "candidate_1"
        assert events == [1]

        # unsubscribed listeners receive nothing afterwards; the still
        # registered broken listener must stay swallowed
        unsubscribe()
        session._csrf_retry_at = 0.0
        session._csrf_obtained_at = time.monotonic() - (CSRF_FRESH_SECS + 60)
        client.get = AsyncMock(return_value=make_response(500))
        assert await session.ensure_csrf() is None
        assert events == [1]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# HTTP failure surface and transport hardening
# ---------------------------------------------------------------------------


def test_http_failures_raise_cloud_request_error_without_retry():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)
        await prime_csrf(session, client, "candidate_1")

        client.put = AsyncMock(return_value=make_response(502))
        with pytest.raises(CloudRequestError) as put_error:
            await session.put(QUASAR_URL, role="quasar", json={})
        assert put_error.value.status == 502
        assert client.put.call_count == 1

        client.get = AsyncMock(return_value=make_response(500))
        with pytest.raises(CloudRequestError) as get_error:
            await session.get(QUASAR_URL, role="quasar")
        assert get_error.value.role == "quasar"
        assert get_error.value.status == 500
        assert client.get.call_count == 1

    asyncio.run(scenario())


def test_ws_handshake_guards():
    async def scenario():
        client = make_client()
        session = YandexSession(client, config=SessionConfig())
        client.ws_connect = AsyncMock(return_value=MagicMock())

        # Cloud websockets stay free of explicit account credentials.
        with pytest.raises(RuntimeError, match="cloud_ws_credentials_not_allowed"):
            await session.ws_connect(
                PUSH_WS_URL, role="cloud_ws", headers={"Authorization": "OAuth t"}
            )
        # local sockets never carry account credentials either
        with pytest.raises(RuntimeError, match="local_ws_credentials_not_allowed"):
            await session.ws_connect(
                LAN_WS_URL, role="local_ws", headers={"Ya-Client-Cookie": "x"}
            )
        with pytest.raises(RuntimeError, match="local_ws_credentials_not_allowed"):
            await session.ws_connect(
                LAN_WS_URL, role="local_ws", headers={"Cookie": "Session_id=x"}
            )
        client.ws_connect.assert_not_called()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# music / glagol token paths
# ---------------------------------------------------------------------------


def test_request_glagol_music_token_flow():
    async def scenario():
        client = make_client()
        session = YandexSession(client, x_token="xtok", config=SessionConfig())

        def post(url, **kwargs):
            assert "oauth.mobile.yandex.net/1/token" in url
            assert kwargs["data"]["access_token"] == "xtok"
            return make_response(200, json_body={"access_token": "music_tok"}, has_json=True)

        client.post = AsyncMock(side_effect=post)
        client.get = AsyncMock(return_value=make_response(200, json_body=[], has_json=True))

        r = await session.request_glagol(GLAGOL_URL, role="glagol_token")
        assert r.status == 200
        assert session.music_token == "music_tok"
        sent = client.get.call_args.kwargs["headers"]
        assert sent["Authorization"] == "OAuth music_tok"

        # the OAuth token is fetched once and reused
        await session.request_glagol(GLAGOL_URL, role="glagol_token")
        assert client.post.call_count == 1

    asyncio.run(scenario())


def test_request_glagol_validates_destination_before_token_fetch():
    async def scenario():
        cfg = SessionConfig(glagol_base_url="https://mirror.example.com")
        client = make_client()
        session = YandexSession(client, x_token="xtok", config=cfg)
        client.get = AsyncMock()
        client.post = AsyncMock()

        # a foreign target never triggers the music/OAuth request
        with pytest.raises(ValueError, match="untrusted_credential_destination"):
            await session.request_glagol(GLAGOL_URL, role="glagol_token")
        client.get.assert_not_called()
        client.post.assert_not_called()

    asyncio.run(scenario())


def test_glagol_403_scoped_to_music_token_without_replay():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(
            client, x_token="xtok", music_token="music_tok", config=cfg
        )
        await prime_csrf(session, client, "candidate_1")

        client.get = AsyncMock(return_value=make_response(403))
        with pytest.raises(CloudRequestError) as exc:
            await session.request_glagol(GLAGOL_URL, role="glagol_token")
        assert exc.value.role == "glagol_token"
        assert exc.value.status == 403

        # no automatic retry, and the refusal never leaks into account state
        assert client.get.call_count == 1
        assert session.music_token is None
        assert session.readiness.auth_state is AuthState.UNKNOWN
        assert session.csrf_token == "candidate_1"

    asyncio.run(scenario())


def test_missing_tokens_block_hidden_oauth():
    async def scenario():
        client = make_client()
        session = YandexSession(client, config=SessionConfig())

        with pytest.raises(RuntimeError, match="x_token_required"):
            await session.request_glagol(GLAGOL_URL, role="glagol_token")
        client.get.assert_not_called()
        client.post.assert_not_called()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# admission and trust boundaries before egress
# ---------------------------------------------------------------------------


def test_cloud_not_published_blocks_before_egress():
    async def scenario():
        client = make_client()
        session = YandexSession(client, config=SessionConfig(), cloud_ready=False)
        client.get = AsyncMock()
        client.post = AsyncMock()
        client.ws_connect = AsyncMock(return_value=MagicMock())

        with pytest.raises(RuntimeError, match="cloud_not_published"):
            await session.get(QUASAR_URL, role="quasar")
        with pytest.raises(RuntimeError, match="cloud_not_published"):
            await session.get(MUSIC_CDN_URL, role="music")
        with pytest.raises(RuntimeError, match="cloud_not_published"):
            await session.ws_connect(PUSH_WS_URL, role="cloud_ws")

        client.get.assert_not_called()
        client.post.assert_not_called()
        client.ws_connect.assert_not_called()

        # the LAN websocket path stays available without cloud admission
        await session.ws_connect(LAN_WS_URL, role="local_ws", ssl=False)
        assert client.ws_connect.await_count == 1
        assert "192.168.1.50" in str(client.ws_connect.call_args.args[0])

    asyncio.run(scenario())


def test_offline_policy_blocks_cloud_roles_and_probes():
    async def scenario():
        client = make_client()
        session = YandexSession(
            client,
            config=SessionConfig(),
            policy=EntryPolicy(force_offline_local=True),
        )
        client.get = AsyncMock()
        client.post = AsyncMock()
        client.ws_connect = AsyncMock(return_value=MagicMock())

        with pytest.raises(RuntimeError, match="cloud_blocked_by_policy"):
            await session.get(QUASAR_URL, role="quasar")
        with pytest.raises(RuntimeError, match="cloud_blocked_by_policy"):
            await session.get(MUSIC_CDN_URL, role="music")
        with pytest.raises(RuntimeError, match="cloud_blocked_by_policy"):
            await session.ws_connect(PUSH_WS_URL, role="cloud_ws")

        # probes are cloud roles too: refresh stays unavailable, never reauth
        assert await session.refresh_cookies() is AuthOutcome.UNAVAILABLE
        assert session.readiness.auth_state is AuthState.UNKNOWN
        client.post.assert_not_called()

        await session.ws_connect(LAN_WS_URL, role="local_ws")
        assert client.ws_connect.await_count == 1

    asyncio.run(scenario())


def test_credential_destination_trust_boundary():
    async def scenario():
        # quasar API requests are credential-bearing: external overrides reject
        mirror = make_client()
        mirror_cfg = SessionConfig(quasar_base_url="https://mirror.example.com")
        mirror_session = YandexSession(mirror, config=mirror_cfg)
        mirror.get = AsyncMock()

        with pytest.raises(ValueError, match="untrusted_credential_destination"):
            await mirror_session.get(QUASAR_URL, role="quasar")
        mirror.get.assert_not_called()

        # unsigned CDN fetches with an empty jar stay allowed as role music
        client = make_client(cookie_domains=())
        session = YandexSession(client, config=SessionConfig())
        client.get = AsyncMock(return_value=make_response(200))
        r = await session.get(MUSIC_CDN_URL, role="music")
        assert r.status == 200
        assert client.get.call_count == 1

        # explicit credential=False cannot bypass a detected OAuth header
        with pytest.raises(ValueError, match="untrusted_credential_destination"):
            await session.get(
                MUSIC_CDN_URL,
                role="music",
                credential=False,
                headers={"Authorization": "OAuth tok"},
            )
        # an explicit Cookie header is credential-bearing too
        with pytest.raises(ValueError, match="untrusted_credential_destination"):
            await session.get(
                MUSIC_CDN_URL, role="music", headers={"Cookie": "Session_id=x"}
            )
        assert client.get.call_count == 1

        # implicitly scoped cookies must never ride to a foreign origin
        cookied = make_client(cookie_domains=("cdn.example.net",))
        cookied_session = YandexSession(cookied, config=SessionConfig())
        cookied.get = AsyncMock()
        with pytest.raises(ValueError, match="untrusted_credential_destination"):
            await cookied_session.get(MUSIC_CDN_URL, role="music")
        cookied.get.assert_not_called()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# retry-after hints
# ---------------------------------------------------------------------------


def test_retry_after_hint_recorded_per_role():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)
        await prime_csrf(session, client, "candidate_1")

        # a failure keeps only the raw hint string per role
        client.put = AsyncMock(
            return_value=make_response(503, headers={"Retry-After": "120"})
        )
        with pytest.raises(CloudRequestError):
            await session.put(QUASAR_URL, role="quasar", json={})
        assert session.retry_after == {"quasar": "120"}

        # success of the role clears the stale hint
        client.put = AsyncMock(return_value=make_response(200))
        r = await session.put(QUASAR_URL, role="quasar", json={})
        assert r.status == 200
        assert session.retry_after == {}

        # a failure without the hint records nothing
        client.put = AsyncMock(return_value=make_response(502))
        with pytest.raises(CloudRequestError):
            await session.put(QUASAR_URL, role="quasar", json={})
        assert session.retry_after == {}

        # the glagol role tracks its own hint
        glagol_session = YandexSession(
            make_client(), x_token="xtok", music_token="music_tok", config=SessionConfig()
        )
        glagol_client = glagol_session._session
        glagol_client.get = AsyncMock(
            return_value=make_response(429, headers={"Retry-After": "60"})
        )
        with pytest.raises(CloudRequestError):
            await glagol_session.request_glagol(GLAGOL_URL, role="glagol_token")
        assert glagol_session.retry_after == {"glagol_token": "60"}

    asyncio.run(scenario())


def test_csrf_invalidation_honors_retry_after_hint():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(csrf_endpoints=["https://yandex.ru/iot"])
        session = YandexSession(client, config=cfg)
        await prime_csrf(session, client, "candidate_1")

        # captured 403 shape plus a Retry-After hint on the same response
        forbidden = captured_forbidden()
        forbidden.headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Retry-After": "400",
        }
        client.put = AsyncMock(return_value=forbidden)
        with pytest.raises(CloudRequestError):
            await session.put(SCENARIO_PUT_URL, role="quasar", json={})

        assert session.csrf_token is None
        remaining = session._csrf_retry_at - time.monotonic()
        # the hint lower bound (400s) replaces the 30s ladder slot
        assert remaining > 350

    asyncio.run(scenario())


def test_retry_after_survives_within_one_fallback_pass():
    async def scenario():
        client = make_client()
        cfg = SessionConfig(
            csrf_endpoints=["https://yandex.ru/iot", "https://yandex.ru/quasar"]
        )
        session = YandexSession(client, config=cfg)

        def route(url, **kwargs):
            if "quasar" in url:
                # second endpoint omits the header
                return make_response(500)
            return make_response(429, headers={"Retry-After": "777"})

        client.get = AsyncMock(side_effect=route)

        assert await session.ensure_csrf() is None
        # the strongest valid hint of the pass survives the omitted header
        assert session.retry_after == {"csrf": "777"}
        remaining = session._csrf_retry_at - time.monotonic()
        assert remaining > 600

        # a stronger hint replaces the weaker one within a pass
        session._csrf_retry_at = 0.0
        session.retry_after.clear()

        def route2(url, **kwargs):
            if "quasar" in url:
                return make_response(429, headers={"Retry-After": "900"})
            return make_response(429, headers={"Retry-After": "100"})

        client.get = AsyncMock(side_effect=route2)
        assert await session.ensure_csrf() is None
        assert session.retry_after == {"csrf": "900"}

    asyncio.run(scenario())
