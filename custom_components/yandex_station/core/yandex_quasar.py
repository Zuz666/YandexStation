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
)

_LOGGER = logging.getLogger(__name__)
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
        self._device_command_locks = {}
        self._reconciliation_lock = asyncio.Lock()
        self.online_updated = asyncio.Event()
        self.online_updated.set()

    def _restore_from_snapshot(self, snapshot: StationSnapshot):
        self.devices = [dict(d) for d in snapshot.devices]
        self.readiness.devices_state = DevicesState.READY
        self.readiness.inventory_source = "cache"
        self.readiness.last_snapshot_at = snapshot.updated_at
        for glagol_id, token_data in snapshot.device_tokens.items():
            if isinstance(token_data, dict) and token_data.get("state") != "invalid":
                self.device_tokens[glagol_id] = token_data.get("token")
        for dev in self.speakers + self.modules:
            did = dev.get("quasar_info", {}).get("device_id")
            if did and did in snapshot.local_config:
                cfg = snapshot.local_config[did]
                if "host" in cfg:
                    dev.setdefault("host", cfg["host"])
                if "port" in cfg:
                    dev.setdefault("port", cfg["port"])

    async def init(self):
        """Основная функция. Возвращает список колонок."""
        snapshot = None
        if self.cache_manager:
            snapshot = await self.cache_manager.async_load()

        if snapshot and snapshot.devices:
            # Cache-first: publish local entities immediately before network (RFC v2 §3.1)
            self._restore_from_snapshot(snapshot)
            if self.policy.effective_cloud_allowed and not self.policy.effective_use_cached_devices:
                try:
                    await self._sync_cloud_devices()
                except Exception as e:
                    _LOGGER.debug(
                        "Initial cloud sync unavailable, using cache: %s",
                        safe_error_summary(e),
                    )
            return

        if self.policy.effective_use_cached_devices:
            _LOGGER.warning("Cache is missing or empty, cannot start in cached mode")
            self.readiness.devices_state = DevicesState.UNAVAILABLE
            raise RuntimeError("cache_missing")

        # No snapshot: initial cloud bootstrap required
        try:
            await asyncio.wait_for(self._sync_cloud_devices(), timeout=15)
        except Exception as e:
            _LOGGER.debug("Cloud devices fetch unavailable: %s", safe_error_summary(e))
            self.readiness.devices_state = DevicesState.UNAVAILABLE
            raise

    async def _sync_cloud_devices(self):
        """Sync devices catalog and scenarios from Yandex cloud."""
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
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise RuntimeError("inventory_unavailable")

        self.devices = []
        for house in resp["households"]:
            self.devices.extend(
                {**device, "house_name": house["name"]} for device in house["all"]
            )
        self.readiness.auth_state = AuthState.VALID
        self.readiness.devices_state = DevicesState.READY
        self.readiness.inventory_source = "cloud"

        if self.cache_manager:
            await self.cache_manager.async_save_devices(self.devices)

        if self.policy.enable_scenarios and self.policy.effective_cloud_allowed:
            await self.load_scenarios()
            if self.readiness.scenarios_state == ScenariosState.READY:
                await self.load_speakers()
            else:
                _LOGGER.warning(
                    "Scenarios catalog is %s; skipping speaker scenario reconciliation",
                    self.readiness.scenarios_state,
                )
        else:
            self.readiness.scenarios_state = ScenariosState.DISABLED

        self.dispatch_update("readiness", {})
    @property
    def speakers(self):
        return [i for i in self.devices if has_quasar(i) and i.get("capabilities")]

    @property
    def modules(self):
        # modules don't have cloud scenarios
        return [i for i in self.devices if has_quasar(i) and not i.get("capabilities")]

    async def load_speakers(self):
        if self.scenarios is None:
            _LOGGER.warning(
                "Cannot reconcile speaker scenarios: catalog is unpopulated (None)"
            )
            return

        async with self._reconciliation_lock:
            hashes = {}
            if self.scenarios:
                for scenario in self.scenarios:
                    try:
                        h = scenario["triggers"][0]["value"]
                        hashes[h] = scenario["id"]
                    except Exception:
                        pass

            creation_journal = {}
            if self.cache_manager and self.cache_manager.snapshot:
                creation_journal = self.cache_manager.snapshot.scenario_creation

            for speaker in self.speakers:
                device_id: str = speaker["id"]
                hash_code = encode(device_id)

                if hash_code in hashes:
                    speaker["scenario_id"] = hashes[hash_code]
                    if self.cache_manager:
                        await self.cache_manager.async_save_scenario_marker(
                            device_id, hash_code, SCENARIO_STATE_CONFIRMED
                        )
                else:
                    if self.cache_manager and getattr(self.cache_manager, "is_untrusted", False) is True:
                        _LOGGER.warning(
                            "Scenario creation journal is untrusted on disk for %s; skipping creation",
                            device_id,
                        )
                        speaker["scenario_id"] = None
                        continue

                    existing_entry = creation_journal.get(device_id)
                    existing_state = (
                        existing_entry.get("state")
                        if isinstance(existing_entry, dict)
                        else None
                    )

                    if existing_state in (SCENARIO_STATE_PENDING, SCENARIO_STATE_CONFIRMED):
                        _LOGGER.info(
                            "Scenario for %s has marker %s; skipping duplicate POST",
                            device_id,
                            existing_state,
                        )
                        if not speaker.get("scenario_id"):
                            speaker["scenario_id"] = None
                    else:
                        if self.cache_manager:
                            verified = await self.cache_manager.async_save_and_verify_scenario_marker(
                                device_id, hash_code, SCENARIO_STATE_PENDING
                            )
                            if not verified:
                                _LOGGER.warning(
                                    "Durable pending marker verification failed on disk for %s; aborting POST",
                                    device_id,
                                )
                                speaker["scenario_id"] = None
                                continue

                        try:
                            sid = await self.add_scenario(device_id, hash_code)
                            speaker["scenario_id"] = sid
                            if self.scenarios is not None:
                                self.scenarios.append(
                                    {"id": sid, "name": f"ХА {device_id}", "triggers": [{"value": hash_code}]}
                                )
                            if self.cache_manager:
                                await self.cache_manager.async_save_scenario_marker(
                                    device_id, hash_code, SCENARIO_STATE_CONFIRMED
                                )
                        except Exception as e:
                            _LOGGER.warning(
                                "Failed to create scenario for %s: %r; pending marker retained",
                                device_id,
                                e,
                            )
                            speaker["scenario_id"] = None
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
        """Получает список сценариев, которые мы ранее создали."""
        try:
            r = await self.session.get(
                "https://iot.quasar.yandex.ru/m/user/scenarios", role="quasar"
            )
            resp = await r.json()
            assert resp["status"] == "ok", resp
            self.scenarios = resp["scenarios"]
            self.readiness.scenarios_state = ScenariosState.READY
        except Exception as e:
            _LOGGER.debug(f"Ошибка загрузки сценариев: {e!r}")
            self.readiness.scenarios_state = ScenariosState.UNAVAILABLE
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
                if not self.policy.effective_cloud_allowed:
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
            r = await self.session.get(
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
        except:
            return
        finally:
            self.online_updated.set()

        for speaker in resp["items"]:
            for device in self.devices:
                if (
                    "quasar_info" not in device
                    or device["quasar_info"]["device_id"] != speaker["id"]
                ):
                    continue
                device["online"] = speaker["online"]
                break

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
                    _ = asyncio.create_task(self.get_voice_trigger(3))

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
        self.updates_task = asyncio.create_task(self.run_forever())

    async def async_stop(self):
        """Cancel and await background tasks and Glagol connections (RFC v2 §2.3, §7.3)."""
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
