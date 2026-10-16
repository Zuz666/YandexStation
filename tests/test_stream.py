import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import jwt
from aiohttp import ClientSession, web
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant

from custom_components.yandex_station.core.lifecycle import (
    StableStreamDispatcher, get_lifecycle,
)
from custom_components.yandex_station.core.policy import EntryPolicy
from custom_components.yandex_station.core.stream import (
    LanOnlyResolver, StreamImplementation, get_url, validate_media_url,
)
from .test_cache import config_entry


def test_stream_rechecks_policy_on_real_get_head_and_hls(tmp_path):
    async def scenario():
        try:
            hass = HomeAssistant(str(tmp_path))
        except TypeError:
            hass = HomeAssistant()
        hass.config.config_dir = str(tmp_path)
        hass.config_entries = ConfigEntries(hass, {})
        lifecycle = get_lifecycle(hass)
        context = lifecycle.context
        policy = EntryPolicy()
        lifecycle.runtimes["entry-1"] = SimpleNamespace(policy=policy, stopping=False)
        entry = config_entry()
        seen = []
        dispatcher = StableStreamDispatcher(context)

        async def media(request):
            seen.append((request.path, request.headers.get("Cookie")))
            if request.path == "/redirect.mp3":
                raise web.HTTPFound("http://198.51.100.10/leak.mp3")
            if request.path == "/list.m3u8":
                return web.Response(text="#EXTM3U\n/audio.mp3\n/nested.m3u8\n",
                                    content_type="application/vnd.apple.mpegurl")
            if request.path == "/external.m3u8":
                return web.Response(text="#EXTM3U\nhttp://198.51.100.10/leak.ts\n")
            if request.path == "/key.m3u8":
                return web.Response(text=(
                    '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,'
                    'URI="http://198.51.100.10/key.mp3"\n/audio.mp3\n'
                ))
            if request.path == "/nested.m3u8":
                return web.Response(text="#EXTM3U\n/audio.mp3\n")
            if request.headers.get("Range") == "bytes=1-2":
                return web.Response(status=206, body=b"ud", headers={
                    "Content-Range": "bytes 1-2/5", "Accept-Ranges": "bytes"})
            return web.Response(body=b"audio", headers={"Accept-Ranges": "bytes"})

        async def signed_route(request):
            token, ext = request.match_info["token"], request.match_info["ext"]
            if request.method == "HEAD":
                return await dispatcher.head(request, token, ext)
            return await dispatcher.get(request, token, ext)

        app = web.Application()
        app.router.add_route("*", "/api/yandex_station/{token}.{ext}", signed_route)
        app.router.add_route("*", "/{name}", media)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        context.hass_url = f"http://127.0.0.1:{port}"
        context.implementation = StreamImplementation(context)
        with patch.object(hass.config_entries, "async_get_entry", return_value=entry) as lookup:
            try:
                # An online-issued JWT must obey the policy changed after issuance.
                signed = get_url(context, "entry-1", context.hass_url + "/audio.mp3")
                public_signed = get_url(
                    context, "entry-1", "http://198.51.100.10/x.mp3"
                )
                lookup.return_value = SimpleNamespace(
                    options={"force_offline_local": True}, disabled_by=None, source="user"
                )
                async with ClientSession() as probe:
                    async with probe.get(public_signed) as result:
                        assert result.status == 403
                lookup.return_value = entry
                policy.force_offline_local = True
                async with ClientSession(cookies={"secret": "PRIVATE_COOKIE"}) as client:
                    async with client.get(signed) as result:
                        assert result.status == 200
                        assert await result.read() == b"audio"
                    async with client.head(signed) as result:
                        assert result.status == 200
                        assert await result.read() == b""
                        assert result.headers["Content-Type"] == "audio/mpeg"
                    async with client.get(signed, headers={"Range": "bytes=1-2"}) as result:
                        assert result.status == 206
                        assert await result.read() == b"ud"
                        assert result.headers["Content-Range"] == "bytes 1-2/5"
                    hostname_media = get_url(
                        context, "entry-1", f"http://localhost:{port}/audio.mp3"
                    )
                    async with client.get(hostname_media) as result:
                        assert result.status == 200
                        assert await result.read() == b"audio"
                    relative = get_url(context, "entry-1", "/audio.mp3")
                    relative_token = urlsplit(relative).path.rsplit("/", 1)[1].rsplit(".", 1)[0]
                    assert jwt.decode(
                        relative_token, context.key, algorithms=["HS256"]
                    )["url"] == "/audio.mp3"
                    with patch(
                        "custom_components.yandex_station.core.stream.async_process_play_media_url",
                        return_value=context.hass_url + "/audio.mp3",
                    ):
                        async with client.get(relative) as result:
                            assert result.status == 200
                            assert await result.read() == b"audio"
                    assert all(cookie is None for _, cookie in seen)

                    for blocked in ("http://198.51.100.10/x.mp3",
                                    "http://user:pass@127.0.0.1/x.mp3",
                                    "//198.51.100.10/x.mp3", "ftp://127.0.0.1/x.mp3"):
                        try:
                            validate_media_url(context, "entry-1", blocked)
                            assert False, blocked
                        except ValueError:
                            pass
                    before = len(seen)
                    public_jwt = jwt.encode({"url": "http://198.51.100.10/x.mp3",
                        "entry_id": "entry-1", "lan_only": False,
                        "exp": int(time.time()) + 60}, context.key, algorithm="HS256")
                    async with client.get(f"{context.hass_url}/api/yandex_station/{public_jwt}.mp3") as result:
                        assert result.status == 403
                    assert len(seen) == before

                    redirect = get_url(context, "entry-1", context.hass_url + "/redirect.mp3")
                    async with client.get(redirect) as result:
                        assert result.status == 403
                    assert seen[-1][0] == "/redirect.mp3"
                    playlist = get_url(context, "entry-1", context.hass_url + "/list.m3u8")
                    async with client.get(playlist) as result:
                        assert result.status == 200
                        lines = (await result.text()).splitlines()
                    assert lines[0] == "#EXTM3U"
                    for child in lines[1:]:
                        token = urlsplit(child).path.rsplit("/", 1)[1].rsplit(".", 1)[0]
                        claims = jwt.decode(token, context.key, algorithms=["HS256"])
                        assert claims["entry_id"] == "entry-1" and claims["lan_only"] is True
                        async with client.get(child) as result:
                            assert result.status == 200
                    external = get_url(context, "entry-1", context.hass_url + "/external.m3u8")
                    async with client.get(external) as result:
                        assert result.status == 403
                    key_playlist = get_url(context, "entry-1", context.hass_url + "/key.m3u8")
                    async with client.get(key_playlist) as result:
                        assert result.status == 403
                    expired = jwt.encode({"url": context.hass_url + "/audio.mp3",
                        "entry_id": "entry-1", "lan_only": True,
                        "exp": int(time.time()) - 10}, context.key, algorithm="HS256")
                    async with client.get(f"{context.hass_url}/api/yandex_station/{expired}.mp3") as result:
                        assert result.status == 403
                    lookup.return_value = None
                    async with client.get(signed) as result:
                        assert result.status == 403
            finally:
                await context.implementation.async_close()
                await runner.cleanup()
                await hass.async_stop()

    asyncio.run(scenario())


def test_hls_encrypted_key_children_are_signed_and_gated(tmp_path):
    async def scenario():
        try:
            hass = HomeAssistant(str(tmp_path))
        except TypeError:
            hass = HomeAssistant()
        hass.config.config_dir = str(tmp_path)
        hass.config_entries = ConfigEntries(hass, {})
        lifecycle = get_lifecycle(hass)
        context = lifecycle.context
        # offline policy keeps every child destination on the LAN
        policy = EntryPolicy(force_offline_local=True)
        lifecycle.runtimes["entry-1"] = SimpleNamespace(policy=policy, stopping=False)
        entry = config_entry()
        seen = []
        dispatcher = StableStreamDispatcher(context)

        async def media(request):
            seen.append(request.path)
            host = request.host
            if request.path.endswith(".m3u8"):
                if request.path == "/segextless.m3u8":
                    # same extensionless URI, but as a plain segment line
                    body = "#EXTM3U\n/key\n/segment.ts\n"
                else:
                    playlists = {
                        "/enc.m3u8": f'URI="http://{host}/encryption.key"',
                        # extensionless and unknown-extension key URIs are
                        # keys, not media segments
                        "/extless.m3u8": f'URI="http://{host}/key"',
                        "/bint.m3u8": f'URI="http://{host}/key.bin"',
                        "/pub.m3u8": 'URI="http://198.51.100.10/encryption.key"',
                    }
                    body = (
                        "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,"
                        f'{playlists[request.path]}\n/segment.ts\n'
                    )
                return web.Response(
                    text=body, content_type="application/vnd.apple.mpegurl"
                )
            if request.path in ("/encryption.key", "/key", "/key.bin"):
                return web.Response(body=b"0123456789abcdef")
            if request.path == "/segment.ts":
                return web.Response(body=b"segment-data")
            raise AssertionError(f"unexpected media path: {request.path}")

        async def signed_route(request):
            token, ext = request.match_info["token"], request.match_info["ext"]
            if request.method == "HEAD":
                return await dispatcher.head(request, token, ext)
            return await dispatcher.get(request, token, ext)

        app = web.Application()
        app.router.add_route("*", "/api/yandex_station/{token}.{ext}", signed_route)
        app.router.add_route("*", "/{name}", media)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        context.hass_url = f"http://127.0.0.1:{port}"
        context.implementation = StreamImplementation(context)
        with patch.object(hass.config_entries, "async_get_entry", return_value=entry):
            try:
                async with ClientSession() as client:

                    async def rewritten_key_url(name):
                        """Fetch a playlist and return its signed key child."""
                        playlist = get_url(
                            context, "entry-1", f"{context.hass_url}/{name}.m3u8"
                        )
                        async with client.get(playlist) as result:
                            assert result.status == 200, name
                            lines = (await result.text()).splitlines()
                        key_line = next(l for l in lines if l.startswith("#EXT-X-KEY"))
                        signed_key = key_line.split('URI="', 1)[1].rstrip('"')
                        assert signed_key.startswith(
                            f"{context.hass_url}/api/yandex_station/"
                        )
                        assert signed_key.endswith(".key")
                        return lines, signed_key

                    # encrypted loopback playlist plays end to end: the key
                    # child is rewritten to a signed local resource and the
                    # bytes stream back as opaque octets
                    lines, signed_key = await rewritten_key_url("enc")
                    token = urlsplit(signed_key).path.rsplit("/", 1)[1].rsplit(".", 1)[0]
                    claims = jwt.decode(token, context.key, algorithms=["HS256"])
                    assert claims["entry_id"] == "entry-1"
                    assert claims["lan_only"] is True
                    assert claims["url"] == f"http://127.0.0.1:{port}/encryption.key"
                    async with client.get(signed_key) as result:
                        assert result.status == 200
                        assert await result.read() == b"0123456789abcdef"
                        assert result.headers["Content-Type"].startswith(
                            "application/octet-stream"
                        )
                    segment = next(l for l in lines if not l.startswith("#"))
                    async with client.get(segment) as result:
                        assert result.status == 200
                        assert await result.read() == b"segment-data"

                    # key URIs without a media extension still proxy as key
                    # resources when extracted from the EXT-X-KEY tag
                    for name, upstream in (
                        ("extless", f"http://127.0.0.1:{port}/key"),
                        ("bint", f"http://127.0.0.1:{port}/key.bin"),
                    ):
                        _, signed = await rewritten_key_url(name)
                        key_token = urlsplit(signed).path.rsplit("/", 1)[1]
                        key_token = key_token.rsplit(".", 1)[0]
                        key_claims = jwt.decode(
                            key_token, context.key, algorithms=["HS256"]
                        )
                        assert key_claims["url"] == upstream
                        async with client.get(signed) as result:
                            assert result.status == 200, name
                            assert await result.read() == b"0123456789abcdef"

                    # negative security: the same extensionless URI as a
                    # plain segment line is not a key resource and stays
                    # blocked - no generic extensionless proxy exists
                    blocked = get_url(
                        context, "entry-1", f"{context.hass_url}/segextless.m3u8"
                    )
                    async with client.get(blocked) as result:
                        assert result.status == 403

                    # negative security: a public key host never passes the
                    # offline LAN gate, whatever the extension
                    public = get_url(
                        context, "entry-1", f"{context.hass_url}/pub.m3u8"
                    )
                    async with client.get(public) as result:
                        assert result.status == 403

                    # the blocked playlist was fetched upstream; denial
                    # happened at child admission, before any key egress
                    assert "/segextless.m3u8" in seen and "/pub.m3u8" in seen
                    assert seen.count("/encryption.key") == 1
            finally:
                await context.implementation.async_close()
                await runner.cleanup()
                await hass.async_stop()

    asyncio.run(scenario())


def test_lan_resolver_rejects_mixed_public_answers(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        original = loop.getaddrinfo

        async def mixed(*args, **kwargs):
            return [
                (2, 1, 6, "", ("127.0.0.1", 80)),
                (2, 1, 6, "", ("198.51.100.10", 80)),
            ]

        monkeypatch.setattr(loop, "getaddrinfo", mixed)
        try:
            try:
                await LanOnlyResolver().resolve("lan.example", 80)
                assert False, "mixed DNS answers must be rejected"
            except OSError:
                pass
        finally:
            monkeypatch.setattr(loop, "getaddrinfo", original)

    asyncio.run(scenario())
