"""Passive system information for every configured Yandex account."""

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.translation import async_get_translations

from . import diagnostics
from .core.const import DATA_CONFIG, DOMAIN
from .core.lifecycle import get_lifecycle


@callback
def async_register(hass: HomeAssistant, register: system_health.SystemHealthRegistration):
    register.async_register_info(_async_info, manage_url="/config/integrations")


async def _async_info(hass: HomeAssistant):
    # Core retains this function across importlib.reload. Resolve the current
    # implementation through the module globals, never a captured function.
    return await _collect_info(hass)


def _fields(info, prefix=""):
    for key, value in info.items():
        name = f"{prefix}_{key}" if prefix else key
        if isinstance(value, dict):
            yield from _fields(value, name)
        elif value is None or type(value) in (str, int, float, bool):
            yield name, "unknown" if value is None else value


async def _collect_info(hass: HomeAssistant):
    entries = hass.config_entries.async_entries(DOMAIN)
    lifecycle = get_lifecycle(hass)
    translations = await async_get_translations(
        hass, hass.config.language, "system_health", {DOMAIN}
    )
    info = {
        "accounts": len(entries),
        "code_reload_enabled": bool(
            hass.data.get(DOMAIN, {}).get(DATA_CONFIG, {}).get("enable_code_reload")
        ),
        "code_reload_in_progress": bool(
            lifecycle._reload_task is not None and not lifecycle._reload_task.done()
        ),
    }
    if last := diagnostics._reload_health(lifecycle.last_reload_result):
        info["last_reload_status"] = last["status"]
        info["last_reload_reason"] = last["reason"]
        duration = last["timings_ms"].get("total_ms")
        if duration is not None:
            info["last_reload_duration_ms"] = duration
    for index, entry in enumerate(entries, 1):
        title = f"{index}. {entry.title or DOMAIN}"
        account, _, _ = await diagnostics._account_diagnostics(
            hass, entry, load_cache=False, include_devices=False
        )
        account.pop("reload", None)
        if account["setup_reason"] is None:
            account.pop("setup_reason")
        if account["cache"]["status"] == "unknown":
            account["cache"] = {"status": "unknown"}
            account.pop("scenario_journal", None)
        account["reload_pending"] = entry.entry_id in lifecycle._reload_requests
        for key, value in _fields(account):
            label = translations.get(
                f"component.{DOMAIN}.system_health.info.{key}",
                key.replace("_", " ").capitalize(),
            )
            info[f"{title} / {label}"] = value
    return info
