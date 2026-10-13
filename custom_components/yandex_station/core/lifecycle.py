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
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

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
        self.hass = context.hass if context else None
        self.entry_ids: set[str] = set()
        self.runtimes: dict[str, Any] = {}
        self.bootstrap_events: dict[str, asyncio.Event] = {}
        self.bootstrap_deadlines: dict[str, float] = {}
        self.setup_reasons: dict[str, str] = {}
        self.ownership_lock = asyncio.Lock()
        self.speaker_owners: dict[str, str] = {}
        self.ownership_conflicts: dict[str, set[str]] = {}
        self.service_targets: dict[str, Any] = {}
        self._service_handlers: dict[str, Any] = {}
        self.active_consumers = 0
        self._consumers_drained = asyncio.Event()
        self._consumers_drained.set()
        self.discovery_listener = None
        self.stream_dispatcher = None
        self.stop_unsubscribe = None
        self._discovery_tasks: set[asyncio.Task] = set()
        self._reload_requests: dict[str, tuple[asyncio.Task | None, Any]] = {}
        self._reload_tasks: set[asyncio.Task] = set()
        self._entry_capability = ContextVar("yandex_station_entry_capability", default=None)
        self._internal_capability = None
        self._internal_entry_id = None
        self._stopping = False

    @property
    def admission_open(self) -> bool:
        return not self._stopping and (
            self.context is None or self.context.admission_open
        )

    def entry_admitted(self, entry_id: str) -> bool:
        return (
            self.admission_open and entry_id not in self.entry_ids
            and entry_id not in self._reload_requests
        )

    @staticmethod
    def entry_enabled(entry) -> bool:
        return entry is not None and entry.disabled_by is None and entry.source != "ignore"

    @asynccontextmanager
    async def entry_operation(self, entry_id: str, operation: str):
        internal = (
            self._internal_capability is not None
            and self._entry_capability.get() is self._internal_capability
            and self._internal_entry_id == entry_id
        )
        if entry_id in self.entry_ids or (not self.admission_open and not internal):
            yield False
            return
        self.entry_ids.add(entry_id)
        if operation == "setup":
            self.bootstrap_events[entry_id] = asyncio.Event()
            self.bootstrap_deadlines[entry_id] = asyncio.get_running_loop().time() + 15
            self.setup_reasons.pop(entry_id, None)
        try:
            yield True
        finally:
            self.finish_bootstrap(entry_id)
            self.entry_ids.discard(entry_id)

    def finish_bootstrap(self, entry_id: str) -> None:
        if event := self.bootstrap_events.get(entry_id):
            event.set()

    async def wait_initial_bootstraps(self, entry_id: str) -> None:
        """Wait only for already-running initial catalogs, never start another probe."""
        loop = asyncio.get_running_loop()
        pending = [
            (other_id, event, self.bootstrap_deadlines[other_id])
            for other_id, event in self.bootstrap_events.items()
            if other_id != entry_id and not event.is_set()
            and self.entry_enabled(self.hass.config_entries.async_get_entry(other_id))
        ]
        for other_id, event, deadline in pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                continue
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except asyncio.TimeoutError:
                self.setup_reasons.setdefault(other_id, "inventory_unavailable")

    async def _async_core_entry_operation(self, operation: str, entry_id: str):
        """Only this manager can issue a capability for the closed hot-reload gate."""
        capability = object()
        self._internal_capability = capability
        self._internal_entry_id = entry_id
        token = self._entry_capability.set(capability)
        try:
            return await getattr(self.hass.config_entries, operation)(entry_id)
        finally:
            self._entry_capability.reset(token)
            self._internal_capability = None
            self._internal_entry_id = None

    def request_entry_reload(self, entry_id: str) -> bool:
        """Reserve once; dispatch only after the requesting task has returned."""
        if (
            not self.admission_open or self.hass is None
            or not self.entry_enabled(self.hass.config_entries.async_get_entry(entry_id))
        ):
            return False
        if entry_id in self._reload_requests:
            return True
        requester = asyncio.current_task()

        def start(_=None):
            if entry_id not in self._reload_requests:
                return
            if not self.admission_open:
                self._reload_requests.pop(entry_id, None)
                return
            task = self.hass.async_create_task(self._async_reload_entry(entry_id))
            self._reload_tasks.add(task)
            task.add_done_callback(self._reload_tasks.discard)

        self._reload_requests[entry_id] = (requester, start)
        if requester is None:
            self.hass.loop.call_soon(start)
        else:
            requester.add_done_callback(start)
        return True

    async def _async_reload_entry(self, entry_id: str):
        try:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if self.admission_open and self.entry_enabled(entry):
                if not await self.hass.config_entries.async_reload(entry_id):
                    self.setup_reasons[entry_id] = "entry_reload_failed"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.setup_reasons[entry_id] = "entry_reload_failed"
            _LOGGER.warning("Entry reload failed: %s", type(error).__name__)
        finally:
            self._reload_requests.pop(entry_id, None)

    @asynccontextmanager
    async def consumer(self):
        if not self.admission_open:
            raise HomeAssistantError("reload_in_progress")
        self.active_consumers += 1
        self._consumers_drained.clear()
        try:
            yield
        finally:
            self.active_consumers -= 1
            if not self.active_consumers:
                self._consumers_drained.set()

    def bind_service(self, name: str, target):
        """Registered handler stays stable; only the reloadable target changes."""
        self.service_targets[name] = target
        if name not in self._service_handlers:
            async def dispatch(call):
                async with self.consumer():
                    return await self.service_targets[name](call)
            self._service_handlers[name] = dispatch
        return self._service_handlers[name]

    def schedule_discovery(self, target, info: dict) -> None:
        """Called on the HA loop; the zeroconf thread only schedules this callback."""
        if not self.admission_open or self.discovery_listener is None:
            return

        async def dispatch():
            try:
                async with self.consumer():
                    await target(info)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _LOGGER.debug("Discovery update unavailable: %s", type(error).__name__)

        task = self.hass.async_create_task(dispatch())
        self._discovery_tasks.add(task)
        task.add_done_callback(self._discovery_tasks.discard)

    async def async_stop_discovery(self) -> None:
        listener, self.discovery_listener = self.discovery_listener, None
        if listener is not None:
            await self.hass.async_add_executor_job(listener.stop)
        tasks = tuple(self._discovery_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._discovery_tasks.clear()

    async def async_stop(self, _event=None) -> None:
        self._stopping = True
        if self.context:
            self.context.admission_open = False
        if self.stop_unsubscribe is not None:
            if _event is None:
                self.stop_unsubscribe()
            self.stop_unsubscribe = None
        await self.async_stop_discovery()
        for requester, start in self._reload_requests.values():
            if requester is not None:
                requester.remove_done_callback(start)
        self._reload_requests.clear()
        tasks = tuple(self._reload_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for runtime in tuple(self.runtimes.values()):
            await runtime.async_stop()

    async def async_hot_reload(
        self, hass: HomeAssistant, enable_code_reload: bool = False
    ) -> dict[str, Any]:
        if not enable_code_reload:
            _LOGGER.warning("Hot reload called but enable_code_reload is False")
            return {"error": "hot_reload_disabled"}

        if self._reload_lock.locked():
            return {"error": "reload_already_in_progress"}
        if self.entry_ids or self._reload_requests:
            return {"error": "entry_operation_in_progress"}
        self.hass = hass

        async with self._reload_lock:
            _LOGGER.info("Starting YandexStation hot reload sequence (RFC v2 §7.3)")

            # 1. Preflight: find relevant entries
            entries = [
                e
                for e in hass.config_entries.async_entries(DOMAIN)
                if self.entry_enabled(e)
            ]

            # 2. Quiesce: close admission & drain in-flight requests (timeout 10s)
            if self.context:
                self.context.admission_open = False
                try:
                    await asyncio.wait_for(asyncio.gather(
                        self.context.drain_event.wait(), self._consumers_drained.wait()
                    ), timeout=10.0)
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
                        ok = await self._async_core_entry_operation(
                            "async_unload", entry.entry_id
                        )
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
                        await self._async_core_entry_operation("async_setup", uid)
                    except Exception as e:
                        _LOGGER.error("Rollback setup failed for %s: %r", uid, e)
                if self.context:
                    self.context.admission_open = True
                return {"error": "unload_failed", "unloaded_entries": unloaded_entries}
            await self.async_stop_discovery()
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
                        if hasattr(stream_mod, "StreamView"):
                            stream_mod.StreamView.key = self.context.key
                            stream_mod.StreamView.hass_url = self.context.hass_url
                            stream_mod.StreamView.hass = self.context.hass
                        root = sys.modules["custom_components.yandex_station"]
                        await root.async_rebind_runtime(hass)
                    else:
                        _LOGGER.error("StreamImplementation missing in reloaded stream module")
                except Exception as e:
                    _LOGGER.error("Failed to rebind stream implementation: %r", e)
                    rebind_ok = False

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
                    ok = await self._async_core_entry_operation("async_setup", entry_id)
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


def get_lifecycle(hass: HomeAssistant) -> LifecycleManager:
    """One domain owner, including Options opened after a failed entry setup."""
    data = hass.data.setdefault(DOMAIN, {})
    if "lifecycle" not in data:
        data["lifecycle"] = LifecycleManager(StreamContext(hass=hass))
    return data["lifecycle"]
