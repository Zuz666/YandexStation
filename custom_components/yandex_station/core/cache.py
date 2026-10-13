"""Snapshot and cache storage seam (RFC v2 §3.2, §3.3).

Narrow module hiding validation and saving of the last-known-good snapshot
for one config entry. The envelope (version, minor_version, key) belongs to
homeassistant.helpers.storage.Store; this module owns only the payload.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1

# scenario creation journal states (RFC v2 §3.2, §5.2)
SCENARIO_STATE_PENDING = "pending_unknown"
SCENARIO_STATE_CONFIRMED = "confirmed"
SCENARIO_STATES = frozenset((SCENARIO_STATE_PENDING, SCENARIO_STATE_CONFIRMED))

# device token invalidation states
TOKEN_STATE_VALID = "valid"
TOKEN_STATE_INVALID = "invalid"

# runtime-only fields of a device record, never persisted (RFC v2 §3.2):
# entity/session/task/callbacks are live objects, host/port belong to
# local_config, online is a temporary state, updates_url and the bound
# scenario_id are valid for the current runtime only
_DEVICE_RUNTIME_KEYS = frozenset(
    (
        "entity",
        "glagol",
        "session",
        "hass",
        "task",
        "update_task",
        "callbacks",
        "update_callbacks",
        "online",
        "host",
        "port",
        "updates_url",
        "scenario_id",
        "hdmi_audio",
    )
)


class _Drop:
    """Sentinel for values that can not be serialized."""

    __slots__ = ()


_DROP = _Drop()


def _clean_json(value):
    """Return a JSON-compatible deep copy, or _DROP for non-JSON values."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            item = _clean_json(item)
            if item is not _DROP:
                cleaned[key] = item
        return cleaned
    if isinstance(value, (list, tuple)):
        cleaned = []
        for item in value:
            item = _clean_json(item)
            if item is not _DROP:
                cleaned.append(item)
        return cleaned
    return _DROP


def _clean_device(device) -> dict | None:
    """Keep usable inventory metadata, never runtime objects or availability."""
    if not isinstance(device, dict):
        return None
    if (
        not isinstance(device.get("id"), str)
        or not device["id"]
        or not isinstance(device.get("name"), str)
        or not isinstance(device.get("type"), str)
        or not device["type"]
        or not isinstance(device.get("capabilities"), list)
        or ("properties" in device and not isinstance(device["properties"], list))
    ):
        return None
    if "quasar_info" in device or device["type"].startswith(
        "devices.types.smart_speaker.yandex."
    ):
        info = device.get("quasar_info")
        if not isinstance(info, dict) or any(
            not isinstance(info.get(key), str) or not info[key]
            for key in ("device_id", "platform")
        ):
            return None
    cleaned = _clean_json(device)
    for key in _DEVICE_RUNTIME_KEYS:
        cleaned.pop(key, None)
    return cleaned


def normalize_inventory(devices: list) -> list[dict]:
    """Reject a partial cloud catalog instead of replacing last-known-good data."""
    if not isinstance(devices, list):
        raise ValueError("inventory_unavailable")
    cleaned = []
    identifiers = set()
    for value in devices:
        device = _clean_device(value)
        if device is None or device["id"] in identifiers:
            raise ValueError("inventory_unavailable")
        identifiers.add(device["id"])
        cleaned.append(device)
    return cleaned


def _device_platforms(devices: list[dict]) -> dict[str, set[str]]:
    platforms = {}
    for device in devices:
        if info := device.get("quasar_info"):
            platforms.setdefault(info["device_id"], set()).add(info["platform"])
    return platforms


def _clean_local_config_entry(entry) -> dict | None:
    """Clean local address and known HDMI configuration by Glagol ID."""
    if not isinstance(entry, dict):
        return None
    cleaned = {}
    host = entry.get("host")
    if isinstance(host, str):
        cleaned["host"] = host
    port = entry.get("port")
    if not isinstance(port, bool) and isinstance(port, int):
        cleaned["port"] = port
    hdmi_audio = entry.get("hdmi_audio")
    if isinstance(hdmi_audio, bool):
        cleaned["hdmi_audio"] = hdmi_audio
    return cleaned or None


def _clean_token_entry(entry) -> dict | None:
    """Clean one device_tokens record (token, obtained_at, state)."""
    if not isinstance(entry, dict):
        return None
    token = entry.get("token")
    state = entry.get("state")
    if not isinstance(token, str) or not token or not isinstance(state, str) or not state:
        return None
    obtained_at = entry.get("obtained_at")
    if obtained_at is not None and not isinstance(obtained_at, str):
        return None
    return {"token": token, "obtained_at": obtained_at, "state": state}


def _clean_tokens(section, devices: list[dict]) -> dict[str, dict]:
    """Normalize token tuples; flat legacy tokens need an unambiguous platform."""
    if not isinstance(section, dict):
        return {}
    cleaned = {}
    device_platforms = _device_platforms(devices)
    for glagol_id, records in section.items():
        if not isinstance(glagol_id, str) or not glagol_id or not isinstance(records, dict):
            continue
        if isinstance(records.get("token"), str):
            platforms = device_platforms.get(glagol_id, ())
            if len(platforms) != 1:
                continue
            records = {next(iter(platforms)): records}
        tokens = {
            platform: record
            for platform, value in records.items()
            if isinstance(platform, str) and platform
            and (record := _clean_token_entry(value)) is not None
        }
        if tokens:
            cleaned[glagol_id] = tokens
    return cleaned


def _clean_scenario_entry(entry) -> dict | None:
    """Clean one scenario_creation record (trigger, state, updated_at)."""
    if not isinstance(entry, dict):
        return None
    trigger = entry.get("trigger")
    state = entry.get("state")
    if not isinstance(trigger, str) or not trigger or state not in SCENARIO_STATES:
        return None
    updated_at = entry.get("updated_at")
    if updated_at is not None and not isinstance(updated_at, str):
        return None
    return {"trigger": trigger, "state": state, "updated_at": updated_at}


def _clean_section(section, clean_entry, section_name: str) -> dict | None:
    """Clean a keyed section; None when the section itself has a wrong type."""
    if not isinstance(section, dict):
        return None
    cleaned = {}
    for key, entry in section.items():
        if not isinstance(key, str) or not key:
            continue
        entry = clean_entry(entry)
        if entry is None:
            _LOGGER.debug("%s: ignore corrupt %s record %s", DOMAIN, section_name, key)
            continue
        cleaned[key] = entry
    return cleaned


def _load_json_file(path) -> dict | None:
    """Read a storage file directly, bypassing the Store memory cache."""
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _require_id(value, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass
class StationSnapshot:
    """Validated, JSON-compatible snapshot payload (RFC v2 §3.2)."""

    entry_id: str
    account_uid: str | None = None
    account_login: str | None = None
    revision: int = 0
    updated_at: str | None = None
    devices: list[dict] = field(default_factory=list)
    local_config: dict[str, dict] = field(default_factory=dict)
    device_tokens: dict[str, dict] = field(default_factory=dict)
    scenario_creation: dict[str, dict] = field(default_factory=dict)
    scenario_creation_trusted: bool = True

    def to_dict(self) -> dict:
        """Build the payload for Store; the envelope is added by Store."""
        devices = [
            device
            for device in (_clean_device(value) for value in self.devices)
            if device is not None
        ]
        journal = _clean_section(
            self.scenario_creation, _clean_scenario_entry, "scenario_creation"
        )
        trusted = (
            self.scenario_creation_trusted
            and journal is not None
            and len(journal) == len(self.scenario_creation)
        )
        return {
            "entry_id": self.entry_id,
            "account_uid": self.account_uid,
            "account_login": self.account_login,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "devices": devices,
            "local_config": _clean_section(
                self.local_config, _clean_local_config_entry, "local_config"
            ) or {},
            "device_tokens": _clean_tokens(self.device_tokens, devices),
            "scenario_creation": journal or {},
            "scenario_creation_trusted": trusted,
        }

    @classmethod
    def from_dict(cls, data) -> "StationSnapshot | None":
        """Validate a raw payload; None for an unknown or corrupt format."""
        if not isinstance(data, dict):
            return None
        entry_id = data.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id:
            return None
        revision = data.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            return None
        account_uid = data.get("account_uid")
        if account_uid is not None and not isinstance(account_uid, str):
            return None
        account_login = data.get("account_login")
        if account_login is not None and not isinstance(account_login, str):
            return None
        updated_at = data.get("updated_at")
        if updated_at is not None and not isinstance(updated_at, str):
            return None
        raw_devices = data.get("devices")
        if not isinstance(raw_devices, list):
            return None
        devices = []
        for device in raw_devices:
            device = _clean_device(device)
            if device is None:
                _LOGGER.debug("%s: ignore corrupt device record", DOMAIN)
                continue
            devices.append(device)
        local_config = _clean_section(
            data.get("local_config", {}), _clean_local_config_entry, "local_config"
        )
        raw_journal = data.get("scenario_creation")
        journal = _clean_section(
            raw_journal, _clean_scenario_entry, "scenario_creation"
        )
        trusted = (
            data.get("scenario_creation_trusted", True) is True
            and journal is not None
            and len(journal) == len(raw_journal)
        )
        return cls(
            entry_id=entry_id,
            account_uid=account_uid,
            account_login=account_login,
            revision=revision,
            updated_at=updated_at,
            devices=devices,
            local_config=local_config or {},
            device_tokens=_clean_tokens(data.get("device_tokens"), devices),
            scenario_creation=journal or {},
            scenario_creation_trusted=trusted,
        )


class CacheManager:
    """Own account-bound storage, serialized writes and their durable read-back."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        account_login: str | None = None,
        account_uid: str | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = _require_id(entry_id, "entry_id")
        self._account_login = account_login
        self._account_uid = account_uid
        self._store = Store(
            hass, SNAPSHOT_VERSION, key=f"{DOMAIN}.{entry_id}",
            private=True, atomic_writes=True,
        )
        self._lock = asyncio.Lock()
        self._snapshot: StationSnapshot | None = None
        self._cache_status = "cache_missing"
        self._scenario_creation_trusted = True
        self._dirty = False
        self._closed = False
        self._metadata_task: asyncio.Task | None = None
        self._flush_now = asyncio.Event()

    @property
    def cache_status(self) -> str:
        return self._cache_status

    @property
    def scenario_creation_trusted(self) -> bool:
        return self._scenario_creation_trusted

    @property
    def snapshot(self) -> StationSnapshot | None:
        return self._snapshot

    @property
    def path(self) -> str:
        return self._store.path

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("cache_closed")

    def _entry_login(self) -> str | None:
        entries = getattr(self._hass, "config_entries", None)
        entry = entries.async_get_entry(self._entry_id) if entries else None
        login = entry.unique_id if entry is not None else None
        return login if isinstance(login, str) and login else None

    def _invalidate_cache(self) -> None:
        self._snapshot = None
        self._cache_status = "cache_invalid"
        self._scenario_creation_trusted = False
        self._dirty = False

    async def async_load(
        self, account_uid: str | None = None
    ) -> StationSnapshot | None:
        """Bind a snapshot locally; migrate legacy metadata without cloud access."""
        async with self._lock:
            self._require_open()
            exists = await self._hass.async_add_executor_job(os.path.exists, self.path)
            if not exists:
                self._cache_status = "cache_missing"
                return None
            # Core may move corrupt JSON aside. Keep the original until a complete
            # inventory can replace it without forgetting the untrusted journal.
            if await self._async_read_payload() is None:
                self._invalidate_cache()
                _LOGGER.warning("%s: cache_invalid entry=%s", DOMAIN, self._entry_id)
                return None
            try:
                data = await self._store.async_load()
            except (
                NotImplementedError, HomeAssistantError, OSError,
                KeyError, TypeError, ValueError,
            ):
                data = None
            snapshot = StationSnapshot.from_dict(data)
            entry_login = self._entry_login()
            login = self._account_login or entry_login
            uid = account_uid if account_uid is not None else self._account_uid
            if (
                snapshot is None
                or snapshot.entry_id != self._entry_id
                or not isinstance(login, str)
                or not login
                or (entry_login is not None and entry_login != login)
                or (
                    snapshot.account_login is not None
                    and snapshot.account_login != login
                )
                or (
                    snapshot.account_uid is not None and uid is not None
                    and snapshot.account_uid != uid
                )
                or (
                    self._account_uid is not None and uid is not None
                    and self._account_uid != uid
                )
                or (snapshot.account_login is None and entry_login != login)
            ):
                self._invalidate_cache()
                _LOGGER.warning("%s: cache_invalid entry=%s", DOMAIN, self._entry_id)
                return None
            snapshot.account_login = login
            if uid is not None:
                snapshot.account_uid = uid
            self._account_login = login
            self._account_uid = snapshot.account_uid
            self._snapshot = snapshot
            self._cache_status = "cache_ready"
            self._scenario_creation_trusted = snapshot.scenario_creation_trusted
            if snapshot.to_dict() != data:
                await self._async_save_locked()
            return snapshot

    async def async_save_devices(
        self,
        devices: list,
        account_uid: str | None = None,
        account_login: str | None = None,
    ) -> int:
        """Persist a complete inventory; never erase creation history with it."""
        cleaned = normalize_inventory(devices)
        if self._snapshot is None:
            await self.async_load(account_uid=account_uid)
        async with self._lock:
            self._require_open()
            entry_login = self._entry_login()
            login = self._account_login or entry_login or account_login
            if (
                not isinstance(login, str) or not login
                or (account_login is not None and account_login != login)
                or (entry_login is not None and entry_login != login)
                or (
                    self._account_uid is not None and account_uid is not None
                    and self._account_uid != account_uid
                )
            ):
                raise ValueError("cache_account_mismatch")
            self._account_login = login
            if account_uid is not None:
                self._account_uid = account_uid
            if self._snapshot is None:
                self._snapshot = StationSnapshot(
                    entry_id=self._entry_id,
                    account_login=login,
                    account_uid=self._account_uid,
                    scenario_creation_trusted=self._scenario_creation_trusted,
                )
            snapshot = self._snapshot
            snapshot.account_login = login
            snapshot.account_uid = self._account_uid
            snapshot.devices = cleaned
            platforms = _device_platforms(cleaned)
            snapshot.device_tokens = {
                glagol_id: {
                    platform: record for platform, record in tokens.items()
                    if platform in platforms[glagol_id]
                }
                for glagol_id, tokens in snapshot.device_tokens.items()
                if glagol_id in platforms
            }
            snapshot.local_config = {
                glagol_id: config for glagol_id, config in snapshot.local_config.items()
                if glagol_id in platforms
            }
            snapshot.updated_at = dt_util.utcnow().isoformat()
            revision = await self._async_save_locked()
            if self._dirty:
                raise RuntimeError("cache_write_failed")
            self._cache_status = "cache_ready"
            return revision

    async def async_save_device_token(
        self, glagol_id: str, platform: str, token: str
    ) -> int:
        """Save a candidate before use; inventory freshness remains unchanged."""
        _require_id(glagol_id, "glagol_id")
        _require_id(platform, "platform")
        _require_id(token, "token")
        async with self._lock:
            self._require_open()
            snapshot = self._ensure_snapshot()
            snapshot.device_tokens.setdefault(glagol_id, {})[platform] = {
                "token": token,
                "obtained_at": dt_util.utcnow().isoformat(),
                "state": TOKEN_STATE_VALID,
            }
            return await self._async_save_locked()

    async def async_invalidate_device_token(
        self, glagol_id: str, platform: str, rejected_token: str
    ) -> bool:
        """Compare-and-invalidate a token, confirming durable absence of its validity."""
        _require_id(glagol_id, "glagol_id")
        _require_id(platform, "platform")
        _require_id(rejected_token, "rejected_token")
        async with self._lock:
            self._require_open()
            if self._snapshot is None:
                return not await self._hass.async_add_executor_job(
                    os.path.exists, self.path
                )
            record = self._snapshot.device_tokens.get(glagol_id, {}).get(platform)
            if record is not None and record["token"] == rejected_token:
                record["state"] = TOKEN_STATE_INVALID
                self._dirty = True
            if self._dirty:
                await self._async_save_locked()
            else:
                self._dirty = not await self._async_verify_durable_marker_locked(
                    self._snapshot.revision
                )
            if self._dirty:
                _LOGGER.warning(
                    "%s: token_cache_write_failed entry=%s", DOMAIN, self._entry_id
                )
            return not self._dirty

    async def async_save_scenario_marker(
        self, device_id: str, trigger: str, state: str
    ) -> int:
        """Persist pending/confirmed identity without changing inventory freshness."""
        record = self._scenario_record(device_id, trigger, state)
        async with self._lock:
            self._require_open()
            self._ensure_snapshot().scenario_creation[device_id] = record
            return await self._async_save_locked()

    async def async_save_and_verify_scenario_marker(
        self, device_id: str, trigger: str, state: str
    ) -> bool:
        """A creation attempt requires durable marker verification under this lock."""
        record = self._scenario_record(device_id, trigger, state)
        async with self._lock:
            self._require_open()
            self._ensure_snapshot().scenario_creation[device_id] = record
            await self._async_save_locked()
            return not self._dirty

    @staticmethod
    def _scenario_record(device_id: str, trigger: str, state: str) -> dict:
        _require_id(device_id, "device_id")
        _require_id(trigger, "trigger")
        if state not in SCENARIO_STATES:
            raise ValueError("unknown_scenario_marker_state")
        return {
            "trigger": trigger, "state": state,
            "updated_at": dt_util.utcnow().isoformat(),
        }

    async def async_save_local_config(
        self,
        glagol_id: str,
        host: str | None = None,
        port: int | None = None,
        hdmi_audio: bool | None = None,
    ) -> None:
        """Merge metadata; one owned delayed write coalesces discovery/config changes."""
        _require_id(glagol_id, "glagol_id")
        if host is not None and not isinstance(host, str):
            raise ValueError("host must be a string")
        if port is not None and (isinstance(port, bool) or not isinstance(port, int)):
            raise ValueError("port must be an integer")
        if hdmi_audio is not None and not isinstance(hdmi_audio, bool):
            raise ValueError("hdmi_audio must be a boolean")
        record = {
            key: value for key, value in (
                ("host", host), ("port", port), ("hdmi_audio", hdmi_audio)
            ) if value is not None
        }
        async with self._lock:
            self._require_open()
            snapshot = self._ensure_snapshot()
            if not record:
                return
            current = snapshot.local_config.setdefault(glagol_id, {})
            if all(current.get(key) == value for key, value in record.items()):
                return
            current.update(record)
            self._dirty = True
            if self._metadata_task is None:
                self._flush_now.clear()
                self._metadata_task = asyncio.create_task(self._async_metadata_flush())

    async def _async_metadata_flush(self) -> None:
        try:
            try:
                await asyncio.wait_for(self._flush_now.wait(), 1)
            except asyncio.TimeoutError:
                pass
            async with self._lock:
                if self._dirty:
                    await self._async_save_locked()
        finally:
            self._metadata_task = None

    async def async_verify_durable_marker(
        self, revision: int, marker: tuple[str, str] | None = None
    ) -> bool:
        """Verify the entire revision and, optionally, a particular marker."""
        async with self._lock:
            return await self._async_verify_durable_marker_locked(revision, marker)

    async def _async_verify_durable_marker_locked(
        self, revision: int, marker: tuple[str, str] | None = None
    ) -> bool:
        snapshot = self._snapshot
        if snapshot is None or snapshot.revision != revision:
            return False
        payload = await self._async_read_payload()
        if payload != snapshot.to_dict():
            self._dirty = True
            return False
        if marker is not None:
            device_id, state = marker
            record = payload["scenario_creation"].get(device_id)
            return isinstance(record, dict) and record.get("state") == state
        return True

    async def async_flush(self) -> bool:
        """Finish accepted metadata/writes; report whether the current snapshot is durable."""
        self._flush_now.set()
        task = self._metadata_task
        if task is not None:
            await asyncio.shield(task)
        async with self._lock:
            if self._dirty and self._snapshot is not None:
                await self._async_save_locked()
            return not self._dirty

    async def async_close(self) -> None:
        """Close admission after producers stop, then finish all accepted writes."""
        async with self._lock:
            self._closed = True
        await self.async_flush()

    async def async_remove(self) -> None:
        """Remove storage only after the owner's producers and writes have stopped."""
        await self.async_close()
        async with self._lock:
            await self._store.async_remove()
            self._snapshot = None
            self._cache_status = "cache_missing"

    def _ensure_snapshot(self) -> StationSnapshot:
        if self._snapshot is None:
            raise RuntimeError("cache_unavailable")
        return self._snapshot

    async def _async_read_payload(self) -> dict | None:
        envelope = await self._hass.async_add_executor_job(_load_json_file, self.path)
        if (
            not isinstance(envelope, dict)
            or envelope.get("version") != SNAPSHOT_VERSION
            or envelope.get("key") != f"{DOMAIN}.{self._entry_id}"
        ):
            return None
        payload = envelope.get("data")
        return payload if isinstance(payload, dict) else None

    async def _async_write_payload(self, payload: dict) -> bool:
        try:
            await self._store.async_save(payload)
            if await self._async_read_payload() == payload:
                return True
        except (HomeAssistantError, OSError) as error:
            _LOGGER.warning(
                "%s: cache_write_failed entry=%s error=%s",
                DOMAIN, self._entry_id, type(error).__name__,
            )
            return False
        _LOGGER.warning(
            "%s: cache_write_failed entry=%s reason=read_back_mismatch",
            DOMAIN, self._entry_id,
        )
        return False

    async def _async_save_locked(self) -> int:
        """Keep the lock until disk I/O finishes even if its caller is cancelled."""
        snapshot = self._ensure_snapshot()
        snapshot.revision += 1
        self._dirty = True
        write = asyncio.create_task(self._async_write_payload(snapshot.to_dict()))
        cancelled = False
        while True:
            try:
                durable = await asyncio.shield(write)
                break
            except asyncio.CancelledError:
                if write.cancelled():
                    raise
                cancelled = True
        self._dirty = not durable
        if cancelled:
            raise asyncio.CancelledError
        return snapshot.revision
