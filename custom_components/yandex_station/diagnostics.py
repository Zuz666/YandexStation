"""Safe account and device diagnostics: structured metadata, never raw responses."""

from datetime import datetime, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .core.cache import CacheManager
from .core.const import DATA_CONFIG, DOMAIN
from .core.lifecycle import get_lifecycle
from .core.policy import EntryPolicy
from .hass.hass_utils import device_config_entry_ids

_AUTH_STATES = frozenset({"unknown", "valid", "reauth_required"})
_CSRF_STATES = frozenset({"unknown", "candidate", "unavailable"})
_DEVICES_STATES = frozenset({"unknown", "ready", "unavailable"})
_SCENARIOS_STATES = frozenset({"unknown", "ready", "unavailable", "disabled"})
_SOURCES = frozenset({"unknown", "cloud", "cache"})
_JOURNAL_STATES = frozenset({"pending_unknown", "confirmed"})
_DEVICE_KINDS = frozenset({
    "smart_speaker", "light", "switch", "sensor", "climate", "vacuum",
    "humidifier", "socket", "thermostat", "camera", "kettle",
})
_SETUP_REASONS = frozenset({
    "cache_missing", "cache_invalid", "cache_unavailable", "cache_write_failed",
    "cache_account_mismatch", "inventory_unavailable",
    "entry_reload_failed", "csrf_unavailable", "invalid_endpoint",
    "cloud_blocked_by_policy", "x_token_required", "ConfigEntryAuthFailed",
    "ConfigEntryNotReady",
})
_RELOAD_STATUSES = frozenset({"accepted", "success", "partial", "failed", "restart_required"})
_RELOAD_REASONS = frozenset({
    "reload_requested", "completed", "entry_setup_failed", "hot_reload_disabled",
    "reload_already_in_progress", "entry_operation_in_progress",
    "runtime_not_initialized", "unsupported_runtime", "baseline_missing",
    "flow_in_progress", "source_preflight_failed", "module_set_changed",
    "stable_contract_changed", "registration_or_storage_schema_changed",
    "top_level_binding_removed", "membership_changed", "drain_timeout",
    "unload_failed", "source_changed_during_reload", "import_failed",
    "rebind_failed", "discovery_stop_failed",
})
_ENTRY_RELOAD_STATES = frozenset({
    "loaded", "setup_retry", "setup_error", "not_loaded", "membership_changed",
})
_TIMING_PHASES = frozenset({
    "preflight_ms", "drain_ms", "unload_ms", "import_ms", "rebind_ms",
    "code_swap_ms", "local_ready_ms", "cloud_ready_ms", "total_ms",
})


def _state(value, allowed: frozenset[str]) -> str:
    value = str(value) if value is not None else "unknown"
    return value if value in allowed else "unknown"


def _device_summary(device: dict, journal: dict) -> dict:
    """Never copy user names, IDs, network fields, action text, or URLs."""
    device_id = device.get("id")
    journal_item = journal.get(device_id) if isinstance(device_id, str) else None
    if not isinstance(journal_item, dict):
        journal_item = {}
    kind = device.get("type")
    segments = kind.split(".") if isinstance(kind, str) else []
    family = segments[2] if len(segments) > 2 and segments[:2] == [
        "devices", "types"
    ] else None
    return {
        "kind": family if family in _DEVICE_KINDS else "other",
        "has_local_protocol": isinstance(device.get("quasar_info"), dict),
        "capability_count": len(device["capabilities"]) if isinstance(
            device.get("capabilities"), list
        ) else 0,
        "property_count": len(device["properties"]) if isinstance(
            device.get("properties"), list
        ) else 0,
        "scenario_journal_state": _state(journal_item.get("state"), _JOURNAL_STATES),
    }


def _cache_age(snapshot) -> int | None:
    if snapshot is None or not isinstance(snapshot.updated_at, str):
        return None
    try:
        when = datetime.fromisoformat(snapshot.updated_at.replace("Z", "+00:00"))
        if when.tzinfo is None:
            return None
        return max(0, int((datetime.now(timezone.utc) - when).total_seconds()))
    except (ValueError, OverflowError):
        return None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.isoformat() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _runtime_health(runtime, snapshot):
    """Allowlisted operational metadata shared with the passive health screen."""
    credentials = runtime.session.credential_metadata()
    tokens = {
        key: value if type(value := credentials.get(key)) is bool else None
        for key in ("cookies_present", "x_token_present", "music_token_present", "csrf_present")
    }
    age = credentials.get("csrf_age_seconds")
    tokens["csrf_age_seconds"] = age if type(age) is int and age >= 0 else None
    local_tokens = {
        "cached_candidates": 0, "cached_rejected": 0,
        "missing": 0, "candidate": 0, "accepted": 0, "rejected": 0,
    }
    if snapshot:
        for records in snapshot.device_tokens.values():
            for record in records.values():
                if record.get("state") == "valid":
                    local_tokens["cached_candidates"] += 1
                elif record.get("state") == "invalid":
                    local_tokens["cached_rejected"] += 1
    for device in runtime.owned_speakers + runtime.owned_modules:
        glagol = getattr(device.get("entity"), "glagol", None)
        if glagol is not None:
            state = str(glagol.token_state)
            if state in {"missing", "candidate", "accepted", "rejected"}:
                local_tokens[state] += 1
    connection = runtime.cloud_connection
    result = {
        "tokens": tokens,
        "device_tokens": local_tokens,
        "cloud_connection": {
            "state": _state(connection.get("state"), frozenset({
                "disabled", "disconnected", "connecting", "connected", "backoff", "stopped",
            })),
            **{
                key: _timestamp(connection.get(key))
                for key in ("connected_at", "last_message_at", "retry_at")
            },
            "last_error": _state(connection.get("last_error"), frozenset({
                "http_error", "connection_error", "connection_closed",
            })),
        },
        "cloud_check": {"status": "not_run"},
    }
    check = getattr(runtime, "last_cloud_check", None)
    if isinstance(check, dict):
        result["cloud_check"] = {
            "status": _state(check.get("status"), frozenset({
                "running", "ok", "failed", "skipped", "cancelled",
            })),
            "checked_at": _timestamp(check.get("checked_at")),
            "reason": _state(check.get("reason"), frozenset({
                "completed", "cloud_blocked_by_policy", "account_stopping",
                "http_error", "timeout", "invalid_response", "connection_error",
                "cancelled", "unexpected_error", "request_blocked_by_policy",
            })),
            **{
                key: value
                for key in ("duration_ms", "device_count", "http_status")
                if type(value := check.get(key)) is int and value >= 0
            },
        }
    return result


def _reload_health(last):
    if not last:
        return None
    entry_states = last.get("entries", {})
    return {
        "status": _state(last.get("status"), _RELOAD_STATUSES),
        "reason": _state(last.get("reason"), _RELOAD_REASONS),
        "entry_state_counts": {
            state: sum(1 for value in entry_states.values() if value == state)
            for state in _ENTRY_RELOAD_STATES
        },
        "timings_ms": {
            phase: value for phase, value in last.get("timings_ms", {}).items()
            if phase in _TIMING_PHASES
            and (value is None or type(value) in (int, float))
        },
    }


async def _account_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, *, load_cache: bool = True,
    include_devices: bool = True,
):
    lifecycle = get_lifecycle(hass)
    runtime = lifecycle.runtimes.get(entry.entry_id)
    config = hass.data.get(DOMAIN, {}).get(DATA_CONFIG, {})
    policy = EntryPolicy.from_configs(config, entry.options)
    cache = runtime.cache_manager if runtime else None
    owns_cache = cache is None and load_cache
    if owns_cache:
        cache = CacheManager(
            hass, entry.entry_id, account_login=entry.unique_id,
            account_uid=entry.data.get("account_uid"),
        )
    try:
        snapshot = cache.snapshot if cache else None
        if snapshot is None and load_cache:
            snapshot = await cache.async_load()
        journal = snapshot.scenario_creation if snapshot else {}
        state_counts = {
            state: sum(1 for item in journal.values() if item.get("state") == state)
            for state in _JOURNAL_STATES
        }
        reason = lifecycle.setup_reasons.get(entry.entry_id)
        result = {
            "runtime_loaded": runtime is not None,
            "entry_state": (
                lifecycle._state(entry) if lifecycle.entry_enabled(entry) else "disabled"
            ),
            "setup_reason": reason if isinstance(reason, str) and reason in _SETUP_REASONS else (
                "setup_unavailable" if reason else None
            ),
            "policy": {
                "force_offline_local": policy.force_offline_local,
                "use_cached_devices": policy.use_cached_devices,
                "enable_scenarios": policy.enable_scenarios,
                "enable_diagnostic_sensors": policy.enable_diagnostic_sensors,
                "cloud_updates": policy.cloud_updates,
                "local_updates": policy.local_updates,
            },
            "cache": {
                "status": _state(
                    cache.cache_status if cache else None,
                    frozenset({"cache_missing", "cache_invalid", "cache_ready"}),
                ),
                "age_seconds": _cache_age(snapshot),
                "device_count": len(snapshot.devices) if snapshot else 0,
                "scenario_creation_trusted": (
                    snapshot.scenario_creation_trusted if snapshot else False
                ),
            },
            "scenario_journal": state_counts,
        }
        if last := _reload_health(lifecycle.last_reload_result):
            result["reload"] = last
        if runtime is not None:
            result["cloud_readiness"] = str(
                runtime.readiness.computed_cloud_state(policy)
            )
            result.update(_runtime_health(runtime, snapshot))
            readiness = runtime.readiness
            result["readiness"] = {
                "auth_state": _state(readiness.auth_state, _AUTH_STATES),
                "csrf_state": _state(readiness.csrf_state, _CSRF_STATES),
                "devices_state": _state(readiness.devices_state, _DEVICES_STATES),
                "scenarios_state": _state(
                    readiness.scenarios_state, _SCENARIOS_STATES
                ),
                "inventory_source": _state(readiness.inventory_source, _SOURCES),
            }
            if include_devices:
                result["devices"] = [
                    _device_summary(device, journal) for device in (runtime.devices or [])
                    if isinstance(device, dict)
                ]
        return result, runtime, journal
    finally:
        if owns_cache:
            await cache.async_close()


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
):
    info, _, _ = await _account_diagnostics(hass, config_entry)
    return info


async def async_get_device_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry, device: DeviceEntry
):
    info, runtime, journal = await _account_diagnostics(hass, config_entry)
    identifiers = {
        value for domain, value in device.identifiers
        if domain == DOMAIN and isinstance(value, str)
    }
    owners = device_config_entry_ids(device)
    if config_entry.entry_id in identifiers and config_entry.entry_id in owners:
        return info
    if config_entry.entry_id not in owners or runtime is None:
        info["error"] = "device_not_found"
        return info
    match = next((
        item for item in (runtime.devices or [])
        if isinstance(item, dict) and (
            item.get("id") in identifiers
            or (
                isinstance(item.get("quasar_info"), dict)
                and item["quasar_info"].get("device_id") in identifiers
            )
        )
    ), None)
    if match is None:
        info["error"] = "device_not_found"
    else:
        info["device"] = _device_summary(match, journal)
    return info
