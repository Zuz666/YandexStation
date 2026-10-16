"""Stable lifecycle anchor and dispatcher (RFC v2 §7.2, §7.3).

This module is NEVER reloaded during hot-reload. It owns the StreamContext,
signing key, admission gate, and the single registered HomeAssistantView.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import logging
import secrets
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_ROOT = "custom_components.yandex_station"
_RELOAD_ORDER = (
    "core.const", "core.policy", "core.protobuf", "core.quasar_info", "core.image",
    "core.cache", "core.utils", "core.stream", "core.yandex_music",
    "core.yandex_session", "core.yandex_glagol", "core.yandex_quasar",
    "core.entity", "hass.shopping_list", "hass.todo_list", "core.yandex_station",
    "hass.hass_utils", "binary_sensor", "button", "calendar", "camera", "climate",
    "conversation", "cover", "diagnostics", "fan", "humidifier", "light", "lock",
    "media_player", "notify", "number", "remote", "select", "sensor", "switch",
    "system_health", "text", "vacuum", "water_heater", "config_flow", "",
)


@dataclass
class StreamContext:
    hass: HomeAssistant
    key: str = field(default_factory=secrets.token_hex)
    hass_url: str = ""
    implementation: Any = None
    admission_open: bool = True
    active_requests: int = 0
    drain_event: asyncio.Event = field(default_factory=asyncio.Event)

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
        self.setup_reasons: dict[str, str] = {}
        self.ownership_lock = asyncio.Lock()
        self.speaker_owners: dict[str, str] = {}
        self.service_targets: dict[str, Any] = {}
        self._service_handlers: dict[str, Any] = {}
        self._consumer_tasks: set[asyncio.Task] = set()
        self._consumers_drained = asyncio.Event()
        self._consumers_drained.set()
        self.discovery_listener = None
        self.stream_dispatcher = None
        self.stop_unsubscribe = None
        self._discovery_tasks: set[asyncio.Task] = set()
        self._reload_requests: dict[str, tuple[asyncio.Task | None, Any]] = {}
        self._reload_tasks: set[asyncio.Task] = set()
        self._button_requester: asyncio.Task | None = None
        self._internal_entry_id: str | None = None
        self._stopping = False
        self._reload_task: asyncio.Task | None = None
        self.last_reload_result: dict[str, Any] | None = None
        self.generation = 0
        self.conversation_available = False

    @property
    def active_consumers(self) -> int:
        return len(self._consumer_tasks)

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
        internal = self._internal_entry_id == entry_id
        if entry_id in self.entry_ids or (not self.admission_open and not internal):
            yield False
            return
        self.entry_ids.add(entry_id)
        if operation == "setup":
            self.setup_reasons.pop(entry_id, None)
        try:
            yield True
        finally:
            self.entry_ids.discard(entry_id)

    async def _async_core_entry_operation(self, operation: str, entry_id: str):
        self._internal_entry_id = entry_id
        try:
            return await getattr(self.hass.config_entries, operation)(entry_id)
        finally:
            self._internal_entry_id = None

    def _consumer_requester(self):
        task = self._button_requester or asyncio.current_task()
        return task if task in self._consumer_tasks and not task.done() else None

    def request_entry_reload(self, entry_id: str) -> bool:
        """Reserve once; dispatch only after the requesting task has returned."""
        if (
            not self.admission_open or self.hass is None
            or not self.entry_enabled(self.hass.config_entries.async_get_entry(entry_id))
        ):
            return False
        if entry_id in self._reload_requests:
            return True
        requester = self._consumer_requester() or asyncio.current_task()

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
    async def consumer(self, entry_id: str | None = None):
        task = asyncio.current_task()
        nested = task in self._consumer_tasks
        internal = entry_id is not None and self._internal_entry_id == entry_id
        if not self.admission_open and not nested and not internal:
            raise HomeAssistantError("reload_in_progress")
        if not nested and task is not None:
            self._consumer_tasks.add(task)
            self._consumers_drained.clear()
            if entry_id is not None:
                self._button_requester = task
        try:
            yield
        finally:
            if not nested and task is not None:
                if self._button_requester is task:
                    self._button_requester = None
                self._consumer_tasks.discard(task)
                if not self._consumer_tasks:
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
        if self._reload_task is not None:
            await asyncio.shield(self._reload_task)
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
        if self.context and self.context.implementation is not None:
            try:
                await asyncio.wait_for(self.context.drain_event.wait(), 10)
            except asyncio.TimeoutError:
                _LOGGER.warning("Stream drain timed out during shutdown")
            await self.context.implementation.async_close()
            self.context.implementation = None

    @staticmethod
    def _compile_sources() -> None:
        root = Path(__file__).resolve().parents[1]
        for path in sorted(root.rglob("*.py")):
            if path.name == "lifecycle.py" and path.parent.name == "core":
                continue
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
    def account_reload_service_handler(self):
        async def handle(call):
            entry_id = call.data["entry_id"]
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.domain != DOMAIN:
                raise HomeAssistantError("entry_not_found")
            if not self.entry_enabled(entry):
                raise HomeAssistantError("entry_disabled")
            if not self.entry_admitted(entry_id) or not self.request_entry_reload(entry_id):
                raise HomeAssistantError("reload_in_progress")

        return handle

    def reload_service_handler(self):
        """Keep the admin handler in the stable module across code generations."""
        async def handle(_call):
            config = self.hass.data.get(DOMAIN, {}).get("config", {})
            requester = self._consumer_requester()
            result = await self.async_hot_reload(
                self.hass, enable_code_reload=config.get("enable_code_reload", False),
                after=requester,
            )
            if requester is not None and result["status"] != "accepted":
                raise HomeAssistantError("reload_code: " + result["reason"])
            try:
                from homeassistant.core import SupportsResponse
            except ImportError:
                if result["status"] not in {"success", "accepted"}:
                    raise HomeAssistantError(
                        "reload_code: " + result["reason"]
                    )
                return None
            return result

        return handle

    @staticmethod
    def _state(entry) -> str:
        state = getattr(entry, "state", None)
        return str(getattr(state, "value", state)).lower()

    def _flows_active(self, config_entries, domain_entry_ids) -> bool:
        return bool(
            config_entries.flow.async_progress_by_handler(
                DOMAIN, include_uninitialized=True
            )
            or any(
                flow["handler"] in domain_entry_ids
                for flow in config_entries.options.async_progress(
                    include_uninitialized=True
                )
            )
        )

    def _refuse(self, reason: str, *, status: str = "failed", **details):
        result = {"status": status, "reason": reason, **details}
        self.last_reload_result = result
        return result

    async def async_hot_reload(
        self, hass: HomeAssistant, enable_code_reload: bool = False,
        *, after: asyncio.Task | None = None,
    ) -> dict[str, Any]:
        """Own the request independently of its waiter or initiating consumer."""
        if not enable_code_reload:
            return self._refuse("hot_reload_disabled")
        if self._reload_task is not None and not self._reload_task.done():
            return self._refuse("reload_already_in_progress")
        if self._stopping or self.entry_ids or self._reload_requests:
            return self._refuse("entry_operation_in_progress")
        if self.context is None or self.context.implementation is None:
            return self._refuse("runtime_not_initialized", status="restart_required")
        entries = {entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)}
        if self._flows_active(hass.config_entries, entries):
            return self._refuse("flow_in_progress")
        self.hass = hass
        task = hass.async_create_task(
            self._async_reload_after_consumer(after) if after is not None
            else self._async_run_hot_reload()
        )
        self._reload_task = task
        task.add_done_callback(
            lambda finished: setattr(self, "_reload_task", None)
            if self._reload_task is finished else None
        )
        if after is not None:
            result = {"status": "accepted", "reason": "reload_requested"}
            self.last_reload_result = result
            return result
        return await asyncio.shield(task)

    async def _async_reload_after_consumer(self, requester):
        await asyncio.wait({requester})
        if self._stopping:
            return self._refuse("entry_operation_in_progress")
        return await self._async_run_hot_reload()

    async def _async_run_hot_reload(self) -> dict[str, Any]:
        async with self._reload_lock:
            loop = asyncio.get_running_loop()
            started = loop.time()
            timings = {}
            if self.context is None or self.context.implementation is None:
                return self._refuse("runtime_not_initialized", status="restart_required")
            if self.entry_ids or self._reload_requests:
                return self._refuse("entry_operation_in_progress")
            config_entries = self.hass.config_entries
            all_entries = config_entries.async_entries(DOMAIN)
            entries = [
                entry for entry in all_entries
                if self.entry_enabled(entry)
                and self._state(entry) in {"loaded", "setup_retry", "setup_error"}
            ]
            domain_entry_ids = {entry.entry_id for entry in all_entries}
            if self._flows_active(config_entries, domain_entry_ids):
                return self._refuse("flow_in_progress")

            try:
                await self.hass.async_add_executor_job(self._compile_sources)
            except (OSError, SyntaxError, ValueError) as error:
                _LOGGER.warning("Reload preflight rejected: %s", type(error).__name__)
                return self._refuse(
                    "source_preflight_failed", status="restart_required"
                )
            timings["preflight_ms"] = round((loop.time() - started) * 1000)

            self.context.admission_open = False
            drain_started = loop.time()
            try:
                await asyncio.wait_for(asyncio.gather(
                    self.context.drain_event.wait(), self._consumers_drained.wait()
                ), timeout=10)
            except asyncio.TimeoutError:
                self.context.admission_open = not self._stopping
                return self._refuse("drain_timeout")
            timings["drain_ms"] = round((loop.time() - drain_started) * 1000)

            await self.async_stop_discovery()
            unloaded = []
            unload_started = loop.time()
            for entry in entries:
                try:
                    ok = await self._async_core_entry_operation(
                        "async_unload", entry.entry_id
                    )
                except Exception as error:
                    _LOGGER.warning("Reload unload failed: %s", type(error).__name__)
                    ok = False
                if not ok or self._state(entry) != "not_loaded":
                    rollback = {}
                    for eid in unloaded:
                        try:
                            rollback[eid] = bool(
                                await self._async_core_entry_operation("async_setup", eid)
                            )
                        except Exception:
                            rollback[eid] = False
                    old_impl, self.context.implementation = self.context.implementation, None
                    if old_impl is not None:
                        await old_impl.async_close()
                    self.context.admission_open = False
                    return self._refuse(
                        "unload_failed", status="restart_required",
                        rollback=rollback, http_status=503, timings_ms=timings,
                    )
                unloaded.append(entry.entry_id)
            timings["unload_ms"] = round((loop.time() - unload_started) * 1000)

            old_implementation = self.context.implementation
            swap_started = loop.time()
            imported = []
            try:
                await self.hass.async_add_executor_job(
                    self._reload_sources, imported, self.conversation_available
                )
            except Exception as error:
                _LOGGER.error("Reload import failed: %s", type(error).__name__)
                self.context.implementation = None
                if old_implementation is not None:
                    await old_implementation.async_close()
                return self._refuse(
                    "import_failed", status="restart_required",
                    phase="import", applied_modules=tuple(imported),
                    timings_ms=timings,
                )
            timings["import_ms"] = round((loop.time() - swap_started) * 1000)

            previous = old_implementation
            self.context.implementation = None
            try:
                if previous is not None:
                    await previous.async_close()
                self.context.implementation = (
                    sys.modules[_ROOT + ".core.stream"].StreamImplementation(self.context)
                )
                await sys.modules[_ROOT].async_rebind_runtime(self.hass)
            except Exception as error:
                _LOGGER.error("Reload rebind failed: %s", type(error).__name__)
                if self.context.implementation is not None:
                    await self.context.implementation.async_close()
                self.context.implementation = None
                return self._refuse(
                    "rebind_failed", status="restart_required", phase="rebind",
                    timings_ms=timings, http_status=503,
                )
            timings["rebind_ms"] = round(
                (loop.time() - swap_started) * 1000 - timings["import_ms"]
            )
            timings["code_swap_ms"] = round((loop.time() - swap_started) * 1000)

            setup_results = {}
            self.generation += 1
            for entry in entries:
                try:
                    ok = await self._async_core_entry_operation(
                        "async_setup", entry.entry_id
                    )
                except Exception as error:
                    _LOGGER.warning("Reload setup failed: %s", type(error).__name__)
                    ok = False
                setup_results[entry.entry_id] = (
                    "loaded" if ok and self._state(entry) == "loaded"
                    else self._state(entry)
                )
            timings["total_ms"] = round((loop.time() - started) * 1000)
            self.context.admission_open = not self._stopping
            status = (
                "success" if all(state == "loaded" for state in setup_results.values())
                else "partial"
            )
            return self._refuse(
                "completed" if status == "success" else "entry_setup_failed",
                status=status, entries=setup_results, timings_ms=timings,
                applied_modules=tuple(imported),
            )

    @staticmethod
    def _reload_sources(imported: list[str], conversation_available: bool) -> None:
        for suffix in _RELOAD_ORDER:
            if suffix == "conversation" and not conversation_available:
                continue
            name = f"{_ROOT}.{suffix}" if suffix else _ROOT
            if name in sys.modules:
                importlib.reload(sys.modules[name])
            else:
                importlib.import_module(name)
            imported.append(name)

def get_lifecycle(hass: HomeAssistant) -> LifecycleManager:
    """One domain owner, including Options opened after a failed entry setup."""
    data = hass.data.setdefault(DOMAIN, {})
    if "lifecycle" not in data:
        data["lifecycle"] = LifecycleManager(StreamContext(hass=hass))
    return data["lifecycle"]

