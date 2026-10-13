import asyncio
import json
import logging
from datetime import datetime

from aiohttp import ClientError, WSMsgType

from .quasar_info import has_quasar
from .yandex_session import CloudRequestError, YandexSession, safe_error_summary
from .policy import (
    AuthState,
    DevicesState,
    EntryPolicy,
    Readiness,
    RecoveryBackoff,
    ScenariosState,
    SendOutcome,
)
from .cache import (
    CacheManager,
    SCENARIO_STATE_CONFIRMED,
    SCENARIO_STATE_PENDING,
    StationSnapshot,
    TOKEN_STATE_VALID,
    normalize_inventory,
)

_LOGGER = logging.getLogger(__name__)
# Runtime-only device fields preserved when an unchanged inventory refresh
# updates existing records in place (RFC v2 §5): entity/session objects and
# locally discovered addresses never come from the cloud catalog.
RUNTIME_DEVICE_FIELDS = (
    "entity", "glagol", "callbacks", "update_task",
    "host", "port", "scenario_id", "hdmi_audio",
)


def _device_fingerprint(device: dict) -> tuple:
    """Membership identity of one device; states, names and hosts excluded."""
    info = device.get("quasar_info") or {}
    caps = tuple(sorted(
        (i.get("type"), (i.get("parameters") or {}).get("instance"))
        for i in device.get("capabilities") or ()
    ))
    props = tuple(sorted(
        (i.get("type"), (i.get("parameters") or {}).get("instance"))
        for i in device.get("properties") or ()
    ))
    return (
        device.get("id"), info.get("device_id"), info.get("platform"),
        device.get("type"), caps, props,
    )


def _inventory_fingerprint(devices) -> tuple:
    return tuple(sorted((_device_fingerprint(d) for d in devices or ()), key=repr))


def _merge_device_records(old_devices: list, new_devices: list) -> None:
    """Update existing dicts in place; runtime fields and identity survive."""
    by_id = {d["id"]: d for d in new_devices}
    for device in old_devices:
        if (new := by_id.get(device["id"])) is None:
            continue
        preserved = {k: device[k] for k in RUNTIME_DEVICE_FIELDS if k in device}
        device.update(new)
        device.update(preserved)

COMMAND_TIMEOUT = 10

IOT_TYPES = {
    "on": "devices.capabilities.on_off",
    "lock": "devices.capabilities.lock",
    "temperature": "devices.capabilities.range",
    "fan_speed": "devices.capabilities.mode",
    "thermostat": "devices.capabilities.mode",
    "program": "devices.capabilities.mode",
    "heat": "devices.capabilities.mode",
    "volume": "devices.capabilities.range",
    "pause": "devices.capabilities.toggle",
    "mute": "devices.capabilities.toggle",
    "channel": "devices.capabilities.range",
    "input_source": "devices.capabilities.mode",
    "brightness": "devices.capabilities.range",
    "color": "devices.capabilities.color_setting",
    "work_speed": "devices.capabilities.mode",
    "humidity": "devices.capabilities.range",
    "ionization": "devices.capabilities.toggle",
    "backlight": "devices.capabilities.toggle",
    # fan
    "oscillation": "devices.capabilities.toggle",
    "controls_locked": "devices.capabilities.toggle",
    # climate
    "swing": "devices.capabilities.mode",
    # kettle:
    "keep_warm": "devices.capabilities.toggle",
    "tea_mode": "devices.capabilities.mode",
    # cover
    "open": "devices.capabilities.range",
    # camera
    "camera_pan": "devices.capabilities.range",
    "camera_tilt": "devices.capabilities.range",
    "get_stream": "devices.capabilities.video_stream",
    # devices.types.remote_car.seat
    "heating_mode": "devices.capabilities.range",
    # devices.types.smart_speaker.yandex.station.orion
    "led_array": "devices.capabilities.led_mask",
    "color_animation": "devices.capabilities.color_animation",
    # don't work
    "hsv": "devices.capabilities.color_setting",
    "rgb": "devices.capabilities.color_setting",
    "scene": "devices.capabilities.color_setting",
    "temperature_k": "devices.capabilities.color_setting",
}

MASK_EN = "0123456789abcdef-"
MASK_RU = "оеаинтсрвлкмдпуяы"


def encode(uid: str) -> str:
    """Кодируем UID в рус. буквы. Яндекс привередливый."""
    return "".join([MASK_RU[MASK_EN.index(s)] if s in MASK_EN else s for s in uid.lower()])


def parse_scenario(data: dict) -> dict:
    result = {
        k: v
        for k, v in data.items()
        if k in ("name", "icon", "steps", "effective_time", "settings")
    }
    result["triggers"] = [parse_trigger(i) for i in data["triggers"]]
    return result


def parse_trigger(data: dict) -> dict:
    result = {k: v for k, v in data.items() if k == "filters"}

    value = data["trigger"]["value"]
    if isinstance(value, dict):
        value = {
            k: v
            for k, v in value.items()
            if k in ("instance", "property_type", "condition")
        }
        value["device_id"] = data["trigger"]["value"]["device"]["id"]

    result["trigger"] = {"type": data["trigger"]["type"], "value": value}
    return result


def parse_device(data: dict) -> dict:
    return {
        "id": data["id"],
        "capabilities": [
            {"type": i["type"], "state": i["state"]} for i in data["capabilities"]
        ],
        "directives": data["directives"],
    }


def scenario_speaker_tts(name: str, trigger: str, device_id: str, text: str) -> dict:
    return {
        "name": name,
        "icon": "home",
        "triggers": [
            {
                "trigger": {"type": "scenario.trigger.voice", "value": trigger},
            }
        ],
        "steps": [
            {
                "type": "scenarios.steps.actions.v2",
                "parameters": {
                    "items": [
                        {
                            "id": device_id,
                            "type": "step.action.item.device",
                            "value": {
                                "id": device_id,
                                "item_type": "device",
                                "capabilities": [
                                    {
                                        "type": "devices.capabilities.quasar",
                                        "state": {
                                            "instance": "tts",
                                            "value": {"text": text},
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            }
        ],
    }


def scenario_speaker_action(
    name: str, trigger: str, device_id: str, action: str
) -> dict:
    return {
        "name": name,
        "icon": "home",
        "triggers": [
            {
                "trigger": {"type": "scenario.trigger.voice", "value": trigger},
            }
        ],
        "steps": [
            {
                "type": "scenarios.steps.actions.v2",
                "parameters": {
                    "items": [
                        {
                            "id": device_id,
                            "type": "step.action.item.device",
                            "value": {
                                "id": device_id,
                                "item_type": "device",
                                "capabilities": [
                                    {
                                        "type": "devices.capabilities.quasar.server_action",
                                        "state": {
                                            "instance": "text_action",
                                            "value": action,
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            }
        ],
    }


class Dispatcher:
    dispatcher: dict[str, list] = None

    def __init__(self):
        self.dispatcher = {}

    def subscribe_update(self, signal: str, target):
        targets = self.dispatcher.setdefault(signal, [])
        if target not in targets:
            targets.append(target)
        return lambda: targets.remove(target)

    def dispatch_update(self, signal: str, message: dict):
        if signal not in self.dispatcher:
            return
        for target in self.dispatcher[signal]:
            target(message)


class YandexQuasar(Dispatcher):
    # all devices
    devices: list[dict] = None
    scenarios: list[dict] = None
    online_updated: asyncio.Event = None
    updates_task: asyncio.Task = None

    def __init__(
        self,
        session: YandexSession,
        policy: EntryPolicy | None = None,
        readiness: Readiness | None = None,
        cache_manager: CacheManager | None = None,
    ):
        super().__init__()
        self.session = session
        self.policy = policy or getattr(session, "policy", None) or EntryPolicy()
        self.readiness = readiness or getattr(session, "readiness", None) or Readiness()
        self.cache_manager = cache_manager
        self.devices = []
        self.scenarios = None
        self.device_tokens = {}
        self.stopping = False
        self.owned_glagol_ids: set[str] = set()
        self.cached_glagol_ids: set[str] = set()
        self.yaml_glagol_hosts: set[str] = set()
        self._device_command_locks = {}
        self._reconciliation_lock = asyncio.Lock()
        self.online_updated = asyncio.Event()
        self.online_updated.set()
        self.updates_task: asyncio.Task | None = None
        self._recovery_task: asyncio.Task | None = None
        self._token_tasks: dict[str, asyncio.Task] = {}
        self._scenario_worker_task: asyncio.Task | None = None
        self._history_tasks: set[asyncio.Task] = set()
        self.fresh_device_ids: set[str] = set()

    def _restore_from_snapshot(self, snapshot: StationSnapshot):
        self.fresh_device_ids.clear()
        self.devices = [dict(d) for d in snapshot.devices]
        self.readiness.devices_state = DevicesState.READY
        self.readiness.inventory_source = "cache"
        self.readiness.last_snapshot_at = snapshot.updated_at
        self.device_tokens = {
            glagol_id: {
                platform: record["token"] for platform, record in tokens.items()
                if record.get("state") == TOKEN_STATE_VALID
            }
            for glagol_id, tokens in snapshot.device_tokens.items()
        }
        if self.session is not None and self.session.account_uid is None:
            self.session.account_uid = snapshot.account_uid
        for dev in self.speakers + self.modules:
            self.cached_glagol_ids.add(dev["quasar_info"]["device_id"])
            did = dev.get("quasar_info", {}).get("device_id")
            if did and did in snapshot.local_config:
                cfg = snapshot.local_config[did]
                if "host" in cfg:
                    dev.setdefault("host", cfg["host"])
                if "port" in cfg:
                    dev.setdefault("port", cfg["port"])
                if "hdmi_audio" in cfg:
                    dev["hdmi_audio"] = cfg["hdmi_audio"]

    async def init(self):
        """Восстанавливает пригодный снимок и сразу возвращается (RFC v2 §5).

        При наличии снимка никаких cookie/CSRF/inventory/scenario/config/token
        запросов: cloud recovery выполняется задачей после публикации сущностей.
        """
        snapshot = None
        if self.cache_manager:
            snapshot = await self.cache_manager.async_load()

        if snapshot is not None:
            # Restore the last-known-good account catalog and local metadata.
            self._restore_from_snapshot(snapshot)
            return

        if self.policy.effective_use_cached_devices:
            reason = self.cache_manager.cache_status if self.cache_manager else "cache_missing"
            _LOGGER.warning("Cached startup unavailable: reason=%s", reason)
            self.readiness.devices_state = DevicesState.UNAVAILABLE
            raise RuntimeError(reason)

        # No snapshot: bounded initial cloud bootstrap, no scenario obligations
        try:
            await asyncio.wait_for(
                self._sync_cloud_devices(allow_reload=False), timeout=15
            )
        except Exception as e:
            _LOGGER.debug("Cloud devices fetch unavailable: %s", safe_error_summary(e))
            self.readiness.devices_state = DevicesState.UNAVAILABLE
            raise

    async def _sync_cloud_devices(self, allow_reload: bool = True) -> bool:
        """Полный cloud GET строит новый нормализованный каталог отдельно.

        Неизменный membership fingerprint: полный снимок сохраняется, существующие
        device dict обновляются на месте, свежие состояния рассылаются через
        dispatcher, reload отсутствует. Изменённый fingerprint: снимок сохраняется
        до всего остального, запрашивается один ordinary reload, старые consumers
        продолжают работать до своего unload. Возвращает True, когда новый каталог
        применён напрямую (bootstrap или reload недоступен).
        """
        _LOGGER.debug("Получение списка устройств из облака.")
        try:
            r = await self.session.get(
                "https://iot.quasar.yandex.ru/m/v3/user/devices", role="quasar"
            )
        except CloudRequestError as error:
            if error.status == 401:
                # A probe/exchange classifies credentials; this status alone cannot.
                # Recovery owns the next inventory attempt, never this response path.
                await self.session.refresh_cookies()
            raise
        resp = await r.json()
        if (
            not isinstance(resp, dict) or resp.get("status") != "ok"
            or not isinstance(resp.get("households"), list)
        ):
            raise RuntimeError("inventory_unavailable")
        devices = []
        for house in resp["households"]:
            if (
                not isinstance(house, dict)
                or not isinstance(house.get("name"), str)
                or not isinstance(house.get("all"), list)
                or any(not isinstance(device, dict) for device in house["all"])
            ):
                raise RuntimeError("inventory_unavailable")
            devices.extend(
                {**device, "house_name": house["name"]} for device in house["all"]
            )
        devices = normalize_inventory(devices)

        old_records = self.devices
        if self.cache_manager is not None and self.cache_manager.snapshot is not None:
            old_records = self.cache_manager.snapshot.devices
        unchanged = _inventory_fingerprint(old_records) == _inventory_fingerprint(devices)

        # снимок сохраняется до любого применения каталога
        if self.cache_manager:
            await self.cache_manager.async_save_devices(
                devices, account_uid=self.session.account_uid
            )
        self.readiness.auth_state = AuthState.VALID
        self.readiness.devices_state = DevicesState.READY
        self.readiness.inventory_source = "cloud"

        if unchanged:
            _merge_device_records(self.devices, devices)
            self.fresh_device_ids = {d["id"] for d in devices}
            for device in self.devices:
                self.dispatch_update(device["id"], device)
            self.dispatch_update("readiness", {})
            return True

        if allow_reload:
            if self._request_inventory_reload():
                _LOGGER.info("Inventory membership changed; ordinary reload requested")
            else:
                # admission закрыт или entry disabled: снимок уже надёжно сохранён,
                # старые consumers продолжают работать до разрешённого reload/setup
                _LOGGER.warning(
                    "Inventory membership changed but reload not admitted; "
                    "keeping current consumers until the next setup"
                )
            self.dispatch_update("readiness", {})
            return False
        # bootstrap (init): новых потребителей ещё нет, применяем напрямую
        self.devices = devices
        self.fresh_device_ids = {d["id"] for d in devices}
        self.dispatch_update("readiness", {})
        return True

    def _request_inventory_reload(self) -> bool:
        cache = self.cache_manager
        hass = getattr(cache, "hass", None) if cache is not None else None
        if hass is None:
            return False
        from .lifecycle import get_lifecycle

        return get_lifecycle(hass).request_entry_reload(cache.entry_id)

    @property
    def speakers(self):
        return [i for i in self.devices if has_quasar(i) and i.get("capabilities")]

    @property
    def modules(self):
        # modules don't have cloud scenarios
        return [i for i in self.devices if has_quasar(i) and not i.get("capabilities")]

    @property
    def owned_speakers(self):
        return [
            device for device in self.speakers
            if device["quasar_info"]["device_id"] in self.owned_glagol_ids
        ]

    @property
    def owned_modules(self):
        return [
            device for device in self.modules
            if device["quasar_info"]["device_id"] in self.owned_glagol_ids
        ]

    @staticmethod
    def _created_scenario_record(sid: str, device_id: str, hash_code: str) -> dict:
        """Runtime-запись созданной пустышки в фактической wire-форме builder."""
        record = scenario_speaker_tts(f"ХА {device_id}", hash_code, device_id, "пустышка")
        record["id"] = sid
        return record


    def _scenario_candidates(self, hash_code: str) -> list[dict]:
        """Кандидаты по типизированному voice trigger и точному hash (§6).

        Проверяются все triggers; поддержаны typed `scenario.trigger.voice`,
        известный flat template и nested `trigger` из wire templates.
        """
        candidates = []
        for scenario in self.scenarios or []:
            if not isinstance(scenario, dict):
                continue
            for trigger in scenario.get("triggers") or []:
                if not isinstance(trigger, dict):
                    continue
                value = None
                if trigger.get("type") == "scenario.trigger.voice":
                    value = trigger.get("value")
                elif "value" in trigger and "type" not in trigger:
                    # известный flat legacy template
                    value = trigger.get("value")
                elif isinstance(trigger.get("trigger"), dict):
                    inner = trigger["trigger"]
                    if inner.get("type") == "scenario.trigger.voice":
                        value = inner.get("value")
                if value == hash_code:
                    candidates.append(scenario)
                    break
        return candidates

    async def _scenario_detail(self, scenario_id: str) -> dict | None:
        """GET `/m/v4/user/scenarios/{id}/edit` для кандидата без полных steps."""
        try:
            r = await self.session.get(
                f"https://iot.quasar.yandex.ru/m/v4/user/scenarios/{scenario_id}/edit",
                role="quasar",
            )
            resp = await r.json()
            if isinstance(resp, dict) and resp.get("status") == "ok":
                return resp.get("scenario")
        except Exception as e:
            _LOGGER.debug(
                "Scenario detail unavailable for %s: %s", scenario_id, type(e).__name__
            )
        return None

    async def _verify_scenario_target(self, scenario: dict, device_id: str) -> bool | None:
        """Полный target кандидата: True/False/None (неполный или неизвестный).

        Wire-форма фактических builders: `step["type"] ==
        "scenarios.steps.actions.v2"` → `parameters.items` →
        `step.action.item.device` с cloud device ID в `item["id"]`/`value["id"]`.
        Target должен быть единственным и нашим: посторонний или дополнительный
        device item не считается совпадением. Имя сценария identity не является.
        """
        steps = scenario.get("steps")
        if not isinstance(steps, list) or not steps:
            scenario = await self._scenario_detail(scenario.get("id"))
            if not isinstance(scenario, dict):
                return None
            steps = scenario.get("steps")
            if not isinstance(steps, list) or not steps:
                return None
        # Собственная пустышка содержит ровно один step и один device item.
        # Дополнительные действия или несовпадающие IDs не доказывают identity.
        if len(steps) != 1 or not isinstance(steps[0], dict):
            return None
        step = steps[0]
        if step.get("type") != "scenarios.steps.actions.v2":
            return None
        parameters = step.get("parameters")
        if not isinstance(parameters, dict):
            return None
        items = parameters.get("items")
        if not isinstance(items, list) or len(items) != 1:
            return None
        item = items[0]
        if not isinstance(item, dict) or item.get("type") != "step.action.item.device":
            return None
        value = item.get("value")
        if not isinstance(value, dict):
            return None
        item_id, value_id = item.get("id"), value.get("id")
        if not isinstance(item_id, str) or not isinstance(value_id, str):
            return None
        if item_id != value_id:
            return False
        return item_id == device_id

    async def _resolve_speaker_scenario(self, speaker: dict, *, allow_creation: bool = True) -> None:
        """Binding только при единственном полном совпадении trigger+target.

        Несколько совпадений, чужой target, неполный candidate/detail или
        неизвестная структура — unresolved и запрет POST для этого устройства,
        а не доказательство отсутствия. Вызывается под reconciliation lock.
        """
        device_id: str = speaker["id"]
        hash_code = encode(device_id)
        speaker.pop("scenario_unresolved", None)

        candidates = self._scenario_candidates(hash_code)
        if candidates:
            verified = [c for c in candidates if await self._verify_scenario_target(c, device_id) is True]
            if len(candidates) == 1 and len(verified) == 1:
                speaker["scenario_id"] = candidates[0]["id"]
                if self.cache_manager:
                    await self.cache_manager.async_save_scenario_marker(
                        device_id, hash_code, SCENARIO_STATE_CONFIRMED
                    )
                return
            speaker["scenario_id"] = None
            speaker["scenario_unresolved"] = True
            _LOGGER.warning(
                "Scenario binding unresolved for %s: candidates=%d verified=%d; POST banned",
                device_id, len(candidates), len(verified),
            )
            return

        speaker["scenario_id"] = None

        if self.cache_manager and not self.cache_manager.scenario_creation_trusted:
            _LOGGER.warning(
                "Scenario creation journal is untrusted on disk for %s; skipping creation",
                device_id,
            )
            return

        journal = {}
        if self.cache_manager and self.cache_manager.snapshot:
            journal = self.cache_manager.snapshot.scenario_creation
        existing = journal.get(device_id)
        existing_state = existing.get("state") if isinstance(existing, dict) else None
        if existing_state in (SCENARIO_STATE_PENDING, SCENARIO_STATE_CONFIRMED):
            _LOGGER.info(
                "Scenario for %s has marker %s; GET-only reconciliation",
                device_id, existing_state,
            )
            return

        if not allow_creation:
            return

        # Без подтверждённой долговременной записи unknown-outcome POST
        # нельзя безопасно восстановить после restart.
        if not self.cache_manager:
            _LOGGER.warning("Scenario marker unavailable for %s; POST banned", device_id)
            return
        if not await self.session.ensure_csrf():
            _LOGGER.warning("CSRF unavailable; scenario creation for %s aborted", device_id)
            return
        verified = await self.cache_manager.async_save_and_verify_scenario_marker(
            device_id, hash_code, SCENARIO_STATE_PENDING
        )
        if not verified:
            _LOGGER.warning(
                "Durable pending marker verification failed on disk for %s; aborting POST",
                device_id,
            )
            return

        try:
            sid = await self.add_scenario(device_id, hash_code)
            speaker["scenario_id"] = sid
            if self.scenarios is not None:
                self.scenarios.append(
                    self._created_scenario_record(sid, device_id, hash_code)
                )
            if self.cache_manager:
                await self.cache_manager.async_save_scenario_marker(
                    device_id, hash_code, SCENARIO_STATE_CONFIRMED
                )
        except Exception as e:
            _LOGGER.warning(
                "Failed to create scenario for %s: %s; pending marker retained",
                device_id, type(e).__name__,
            )
            speaker["scenario_id"] = None

    async def load_speakers(self):
        if self.scenarios is None:
            _LOGGER.warning(
                "Cannot reconcile speaker scenarios: catalog is unpopulated (None)"
            )
            return
        async with self._reconciliation_lock:
            for speaker in self.owned_speakers:
                await self._resolve_speaker_scenario(speaker)
        self.dispatch_update("readiness", {})
    async def retry_scenario_creation(self, device_id: str, *, confirm_risk: bool = False) -> str:
        """Ручное Recovery: одна подтверждённая попытка на устройство (§6).

        Свежий GET и detail resolution под тем же lock. Найден match — привязка
        без POST; конфликт/unavailable — отказ; подтверждённого match нет и
        пользователь принял риск — marker заменяется новой revision и выполняется
        ровно один POST. Промежуточного удаления marker нет.
        """
        speaker = next(
            (s for s in self.owned_speakers if s.get("id") == device_id), None
        )
        if speaker is None:
            raise ValueError("unknown_device")
        if not (self.policy.effective_cloud_allowed and self.policy.enable_scenarios):
            raise RuntimeError("scenarios_disabled")
        async with self._reconciliation_lock:
            await self.load_scenarios()
            if self.readiness.scenarios_state != ScenariosState.READY:
                raise RuntimeError("scenarios_unavailable")
            self.scenarios = self.scenarios if self.scenarios is not None else []
            await self._resolve_speaker_scenario(speaker, allow_creation=False)
            if speaker.get("scenario_id"):
                return "bound"
            if speaker.get("scenario_unresolved"):
                raise RuntimeError("scenario_conflict")

            if not confirm_risk:
                raise RuntimeError("confirm_required")

            if not self.cache_manager:
                raise RuntimeError("marker_unavailable")
            hash_code = encode(device_id)
            if not await self.session.ensure_csrf():
                raise RuntimeError("csrf_unavailable")
            verified = await self.cache_manager.async_save_and_verify_scenario_marker(
                device_id, hash_code, SCENARIO_STATE_PENDING
            )
            if not verified:
                raise RuntimeError("marker_verification_failed")
            try:
                sid = await self.add_scenario(device_id, hash_code)
            except Exception:
                _LOGGER.warning(
                    "Manual scenario creation failed for %s; pending marker retained",
                    device_id,
                )
                raise
            speaker["scenario_id"] = sid
            if self.scenarios is not None:
                self.scenarios.append(
                    self._created_scenario_record(sid, device_id, hash_code)
                )
            if self.cache_manager:
                await self.cache_manager.async_save_scenario_marker(
                    device_id, hash_code, SCENARIO_STATE_CONFIRMED
                )
            return "created"
    async def load_speaker_config(self, device: dict):
        """Загружаем device_id и platform для колонок. Они не приходят с полным
        списком устройств.
        """
        r = await self.session.get(
            f"https://iot.quasar.yandex.ru/m/user/devices/{device['id']}/configuration",
            role="quasar",
        )
        resp = await r.json()
        assert resp["status"] == "ok", resp
        # device_id and platform
        device.update(resp["quasar_info"])

    async def load_scenarios(self):
        """Получает список сценариев; failed GET очищает executable bindings."""
        if not (self.policy.effective_cloud_allowed and self.policy.enable_scenarios):
            self.scenarios = None
            self.readiness.scenarios_state = ScenariosState.DISABLED
            for speaker in self.owned_speakers:
                speaker["scenario_id"] = None
            self.dispatch_update("readiness", {})
            return
        try:
            r = await self.session.get(
                "https://iot.quasar.yandex.ru/m/user/scenarios", role="quasar"
            )
            resp = await r.json()
            assert resp["status"] == "ok", resp
            scenarios = resp["scenarios"]
            if not isinstance(scenarios, list):
                raise RuntimeError("scenarios_unavailable")
            self.scenarios = scenarios
            self.readiness.scenarios_state = ScenariosState.READY
            self.dispatch_update("readiness", {})
        except Exception as e:
            _LOGGER.debug("Scenario catalog unavailable: %s", type(e).__name__)
            # старый список допустим только в diagnostics: bindings очищаются
            self.scenarios = None
            self.readiness.scenarios_state = ScenariosState.UNAVAILABLE
            for speaker in self.owned_speakers:
                speaker["scenario_id"] = None
            self.dispatch_update("readiness", {})
    def _ensure_scenario_worker(self):
        """Отдельный worker: старт при разрешённом cloud/recovery (§6)."""
        if self.stopping or self._scenario_worker_task is not None:
            return
        if not (self.policy.effective_cloud_allowed and self.policy.enable_scenarios):
            self.readiness.scenarios_state = ScenariosState.DISABLED
            return
        self._scenario_worker_task = asyncio.create_task(self._scenario_worker())

    async def _scenario_worker(self):
        """GET по backoff до полного согласования; завершается после него."""
        backoff = RecoveryBackoff()
        current = asyncio.current_task()
        try:
            while not self.stopping:
                await self.load_scenarios()
                state = self.readiness.scenarios_state
                if state == ScenariosState.READY:
                    await self.load_speakers()
                    return
                if state == ScenariosState.DISABLED:
                    return
                delay = backoff.next_delay(self.session.retry_after.get("quasar"))
                _LOGGER.debug("Scenario worker retry in %.1fs", delay)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOGGER.warning("Scenario worker stopped: %s", safe_error_summary(e))
        finally:
            if self._scenario_worker_task is current:
                self._scenario_worker_task = None

    def _schedule_history(self):
        """History задача tracked: disabled/offline отменяет и дожидается (§6)."""
        if self.stopping or not (
            self.policy.effective_cloud_allowed
            and not self.policy.effective_use_cached_devices
        ):
            return
        task = asyncio.create_task(self.get_voice_trigger(3))
        self._history_tasks.add(task)
        task.add_done_callback(self._history_tasks.discard)
    async def update_scenario(self, name: str):
        # check if we known scenario name
        sid = next((i["id"] for i in self.scenarios if i["name"] == name), None)

        if sid is None:
            # reload scenarios list
            await self.load_scenarios()
            sid = next(i["id"] for i in self.scenarios if i["name"] == name)

        # load scenario info
        r = await self.session.get(
            f"https://iot.quasar.yandex.ru/m/v4/user/scenarios/{sid}/edit",
            role="quasar",
        )
        resp = await r.json()
        assert resp["status"] == "ok"

        # convert to scenario patch
        payload = parse_scenario(resp["scenario"])
        r = await self.session.put(
            f"https://iot.quasar.yandex.ru/m/v3/user/scenarios/{sid}", json=payload,
            role="quasar",
        )
        resp = await r.json()
        assert resp["status"] == "ok", resp

    async def add_scenario(self, device_id: str, hash: str) -> str:
        """Добавляет сценарий-пустышку."""
        payload = scenario_speaker_tts("ХА " + device_id, hash, device_id, "пустышка")
        r = await self.session.post(
            "https://iot.quasar.yandex.ru/m/v4/user/scenarios", json=payload,
            role="quasar",
        )
        resp = await r.json()
        assert resp["status"] == "ok", resp
        return resp["scenario_id"]

    async def send(
        self, device: dict, text: str, is_tts: bool = False
    ) -> SendOutcome:
        """Send one PUT/action pair; never replay an uncertain side effect."""
        action_dispatched = False
        phase = "lock_wait"
        reason = "ok"
        entity_id = getattr(device.get("entity"), "entity_id", None)
        operation = "tts" if is_tts else "command"

        async def execute():
            nonlocal action_dispatched, phase, reason
            lock = self._device_command_locks.setdefault(device["id"], asyncio.Lock())
            async with lock:
                phase = "admission"
                sid = device.get("scenario_id")
                if self.stopping or device.get("quasar_info", {}).get(
                    "device_id"
                ) not in self.owned_glagol_ids:
                    reason = "speaker_not_owned"
                elif not self.policy.effective_cloud_allowed:
                    reason = "cloud_blocked_by_policy"
                elif not self.policy.enable_scenarios:
                    reason = "scenarios_disabled"
                elif not sid:
                    reason = "scenario_missing"
                elif self.readiness.scenarios_state != ScenariosState.READY:
                    reason = "scenarios_unavailable"
                else:
                    reason = "ok"
                if reason != "ok":
                    return SendOutcome.UNAVAILABLE
                phase = "csrf"
                if not await self.session.ensure_csrf():
                    reason = "csrf_unavailable"
                    return SendOutcome.UNAVAILABLE
                device_id = device["id"]
                payload = (
                    scenario_speaker_tts("ХА " + device_id, encode(device_id), device_id, text)
                    if is_tts
                    else scenario_speaker_action("ХА " + device_id, encode(device_id), device_id, text)
                )
                phase = "scenario_update"
                response = await self.session.put(
                    f"https://iot.quasar.yandex.ru/m/v4/user/scenarios/{sid}",
                    role="quasar",
                    json=payload,
                )
                body = await response.json()
                if not isinstance(body, dict) or body.get("status") != "ok":
                    reason = "invalid_api_response"
                    return SendOutcome.UNAVAILABLE
                phase = "scenario_action"
                action_dispatched = True
                response = await self.session.post(
                    f"https://iot.quasar.yandex.ru/m/user/scenarios/{sid}/actions",
                    role="quasar",
                )
                body = await response.json()
                if not isinstance(body, dict) or body.get("status") != "ok":
                    reason = "invalid_api_response"
                    return SendOutcome.UNKNOWN_OUTCOME
                return SendOutcome.SENT

        try:
            outcome = await asyncio.wait_for(execute(), COMMAND_TIMEOUT)
        except asyncio.CancelledError:
            if action_dispatched:
                device["last_command_outcome"] = SendOutcome.UNKNOWN_OUTCOME.value
                _LOGGER.warning(
                    "Cloud command: entity=%s operation=%s phase=%s "
                    "outcome=unknown_outcome reason=cancelled",
                    entity_id, operation, phase,
                )
            raise
        except CloudRequestError as error:
            reason = safe_error_summary(error)
            if error.status == 404:
                device.pop("scenario_id", None)
                self.scenarios = None
                self.readiness.scenarios_state = ScenariosState.UNAVAILABLE
                self.dispatch_update("readiness", {})
            outcome = (
                SendOutcome.UNKNOWN_OUTCOME if action_dispatched else SendOutcome.UNAVAILABLE
            )
        except (ClientError, OSError, asyncio.TimeoutError, ValueError, RuntimeError) as error:
            reason = safe_error_summary(error)
            before_dispatch = isinstance(error, (ValueError, RuntimeError)) and str(error) in (
                "csrf_unavailable", "cloud_not_published", "cloud_blocked_by_policy",
                "invalid_endpoint", "untrusted_credential_destination",
            )
            outcome = (
                SendOutcome.UNKNOWN_OUTCOME
                if action_dispatched and not before_dispatch
                else SendOutcome.UNAVAILABLE
            )
        previous = device.get("last_command_outcome")
        device["last_command_outcome"] = outcome.value
        if outcome != SendOutcome.SENT:
            log = _LOGGER.warning if previous != outcome.value else _LOGGER.debug
            log(
                "Cloud command: entity=%s operation=%s phase=%s outcome=%s reason=%s",
                entity_id, operation, phase, outcome.value, reason,
            )
        elif previous and previous != SendOutcome.SENT.value:
            _LOGGER.info(
                "Cloud command recovered: entity=%s operation=%s", entity_id, operation
            )
        return outcome

    async def load_local_speakers(self):
        """Загружает список локальных колонок. Не используется."""
        try:
            r = await self.session.request_glagol(
                "https://quasar.yandex.net/glagol/device_list", role="glagol_token"
            )
            resp = await r.json()
            return [
                {"device_id": d["id"], "name": d["name"], "platform": d["platform"]}
                for d in resp["devices"]
            ]

        except:
            _LOGGER.exception("Load local speakers")
            return None

    async def get_device_config(self, device: dict) -> (dict, str):
        did = device["id"]
        r = await self.session.get(
            f"https://iot.quasar.yandex.ru/m/v2/user/devices/{did}/configuration",
            role="quasar",
        )
        resp = await r.json()
        assert resp["status"] == "ok", resp
        return resp["quasar_config"], resp["quasar_config_version"]

    async def set_device_config(self, device: dict, config: dict, version: str):
        _LOGGER.debug(f"Меняем конфиг станции: {config}")

        did = device["id"]
        r = await self.session.post(
            f"https://iot.quasar.yandex.ru/m/v3/user/devices/{did}/configuration/quasar",
            json={"config": config, "version": version},
            role="quasar",
        )
        resp = await r.json()
        assert resp["status"] == "ok", resp

    async def get_device(self, device: dict):
        r = await self.session.get(
            f"https://iot.quasar.yandex.ru/m/user/{device['item_type']}s/{device['id']}",
            role="quasar",
        )
        resp = await r.json()
        assert resp["status"] == "ok", resp
        return resp

    async def device_action(self, device: dict, instance: str, value, relative=False):
        action = {
            "state": {"instance": instance, "value": value},
            "type": IOT_TYPES.get(instance, "devices.capabilities.custom.button"),
        }

        if relative:
            action["state"]["relative"] = True

        r = await self.session.post(
            f"https://iot.quasar.yandex.ru/m/user/{device['item_type']}s/{device['id']}/actions",
            json={"actions": [action]},
            role="quasar",
        )
        resp = await r.json()
        assert resp["status"] == "ok", resp

        await asyncio.sleep(1)

        device = await self.get_device(device)
        self.dispatch_update(device["id"], device)

    async def get_device_action(self, device: dict, instance: str, value) -> list[dict]:
        _LOGGER.debug(f"Device action: {instance}={value}")

        action = {
            "state": {"instance": instance, "value": value},
            "type": IOT_TYPES[instance],
        }

        url = f"https://iot.quasar.yandex.ru/m/user/{device['item_type']}s/{device['id']}/actions"
        r = await self.session.post(url, role="quasar", json={"actions": [action]})
        resp = await r.json()
        assert resp["status"] == "ok", resp

        return resp["devices"]

    async def device_actions(self, device: dict, **kwargs):
        _LOGGER.debug(f"Device action: {kwargs}")

        actions = []
        for k, v in kwargs.items():
            type_ = (
                "devices.capabilities.custom.button" if k.isdecimal() else IOT_TYPES[k]
            )
            state = (
                {"instance": k, "value": v, "relative": True}
                if k in ("volume", "channel")
                else {"instance": k, "value": v}
            )
            actions.append({"type": type_, "state": state})

        r = await self.session.post(
            f"https://iot.quasar.yandex.ru/m/user/{device['item_type']}s/{device['id']}/actions",
            json={"actions": actions},
            role="quasar",
        )
        resp = await r.json()
        assert resp["status"] == "ok", resp

        # update device state
        device = await self.get_device(device)
        self.dispatch_update(device["id"], device)

    async def device_color(self, device: dict, **kwargs):
        _LOGGER.debug(f"Device color: {kwargs}")

        r = await self.session.post(
            f"https://iot.quasar.yandex.ru/m/v3/user/custom/group/color/apply",
            json={"device_ids": [device["id"]], **kwargs},
            role="quasar",
        )
        resp = await r.json()
        assert resp["status"] == "ok", resp

        # update device state
        device = await self.get_device(device)
        self.dispatch_update(device["id"], device)

    async def update_online_stats(self):
        if not self.policy.effective_cloud_allowed:
            return

        if not self.online_updated.is_set():
            await self.online_updated.wait()
            return

        self.online_updated.clear()

        # _LOGGER.debug(f"Update speakers online status")

        try:
            r = await self.session.get(
                "https://quasar.yandex.ru/devices_online_stats", role="account"
            )
            resp = await r.json()
            assert resp["status"] == "ok", resp
            for speaker in resp["items"]:
                for device in self.devices:
                    if (
                        "quasar_info" not in device
                        or device["quasar_info"]["device_id"] != speaker["id"]
                    ):
                        continue
                    device["online"] = speaker["online"]
                    entity = device.get("entity")
                    if (
                        entity is not None
                        and getattr(entity, "local_state", False) is None
                    ):
                        entity._attr_available = bool(speaker["online"])
                        if getattr(entity, "hass", None):
                            entity.async_write_ha_state()
                    break
        except:
            return
        finally:
            self.online_updated.set()

    async def connect(self):
        if self.policy.effective_use_cached_devices or not self.policy.effective_cloud_allowed:
            _LOGGER.debug("Skipping cloud connect() due to cache-only/offline policy")
            return

        r = await self.session.get(
            "https://iot.quasar.yandex.ru/m/v3/user/devices", role="quasar"
        )
        resp = await r.json()
        assert resp["status"] == "ok", resp

        for house in resp["households"]:
            if "sharing_info" in house:
                continue
            for device in house["all"]:
                self.dispatch_update(device["id"], device)

        ws = await self.session.ws_connect(
            resp["updates_url"], role="cloud_ws", heartbeat=60
        )
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            resp = msg.json()
            # "ping", "update_scenario_list"
            operation = resp.get("operation")
            if operation == "update_states":
                try:
                    resp = json.loads(resp["message"])
                    for device in resp["updated_devices"]:
                        self.dispatch_update(device["id"], device)
                except Exception as e:
                    _LOGGER.debug(f"Parse quasar update error: {msg.data}", exc_info=e)

            elif operation == "update_scenario_list":
                if '"source":"create_scenario_launch"' in resp["message"]:
                    self._schedule_history()

    async def devices_passive_update(self, *args):
        if self.policy.effective_use_cached_devices or not self.policy.effective_cloud_allowed:
            _LOGGER.debug("Skipping devices_passive_update due to cache-only/offline policy")
            return

        try:
            r = await self.session.get(
                "https://iot.quasar.yandex.ru/m/v3/user/devices", role="quasar"
            )
            resp = await r.json()
            assert resp["status"] == "ok", resp

            for house in resp["households"]:
                if "sharing_info" in house:
                    continue
                for device in house["all"]:
                    self.dispatch_update(device["id"], device)
        except Exception as e:
            _LOGGER.debug(f"Devices forceupdate problem: {repr(e)}")

    async def get_voice_trigger(self, retries: int = 0):
        if self.stopping or not (
            self.policy.effective_cloud_allowed
            and not self.policy.effective_use_cached_devices
        ):
            return
        try:
            # 1. Get all scenarios history
            r = await self.session.get(
                "https://iot.quasar.yandex.ru/m/user/scenarios/history", role="quasar"
            )
            raw = await r.json()

            # 2. Search latest scenario with voice trigger
            for scenario in raw["scenarios"]:
                if scenario["trigger_type"] == "scenario.trigger.voice":
                    break
            else:
                return

            # 3. Check if scenario too old
            d1 = datetime.strptime(r.headers["Date"], "%a, %d %b %Y %H:%M:%S %Z")
            d2 = datetime.strptime(scenario["launch_time"], "%Y-%m-%dT%H:%M:%SZ")
            dt = (d1 - d2).total_seconds()
            if dt > 5:
                # try to get history once more
                if retries:
                    # 3 - 0.5s, 2 - 1.5s, 1 - 2.5s, 0 - skip
                    await asyncio.sleep(3.5 - retries)
                    await self.get_voice_trigger(retries - 1)
                return

            # 4. Get speakers from launch devices
            r = await self.session.get(
                f"https://iot.quasar.yandex.ru/m/v4/user/scenarios/launches/{scenario['id']}",
                role="quasar",
            )
            raw = await r.json()

            for step in raw["launch"]["steps"]:
                for item in step["parameters"]["items"]:
                    if item["type"] != "step.action.item.device":
                        continue
                    device = item["value"]
                    # 5. Check if speaker device
                    if "quasar_info" not in device:
                        continue
                    device["scenario_name"] = raw["launch"]["name"]
                    self.dispatch_update(device["id"], device)

        except Exception as e:
            _LOGGER.debug("Can't get voice scenario", exc_info=e)

    async def run_forever(self):
        backoff = RecoveryBackoff()
        while not self.session.closed:
            try:
                await self.connect()
            except Exception as error:
                reason = safe_error_summary(error)
            else:
                backoff.reset()
                reason = "connection_closed"
            retry_after = (
                self.session.retry_after.get("cloud_ws")
                or self.session.retry_after.get("quasar")
            )
            delay = backoff.next_delay(retry_after)
            _LOGGER.debug(
                "Cloud updates reconnect: reason=%s retry_in=%.1fs", reason, delay
            )
            await asyncio.sleep(delay)

    def start(self):
        """Запускает recovery после публикации; WS-подписку — только по policy."""
        if not self.policy.effective_cloud_allowed or self.stopping:
            return
        if self._recovery_task is None:
            self._recovery_task = asyncio.create_task(
                self._async_recover_after_publication()
            )
        if self.policy.effective_cloud_updates and self.updates_task is None:
            self.updates_task = asyncio.create_task(self.run_forever())

    async def _async_recover_after_publication(self):
        """Один проход cloud-read/scenarios/token-fetch после публикации (§5).

        Никаких ожиданий чужих unload: при изменённом membership ordinary reload
        уже запрошен, задача завершается без подмены старых consumers.
        """
        try:
            if not self.policy.effective_use_cached_devices:
                if not await self._sync_cloud_devices():
                    return
            if self.stopping:
                return
            # сценарный worker: свой backoff, завершается после полного согласования
            self._ensure_scenario_worker()
            if any(
                getattr(s.get("entity"), "local_state", False) is None
                for s in self.owned_speakers
            ):
                await self.update_online_stats()
            if self.stopping:
                return
            # single-flight token-fetch только для колонок без известного токена
            for speaker in self.owned_speakers + self.owned_modules:
                info = speaker["quasar_info"]
                if info["device_id"] in self.device_tokens:
                    continue
                token = await self.async_fetch_device_token(speaker)
                if self.stopping:
                    return
                entity = speaker.get("entity")
                if token and entity is not None and getattr(entity, "hass", None):
                    try:
                        await entity.init_local_mode()
                    except Exception as e:
                        _LOGGER.debug(
                            "Local reconnect after token fetch failed: %s",
                            type(e).__name__,
                        )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOGGER.warning(
                "Recovery after publication failed: %s", safe_error_summary(e)
            )

    def async_fetch_device_token(self, device: dict) -> asyncio.Task:
        """Single-flight token fetch per Glagol device (RFC v2 §5)."""
        did = device["quasar_info"]["device_id"]
        task = self._token_tasks.get(did)
        if task is not None and not task.done():
            return task
        task = asyncio.create_task(self._async_fetch_device_token(device))
        self._token_tasks[did] = task
        task.add_done_callback(lambda _t, did=did: self._token_tasks.pop(did, None))
        return task

    async def _async_fetch_device_token(self, device: dict) -> str | None:
        info = device["quasar_info"]
        did, platform = info["device_id"], info["platform"]
        if not self.policy.effective_cloud_allowed:
            _LOGGER.debug("Токен %s не запрашивается: cloud заблокирован policy", did)
            return None
        r = await self.session.request_glagol(
            "https://quasar.yandex.net/glagol/token",
            role="glagol_token",
            params={"device_id": did, "platform": platform},
        )
        # @dext0r: fix bug with wrong content-type
        resp = json.loads(await r.text())
        token = resp.get("token") if isinstance(resp, dict) else None
        if not isinstance(token, str) or not token or resp.get("status") != "ok":
            raise RuntimeError("device_token_unavailable")
        if self.cache_manager is not None:
            await self.cache_manager.async_save_device_token(did, platform, token)
        self.device_tokens.setdefault(did, {})[platform] = token
        return token

    async def async_stop(self):
        """Cancel and await background tasks and Glagol connections (RFC v2 §2.3, §7.3)."""
        self.stopping = True
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            try:
                await self._recovery_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recovery_task = None
        token_tasks = list(self._token_tasks.values())
        for task in token_tasks:
            task.cancel()
        if token_tasks:
            await asyncio.gather(*token_tasks, return_exceptions=True)
        self._token_tasks.clear()
        if self._scenario_worker_task is not None:
            self._scenario_worker_task.cancel()
            try:
                await self._scenario_worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._scenario_worker_task = None
        history_tasks = tuple(self._history_tasks)
        for task in history_tasks:
            task.cancel()
        if history_tasks:
            await asyncio.gather(*history_tasks, return_exceptions=True)
        self._history_tasks.clear()
        if self.updates_task:
            self.updates_task.cancel()
            try:
                await self.updates_task
            except (asyncio.CancelledError, Exception):
                pass
            self.updates_task = None

        for speaker in self.speakers + self.modules:
            entity = speaker.get("entity")
            if entity and getattr(entity, "glagol", None):
                try:
                    await entity.glagol.stop()
                except Exception:
                    pass

        await self.session.async_stop()
        if self.cache_manager:
            await self.cache_manager.async_close()
        self.dispatcher.clear()

    async def set_account_config(self, key: str, value):
        kv = ACCOUNT_CONFIG.get(key)
        assert kv and value in kv["values"], f"{key}={value}"

        if kv.get("api") == "user/settings":
            # https://iot.quasar.yandex.ru/m/user/settings
            r = await self.session.post(
                f"https://iot.quasar.yandex.ru/m/user/settings",
                json={kv["key"]: kv["values"][value]},
                role="quasar",
            )

        else:
            r = await self.session.get(
                "https://quasar.yandex.ru/get_account_config", role="account"
            )
            resp = await r.json()
            assert resp["status"] == "ok", resp

            payload: dict = resp["config"]
            payload[kv["key"]] = kv["values"][value]

            r = await self.session.post(
                "https://quasar.yandex.ru/set_account_config", json=payload,
                role="account",
            )

        resp = await r.json()
        assert resp["status"] == "ok", resp

    async def get_alarms(self, device: dict):
        r = await self.session.post(
            "https://rpc.alice.yandex.ru/gproxy/get_alarms",
            json={"device_ids": [device["quasar_info"]["device_id"]]},
            headers=ALARM_HEADERS,
            role="rpc",
        )
        resp = await r.json()
        return resp["alarms"]

    async def create_alarm(self, device: dict, alarm: dict) -> bool:
        alarm["device_id"] = device["quasar_info"]["device_id"]
        resp = await self.session.post(
            "https://rpc.alice.yandex.ru/gproxy/create_alarm",
            json={"alarm": alarm, "device_type": device["type"]},
            headers=ALARM_HEADERS,
            role="rpc",
        )
        return resp.ok

    async def change_alarm(self, device: dict, alarm: dict) -> bool:
        alarm["device_id"] = device["quasar_info"]["device_id"]
        resp = await self.session.post(
            "https://rpc.alice.yandex.ru/gproxy/change_alarm",
            json={"alarm": alarm, "device_type": device["type"]},
            headers=ALARM_HEADERS,
            role="rpc",
        )
        return resp.ok

    async def cancel_alarms(self, device: dict, alarm_id: str) -> bool:
        resp = await self.session.post(
            "https://rpc.alice.yandex.ru/gproxy/cancel_alarms",
            json={
                "device_alarm_ids": [
                    {
                        "alarm_id": alarm_id,
                        "device_id": device["quasar_info"]["device_id"],
                    }
                ],
            },
            headers=ALARM_HEADERS,
            role="rpc",
        )
        return resp.ok


ALARM_HEADERS = {
    "accept": "application/json",
    "origin": "https://yandex.ru",
    "x-ya-app-type": "iot-app",
    "x-ya-application": '{"app_id":"unknown","uuid":"unknown","lang":"ru"}',
}


BOOL_CONFIG = {"да": True, "нет": False}
ACCOUNT_CONFIG = {
    "без лишних слов": {
        "api": "user/settings",
        "key": "iot",
        "values": {
            "да": {"response_reaction_type": "sound"},
            "нет": {"response_reaction_type": "nlg"},
        },
    },
    "ответить шепотом": {
        "api": "user/settings",
        "key": "tts_whisper",
        "values": BOOL_CONFIG,
    },
    "анонсировать треки": {
        "api": "user/settings",
        "key": "music",
        "values": {
            "да": {"announce_tracks": True},
            "нет": {"announce_tracks": False},
        },
    },
    "скрывать названия товаров": {
        "api": "user/settings",
        "key": "order",
        "values": {
            "да": {"hide_item_names": True},
            "нет": {"hide_item_names": False},
        },
    },
    "звук активации": {"key": "jingle", "values": BOOL_CONFIG},  # /get_account_config
    "одним устройством": {
        "key": "smartActivation",  # /get_account_config
        "values": BOOL_CONFIG,
    },
    "понимать детей": {
        "key": "useBiometryChildScoring",  # /get_account_config
        "values": BOOL_CONFIG,
    },
    "рассказывать о навыках": {
        "key": "aliceProactivity",  # /get_account_config
        "values": BOOL_CONFIG,
    },
    "адаптивная громкость": {
        "key": "aliceAdaptiveVolume",  # /get_account_config
        "values": {
            "да": {"enabled": True},
            "нет": {"enabled": False},
        },
    },
    "кроссфейд": {
        "key": "audio_player",  # /get_account_config
        "values": {
            "да": {"crossfadeEnabled": True},
            "нет": {"crossfadeEnabled": False},
        },
    },
    "взрослый голос": {
        "key": "contentAccess",  # /get_account_config
        "values": {
            "умеренный": "medium",
            "семейный": "children",
            "безопасный": "safe",
            "без ограничений": "without",
        },
    },
    "детский голос": {
        "key": "childContentAccess",  # /get_account_config
        "values": {
            "безопасный": "safe",
            "семейный": "children",
        },
    },
    "имя": {
        "key": "spotter",  # /get_account_config
        "values": {
            "алиса": "alisa",
            "яндекс": "yandex",
        },
    },
}
