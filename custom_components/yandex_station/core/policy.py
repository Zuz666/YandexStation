"""Per-entry session policy and trust boundaries (RFC v2 step 2).

Every network caller must pass an explicit role string. Trust is
role-scoped: credential-bearing requests may only target built-in Yandex
origins plus their documented yandex.ru/yandex.com counterpart at the
default port. Configured external overrides are permitted for anonymous
requests only.
"""

from __future__ import annotations

import ipaddress
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        def __str__(self) -> str:
            return self.value


class AuthState(StrEnum):
    UNKNOWN = "unknown"
    VALID = "valid"
    REAUTH_REQUIRED = "reauth_required"


class CsrfState(StrEnum):
    UNKNOWN = "unknown"
    CANDIDATE = "candidate"
    UNAVAILABLE = "unavailable"


class DevicesState(StrEnum):
    UNKNOWN = "unknown"
    READY = "ready"
    UNAVAILABLE = "unavailable"


class ScenariosState(StrEnum):
    DISABLED = "disabled"
    UNKNOWN = "unknown"
    READY = "ready"
    UNAVAILABLE = "unavailable"


class TokenState(StrEnum):
    MISSING = "missing"
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class CloudState(StrEnum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    DISABLED = "disabled"
    REAUTH_REQUIRED = "reauth_required"


class AuthOutcome(StrEnum):
    """Result of cookie refresh and x_token exchange, never a bool."""

    AUTHENTICATED = "authenticated"
    CREDENTIAL_REJECTED = "credential_rejected"
    UNAVAILABLE = "unavailable"


class SendOutcome(StrEnum):
    """Result of scenario text PUT + action POST pairs (Quasar send)."""

    SENT = "sent"
    UNAVAILABLE = "unavailable"
    UNKNOWN_OUTCOME = "unknown_outcome"


DEFAULT_QUASAR_BASE_URL = "https://iot.quasar.yandex.ru"
DEFAULT_ACCOUNT_BASE_URL = "https://quasar.yandex.ru"
DEFAULT_PASSPORT_BASE_URL = "https://passport.yandex.ru"
DEFAULT_MOBILEPROXY_BASE_URL = "https://mobileproxy.passport.yandex.net"
DEFAULT_GLAGOL_BASE_URL = "https://quasar.yandex.net/glagol"
DEFAULT_RPC_BASE_URL = "https://rpc.alice.yandex.ru"

#: Authoritative role -> (config field, default base URL) table; endpoint
#: forms, configuration validation and role trust all derive from it.
ROLE_BASE_FIELDS = {
    "quasar": ("quasar_base_url", DEFAULT_QUASAR_BASE_URL),
    "account": ("account_base_url", DEFAULT_ACCOUNT_BASE_URL),
    "passport": ("passport_base_url", DEFAULT_PASSPORT_BASE_URL),
    "mobileproxy": ("mobileproxy_base_url", DEFAULT_MOBILEPROXY_BASE_URL),
    "glagol_token": ("glagol_base_url", DEFAULT_GLAGOL_BASE_URL),
    "rpc": ("rpc_base_url", DEFAULT_RPC_BASE_URL),
}

#: Explicit request roles; no role is ever inferred from a hostname.
SESSION_ROLES = frozenset(ROLE_BASE_FIELDS) | {
    "cookie_probe", "csrf", "music", "cloud_ws", "local_ws",
}

COOKIE_STORAGE_VALIDATORS = ("status_ok", "uid_in_storage")

DEFAULT_COOKIE_STORAGE_ENDPOINTS = [
    {"url": "https://quasar.yandex.ru/get_account_config", "validator": "status_ok"},
    {"url": "https://yandex.ru/iot?storage=1", "validator": "uid_in_storage"},
]

#: Legacy string endpoints are migrated only for these two exact forms.
_LEGACY_COOKIE_ENDPOINT_VALIDATORS = {
    "https://quasar.yandex.ru/get_account_config": "status_ok",
    "https://yandex.ru/iot?storage=1": "uid_in_storage",
}

DEFAULT_CSRF_ENDPOINTS = [
    "https://yandex.ru/iot",
    "https://yandex.ru/quasar",
]

#: Music/CDN hosts that may receive OAuth credentials.
_MUSIC_CREDENTIAL_HOSTS = frozenset(
    {"api.music.yandex.net", "oauth.mobile.yandex.net"}
)

_CLOUD_WS_PUSH_HOST = "push.yandex.ru"

#: The legacy ``domain`` option accepts only the two documented mirrors.
_DOCUMENTED_DOMAINS = frozenset({"yandex.ru", "yandex.com"})

#: Default ports used for exact scheme+host+port origin equality.
_SCHEME_DEFAULT_PORTS = {"https": 443, "wss": 443, "ws": 80, "http": 80}

#: Deterministic LAN ranges (no hostname trust); stream media shares them.
LAN_V4_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
)
LAN_V6_NETWORKS = (
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

_RECOVERY_LADDER = (30.0, 60.0, 120.0, 300.0)
_MAX_RECOVERY_DELAY = 300.0
_RETRY_AFTER_SECONDS_RE = re.compile(r"^[0-9]+$")


def _now() -> float:
    """Wall clock used to interpret HTTP-date Retry-After values."""
    return datetime.now(timezone.utc).timestamp()


def _url_parts(url: Any):
    """Parse url, returning None for anything unparseable or port-invalid."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - access validates port syntax and range
    except ValueError:
        return None
    return parts


def _effective_port(parts) -> int | None:
    """Explicit port or the scheme default; None for unknown schemes."""
    if parts.port is not None:
        return parts.port
    return _SCHEME_DEFAULT_PORTS.get((parts.scheme or "").lower())


def _origin_of(url: str) -> tuple[str, str, int] | None:
    """Exact origin (scheme, hostname, effective port) or None."""
    parts = _url_parts(url)
    if parts is None:
        return None
    scheme = (parts.scheme or "").lower()
    host = parts.hostname or ""
    port = _effective_port(parts)
    if not host or port is None:
        return None
    return (scheme, host, port)


def _format_netloc(host: str, port: int | None) -> str:
    """Serialize netloc with RFC 3986 brackets for IPv6 literals."""
    host_text = f"[{host}]" if ":" in host else host
    return f"{host_text}:{port}" if port is not None else host_text


def _variant_origins(url: str, *, scheme: str | None = None) -> set:
    """The URL's origin plus its documented yandex.ru/.com counterpart."""
    origin = _origin_of(url)
    if origin is None:
        return set()
    use_scheme = scheme or origin[0]
    return {(use_scheme, host, origin[2]) for host in _domain_variants(origin[1])}


def _domain_variants(host: str) -> set[str]:
    """The host plus its documented yandex.ru/yandex.com counterpart."""
    for base, alt in (("yandex.ru", "yandex.com"), ("yandex.com", "yandex.ru")):
        if host == base:
            return {host, alt}
        if host.endswith("." + base):
            return {host, f"{host[: -(len(base) + 1)]}.{alt}"}
    return {host}


def _is_lan_host(host: str) -> bool:
    """Literal loopback/RFC1918/ULA/link-local addresses only.

    Hostnames (including localhost) are never trusted; membership is
    checked against explicit networks so behavior is identical on every
    Python version.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    networks = LAN_V4_NETWORKS if addr.version == 4 else LAN_V6_NETWORKS
    return any(addr in network for network in networks)


def _check_https_endpoint(
    url: str,
    *,
    allowed_queries: tuple[str, ...] = (),
    require_path: bool = False,
) -> None:
    """Config-time endpoint validation with a safe error reason."""
    parts = _url_parts(url)
    if parts is None:
        raise ValueError("invalid_endpoint")
    if (parts.scheme or "").lower() != "https":
        raise ValueError("invalid_endpoint")
    if parts.username is not None or parts.password is not None:
        raise ValueError("invalid_endpoint")
    if parts.fragment:
        raise ValueError("invalid_endpoint")
    if parts.query and parts.query not in allowed_queries:
        raise ValueError("invalid_endpoint")
    if not parts.hostname:
        raise ValueError("invalid_endpoint")
    if require_path and parts.path in ("", "/"):
        raise ValueError("invalid_endpoint")


def _normalize_cookie_endpoints(endpoints: Any) -> list[dict[str, str]]:
    if endpoints is None:
        return [dict(endpoint) for endpoint in DEFAULT_COOKIE_STORAGE_ENDPOINTS]
    if isinstance(endpoints, str):
        endpoints = [endpoints]
    if not isinstance(endpoints, list):
        raise ValueError("invalid_endpoint")
    normalized: list[dict[str, str]] = []
    for endpoint in endpoints:
        if isinstance(endpoint, str):
            validator = _LEGACY_COOKIE_ENDPOINT_VALIDATORS.get(endpoint)
            if validator is None:
                raise ValueError("invalid_endpoint")
            normalized.append({"url": endpoint, "validator": validator})
            continue
        if not isinstance(endpoint, dict) or set(endpoint) != {"url", "validator"}:
            raise ValueError("invalid_endpoint")
        url = endpoint["url"]
        validator = endpoint["validator"]
        if not isinstance(url, str) or validator not in COOKIE_STORAGE_VALIDATORS:
            raise ValueError("invalid_endpoint")
        normalized.append({"url": url, "validator": validator})
    return normalized


def _normalize_csrf_endpoints(endpoints: Any) -> list[str]:
    if endpoints is None:
        return list(DEFAULT_CSRF_ENDPOINTS)
    if isinstance(endpoints, str):
        endpoints = [endpoints]
    if not isinstance(endpoints, list) or not all(
        isinstance(endpoint, str) and endpoint for endpoint in endpoints
    ):
        raise ValueError("invalid_endpoint")
    return list(endpoints)


def _swap_base_prefix(url: str, default_base: str, custom_base: str) -> str:
    """Origin+path-boundary replacement of a default base with a configured one.

    Hostname and port must match the default base exactly, so lookalike
    suffix hosts and odd ports are never rewritten.
    """
    parts = _url_parts(url)
    base_parts = _url_parts(default_base)
    if parts is None or base_parts is None:
        return url
    if (parts.scheme or "").lower() != (base_parts.scheme or "").lower():
        return url
    if parts.hostname != base_parts.hostname:
        return url
    if _effective_port(parts) != _effective_port(base_parts):
        return url
    base_path = base_parts.path.rstrip("/")
    path = parts.path or "/"
    if base_path:
        if path == base_path:
            rest = "/"
        elif path.startswith(base_path + "/"):
            rest = path[len(base_path):]
        else:
            return url
    else:
        rest = path
    custom_parts = _url_parts(custom_base)
    if custom_parts is None or not custom_parts.hostname:
        return url
    custom_path = custom_parts.path.rstrip("/")
    if custom_path and (path == custom_path or path.startswith(custom_path + "/")):
        # Skip only when the URL is already on the configured origin
        # (scheme, hostname, effective port); a same-path prefix on the
        # default host must still be rewritten to the configured base.
        if (
            (parts.scheme or "").lower(),
            parts.hostname,
            _effective_port(parts),
        ) == (
            (custom_parts.scheme or "").lower(),
            custom_parts.hostname,
            _effective_port(custom_parts),
        ):
            return url
    new_path = custom_path + rest if custom_path else rest
    netloc = _format_netloc(custom_parts.hostname, custom_parts.port)
    return urlunsplit(
        (parts.scheme.lower(), netloc, new_path, parts.query, parts.fragment)
    )


def _apply_domain_suffix(url: str, domain: str) -> str:
    """Rewrite exact yandex.ru/yandex.com host suffixes to the domain option."""
    parts = _url_parts(url)
    if parts is None:
        return url
    host = parts.hostname
    if not host:
        return url
    new_host = None
    for base in ("yandex.ru", "yandex.com"):
        if host == base:
            new_host = domain
            break
        if host.endswith("." + base):
            new_host = host[: -(len(base) + 1)] + "." + domain
            break
    if new_host is None:
        return url
    netloc = _format_netloc(new_host, parts.port)
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def retry_after_seconds(value: Any) -> float | None:
    """Parse Retry-After seconds or HTTP-date into a relative lower bound."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if _RETRY_AFTER_SECONDS_RE.match(value):
        seconds = float(value)
        return seconds if isfinite(seconds) else None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp() - _now()
    except (OSError, OverflowError, ValueError):
        return None


def _config_value(
    entry_options: dict[str, Any],
    yaml_config: dict[str, Any],
    key: str,
    default: Any = None,
) -> Any:
    """Entry options override YAML; only None/empty-string count as absent."""
    for source in (entry_options, yaml_config):
        if key in source and source[key] not in (None, ""):
            return source[key]
    return default


@dataclass
class EntryPolicy:
    enable_scenarios: bool = True
    use_cached_devices: bool = False
    force_offline_local: bool = False
    cloud_updates: bool = True
    local_updates: bool = True
    enable_diagnostic_sensors: bool = False

    @property
    def effective_use_cached_devices(self) -> bool:
        return self.force_offline_local or self.use_cached_devices

    @property
    def effective_cloud_allowed(self) -> bool:
        return not self.force_offline_local

    @property
    def effective_cloud_updates(self) -> bool:
        return self.effective_cloud_allowed and self.cloud_updates

    @classmethod
    def from_configs(
        cls,
        yaml_config: dict[str, Any] | None = None,
        entry_options: dict[str, Any] | None = None,
    ) -> EntryPolicy:
        yaml_config = yaml_config or {}
        entry_options = entry_options or {}

        def get_val(key: str, default: bool) -> bool:
            return bool(_config_value(entry_options, yaml_config, key, default))

        return cls(
            enable_scenarios=get_val("enable_scenarios", True),
            use_cached_devices=get_val("use_cached_devices", False),
            force_offline_local=get_val("force_offline_local", False),
            cloud_updates=get_val("cloud_updates", True),
            local_updates=get_val("local_updates", True),
            enable_diagnostic_sensors=get_val("enable_diagnostic_sensors", False),
        )


@dataclass
class SessionConfig:
    """Per-entry network configuration and trust policy.

    Six base URL defaults, normalized cookie storage probes and CSRF HTML
    endpoints. Configuration is validated on construction and after
    ``from_configs``; runtime destinations are checked per request role by
    ``validate_destination``.
    """

    domain: str | None = None
    proxy: str | None = None
    ssl: bool | None = None

    quasar_base_url: str | None = None
    account_base_url: str | None = None
    passport_base_url: str | None = None
    mobileproxy_base_url: str | None = None
    glagol_base_url: str | None = None
    rpc_base_url: str | None = None

    cookie_storage_endpoints: list[dict[str, str]] | None = None
    csrf_endpoints: list[str] | None = None
    _explicit_probe_roles: frozenset[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.domain is not None:
            self.domain = str(self.domain).strip().lower() or None
        # Resolve domain defaults once; explicit values (even equal defaults) win.
        for attr, default in ROLE_BASE_FIELDS.values():
            if getattr(self, attr) is None:
                setattr(self, attr, _apply_domain_suffix(default, self.domain)
                        if self.domain else default)
        self._explicit_probe_roles = frozenset(
            role for role, endpoints in (
                ("cookie_probe", self.cookie_storage_endpoints),
                ("csrf", self.csrf_endpoints),
            ) if endpoints is not None
        )
        self.cookie_storage_endpoints = _normalize_cookie_endpoints(
            self.cookie_storage_endpoints
        )
        self.csrf_endpoints = _normalize_csrf_endpoints(self.csrf_endpoints)
        self.validate()

    @classmethod
    def from_configs(
        cls,
        yaml_config: dict[str, Any] | None = None,
        entry_options: dict[str, Any] | None = None,
    ) -> SessionConfig:
        yaml_config = yaml_config or {}
        entry_options = entry_options or {}

        bases = {
            attr: _config_value(entry_options, yaml_config, attr)
            for attr, _default in ROLE_BASE_FIELDS.values()
        }

        return cls(
            domain=_config_value(entry_options, yaml_config, "domain"),
            proxy=_config_value(entry_options, yaml_config, "proxy"),
            ssl=_config_value(entry_options, yaml_config, "ssl"),
            cookie_storage_endpoints=_config_value(
                entry_options, yaml_config, "cookie_storage_endpoints"
            ),
            csrf_endpoints=_config_value(entry_options, yaml_config, "csrf_endpoints"),
            **bases,
        )

    def validate(self) -> None:
        """Validate configuration with safe ValueError reasons."""
        if self.domain is not None and self.domain not in _DOCUMENTED_DOMAINS:
            raise ValueError("invalid_endpoint")
        for attr, _default in ROLE_BASE_FIELDS.values():
            _check_https_endpoint(getattr(self, attr))
        if not self.cookie_storage_endpoints:
            raise ValueError("invalid_endpoint")
        for endpoint in self.cookie_storage_endpoints:
            _check_https_endpoint(
                endpoint["url"], allowed_queries=("storage=1",), require_path=True
            )
        if not self.csrf_endpoints:
            raise ValueError("invalid_endpoint")
        for endpoint in self.csrf_endpoints:
            _check_https_endpoint(endpoint, require_path=True)

    def resolve_url(self, url: str, role: str) -> str:
        """Resolve default origins through domain defaults, then explicit overrides.

        Full explicit probe lists take precedence over base settings. Repeated
        resolution preserves prefixes. Destination trust is checked separately.
        """
        if role not in SESSION_ROLES:
            raise ValueError("invalid_endpoint")
        if not url:
            return url

        if role in self._explicit_probe_roles or role in ("music", "local_ws"):
            return url
        bases = (
            (ROLE_BASE_FIELDS[role],) if role in ROLE_BASE_FIELDS
            else ROLE_BASE_FIELDS.values() if role in ("cookie_probe", "csrf")
            else ()
        )
        for attr, default in bases:
            for source in (default, _apply_domain_suffix(default, "yandex.com")):
                if _origin_of(url) == _origin_of(source):
                    return _swap_base_prefix(url, source, getattr(self, attr))
        if self.domain and role in ("cookie_probe", "csrf", "cloud_ws"):
            return _apply_domain_suffix(url, self.domain)
        return url

    def validate_destination(
        self, url: str, role: str, *, credential: bool = False
    ) -> None:
        """Validate a resolved runtime URL for role before transport.

        Origins compare scheme, hostname and effective port exactly.
        Raises ValueError with reason 'invalid_endpoint' for malformed or
        role-foreign URLs and 'untrusted_credential_destination' when a
        credential-bearing request would leave the role's built-in default
        origins (documented yandex.ru/.com counterparts at the default
        port); external overrides, other ports and local websockets never
        receive credentials. The explicit ssl=False option remains supported.
        """
        if role not in SESSION_ROLES:
            raise ValueError("invalid_endpoint")
        parts = _url_parts(url)
        if parts is None:
            raise ValueError("invalid_endpoint")
        scheme = (parts.scheme or "").lower()
        host = parts.hostname or ""
        if (
            parts.username is not None
            or parts.password is not None
            or parts.fragment
            or not host
        ):
            raise ValueError("invalid_endpoint")

        if role == "local_ws":
            # Glagol local sockets are TLS websockets on LAN literals;
            # hostnames (including localhost) are never trusted and cloud
            # credentials never ride a local socket.
            if scheme != "wss":
                raise ValueError("invalid_endpoint")
            if not _is_lan_host(host):
                raise ValueError("invalid_endpoint")
            if credential:
                raise ValueError("untrusted_credential_destination")
            return

        origin = _origin_of(url)
        if origin is None:
            raise ValueError("invalid_endpoint")

        if role == "cloud_ws":
            if scheme != "wss":
                raise ValueError("invalid_endpoint")
            trusted = _variant_origins(f"wss://{_CLOUD_WS_PUSH_HOST}")
            trusted |= _variant_origins(DEFAULT_QUASAR_BASE_URL, scheme="wss")
            if not credential:
                configured = _origin_of(self.quasar_base_url)
                if configured:
                    trusted.add(("wss", configured[1], configured[2]))
        elif role == "music":
            # anonymous Music/CDN downloads may go anywhere over https;
            # OAuth credentials only to the built-in music hosts
            if scheme != "https":
                raise ValueError("invalid_endpoint")
            if credential and origin not in {
                ("https", host, 443) for host in _MUSIC_CREDENTIAL_HOSTS
            }:
                raise ValueError("untrusted_credential_destination")
            return
        else:
            if scheme != "https":
                raise ValueError("invalid_endpoint")
            if role in ROLE_BASE_FIELDS:
                attr, default = ROLE_BASE_FIELDS[role]
                trusted = _variant_origins(default)
                if not credential:
                    # credential requests never follow external overrides
                    trusted |= _variant_origins(getattr(self, attr))
            else:
                # cookie_probe / csrf: defaults always, configured only
                # for credential-free probes
                if role == "cookie_probe":
                    urls = [e["url"] for e in DEFAULT_COOKIE_STORAGE_ENDPOINTS]
                    if not credential:
                        urls += [e["url"] for e in self.cookie_storage_endpoints]
                else:
                    urls = list(DEFAULT_CSRF_ENDPOINTS)
                    if not credential:
                        urls += self.csrf_endpoints
                trusted = set()
                for endpoint_url in urls:
                    trusted |= _variant_origins(endpoint_url)

        if origin not in trusted:
            raise ValueError(
                "untrusted_credential_destination"
                if credential
                else "invalid_endpoint"
            )


class RecoveryBackoff:
    """Shared recovery backoff for cloud/scenario/token workers.

    Ladder 30/60/120/300 seconds with +/-20% jitter capped at 300. A parsed
    Retry-After header (delta-seconds or HTTP-date) is a lower bound and may
    exceed the cap. reset() restarts the ladder after a successful group
    recovery; instances are cheap and never shared implicitly.
    """

    def __init__(self) -> None:
        self._attempt = 0

    def next_delay(self, retry_after: str | None = None) -> float:
        index = min(self._attempt, len(_RECOVERY_LADDER) - 1)
        self._attempt += 1
        delay = _RECOVERY_LADDER[index] * random.uniform(0.8, 1.2)
        delay = min(delay, _MAX_RECOVERY_DELAY)
        lower_bound = retry_after_seconds(retry_after)
        if lower_bound is not None and lower_bound > delay:
            return lower_bound
        return delay

    def reset(self) -> None:
        self._attempt = 0


@dataclass
class Readiness:
    auth_state: AuthState = AuthState.UNKNOWN
    csrf_state: CsrfState = CsrfState.UNKNOWN
    devices_state: DevicesState = DevicesState.UNKNOWN
    inventory_source: str = "unknown"
    last_snapshot_at: str | None = None
    scenarios_state: ScenariosState = ScenariosState.UNKNOWN

    def computed_cloud_state(self, policy: EntryPolicy) -> CloudState:
        if not policy.effective_cloud_allowed:
            return CloudState.OFFLINE
        if self.auth_state == AuthState.REAUTH_REQUIRED:
            return CloudState.REAUTH_REQUIRED
        if (
            self.auth_state == AuthState.UNKNOWN
            or self.devices_state == DevicesState.UNKNOWN
        ):
            return CloudState.UNKNOWN
        if (
            self.devices_state == DevicesState.UNAVAILABLE
            or self.auth_state != AuthState.VALID
        ):
            return CloudState.DEGRADED
        if policy.enable_scenarios and self.scenarios_state == ScenariosState.UNAVAILABLE:
            return CloudState.DEGRADED
        return CloudState.ONLINE
