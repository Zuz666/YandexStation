from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit


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


DEFAULT_COOKIE_STORAGE_ENDPOINTS = [
    "https://quasar.yandex.ru/get_account_config",
    "https://yandex.ru/iot?storage=1",
]

DEFAULT_CSRF_ENDPOINTS = [
    "https://yandex.ru/iot",
    "https://yandex.ru/quasar",
]


@dataclass
class EntryPolicy:
    enable_scenarios: bool = True
    use_cached_devices: bool = False
    force_offline_local: bool = False
    cloud_updates: bool = True
    local_updates: bool = True

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
            if key in entry_options and entry_options[key] not in (None, ""):
                return bool(entry_options[key])
            if key in yaml_config and yaml_config[key] not in (None, ""):
                return bool(yaml_config[key])
            return default

        return cls(
            enable_scenarios=get_val("enable_scenarios", True),
            use_cached_devices=get_val("use_cached_devices", False),
            force_offline_local=get_val("force_offline_local", False),
            cloud_updates=get_val("cloud_updates", True),
            local_updates=get_val("local_updates", True),
        )


@dataclass
class SessionConfig:
    domain: str | None = None
    proxy: str | None = None
    ssl: bool | None = None

    quasar_base_url: str = "https://iot.quasar.yandex.ru"
    account_base_url: str = "https://quasar.yandex.ru"
    passport_base_url: str = "https://passport.yandex.ru"
    mobileproxy_base_url: str = "https://mobileproxy.passport.yandex.net"
    glagol_base_url: str = "https://quasar.yandex.net/glagol"
    rpc_base_url: str = "https://rpc.alice.yandex.ru"

    cookie_storage_endpoints: list[str] = field(
        default_factory=lambda: list(DEFAULT_COOKIE_STORAGE_ENDPOINTS)
    )
    csrf_endpoints: list[str] = field(
        default_factory=lambda: list(DEFAULT_CSRF_ENDPOINTS)
    )

    @classmethod
    def from_configs(
        cls,
        yaml_config: dict[str, Any] | None = None,
        entry_options: dict[str, Any] | None = None,
    ) -> SessionConfig:
        yaml_config = yaml_config or {}
        entry_options = entry_options or {}

        def get_opt(key: str, default: Any = None) -> Any:
            if key in entry_options and entry_options[key] not in (None, ""):
                return entry_options[key]
            if key in yaml_config and yaml_config[key] not in (None, ""):
                return yaml_config[key]
            return default

        inst = cls(
            domain=get_opt("domain"),
            proxy=get_opt("proxy"),
            ssl=get_opt("ssl"),
            quasar_base_url=get_opt("quasar_base_url", "https://iot.quasar.yandex.ru"),
            account_base_url=get_opt("account_base_url", "https://quasar.yandex.ru"),
            passport_base_url=get_opt("passport_base_url", "https://passport.yandex.ru"),
            mobileproxy_base_url=get_opt(
                "mobileproxy_base_url", "https://mobileproxy.passport.yandex.net"
            ),
            glagol_base_url=get_opt("glagol_base_url", "https://quasar.yandex.net/glagol"),
            rpc_base_url=get_opt("rpc_base_url", "https://rpc.alice.yandex.ru"),
        )
        custom_cookie = get_opt("cookie_storage_endpoints")
        if custom_cookie and isinstance(custom_cookie, list):
            inst.cookie_storage_endpoints = list(custom_cookie)

        custom_csrf = get_opt("csrf_endpoints")
        if custom_csrf and isinstance(custom_csrf, list):
            inst.csrf_endpoints = list(custom_csrf)

        return inst

    def resolve_url(self, url: str) -> str:
        if not self.domain:
            return url
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if not hostname:
            return url
        # Only rewrite Yandex hosts where yandex.ru is the base domain
        if hostname.endswith("yandex.ru"):
            new_hostname = hostname[:-9] + self.domain
            netloc = new_hostname
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
        return url


@dataclass
class Readiness:
    auth_state: AuthState = AuthState.UNKNOWN
    csrf_state: CsrfState = CsrfState.UNKNOWN
    devices_state: DevicesState = DevicesState.UNKNOWN
    inventory_source: str = "unknown"
    last_snapshot_at: str | None = None
    scenarios_state: ScenariosState = ScenariosState.UNKNOWN
    local_state: dict[str, Any] | None = None
    token_state: TokenState = TokenState.MISSING

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
