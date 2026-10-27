import json
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import patch

import pytest

from custom_components.yandex_station.core.policy import (
    DEFAULT_COOKIE_STORAGE_ENDPOINTS,
    DEFAULT_CSRF_ENDPOINTS,
    AuthOutcome,
    AuthState,
    CloudState,
    CsrfState,
    DevicesState,
    EntryPolicy,
    Readiness,
    RecoveryBackoff,
    ScenariosState,
    SendOutcome,
    SessionConfig,
)

_RANDOM = "custom_components.yandex_station.core.policy.random"
_NOW = "custom_components.yandex_station.core.policy._now"


def test_entry_policy_resolver_precedence():
    # options > YAML > defaults
    policy = EntryPolicy.from_configs(
        yaml_config={"enable_scenarios": False, "cloud_updates": False},
        entry_options={"enable_scenarios": True, "force_offline_local": False},
    )
    assert policy.enable_scenarios is True
    assert policy.cloud_updates is False
    assert policy.force_offline_local is False

    # explicit False in options is preserved, not dropped
    policy2 = EntryPolicy.from_configs(
        yaml_config={"local_updates": True},
        entry_options={"local_updates": False},
    )
    assert policy2.local_updates is False

    # empty string or None in options clears the override (falls to YAML/default)
    policy3 = EntryPolicy.from_configs(
        yaml_config={"enable_scenarios": False},
        entry_options={"enable_scenarios": None, "cloud_updates": ""},
    )
    assert policy3.enable_scenarios is False
    assert policy3.cloud_updates is True


def test_entry_policy_offline_gates():
    policy = EntryPolicy()
    assert policy.effective_cloud_allowed is True
    assert policy.effective_use_cached_devices is False

    offline = EntryPolicy(force_offline_local=True, use_cached_devices=False)
    assert offline.effective_cloud_allowed is False
    assert offline.effective_cloud_updates is False
    # force_offline_local forces cached devices and blocks cloud
    assert offline.effective_use_cached_devices is True


def test_readiness_and_computed_cloud_state():
    readiness = Readiness()
    assert readiness.auth_state == AuthState.UNKNOWN
    assert readiness.csrf_state == CsrfState.UNKNOWN
    assert readiness.devices_state == DevicesState.UNKNOWN
    assert readiness.scenarios_state == ScenariosState.UNKNOWN

    assert readiness.computed_cloud_state(EntryPolicy()) == CloudState.UNKNOWN

    readiness.auth_state = AuthState.REAUTH_REQUIRED
    assert readiness.computed_cloud_state(EntryPolicy()) == CloudState.REAUTH_REQUIRED

    readiness.auth_state = AuthState.VALID
    readiness.devices_state = DevicesState.READY
    readiness.scenarios_state = ScenariosState.READY
    assert readiness.computed_cloud_state(EntryPolicy()) == CloudState.ONLINE

    readiness.scenarios_state = ScenariosState.UNAVAILABLE
    assert readiness.computed_cloud_state(EntryPolicy()) == CloudState.DEGRADED
    assert readiness.computed_cloud_state(EntryPolicy(enable_scenarios=False)) == CloudState.ONLINE
    assert readiness.computed_cloud_state(EntryPolicy(force_offline_local=True)) == CloudState.OFFLINE


def test_unknown_request_roles_are_rejected():
    with pytest.raises(ValueError, match="invalid_endpoint"):
        SessionConfig().validate_destination("https://iot.quasar.yandex.ru/x", "bogus_role")


def test_domain_provenance_from_configs():
    # entry options > YAML > defaults provenance decides explicitness, not
    # a value comparison against the old default
    cfg = SessionConfig.from_configs(
        yaml_config={"domain": "yandex.com"},
        entry_options={"passport_base_url": "https://passport.yandex.ru"},
    )
    assert cfg.passport_base_url == "https://passport.yandex.ru"

    # an empty-string option keeps fallback semantics; the YAML explicit
    # base below it stays an exact endpoint
    cfg2 = SessionConfig.from_configs(
        yaml_config={
            "domain": "yandex.com",
            "quasar_base_url": "https://iot.quasar.yandex.ru",
        },
        entry_options={"quasar_base_url": ""},
    )
    assert cfg2.quasar_base_url == "https://iot.quasar.yandex.ru"

    # an explicitly configured list equal to the defaults is explicit too
    cfg3 = SessionConfig.from_configs(
        entry_options={
            "domain": "yandex.com",
            "csrf_endpoints": list(DEFAULT_CSRF_ENDPOINTS),
        }
    )
    assert cfg3.csrf_endpoints == ["https://yandex.ru/iot"]

    # with only domain configured the default hosts are still mirrored
    cfg4 = SessionConfig.from_configs(entry_options={"domain": "yandex.com"})
    assert cfg4.account_base_url == "https://quasar.yandex.com"
    assert cfg4.cookie_storage_endpoints == [
        {"url": "https://yandex.com/iot?storage=1", "validator": "uid_in_storage"}
    ]


def test_config_validation_rejects_unsafe_endpoints():
    with pytest.raises(ValueError, match="invalid_endpoint"):
        SessionConfig(quasar_base_url="http://iot.quasar.yandex.ru")
    with pytest.raises(ValueError, match="invalid_endpoint"):
        SessionConfig(passport_base_url="https://user:pass@passport.yandex.ru/")
    with pytest.raises(ValueError, match="invalid_endpoint"):
        SessionConfig(rpc_base_url="https://rpc.alice.yandex.ru#frag")
    with pytest.raises(ValueError, match="invalid_endpoint"):
        SessionConfig(domain="yandex.by")
    with pytest.raises(ValueError, match="invalid_endpoint"):
        SessionConfig(domain="evil.com")
    with pytest.raises(ValueError, match="invalid_endpoint"):
        SessionConfig(cookie_storage_endpoints=[])
    with pytest.raises(ValueError, match="invalid_endpoint"):
        SessionConfig(csrf_endpoints=[])
    with pytest.raises(ValueError, match="invalid_endpoint"):
        SessionConfig(
            cookie_storage_endpoints=[
                {"url": "https://quasar.yandex.ru/x", "validator": "guess_by_200"}
            ]
        )
def test_endpoint_override_precedence():
    yaml_eps = [
        {"url": "https://quasar.yandex.ru/from_yaml", "validator": "status_ok"}
    ]
    option_eps = [
        {"url": "https://quasar.yandex.ru/from_options", "validator": "uid_in_storage"}
    ]
    cfg = SessionConfig.from_configs(
        yaml_config={"cookie_storage_endpoints": yaml_eps},
        entry_options={"cookie_storage_endpoints": option_eps},
    )
    assert cfg.cookie_storage_endpoints == option_eps

    # None/empty in options falls back to YAML, then defaults
    cfg2 = SessionConfig.from_configs(
        yaml_config={"cookie_storage_endpoints": yaml_eps},
        entry_options={"cookie_storage_endpoints": None},
    )
    assert cfg2.cookie_storage_endpoints == yaml_eps

    cfg3 = SessionConfig.from_configs(
        yaml_config={"csrf_endpoints": ["https://yandex.ru/yaml_csrf"]},
        entry_options={},
    )
    assert cfg3.csrf_endpoints == ["https://yandex.ru/yaml_csrf"]

    cfg4 = SessionConfig.from_configs()
    assert cfg4.cookie_storage_endpoints == DEFAULT_COOKIE_STORAGE_ENDPOINTS
    assert cfg4.csrf_endpoints == DEFAULT_CSRF_ENDPOINTS


def test_session_config_instances_isolated():
    cfg1 = SessionConfig(domain="yandex.com", proxy="http://proxy1:8080")
    cfg2 = SessionConfig(domain=None, proxy="http://proxy2:8080")

    assert cfg1.account_base_url == "https://quasar.yandex.com"
    assert cfg2.account_base_url == "https://quasar.yandex.ru"

    # mutating one instance never leaks into another or into defaults
    cfg1.cookie_storage_endpoints.append(
        {"url": "https://quasar.yandex.ru/extra", "validator": "status_ok"}
    )
    cfg1.csrf_endpoints.append("https://yandex.ru/extra")
    assert len(cfg2.cookie_storage_endpoints) == 1
    assert len(cfg2.csrf_endpoints) == 1
    assert len(DEFAULT_COOKIE_STORAGE_ENDPOINTS) == 1
    assert len(DEFAULT_CSRF_ENDPOINTS) == 1


def test_validate_destination_yandex_domain_suffix_for_credentials():
    cfg = SessionConfig()
    cfg.validate_destination(
        "https://iot.quasar.yandex.ru/m/v3/user/devices", "quasar", credential=True
    )
    cfg.validate_destination(
        "https://iot.quasar.yandex.com/m/v3/user/devices", "quasar", credential=True
    )
    cfg.validate_destination(
        "https://passport.yandex.ru:443/pwl-yandex", "passport", credential=True
    )
    with pytest.raises(ValueError, match="untrusted_credential_destination"):
        cfg.validate_destination(
            "https://mirror.example.org/m/v3/user/devices", "quasar", credential=True
        )
def test_enum_string_representation():
    # fallback StrEnum must render plain wire strings for logs/diagnostics
    assert str(AuthOutcome.UNAVAILABLE) == "unavailable"
    assert str(SendOutcome.UNKNOWN_OUTCOME) == "unknown_outcome"
    assert str(AuthState.REAUTH_REQUIRED) == "reauth_required"
    assert str(ScenariosState.UNAVAILABLE) == "unavailable"
    assert json.dumps({"outcome": AuthOutcome.CREDENTIAL_REJECTED}) == (
        '{"outcome": "credential_rejected"}'
    )


def test_validate_destination_external_override_blocks_credentials():
    cfg = SessionConfig(quasar_base_url="https://mirror.example.org")
    # anonymous requests to the admin override are allowed
    cfg.validate_destination("https://mirror.example.org/m/v3/user/devices", "quasar")
    # credential-bearing requests never leave built-in trusted origins
    with pytest.raises(ValueError, match="untrusted_credential_destination"):
        cfg.validate_destination(
            "https://mirror.example.org/m/v3/user/devices", "quasar", credential=True
        )


def test_validate_destination_rejects_malformed_urls():
    cfg = SessionConfig()
    for url in (
        "http://iot.quasar.yandex.ru/x",
        "https://user:pass@iot.quasar.yandex.ru/x",
        "https://iot.quasar.yandex.ru/x#frag",
        "https://iot.quasar.yandex.ru:99999/x",
        "ftp://iot.quasar.yandex.ru/x",
        "",
    ):
        with pytest.raises(ValueError, match="invalid_endpoint"):
            cfg.validate_destination(url, "quasar")


def test_validate_destination_music_role():
    cfg = SessionConfig()
    # unsigned CDN downloads over HTTPS are fine
    cfg.validate_destination("https://cdn-userproxy.example.net/file.mp3", "music")
    # OAuth-bearing music calls are restricted to the two trusted hosts
    cfg.validate_destination("https://api.music.yandex.net/tracks", "music", credential=True)
    cfg.validate_destination("https://oauth.mobile.yandex.net/1/token", "music", credential=True)
    with pytest.raises(ValueError, match="untrusted_credential_destination"):
        cfg.validate_destination(
            "https://cdn-userproxy.example.net/file.mp3", "music", credential=True
        )
    with pytest.raises(ValueError, match="invalid_endpoint"):
        cfg.validate_destination("http://api.music.yandex.net/tracks", "music")


def test_validate_destination_local_ws_lan_only():
    cfg = SessionConfig()
    cfg.validate_destination("wss://192.168.1.42:1961/", "local_ws")
    cfg.validate_destination("wss://10.0.0.7:1961/", "local_ws")
    cfg.validate_destination("wss://[fe80::1]:1961/", "local_ws")
    cfg.validate_destination("wss://[::1]:1961/", "local_ws")
    cfg.validate_destination("wss://[fc00::1]:1961/", "local_ws")
    # public IPs, .local hostnames and cloud hosts are not LAN endpoints
    for url in (
        "wss://8.8.8.8:1961/",
        "wss://station.local:1961/",
        "wss://localhost:1961/",
        "wss://240.0.0.1:1961/",
        "ws://192.168.1.42:1961/",
        "wss://api.yandex.ru/",
        "https://192.168.1.42/",
    ):
        with pytest.raises(ValueError, match="invalid_endpoint"):
            cfg.validate_destination(url, "local_ws")
    # cloud credentials never ride a local socket
    with pytest.raises(ValueError, match="untrusted_credential_destination"):
        cfg.validate_destination("wss://192.168.1.42:1961/", "local_ws", credential=True)


def test_validate_destination_cloud_ws_and_probes():
    cfg = SessionConfig()
    cfg.validate_destination("wss://push.yandex.ru/v2/events", "cloud_ws", credential=True)
    cfg.validate_destination("wss://push.yandex.com/v2/events", "cloud_ws", credential=True)
    cfg.validate_destination("wss://mirror.example.org/v2/events", "cloud_ws")
    with pytest.raises(ValueError, match="untrusted_credential_destination"):
        cfg.validate_destination(
            "wss://mirror.example.org/v2/events", "cloud_ws", credential=True
        )
    for url in ("ws://push.yandex.ru/", "https://push.yandex.ru/"):
        with pytest.raises(ValueError, match="invalid_endpoint"):
            cfg.validate_destination(url, "cloud_ws")
    cfg.validate_destination(
        "https://yandex.ru/iot?storage=1", "cookie_probe", credential=True
    )
    cfg.validate_destination(
        "https:// probe.example.org/p".replace(" ", ""), "cookie_probe"
    )
    with pytest.raises(ValueError, match="untrusted_credential_destination"):
        cfg.validate_destination(
            "https://probe.example.org/p", "cookie_probe", credential=True
        )

def test_ssl_disabled_does_not_expand_credential_destinations():
    cfg = SessionConfig(ssl=False, passport_base_url="https://mirror.example.org")
    with pytest.raises(ValueError, match="untrusted_credential_destination"):
        cfg.validate_destination(
            "https://mirror.example.org/auth/session/", "passport", credential=True
        )


def test_recovery_backoff_ladder_and_jitter_bounds():
    backoff = RecoveryBackoff()
    with patch(_RANDOM) as rnd:
        rnd.uniform.return_value = 1.0
        assert [backoff.next_delay() for _ in range(6)] == [30.0, 60.0, 120.0, 300.0, 300.0, 300.0]
    backoff.reset()
    with patch(_RANDOM) as rnd:
        rnd.uniform.return_value = 0.8
        assert backoff.next_delay() == pytest.approx(24.0)
        rnd.uniform.return_value = 1.2
        assert backoff.next_delay() == pytest.approx(72.0)
    # the +/-20% jitter is capped at 300 on the top rung
    fresh = RecoveryBackoff()
    with patch(_RANDOM) as rnd:
        rnd.uniform.return_value = 1.2
        for expected in (36.0, 72.0, 144.0, 300.0):
            assert fresh.next_delay() == pytest.approx(expected)


def test_recovery_backoff_retry_after_lower_bound():
    backoff = RecoveryBackoff()
    with patch(_RANDOM) as rnd:
        rnd.uniform.return_value = 1.0
        # a lower bound below the ladder changes nothing
        assert backoff.next_delay("0") == 30.0
        # between rungs it raises the delay
        assert backoff.next_delay("90") == 90.0
        # and may exceed the 300 cap
        assert backoff.next_delay("3600") == 3600.0
    # malformed headers fall back to the ladder
    with patch(_RANDOM) as rnd:
        rnd.uniform.return_value = 1.0
        for value in ("soon", "-5", "", None):
            assert RecoveryBackoff().next_delay(value) == 30.0


def test_recovery_backoff_http_date():
    fixed_now = 1_800_000_000.0
    future = datetime.fromtimestamp(fixed_now + 120, tz=timezone.utc)
    past = datetime.fromtimestamp(fixed_now - 600, tz=timezone.utc)
    with patch(_NOW, return_value=fixed_now), patch(_RANDOM) as rnd:
        rnd.uniform.return_value = 1.0
        assert RecoveryBackoff().next_delay(format_datetime(future, usegmt=True)) == pytest.approx(120.0)
        # a date in the past is not a lower bound
        assert RecoveryBackoff().next_delay(format_datetime(past, usegmt=True)) == 30.0


def test_recovery_backoff_reset_and_instance_isolation():
    first = RecoveryBackoff()
    second = RecoveryBackoff()
    with patch(_RANDOM) as rnd:
        rnd.uniform.return_value = 1.0
        first.next_delay()
        first.next_delay()
        # independent counters
        assert second.next_delay() == 30.0
        # reset restarts the ladder
        first.reset()
        assert first.next_delay() == 30.0
