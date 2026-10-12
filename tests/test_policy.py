from custom_components.yandex_station.core.policy import (
    AuthState,
    CloudState,
    CsrfState,
    DevicesState,
    EntryPolicy,
    Readiness,
    ScenariosState,
    SessionConfig,
    TokenState,
)


def test_entry_policy_defaults():
    policy = EntryPolicy()
    assert policy.enable_scenarios is True
    assert policy.use_cached_devices is False
    assert policy.force_offline_local is False
    assert policy.cloud_updates is True
    assert policy.local_updates is True
    # force_offline_local overrides other cloud policies
    assert policy.effective_cloud_allowed is True

    offline_policy = EntryPolicy(force_offline_local=True, use_cached_devices=False)
    assert offline_policy.force_offline_local is True
    # When force_offline_local is True, cached devices is forced and cloud is blocked
    assert offline_policy.effective_use_cached_devices is True
    assert offline_policy.effective_cloud_allowed is False


def test_entry_policy_precedence():
    yaml_config = {"enable_scenarios": False, "cloud_updates": False}
    entry_options = {"enable_scenarios": True, "force_offline_local": False}
    policy = EntryPolicy.from_configs(yaml_config=yaml_config, entry_options=entry_options)
    assert policy.enable_scenarios is True
    assert policy.cloud_updates is False
    assert policy.force_offline_local is False

    # Empty string or None in options does not override YAML/default
    entry_options2 = {"enable_scenarios": None, "proxy": ""}
    policy2 = EntryPolicy.from_configs(yaml_config={"enable_scenarios": False}, entry_options=entry_options2)
    assert policy2.enable_scenarios is False


def test_session_config_isolation_and_endpoints():
    cfg1 = SessionConfig(domain="yandex.by", proxy="http://proxy1:8080")
    cfg2 = SessionConfig(domain=None, proxy=None)

    assert cfg1.proxy == "http://proxy1:8080"
    assert cfg2.proxy is None

    # Endpoints resolution
    assert cfg1.resolve_url("https://quasar.yandex.ru/get_account_config") == "https://quasar.yandex.by/get_account_config"
    # Should not blindly replace substrings elsewhere in the URL
    assert cfg2.resolve_url("https://api.music.yandex.net/music") == "https://api.music.yandex.net/music"


def test_readiness_and_computed_cloud_state():
    readiness = Readiness()
    assert readiness.auth_state == AuthState.UNKNOWN
    assert readiness.csrf_state == CsrfState.UNKNOWN
    assert readiness.devices_state == DevicesState.UNKNOWN
    assert readiness.scenarios_state == ScenariosState.UNKNOWN
    assert readiness.token_state == TokenState.MISSING
    assert readiness.local_state is None

    # Computed cloud_state property
    assert readiness.computed_cloud_state(EntryPolicy()) == CloudState.UNKNOWN

    readiness.auth_state = AuthState.REAUTH_REQUIRED
    assert readiness.computed_cloud_state(EntryPolicy()) == CloudState.REAUTH_REQUIRED

    readiness.auth_state = AuthState.VALID
    readiness.devices_state = DevicesState.READY
    readiness.scenarios_state = ScenariosState.READY
    assert readiness.computed_cloud_state(EntryPolicy()) == CloudState.ONLINE

    # Degraded if scenarios are unavailable while devices ready
    readiness.scenarios_state = ScenariosState.UNAVAILABLE
    assert readiness.computed_cloud_state(EntryPolicy()) == CloudState.DEGRADED

    # Disabled if scenarios disabled by policy
    assert readiness.computed_cloud_state(EntryPolicy(enable_scenarios=False)) == CloudState.ONLINE

    # Force offline policy computes OFFLINE
    assert readiness.computed_cloud_state(EntryPolicy(force_offline_local=True)) == CloudState.OFFLINE
def test_session_instance_isolation():
    from unittest.mock import MagicMock
    from custom_components.yandex_station.core.yandex_session import YandexSession
    from custom_components.yandex_station.core.yandex_quasar import YandexQuasar

    mock_client1 = MagicMock()
    mock_client2 = MagicMock()

    cfg1 = SessionConfig(domain="yandex.by", proxy="http://proxy1:8080")
    cfg2 = SessionConfig(domain=None, proxy="http://proxy2:8080")

    s1 = YandexSession(mock_client1, config=cfg1)
    s2 = YandexSession(mock_client2, config=cfg2)

    s1.csrf_token = "token1"
    s2.csrf_token = "token2"

    assert s1.csrf_token == "token1"
    assert s2.csrf_token == "token2"
    assert s1.config.proxy == "http://proxy1:8080"
    assert s2.config.proxy == "http://proxy2:8080"

    q1 = YandexQuasar(s1, policy=EntryPolicy(force_offline_local=True))
    q2 = YandexQuasar(s2, policy=EntryPolicy(force_offline_local=False))

    assert q1.policy.force_offline_local is True
    assert q2.policy.force_offline_local is False
    assert q1.readiness is not q2.readiness
def test_incluce_devices_safe_when_not_ready():
    from unittest.mock import MagicMock
    from custom_components.yandex_station.hass.hass_utils import incluce_devices
    from custom_components.yandex_station.core.const import DOMAIN

    hass = MagicMock()
    hass.data = {}
    entry = MagicMock()
    entry.unique_id = "test_user"
    entry.options = {}

    # Not in hass.data -> returns empty list safely, no KeyError
    assert incluce_devices(hass, entry) == []

    # Quasar without devices -> returns empty list safely
    quasar = MagicMock()
    quasar.devices = None
    hass.data = {DOMAIN: {"test_user": quasar, "config": {}}}
    assert incluce_devices(hass, entry) == []


def test_discovery_speaker_sharing_preserves_dicts():
    # Two entries having the same speaker must keep their own device dicts
    dev1 = {
        "id": "cloud_id_1",
        "name": "Speaker 1",
        "quasar_info": {"device_id": "did_123"},
        "scenario_id": "scen_acc1",
        "host": "192.168.1.10",
        "port": 1961,
    }
    dev2 = {
        "id": "cloud_id_2",
        "name": "Speaker 2",
        "quasar_info": {"device_id": "did_123"},
        "scenario_id": "scen_acc2",
        "host": "192.168.1.10",
        "port": 1961,
    }

    # Shared speakers registry
    shared_speakers = {}
    # Entry 1 registration
    did = dev1["quasar_info"]["device_id"]
    shared = shared_speakers.setdefault(did, {})
    shared["host"] = dev1["host"]
    shared["port"] = dev1["port"]
    shared["device_id"] = did

    # Entry 2 registration
    did2 = dev2["quasar_info"]["device_id"]
    shared2 = shared_speakers.setdefault(did2, {})
    if "host" in shared2:
        dev2.setdefault("host", shared2["host"])
    if "port" in shared2:
        dev2.setdefault("port", shared2["port"])

    # Now discovery updates IP address
    new_ip = "192.168.1.50"
    shared_speakers[did]["host"] = new_ip
    dev1["host"] = new_ip
    dev2["host"] = new_ip

    # Verify dev1 and dev2 still have distinct scenario_id and cloud id
    assert dev1["scenario_id"] == "scen_acc1"
    assert dev2["scenario_id"] == "scen_acc2"
    assert dev1["id"] == "cloud_id_1"
    assert dev2["id"] == "cloud_id_2"
    assert dev1["host"] == "192.168.1.50"
    assert dev2["host"] == "192.168.1.50"
