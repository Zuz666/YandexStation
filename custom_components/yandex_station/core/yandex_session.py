import asyncio
import base64
import json
import logging
import pickle
import re
import time
from enum import Enum
from typing import Awaitable
from urllib.parse import urljoin, urlsplit

from aiohttp import (
    ClientResponse, ClientResponseError, ClientSession, ClientTimeout,
    TraceConfig, WSServerHandshakeError,
)
from yarl import URL

from .policy import (
    AuthOutcome,
    AuthState,
    CsrfState,
    Readiness,
    RecoveryBackoff,
    SessionConfig,
    retry_after_seconds,
)

_LOGGER = logging.getLogger(__name__)

# Cloud roles: blocked before transport while the offline policy is active or
# before the cached publication admission opens. local_ws stays on the LAN.
CLOUD_ROLES = frozenset(
    {
        "quasar",
        "account",
        "passport",
        "mobileproxy",
        "glagol_token",
        "rpc",
        "cookie_probe",
        "csrf",
        "music",
        "cloud_ws",
    }
)

# Roles whose requests carry account credentials (cookie session or explicit
# OAuth headers). External base overrides are never trusted for them.
CREDENTIAL_ROLES = CLOUD_ROLES - {"music"}

# Headers that make any request credential-bearing regardless of its role.
CREDENTIAL_HEADERS = frozenset(
    {"authorization", "ya-consumer-authorization", "ya-client-cookie", "cookie"}
)

# Mutating quasar methods require a CSRF candidate before dispatch.
CSRF_METHODS = frozenset({"post", "put", "patch", "delete"})

# Refresh the opaque CSRF candidate before the primary client's 24h window.
CSRF_FRESH_SECS = 24 * 3600 - 5 * 60

# Bound of the validated redirect chain for the cookie/OAuth exchange.
MAX_AUTH_REDIRECTS = 5

# aiohttp 3.8 rounds timeouts >=5s up to the next second; stay below that threshold.
ATTEMPT_TIMEOUT = 4.9

OAUTH_TOKEN_INVALID = "oauth_token.invalid"


def oauth_token_rejected(resp) -> bool:
    """Exact ``status=error`` with an ``errors`` list containing ``oauth_token.invalid``.

    Everything else (HTML challenge, 5xx proxy answer, substring matches on
    ``invalid``) is never a confirmed token rejection.
    """
    return (
        isinstance(resp, dict)
        and resp.get("status") == "error"
        and isinstance(resp.get("errors"), list)
        and OAUTH_TOKEN_INVALID in resp["errors"]
    )


class CloudRequestError(Exception):
    """HTTP-level cloud failure.

    Carries only the request role and the HTTP status (both safe to log in
    full); never URLs, headers or response bodies.
    """

    def __init__(self, role: str, status: int):
        super().__init__(f"{role}: HTTP {status}")
        self.role = role
        self.status = status


async def _reject_automatic_redirect(session, context, params):
    # aiohttp 3.8 ws_connect has no allow_redirects argument; guard before its next hop.
    params.response.release()
    raise CloudRequestError("redirect", params.response.status)


def safe_error_summary(error: Exception) -> str:
    """Keep actionable categories/status, never an arbitrary exception payload."""
    if isinstance(error, CloudRequestError):
        return str(error)
    name = type(error).__name__
    if isinstance(error, ClientResponseError):
        return f"{name}: HTTP {error.status}"
    if isinstance(error, json.JSONDecodeError):
        return f"{name}: line={error.lineno} column={error.colno}"
    if isinstance(error, OSError) and error.errno is not None:
        return f"{name}: errno={error.errno}"
    if isinstance(error, (ValueError, RuntimeError)) and str(error) in {
        "invalid_endpoint", "untrusted_credential_destination",
        "cloud_blocked_by_policy", "cloud_not_published", "cloud_updates_disabled",
        "cloud_ws_credentials_not_allowed",
        "local_ws_credentials_not_allowed", "csrf_unavailable",
        "x_token_required", "music_token_unavailable", "inventory_unavailable",
    }:
        return str(error)
    return name


class LoginResponse:
    def __init__(self, resp: dict):
        self.raw = resp

    @property
    def ok(self):
        return self.raw.get("status") == "ok"

    @property
    def errors(self):
        return self.raw.get("errors", [])

    @property
    def error(self):
        return self.raw["errors"][0]

    @property
    def display_login(self):
        return self.raw["display_login"]

    @property
    def x_token(self):
        return self.raw["x_token"]

    @property
    def oauth_token_invalid(self) -> bool:
        """Whether Passport confirmed this exact token rejection."""
        return oauth_token_rejected(self.raw)


class _ProbeResult(Enum):
    """Cookie probe resolution; never inferred from HTTP statuses."""

    AUTHENTICATED = "authenticated"
    ABSENT = "cookies_absent_or_invalid"
    MISMATCH = "account_mismatch"
    UNKNOWN = "unknown"


class BasicSession:
    """Transport with per-instance policy/config; no class-level defaults."""

    def __init__(
        self,
        session: ClientSession,
        config: SessionConfig | None = None,
        policy=None,
        cloud_ready: bool = True,
    ):
        self._session = session
        self.config = config or SessionConfig()
        if self.config.ssl is False:
            _LOGGER.warning(
                "TLS certificate verification is disabled (ssl=false). "
                "A proxy or man-in-the-middle can read or alter cookies, tokens "
                "and commands; use only with a trusted diagnostic proxy."
            )
        self.policy = policy
        self.cloud_ready = cloud_ready
        self.retry_after: dict[str, str] = {}
        if not any(
            _reject_automatic_redirect in trace.on_request_redirect
            for trace in session.trace_configs
        ):
            trace = TraceConfig()
            trace.on_request_redirect.append(_reject_automatic_redirect)
            trace.freeze()
            session.trace_configs.append(trace)

    @staticmethod
    def _has_credentials(kwargs: dict) -> bool:
        headers = kwargs.get("headers") or {}
        return bool(kwargs.get("auth") or kwargs.get("cookies")) or any(
            str(name).lower() in CREDENTIAL_HEADERS for name in headers
        )

    def _check_role_admission(self, role: str) -> None:
        """Reject cloud roles before any network egress."""
        if role not in CLOUD_ROLES:
            return
        policy = self.policy
        if policy is not None and not policy.effective_cloud_allowed:
            raise RuntimeError("cloud_blocked_by_policy")
        if not self.cloud_ready:
            raise RuntimeError("cloud_not_published")
        if role == "cloud_ws" and policy is not None and not policy.effective_cloud_updates:
            raise RuntimeError("cloud_updates_disabled")

    def _record_retry_after(
        self, role: str, headers, *, keep_existing: bool = False
    ) -> None:
        """Track the latest Retry-After hint per role.

        Only the raw hint string is kept as a lower bound for the recovery
        backoff; raw headers never reach exceptions or logs. Inside one
        fallback pass (``keep_existing``) the strongest valid hint survives
        even when another endpoint omits the header.
        """
        value = None
        try:
            value = headers.get("Retry-After") if headers else None
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            value = value.strip()
            if keep_existing and role in self.retry_after:
                previous = retry_after_seconds(self.retry_after[role])
                current = retry_after_seconds(value)
                if previous is not None and (current is None or current <= previous):
                    return
            self.retry_after[role] = value
        elif not keep_existing:
            self.retry_after.pop(role, None)

    def _validate_destination(self, resolved: str, role: str, credential: bool) -> None:
        try:
            self.config.validate_destination(resolved, role, credential=credential)
        except ValueError as err:
            _LOGGER.warning(
                "Request blocked before transport: role=%s reason=%s",
                role, safe_error_summary(err),
            )
            raise

    def _jar_sends_cookies(self, resolved_url: str) -> bool:
        """Whether the jar would attach cookies to this destination.

        Implicitly scoped cookies make the request credential-bearing: a
        configured foreign destination must never receive them.
        """
        jar = getattr(self._session, "cookie_jar", None)
        if jar is None:
            return False
        try:
            return bool(jar.filter_cookies(URL(resolved_url)))
        except Exception:
            # jar cannot be inspected: assume cookies apply
            return True

    def _resolve_destination(self, url: str, role: str, credential: bool) -> str:
        resolved = self.config.resolve_url(url, role)
        self._validate_destination(resolved, role, credential)
        return resolved

    @staticmethod
    def _clamp_attempt_timeout(kwargs: dict) -> None:
        """Clamp every HTTP attempt to the 5 second budget, no bypass."""
        timeout = kwargs.get("timeout", ATTEMPT_TIMEOUT)
        if isinstance(timeout, ClientTimeout):
            total = timeout.total
            if total is None or total <= 0 or total > ATTEMPT_TIMEOUT:
                timeout = ClientTimeout(
                    total=ATTEMPT_TIMEOUT,
                    connect=timeout.connect,
                    sock_connect=timeout.sock_connect,
                    sock_read=timeout.sock_read,
                )
        elif isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            timeout = min(float(timeout), ATTEMPT_TIMEOUT) if timeout > 0 else ATTEMPT_TIMEOUT
        else:
            timeout = ATTEMPT_TIMEOUT
        kwargs["timeout"] = timeout

    def _request(
        self,
        method: str,
        url: str,
        *,
        role: str,
        credential: bool | None = None,
        **kwargs,
    ) -> Awaitable[ClientResponse]:
        """Internal request with per-instance policy and trust enforcement.

        Policy and trust failures happen before any network egress.
        Automatic redirects stay disabled (no caller bypass) and every
        attempt is clamped to the 5 second budget. Cookies that the jar
        would attach to the resolved destination - like explicit
        credential headers - make the request credential-bearing even when
        ``credential=False`` was requested.
        """
        self._check_role_admission(role)
        resolved = self.config.resolve_url(url, role)
        credential_bearing = self._has_credentials(
            kwargs
        ) or self._jar_sends_cookies(resolved)
        if credential is None:
            credential = role in CREDENTIAL_ROLES or credential_bearing
        elif not credential:
            # explicit False cannot bypass credential detection
            credential = credential_bearing
        self._validate_destination(resolved, role, credential)
        kwargs["proxy"] = self.config.proxy
        kwargs["ssl"] = self.config.ssl
        self._clamp_attempt_timeout(kwargs)
        kwargs["allow_redirects"] = False
        return getattr(self._session, method)(resolved, **kwargs)

    def _get(self, url: str, *, role: str, **kwargs) -> Awaitable[ClientResponse]:
        return self._request("get", url, role=role, **kwargs)

    def _post(self, url: str, *, role: str, **kwargs) -> Awaitable[ClientResponse]:
        return self._request("post", url, role=role, **kwargs)

    @property
    def closed(self):
        return self._session.closed

    @property
    def client_session(self):
        return self._session


class YandexSession(BasicSession):
    """Class for login in yandex via username, token, capcha."""

    def __init__(
        self,
        session: ClientSession,
        x_token: str = None,
        music_token: str = None,
        cookie: str = None,
        config: SessionConfig | None = None,
        readiness: Readiness | None = None,
        policy=None,
        account_uid: str | None = None,
        cloud_ready: bool = True,
        **kwargs,
    ):
        """
        :param x_token: optional x-token
        :param music_token: optional token for glagol API
        :param cookie: optional base64 cookie from last session
        """
        super().__init__(
            session, config=config, policy=policy, cloud_ready=cloud_ready
        )
        self.readiness = readiness or Readiness()
        self.account_uid = account_uid

        self.x_token = x_token
        self.music_token = music_token

        self.auth_headers = None
        self.auth_json = None

        # CSRF candidate lifecycle: single-flight fetch with shared backoff
        self.csrf_token = None
        self._csrf_generation = 0
        self._csrf_retry_at = 0.0
        self._csrf_obtained_at = None
        self._csrf_fetch_task: asyncio.Task | None = None
        self._csrf_backoff = RecoveryBackoff()

        self._last_request_ts = 0.0
        self._last_good_probe_url = None
        self._update_listeners = []

        setattr(session.cookie_jar, "_quote_cookie", False)

        if cookie:
            cookie_jar = session.cookie_jar
            # https://github.com/aio-libs/aiohttp/issues/7216
            _cookies = cookie_jar._cookies
            try:
                raw = base64.b64decode(cookie)
                cookie_jar._cookies = pickle.loads(raw)
                # same as CookieJar._do_expiration()
                cookie_jar.clear(lambda x: False)
            except:
                cookie_jar._cookies = _cookies

    def add_update_listener(self, coro):
        """Listeners to handle automatic cookies update."""
        self._update_listeners.append(coro)

    async def async_stop(self) -> None:
        """Cancel and await owned background work (entry teardown).

        The shielded CSRF fetch task must not leak past unload.
        """
        task = self._csrf_fetch_task
        self._csrf_fetch_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def get_qr(self) -> str:
        """Get link to QR-code auth."""
        r = await self._get("https://passport.yandex.ru/pwl-yandex", role="passport")
        assert r.ok, r.status

        resp = await r.text()
        m = re.search(r'__CSRF__ = "([^"]+)', resp)

        self.auth_headers = {"X-CSRF-Token": m[1]}

        r = await self._post(
            "https://passport.yandex.ru/pwl-yandex/api/passport/auth/password/submit",
            role="passport",
            json={"retpath": "https://passport.yandex.ru/"},
            headers=self.auth_headers,
        )
        assert r.ok, r.status
        self.auth_json = await r.json()

        r = await self._post(
            "https://passport.yandex.ru/pwl-yandex/api/passport/auth/magic/code",
            role="passport",
            data={
                "location_id": "0",
                "magic_track_id": self.auth_json["track_id"],
                "track_id": "",
            },
            headers=self.auth_headers,
        )
        assert r.ok, r.status
        resp = await r.json()

        return resp["link"]

    async def login_qr(self) -> LoginResponse:
        """Check if already logged in."""
        r = await self._post(
            "https://passport.yandex.ru/pwl-yandex/api/passport/auth/magic/code/status",
            role="passport",
            json=self.auth_json,
            headers=self.auth_headers,
        )
        assert r.ok, r.status

        resp = await r.json()
        if resp.get("state") != "otp_auth_finished":
            return LoginResponse({})

        r = await self._post(
            "https://passport.yandex.ru/pwl-yandex/api/passport/sessions/get_session",
            role="passport",
            data={"track_id": resp["trackId"]},
            headers=self.auth_headers,
        )
        assert r.ok, r.status

        return await self.login_cookies()

    async def login_cookies(self, cookies: str = None) -> LoginResponse:
        """Support three formats:
        1. Empty - cookies will be loaded from the session
        2. JSON from Copy Cookies (Google Chrome extension)
           https://chrome.google.com/webstore/detail/copy-cookies/jcbpglbplpblnagieibnemmkiamekcdg
        3. Raw cookie string `key1=value1; key2=value2`

        For JSON format support cookies from different Yandex domains.
        """
        host = "passport.yandex.ru"
        if cookies is None:
            cookies = "; ".join(
                [
                    f"{c.key}={c.value}"
                    for c in self._session.cookie_jar
                    if c["domain"].endswith("yandex.ru")
                ]
            )
        elif cookies[0] == "[":
            # @dext0r: fix cookies auth
            raw = json.loads(cookies)
            host = next(p["domain"] for p in raw if p["domain"].startswith(".yandex."))
            cookies = "; ".join([f"{p['name']}={p['value']}" for p in raw])

        r = await self._post(
            "https://mobileproxy.passport.yandex.net/1/bundle/oauth/token_by_sessionid",
            role="mobileproxy",
            data={
                "client_id": "c0ebe342af7d48fbbbfcf2d2eedb8f9e",
                "client_secret": "ad0a908f0aa341a182a37ecd75bc319e",
            },
            headers={"Ya-Client-Host": host, "Ya-Client-Cookie": cookies},
        )
        resp = await r.json()
        x_token = resp["access_token"]

        return await self.validate_token(x_token)

    async def validate_token(self, x_token: str) -> LoginResponse:
        """Return user info using token."""
        r = await self._get(
            "https://mobileproxy.passport.yandex.net/1/bundle/account/short_info/?avatar_size=islands-300",
            role="mobileproxy",
            headers={"Authorization": f"OAuth {x_token}"},
        )
        if r.status >= 500:
            # a 5xx body never proves identity, success or rejection
            return LoginResponse({"x_token": x_token})
        try:
            resp = await r.json()
        except Exception:
            resp = None
        if not isinstance(resp, dict):
            resp = {}
        resp["x_token"] = x_token
        return LoginResponse(resp)

    async def login_token(self, x_token: str) -> AuthOutcome:
        """Exchange x-token for session cookies.

        The exchange follows only validated passport destinations (max 5
        hops) and requires an authenticated cookie probe afterwards; only
        then credentials are persisted. CREDENTIAL_REJECTED is reported
        only for the exact ``oauth_token.invalid`` decoder; a UID mismatch
        after a fresh exchange is an unknown outcome, never a revocation.
        """
        _LOGGER.debug("Login in Yandex with token")

        payload = {"type": "x-token", "retpath": "https://www.yandex.ru"}
        headers = {"Ya-Consumer-Authorization": f"OAuth {x_token}"}
        r = await self._post(
            "https://mobileproxy.passport.yandex.net/1/bundle/auth/x_token/",
            role="mobileproxy",
            data=payload,
            headers=headers,
        )
        if r.status >= 500:
            self.readiness.auth_state = AuthState.UNKNOWN
            return AuthOutcome.UNAVAILABLE
        try:
            resp = await r.json()
        except Exception:
            # an HTML challenge or non-JSON answer is never a rejection
            self.readiness.auth_state = AuthState.UNKNOWN
            return AuthOutcome.UNAVAILABLE
        if not isinstance(resp, dict):
            self.readiness.auth_state = AuthState.UNKNOWN
            return AuthOutcome.UNAVAILABLE

        if resp.get("status") != "ok":
            if oauth_token_rejected(resp):
                self.readiness.auth_state = AuthState.REAUTH_REQUIRED
                return AuthOutcome.CREDENTIAL_REJECTED
            _LOGGER.debug("Login with token error: role=mobileproxy")
            self.readiness.auth_state = AuthState.UNKNOWN
            return AuthOutcome.UNAVAILABLE

        host = resp.get("passport_host")
        track_id = resp.get("track_id")
        if not isinstance(host, str) or not host:
            self.readiness.auth_state = AuthState.UNKNOWN
            return AuthOutcome.UNAVAILABLE
        if not isinstance(track_id, str) or not track_id:
            self.readiness.auth_state = AuthState.UNKNOWN
            return AuthOutcome.UNAVAILABLE

        if not await self._finish_passport_session(host, track_id):
            self.readiness.auth_state = AuthState.UNKNOWN
            return AuthOutcome.UNAVAILABLE

        # exchange done: cookies must be confirmed by an authenticated probe
        probe = await self._probe_cookies()
        if probe is _ProbeResult.AUTHENTICATED:
            self.x_token = x_token
            self.readiness.auth_state = AuthState.VALID
            await self._handle_update()
            return AuthOutcome.AUTHENTICATED
        # mismatch or outage after the exchange: keep credentials, report
        # an unknown outcome (never an account revocation)
        self.readiness.auth_state = AuthState.UNKNOWN
        return AuthOutcome.UNAVAILABLE

    async def _finish_passport_session(self, host: str, track_id: str) -> bool:
        """Follow the validated passport redirect chain (max 5 hops).

        Every Location is resolved and validated as a passport credential
        destination before anything is followed; success requires the exact
        ``/auth/finish`` path on a validated target, so a foreign host
        offering ``/auth/finish`` never counts. The secret ``track_id`` is
        sent only to the first verified destination.
        """
        url: str | None = f"{host.rstrip('/')}/auth/session/"
        sent_track = False

        for hop in range(MAX_AUTH_REDIRECTS + 1):
            resolved = self.config.resolve_url(url, "passport")
            self._validate_destination(resolved, "passport", credential=True)

            params = {"track_id": track_id} if not sent_track else None
            sent_track = True
            r = await self._get(
                resolved, role="passport", params=params, allow_redirects=False
            )
            location = r.headers.get("Location")
            if not isinstance(location, str) or not location:
                return False

            target = urljoin(resolved, location)
            target_resolved = self.config.resolve_url(target, "passport")
            try:
                self._validate_destination(
                    target_resolved, "passport", credential=True
                )
            except ValueError as err:
                _LOGGER.warning("Passport redirect blocked: reason=%s", safe_error_summary(err))
                return False
            if urlsplit(target_resolved).path == "/auth/finish":
                return True
            if hop == MAX_AUTH_REDIRECTS:
                return False
            url = target

        return False

    async def refresh_cookies(self) -> AuthOutcome:
        """Validate the cookie session across fallback cookie probes.

        Probes are tried last-good first, then in configured order. Only an
        explicit empty/null UID in a proper storage envelope (or no
        applicable cookies on the validated probe destinations) confirms
        absent cookies and allows the x-token exchange; HTTP statuses,
        timeouts, HTML challenges and unknown schemas never do.
        """
        if not self._has_usable_cookie_jar():
            # no applicable cookies locally: absence is known without probes
            return await self._recover_absent_cookies()

        probe = await self._probe_cookies()
        if probe is _ProbeResult.AUTHENTICATED:
            self.readiness.auth_state = AuthState.VALID
            return AuthOutcome.AUTHENTICATED
        if probe in (_ProbeResult.ABSENT, _ProbeResult.MISMATCH):
            # cookies are confirmed unusable for this account; the x-token
            # may restore the correct ones, a mismatch is never a revocation
            return await self._recover_absent_cookies()

        # every probe was inconclusive: never infer rejection or absence
        self.readiness.auth_state = AuthState.UNKNOWN
        return AuthOutcome.UNAVAILABLE

    async def _recover_absent_cookies(self) -> AuthOutcome:
        """Re-exchange the x-token for known-absent/unusable cookies."""
        if self.x_token:
            try:
                return await self.login_token(self.x_token)
            except Exception as e:
                _LOGGER.debug(
                    "Cookie exchange failed: role=mobileproxy reason=%s",
                    safe_error_summary(e),
                )
                self.readiness.auth_state = AuthState.UNKNOWN
                return AuthOutcome.UNAVAILABLE

        # absent cookies without an x-token is a missing credential, not a
        # confirmed rejection: keep stored data and report unavailable
        self.readiness.auth_state = AuthState.UNKNOWN
        return AuthOutcome.UNAVAILABLE

    def _has_usable_cookie_jar(self) -> bool:
        """Whether the jar would send cookies to any validated probe endpoint.

        Local absence is only known from applicable cookies on validated
        probe destinations; an uninspectable jar stays unknown
        (conservative: probes decide).
        """
        jar = getattr(self._session, "cookie_jar", None)
        if jar is None:
            return True
        destinations = []
        for probe in self._ordered_cookie_probes():
            try:
                resolved = self.config.resolve_url(probe["url"], "cookie_probe")
                self.config.validate_destination(
                    resolved, "cookie_probe", credential=True
                )
            except ValueError:
                continue
            destinations.append(resolved)
        if not destinations:
            # no validated probe destination: absence cannot be known here
            return True
        try:
            return any(bool(jar.filter_cookies(URL(url))) for url in destinations)
        except Exception:
            return True

    def _ordered_cookie_probes(self) -> list[dict[str, str]]:
        probes: list[dict[str, str]] = []
        for endpoint in self.config.cookie_storage_endpoints:
            if not isinstance(endpoint, dict):
                continue
            url = endpoint.get("url")
            validator = endpoint.get("validator")
            if not isinstance(url, str) or not url:
                continue
            if validator not in ("status_ok", "uid_in_storage"):
                continue
            probes.append({"url": url, "validator": validator})

        # the last successful probe is tried first, the rest keep order
        if self._last_good_probe_url:
            probes.sort(key=lambda probe: probe["url"] != self._last_good_probe_url)
        return probes

    async def _probe_cookies(self) -> _ProbeResult:
        """Probe all configured cookie storage endpoints."""
        absent = False
        mismatch = False

        for probe in self._ordered_cookie_probes():
            result = await self._probe_cookie_endpoint(
                probe["url"], probe["validator"]
            )
            if result is _ProbeResult.AUTHENTICATED:
                self._last_good_probe_url = probe["url"]
                return result
            if result is _ProbeResult.ABSENT:
                absent = True
            elif result is _ProbeResult.MISMATCH:
                mismatch = True

        if mismatch:
            return _ProbeResult.MISMATCH
        if absent:
            return _ProbeResult.ABSENT
        return _ProbeResult.UNKNOWN

    async def _probe_cookie_endpoint(self, url: str, validator: str) -> _ProbeResult:
        """Interpret one cookie storage probe response."""
        try:
            r = await self._get(url, role="cookie_probe")
            if r.status != 200:
                _LOGGER.debug(
                    "Cookie probe unavailable: validator=%s HTTP=%s", validator, r.status
                )
                r.release()
                return _ProbeResult.UNKNOWN
            try:
                resp = await r.json()
            except Exception as error:
                _LOGGER.debug(
                    "Cookie probe invalid response: validator=%s reason=%s",
                    validator, safe_error_summary(error),
                )
                return _ProbeResult.UNKNOWN
            if not isinstance(resp, dict):
                return _ProbeResult.UNKNOWN

            if validator == "status_ok":
                if resp.get("status") == "ok":
                    return _ProbeResult.AUTHENTICATED
                return _ProbeResult.UNKNOWN

            if validator != "uid_in_storage":
                return _ProbeResult.UNKNOWN
            storage = resp.get("storage")
            if not isinstance(storage, dict):
                return _ProbeResult.UNKNOWN
            user = storage.get("user")
            if not isinstance(user, dict) or "uid" not in user:
                return _ProbeResult.UNKNOWN

            uid = user["uid"]
            if uid is None or (isinstance(uid, str) and not uid.strip()):
                return _ProbeResult.ABSENT
            if isinstance(uid, bool) or not isinstance(uid, (str, int)):
                return _ProbeResult.UNKNOWN
            uid_s = str(uid).strip()
            if not uid_s:
                return _ProbeResult.ABSENT
            if (
                self.account_uid is not None
                and uid_s != str(self.account_uid).strip()
            ):
                return _ProbeResult.MISMATCH
            return _ProbeResult.AUTHENTICATED
        except ValueError as e:
            _LOGGER.debug("Cookie probe blocked: role=cookie_probe reason=%s", safe_error_summary(e))
            return _ProbeResult.UNKNOWN
        except Exception as e:
            _LOGGER.debug(
                "Cookie probe failed: role=cookie_probe reason=%s", safe_error_summary(e)
            )
            return _ProbeResult.UNKNOWN

    async def ensure_csrf(self) -> str | None:
        """Return the current CSRF candidate, fetching one when allowed.

        Single-flight: concurrent callers share one fallback pass and a
        cancelled command never cancels the shared attempt. After a failed
        pass the next one waits for the recovery backoff slot. A candidate
        past its proactive freshness window is dropped before the fetch so
        a failed pass or cooldown never resurrects the stale token.
        """
        task = self._csrf_fetch_task
        if task is not None and not task.done():
            return await self._join_csrf_fetch(task)

        if self.csrf_token is not None:
            obtained = self._csrf_obtained_at
            if obtained is None or time.monotonic() - obtained < CSRF_FRESH_SECS:
                return self.csrf_token
            # proactive window passed: drop the opaque candidate first
            self.csrf_token = None
            self.readiness.csrf_state = CsrfState.UNAVAILABLE

        if time.monotonic() < self._csrf_retry_at:
            # cooldown: no fetch and no queue; commands finish unavailable
            return self.csrf_token

        task = asyncio.create_task(self._csrf_fetch_pass())
        self._csrf_fetch_task = task
        return await self._join_csrf_fetch(task)

    @staticmethod
    async def _join_csrf_fetch(task: asyncio.Task) -> str | None:
        # shield: one command's deadline must not cancel the shared attempt
        return await asyncio.shield(task)

    async def _csrf_fetch_pass(self) -> str | None:
        """One pass over the configured CSRF endpoints (fallback bypass)."""
        self.retry_after.pop("csrf", None)
        for url in self.config.csrf_endpoints:
            if not isinstance(url, str) or not url:
                continue
            try:
                r = await self._get(url, role="csrf")
                if r.status != 200:
                    self._record_retry_after("csrf", r.headers, keep_existing=True)
                    _LOGGER.debug("CSRF endpoint unavailable: role=csrf HTTP=%s", r.status)
                    r.release()
                    continue
                raw = await r.text()
                m = re.search(r'"csrfToken2":"([^"]+)"', raw)
                if m:
                    self._apply_csrf_candidate(m.group(1))
                    return self.csrf_token
            except ValueError as e:
                _LOGGER.debug("CSRF fetch blocked: role=csrf reason=%s", safe_error_summary(e))
            except Exception as e:
                _LOGGER.debug(
                    "CSRF fetch failed: role=csrf reason=%s", safe_error_summary(e)
                )

        self._csrf_retry_at = max(
            self._csrf_retry_at,
            time.monotonic()
            + self._csrf_backoff.next_delay(self.retry_after.get("csrf")),
        )
        self.readiness.csrf_state = CsrfState.UNAVAILABLE
        _LOGGER.debug(
            "CSRF unavailable: retry_in=%.1fs",
            max(0.0, self._csrf_retry_at - time.monotonic()),
        )
        return None

    def _apply_csrf_candidate(self, token: str) -> None:
        """Install a fetched candidate; generation advances per fetch.

        The opaque token stays a candidate (never proof of validity or
        expiry); auth state is not changed by a fetch.
        """
        self.csrf_token = token
        self._csrf_generation += 1
        self._csrf_obtained_at = time.monotonic()
        self.readiness.csrf_state = CsrfState.CANDIDATE
        self.retry_after.pop("csrf", None)

    def _csrf_invalidate_current(
        self, sent_token: str, sent_generation: int, retry_after: str | None = None
    ) -> None:
        """Cautiously drop the unproven candidate after a 403.

        Only the still-current token+generation pair is invalidated; a late
        response of an older generation (even with the same token string)
        never resets a newer candidate. Auth state, cookies and x_token are
        untouched, no refetch/replay/OAuth is triggered here - just one
        backoff slot (honoring the response Retry-After hint, if any) for
        the next separate operation.
        """
        if self.csrf_token != sent_token or self._csrf_generation != sent_generation:
            return
        self.csrf_token = None
        self.readiness.csrf_state = CsrfState.UNAVAILABLE
        self._csrf_retry_at = max(
            self._csrf_retry_at,
            time.monotonic() + self._csrf_backoff.next_delay(retry_after),
        )

    def _csrf_confirm_current(self, sent_token: str, sent_generation: int) -> None:
        """Only success of a still-live token+generation pair resets the chain."""
        if (
            self.csrf_token is None
            or self.csrf_token != sent_token
            or self._csrf_generation != sent_generation
        ):
            return
        self._csrf_retry_at = 0.0
        self._csrf_backoff.reset()

    async def get_music_token(self, x_token: str) -> str:
        """Get music token using x-token. Usual you should'n call this method."""
        _LOGGER.debug("Get music token")

        payload = {
            # Thanks to https://github.com/MarshalX/yandex-music-api/
            "client_secret": "53bc75238f0c4d08a118e51fe9203300",
            "client_id": "23cabbbdc6cd418abb4b39c32c41195d",
            "grant_type": "x-token",
            "access_token": x_token,
        }
        r = await self._post(
            "https://oauth.mobile.yandex.net/1/token",
            role="music",
            credential=True,
            data=payload,
        )
        if r.status != 200:
            self._record_retry_after("music", r.headers)
            r.release()
            raise CloudRequestError("music", r.status)
        try:
            resp = await r.json()
        except Exception:
            raise RuntimeError("music_token_unavailable") from None
        if not isinstance(resp, dict) or "access_token" not in resp:
            raise RuntimeError("music_token_unavailable")
        return resp["access_token"]

    async def get(self, url: str, *, role: str, **kwargs) -> ClientResponse:
        if role == "glagol_token":
            return await self.request_glagol(url, role=role, **kwargs)
        return await self.request("get", url, role=role, **kwargs)

    async def post(self, url: str, *, role: str, **kwargs) -> ClientResponse:
        return await self.request("post", url, role=role, **kwargs)

    async def put(self, url: str, *, role: str, **kwargs) -> ClientResponse:
        return await self.request("put", url, role=role, **kwargs)

    @staticmethod
    def _check_ws_handshake(role: str, kwargs: dict) -> None:
        """Cloud account credentials stay off websocket headers."""
        credential_header = BasicSession._has_credentials(kwargs)
        if role == "local_ws":
            if credential_header:
                # the conversationToken rides Glagol messages, never headers
                raise RuntimeError("local_ws_credentials_not_allowed")
            return
        if credential_header:
            raise RuntimeError("cloud_ws_credentials_not_allowed")

    async def ws_connect(self, url: str, *, role: str, **kwargs):
        self._check_role_admission(role)
        if role == "local_ws":
            kwargs["proxy"] = None
            kwargs.pop("proxy_auth", None)
        else:
            kwargs.setdefault("proxy", self.config.proxy)
        kwargs.setdefault("ssl", self.config.ssl)
        self._check_ws_handshake(role, kwargs)
        resolved = self._resolve_destination(url, role, credential=role == "cloud_ws")
        try:
            ws = await asyncio.wait_for(
                self._session.ws_connect(resolved, **kwargs), ATTEMPT_TIMEOUT
            )
            self.retry_after.pop(role, None)
            return ws
        except WSServerHandshakeError as error:
            self._record_retry_after(role, error.headers)
            raise CloudRequestError(role, error.status) from None
        except CloudRequestError as error:
            raise CloudRequestError(role, error.status) from None

    async def request(
        self, method: str, url: str, *, role: str, **kwargs
    ) -> ClientResponse:
        """Public request function.

        No automatic retries or replay: fallback endpoints are bypassed by
        the session only inside the CSRF/cookie passes; group retries belong
        to the Quasar workers. Mutating quasar requests require a CSRF
        candidate before dispatch. Auth success is owned by the cookie
        probes and validated inventory, never by a bare 2xx here.
        """
        method = method.lower()

        # DDoS protection for Yandex servers
        while (delay := self._last_request_ts + 0.2 - time.time()) > 0:
            await asyncio.sleep(delay)
        self._last_request_ts = time.time()

        sent_token = None
        sent_generation = None
        if role == "quasar" and method in CSRF_METHODS:
            self._check_role_admission(role)
            self._resolve_destination(url, role, credential=True)
            csrf = await self.ensure_csrf()
            if csrf is None:
                raise RuntimeError("csrf_unavailable")
            sent_token = csrf
            sent_generation = self._csrf_generation
            kwargs.setdefault("headers", {})["x-csrf-token"] = csrf

        r = await self._request(method, url, role=role, **kwargs)
        if 200 <= r.status < 300:
            if sent_generation is not None:
                self._csrf_confirm_current(sent_token, sent_generation)
            self.retry_after.pop(role, None)
            return r

        self._record_retry_after(role, r.headers)
        r.release()

        if r.status == 403 and sent_token is not None:
            self._csrf_invalidate_current(
                sent_token, sent_generation, self.retry_after.get(role)
            )

        raise CloudRequestError(role, r.status)

    async def request_glagol(
        self, url: str, *, role: str, method: str = "get", **kwargs
    ) -> ClientResponse:
        """Authenticated music/glagol API path (explicit OAuth, no replay).

        Role admission and the final destination are validated before any
        music/OAuth request, even when the music token is missing; a
        foreign target never triggers the token fetch. A music refusal is
        scoped to the music token; it never changes account auth or CSRF.
        """
        self._check_role_admission(role)
        resolved = self.config.resolve_url(url, role)
        self._validate_destination(resolved, role, credential=True)

        headers = kwargs.setdefault("headers", {})
        if not self.music_token:
            if not self.x_token:
                # a missing token never authorizes a hidden OAuth request
                raise RuntimeError("x_token_required")
            self.music_token = await self.get_music_token(self.x_token)
            await self._handle_update()

        # OAuth should be capitalize, or music will be 128 bitrate quality
        headers["Authorization"] = f"OAuth {self.music_token}"
        r = await self._request(method, url, role=role, credential=True, **kwargs)
        if r.status == 200:
            self.retry_after.pop(role, None)
            return r
        self._record_retry_after(role, r.headers)
        r.release()
        if r.status in (401, 403):
            self.music_token = None
        raise CloudRequestError(role, r.status)

    @property
    def cookie(self):
        raw = pickle.dumps(
            getattr(self._session.cookie_jar, "_cookies"), pickle.HIGHEST_PROTOCOL
        )
        return base64.b64encode(raw).decode()

    async def _handle_update(self):
        for coro in self._update_listeners:
            await coro(
                x_token=self.x_token, music_token=self.music_token, cookie=self.cookie
            )
