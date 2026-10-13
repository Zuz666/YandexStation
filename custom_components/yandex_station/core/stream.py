"""Entry-bound media proxy. Stable StreamContext owns its key and route."""

import asyncio
import ipaddress
import logging
import re
import socket
import time
from urllib.parse import urljoin, urlsplit

import jwt
from aiohttp import (
    ClientError, ClientSession, ClientTimeout, DummyCookieJar, TCPConnector, hdrs, web,
)
from aiohttp.abc import AbstractResolver
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DATA_CONFIG, DOMAIN
from .lifecycle import StreamContext, get_lifecycle

_LOGGER = logging.getLogger(__name__)

MIME_TYPES = {
    "flac": "audio/x-flac",
    "aac": "audio/aac",
    "he.aac": "audio/aac",
    "mp3": "audio/mpeg",
    # https://www.rfc-editor.org/rfc/rfc4337.txt
    "flac.mp4": "audio/mp4",
    "aac.mp4": "audio/mp4",
    "he.aac.mp4": "audio/mp4",
    # application/vnd.apple.mpegurl
    "m3u8": "application/x-mpegURL",
    "ts": "video/MP2T",
    "gif": "image/gif",
    "mp4": "video/mp4",
    "wav": "audio/wav",
}


def get_ext(url: str) -> str:
    return urlsplit(url).path.rsplit(".", 1)[-1]


_LAN_V4 = tuple(ipaddress.ip_network(value) for value in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12",
    "192.168.0.0/16", "169.254.0.0/16",
))
_LAN_V6 = tuple(ipaddress.ip_network(value) for value in (
    "::1/128", "fc00::/7", "fe80::/10",
))
_NUMERIC_HOST = re.compile(
    r"(?:0[xX][0-9a-fA-F]+|[0-9]+)"
    r"(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+))*\.?"
)


def _lan_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in network for network in (_LAN_V4 if ip.version == 4 else _LAN_V6))


def _entry_policy(context: StreamContext, entry_id: str):
    lifecycle = get_lifecycle(context.hass)
    entry = context.hass.config_entries.async_get_entry(entry_id)
    runtime = lifecycle.runtimes.get(entry_id)
    if (
        lifecycle.context is not context or not lifecycle.entry_enabled(entry)
        or runtime is None or runtime.stopping
    ):
        raise ValueError("entry_unavailable")
    return entry, runtime.policy


def is_offline(context: StreamContext, entry_id: str) -> bool:
    """Current entry options and loaded policy; never weaken an active offline gate."""
    entry, loaded_policy = _entry_policy(context, entry_id)
    config = context.hass.data.get(DOMAIN, {}).get(DATA_CONFIG) or {}
    override = entry.options.get("force_offline_local")
    if override in (None, ""):
        override = config.get("force_offline_local")
    return loaded_policy.force_offline_local or bool(override)


def _destination(context: StreamContext, entry_id: str, url: str, offline: bool) -> str:
    _entry_policy(context, entry_id)
    if not isinstance(url, str) or not url or url.startswith("//"):
        raise ValueError("media_destination_blocked")
    if url.startswith("/"):
        if not context.hass_url:
            raise ValueError("local_origin_unavailable")
        url = urljoin(context.hass_url.rstrip("/") + "/", url)
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as error:
        raise ValueError("media_destination_blocked") from error
    if (
        parts.scheme not in ("http", "https") or not host or parts.username is not None
        or parts.password is not None or parts.fragment or
        (port is not None and not 0 < port < 65536) or
        any(char.isspace() for char in url) or "%" in host
    ):
        raise ValueError("media_destination_blocked")
    if offline:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # aiohttp 3.8 treats octal/hex/short IPv4 as literals and skips
            # AbstractResolver entirely. Never let a noncanonical numeric host
            # reach that connector path; DNS names still use LanOnlyResolver.
            if ":" in host or _NUMERIC_HOST.fullmatch(host):
                raise ValueError("media_destination_blocked")
            try:
                socket.inet_aton(host)
            except OSError:
                pass
            else:
                raise ValueError("media_destination_blocked")
        else:
            if not _lan_address(host):
                raise ValueError("media_destination_blocked")
    return url


def validate_media_url(context: StreamContext, entry_id: str, url: str) -> str:
    """Preflight known destinations; hostname DNS is enforced by the connector."""
    return _destination(context, entry_id, url, is_offline(context, entry_id))


def _sign(context: StreamContext, entry_id: str, url: str, ext: str,
          expires_at: int, lan_only: bool) -> str:
    token = jwt.encode({
        "url": url, "entry_id": entry_id, "lan_only": lan_only, "exp": expires_at,
    }, context.key, algorithm="HS256")
    return f"{context.hass_url}/api/yandex_station/{token}.{ext}"


def get_url(context: StreamContext, entry_id: str, url: str,
            ext: str | None = None, expires: int = 3600) -> str:
    offline = is_offline(context, entry_id)
    validated = _destination(context, entry_id, url, offline)
    ext = ext.replace("-", ".") if ext else get_ext(validated)
    if ext not in MIME_TYPES or not context.hass_url or expires <= 0:
        raise ValueError("media_url_unavailable")
    # Keep a local relative path so Core can add its media-source auth token
    # when the signed proxy URL is actually requested.
    source = url if url.startswith("/") else validated
    return _sign(context, entry_id, source, ext, int(time.time()) + expires, offline)


def copy_headers(headers: dict, names: tuple) -> dict:
    return {k: v for k in names if (v := headers.get(k))}


CONTENT_TYPES = {
    "audio/aac": "aac",
    "audio/mpeg": "mp3",
    "audio/x-flac": "flac",
    "application/vnd.apple.mpegurl": "m3u8",
    "application/x-mpegURL": "m3u8",
}

REQUEST_HEADERS = (hdrs.RANGE,)
RESPONSE_HEADERS = (hdrs.ACCEPT_RANGES, hdrs.CONTENT_LENGTH, hdrs.CONTENT_RANGE)
STREAM_TIMEOUT = ClientTimeout(sock_connect=10, sock_read=300)  # 10sec/5min


class LanOnlyResolver(AbstractResolver):
    """DNS decision in the connector actually making the media connection."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        answers = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        if not answers or any(not _lan_address(answer[4][0]) for answer in answers):
            raise OSError("media_destination_blocked")
        return [
            {
                "hostname": host, "host": answer[4][0], "port": port,
                "family": answer[0], "proto": answer[2], "flags": 0,
            }
            for answer in answers
        ]

    async def close(self):
        pass


async def get_content_type(context: StreamContext, entry_id: str, url: str) -> str | None:
    """HEAD/GET probe shares the stream's current policy and LAN-only connector."""
    validate_media_url(context, entry_id, url)
    implementation = context.implementation
    if implementation is None:
        raise ValueError("stream_unavailable")
    try:
        return await implementation.probe_content_type(entry_id, url)
    except (ClientError, OSError, asyncio.TimeoutError) as error:
        _LOGGER.debug("Media content type unavailable: %s", type(error).__name__)
        return None


_BLOCK_REASONS = frozenset({
    "entry_unavailable", "media_destination_blocked", "local_origin_unavailable",
    "media_url_unavailable", "media_extension_blocked", "invalid_media_token",
    "media_redirect_blocked", "media_segment_blocked",
})


def _safe_block_reason(error: ValueError) -> str:
    reason = error.args[0] if error.args else None
    return reason if isinstance(reason, str) and reason in _BLOCK_REASONS else "media_destination_blocked"

_HLS_URI = re.compile(r'URI="([^"]+)"')
_REDIRECTS = frozenset((301, 302, 303, 307, 308))


class StreamImplementation:

    def __init__(self, context: StreamContext):
        self.context = context
        self.session: ClientSession | None = None
        self._lan_session: ClientSession | None = None

    async def async_close(self):
        session, self._lan_session = self._lan_session, None
        if session is not None:
            await session.close()

    def _session(self, offline: bool) -> ClientSession:
        if not offline:
            if self.session is None:
                self.session = async_get_clientsession(self.context.hass)
            return self.session
        if self._lan_session is None:
            self._lan_session = ClientSession(
                connector=TCPConnector(resolver=LanOnlyResolver(), use_dns_cache=False),
                cookie_jar=DummyCookieJar(),
                trust_env=False,
            )
        return self._lan_session

    def _decode(self, token: str, ext: str):
        if ext not in MIME_TYPES:
            raise ValueError("media_extension_blocked")
        try:
            data = jwt.decode(token, self.context.key, algorithms=["HS256"])
        except jwt.InvalidTokenError as error:
            raise ValueError("invalid_media_token") from error
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("entry_id"), str)
            or not isinstance(data.get("url"), str)
            or type(data.get("lan_only")) is not bool
            or "exp" not in data
        ):
            raise ValueError("invalid_media_token")
        entry_id = data["entry_id"]
        offline = is_offline(self.context, entry_id) or data["lan_only"]
        _destination(self.context, entry_id, data["url"], offline)
        url = (
            async_process_play_media_url(self.context.hass, data["url"])
            if data["url"].startswith("/")
            else data["url"]
        )
        url = _destination(self.context, entry_id, url, offline)
        return entry_id, url, offline, data["exp"]

    async def _request(self, method: str, entry_id: str, url: str,
                       offline: bool, headers: dict | None = None):
        session = self._session(offline)
        for attempt in range(6):
            url = _destination(self.context, entry_id, url, offline)
            # An online-issued token cannot bypass a later offline toggle.
            if is_offline(self.context, entry_id) and not offline:
                offline = True
                session = self._session(True)
                url = _destination(self.context, entry_id, url, True)
            response = await session.request(
                method, url, headers=headers, timeout=STREAM_TIMEOUT, allow_redirects=False
            )
            if response.status not in _REDIRECTS:
                return response, url
            location = response.headers.get(hdrs.LOCATION)
            response.release()
            if not location or attempt == 5:
                raise ValueError("media_redirect_blocked")
            url = _destination(self.context, entry_id, urljoin(url, location), offline)
        raise ValueError("media_redirect_blocked")

    async def probe_content_type(self, entry_id: str, url: str) -> str | None:
        offline = is_offline(self.context, entry_id)
        response, _ = await self._request("HEAD", entry_id, url, offline)
        async with response:
            content_type = response.content_type
        if content_type.startswith("text/html"):
            # Icecast may return text/html for HEAD but audio for GET.
            response, _ = await self._request("GET", entry_id, url, offline)
            async with response:
                return CONTENT_TYPES.get(response.content_type)
        return CONTENT_TYPES.get(content_type)

    def _hls_body(self, body: str, base_url: str, entry_id: str,
                  offline: bool, expires_at: int) -> str:
        def child(value: str) -> str:
            url = _destination(self.context, entry_id, urljoin(base_url, value), offline)
            ext = get_ext(url)
            if ext not in MIME_TYPES:
                raise ValueError("media_segment_blocked")
            return _sign(self.context, entry_id, url, ext, expires_at, offline)

        lines = []
        for line in body.splitlines():
            item = line.strip()
            if not item:
                lines.append(line)
            elif item.startswith("#"):
                lines.append(_HLS_URI.sub(lambda match: f'URI="{child(match[1])}"', line))
            else:
                lines.append(child(item))
        return "\n".join(lines)

    async def head(self, request: web.Request, token: str, ext: str):
        try:
            entry_id, url, offline, _ = self._decode(token, ext)
            response, _ = await self._request(
                "HEAD", entry_id, url, offline,
                copy_headers(request.headers, REQUEST_HEADERS)
            )
            async with response:
                headers = copy_headers(response.headers, RESPONSE_HEADERS)
                headers[hdrs.CONTENT_TYPE] = MIME_TYPES[ext]
                return web.Response(status=response.status, headers=headers)
        except ValueError as error:
            _LOGGER.debug("Media HEAD blocked: %s", _safe_block_reason(error))
            return web.Response(status=403)
        except (ClientError, OSError, asyncio.TimeoutError) as error:
            _LOGGER.debug("Media HEAD unavailable: %s", type(error).__name__)
            return web.Response(status=502)

    async def get(self, request: web.Request, token: str, ext: str):
        try:
            entry_id, url, offline, expires_at = self._decode(token, ext)
            response, effective_url = await self._request(
                "GET", entry_id, url, offline,
                copy_headers(request.headers, REQUEST_HEADERS)
            )
            async with response:
                if ext == "m3u8":
                    body = self._hls_body(
                        await response.text(), effective_url, entry_id, offline, expires_at
                    )
                    return web.Response(
                        status=response.status, text=body,
                        headers={
                            hdrs.ACCESS_CONTROL_ALLOW_HEADERS: "*",
                            hdrs.ACCESS_CONTROL_ALLOW_ORIGIN: "*",
                            hdrs.CONTENT_TYPE: MIME_TYPES[ext],
                        },
                    )
                headers = copy_headers(response.headers, RESPONSE_HEADERS)
                headers[hdrs.CONTENT_TYPE] = MIME_TYPES[ext]
                if ext == "ts":
                    headers[hdrs.ACCESS_CONTROL_ALLOW_HEADERS] = "*"
                    headers[hdrs.ACCESS_CONTROL_ALLOW_ORIGIN] = "*"
                downstream = web.StreamResponse(status=response.status, headers=headers)
                downstream.force_close()
                await downstream.prepare(request)
                try:
                    while chunk := await response.content.readany():
                        await downstream.write(chunk)
                except (ClientError, OSError, asyncio.TimeoutError) as error:
                    _LOGGER.debug("Media stream interrupted: %s", type(error).__name__)
                return downstream
        except ValueError as error:
            _LOGGER.debug("Media GET blocked: %s", _safe_block_reason(error))
            return web.Response(status=403)
        except (ClientError, OSError, asyncio.TimeoutError) as error:
            _LOGGER.debug("Media GET unavailable: %s", type(error).__name__)
            return web.Response(status=502)
