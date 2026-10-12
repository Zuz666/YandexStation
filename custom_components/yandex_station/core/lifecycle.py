"""Stable lifecycle anchor and dispatcher (RFC v2 §7.2, §7.3).

This module is NEVER reloaded during hot-reload. It owns the StreamContext,
signing key, admission gate, and the single registered HomeAssistantView.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import secrets
import sys
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

RELOADABLE_MODULES = [
    "custom_components.yandex_station.core.const",
    "custom_components.yandex_station.core.policy",
    "custom_components.yandex_station.core.cache",
    "custom_components.yandex_station.core.utils",
    "custom_components.yandex_station.core.stream",
    "custom_components.yandex_station.core.yandex_session",
    "custom_components.yandex_station.core.yandex_glagol",
    "custom_components.yandex_station.core.quasar_info",
    "custom_components.yandex_station.core.yandex_quasar",
    "custom_components.yandex_station.core.yandex_station",
    "custom_components.yandex_station.hass.hass_utils",
    "custom_components.yandex_station.hass.shopping_list",
    "custom_components.yandex_station.hass.todo_list",
    "custom_components.yandex_station.media_player",
    "custom_components.yandex_station.select",
    "custom_components.yandex_station.calendar",
    "custom_components.yandex_station.config_flow",
    "custom_components.yandex_station",
]


@dataclass
class StreamContext:
    hass: HomeAssistant
    key: str = field(default_factory=secrets.token_hex)
    hass_url: str = ""
    implementation: Any = None
    admission_open: bool = True
    active_requests: int = 0
    drain_event: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self):
        self.drain_event.set()

    def acquire_request(self) -> bool:
        if not self.admission_open or self.implementation is None:
            return False
        self.active_requests += 1
        self.drain_event.clear()
        return True

    def release_request(self) -> None:
        self.active_requests = max(0, self.active_requests - 1)
        if self.active_requests == 0:
            self.drain_event.set()


class StableStreamDispatcher(HomeAssistantView):
    """Stable HTTP dispatcher registered once into Home Assistant (RFC v2 §7.2).

    Delegates requests to context.implementation without re-registering views.
    """

    requires_auth = False
    url = r"/api/yandex_station/{token:[\w-]+.[\w-]+.[\w-]+}.{ext}"
    name = "api:yandex_station"

    def __init__(self, context: StreamContext):
        self.context = context

    async def get(self, request: web.Request, token: str, ext: str) -> web.StreamResponse:
        if not self.context.acquire_request():
            return web.Response(status=503, text="Service Unavailable: Reload in progress")

        try:
            impl = self.context.implementation
            if impl is None:
                return web.Response(status=503, text="Service Unavailable: No implementation")
            return await impl.get(request, token, ext)
        finally:
            self.context.release_request()

    async def head(self, request: web.Request, token: str, ext: str) -> web.Response:
        if not self.context.acquire_request():
            return web.Response(status=503, text="Service Unavailable: Reload in progress")

        try:
            impl = self.context.implementation
            if impl is None:
                return web.Response(status=503, text="Service Unavailable: No implementation")
            return await impl.head(request, token, ext)
        finally:
            self.context.release_request()


class LifecycleManager:
    """Orchestrates quiesce, module reload, and entry re-setup (RFC v2 §7.3)."""

    def __init__(self, context: StreamContext | None = None):
        self.context = context
        self._reload_lock = asyncio.Lock()

    async def async_hot_reload(
        self, hass: HomeAssistant, enable_code_reload: bool = False
    ) -> dict[str, Any]:
        if not enable_code_reload:
            _LOGGER.warning("Hot reload called but enable_code_reload is False")
            return {"error": "hot_reload_disabled"}

        if self._reload_lock.locked():
            return {"error": "reload_already_in_progress"}

        async with self._reload_lock:
            _LOGGER.info("Starting YandexStation hot reload sequence (RFC v2 §7.3)")

            # 1. Preflight: find relevant entries
            entries = [
                e
                for e in hass.config_entries.async_entries(DOMAIN)
                if getattr(e, "disabled_by", None) is None
                or str(getattr(e, "disabled_by", None)).lower() in ("none", "")
            ]

            # 2. Quiesce: close admission & drain in-flight requests (timeout 10s)
            if self.context:
                self.context.admission_open = False
                try:
                    await asyncio.wait_for(self.context.drain_event.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    _LOGGER.warning("Drain timeout exceeded (10s); aborting hot-reload")
                    self.context.admission_open = True
                    return {"error": "drain_timeout"}

            # 3. Unload affected loaded entries through Core
            unloaded_entries = []
            failed_unload = False
            for entry in entries:
                state_val = getattr(entry, "state", None)
                state_str = str(getattr(state_val, "value", state_val)).lower()
                is_loaded = state_str == "loaded"
                if is_loaded:
                    try:
                        ok = await hass.config_entries.async_unload(entry.entry_id)
                        if ok:
                            unloaded_entries.append(entry.entry_id)
                        else:
                            failed_unload = True
                            break
                    except Exception as e:
                        _LOGGER.error("Failed to unload entry %s: %r", entry.entry_id, e)
                        failed_unload = True
                        break

            if failed_unload:
                _LOGGER.warning("Unload failed; rolling back unloaded entries with old code")
                for uid in unloaded_entries:
                    try:
                        await hass.config_entries.async_setup(uid)
                    except Exception as e:
                        _LOGGER.error("Rollback setup failed for %s: %r", uid, e)
                if self.context:
                    self.context.admission_open = True
                return {"error": "unload_failed", "unloaded_entries": unloaded_entries}
            # 4. In-place importlib.reload of domain modules
            reloaded_modules = []
            try:
                for mod_name in RELOADABLE_MODULES:
                    if mod_name in sys.modules:
                        mod = sys.modules[mod_name]
                        importlib.reload(mod)
                        reloaded_modules.append(mod_name)
            except Exception as e:
                _LOGGER.critical("Exception during importlib.reload: %r; restart_required", e)
                return {
                    "error": "reload_failed",
                    "exception": repr(e),
                    "restart_required": True,
                    "reloaded_modules": reloaded_modules,
                }

            # 5. Rebind stream implementation
            rebind_ok = False
            if self.context:
                try:
                    stream_mod = sys.modules.get("custom_components.yandex_station.core.stream")
                    if stream_mod and hasattr(stream_mod, "StreamImplementation"):
                        new_impl = stream_mod.StreamImplementation(self.context)
                        self.context.implementation = new_impl
                        rebind_ok = True
                    else:
                        _LOGGER.error("StreamImplementation missing in reloaded stream module")
                except Exception as e:
                    _LOGGER.error("Failed to rebind stream implementation: %r", e)

            if not rebind_ok and self.context:
                self.context.admission_open = False
                self.context.implementation = None
                return {
                    "error": "rebind_failed",
                    "reloaded_modules_count": len(reloaded_modules),
                    "http_status": 503,
                }
            # 6. Re-setup entries through Core
            setup_results = {}
            for entry_id in unloaded_entries:
                try:
                    ok = await hass.config_entries.async_setup(entry_id)
                    setup_results[entry_id] = ok
                except Exception as e:
                    setup_results[entry_id] = False
                    _LOGGER.error("Re-setup of entry %s failed: %r", entry_id, e)

            # 7. Re-open admission
            if self.context:
                self.context.admission_open = True

            return {
                "success": True,
                "reloaded_modules_count": len(reloaded_modules),
                "setup_results": setup_results,
            }
