"""Snapshot and cache storage seam (RFC v2 §3.2, §3.3).

Narrow module hiding validation and saving of the last-known-good snapshot
for one config entry. The envelope (version, minor_version, key) belongs to
homeassistant.helpers.storage.Store; this module owns only the payload.
"""

import asyncio
import json
import logging
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
    """Clean one cloud device record; None for a record without a valid id."""
    if not isinstance(device, dict):
        return None
    device_id = device.get("id")
    if not isinstance(device_id, str) or not device_id:
        return None
    cleaned = _clean_json(device)
    for key in _DEVICE_RUNTIME_KEYS:
        cleaned.pop(key, None)
    return cleaned


def _clean_local_config_entry(entry) -> dict | None:
    """Clean one local_config record (host/port by glagol id)."""
    if not isinstance(entry, dict):
        return None
    cleaned = {}
    host = entry.get("host")
    if isinstance(host, str):
        cleaned["host"] = host
    port = entry.get("port")
    if not isinstance(port, bool) and isinstance(port, int):
        cleaned["port"] = port
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

    def to_dict(self) -> dict:
        """Build the payload for Store; the envelope is added by Store."""
        return {
            "entry_id": self.entry_id,
            "account_uid": self.account_uid,
            "account_login": self.account_login,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "devices": [
                device
                for device in (_clean_device(i) for i in self.devices)
                if device is not None
            ],
            "local_config": _clean_section(
                self.local_config, _clean_local_config_entry, "local_config"
            )
            or {},
            "device_tokens": _clean_section(
                self.device_tokens, _clean_token_entry, "device_tokens"
            )
            or {},
            "scenario_creation": _clean_section(
                self.scenario_creation, _clean_scenario_entry, "scenario_creation"
            )
            or {},
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
            data.get("local_config"), _clean_local_config_entry, "local_config"
        )
        device_tokens = _clean_section(
            data.get("device_tokens"), _clean_token_entry, "device_tokens"
        )
        scenario_creation = _clean_section(
            data.get("scenario_creation"), _clean_scenario_entry, "scenario_creation"
        )
        if local_config is None or device_tokens is None or scenario_creation is None:
            return None
        return cls(
            entry_id=entry_id,
            account_uid=account_uid,
            account_login=account_login,
            revision=revision,
            updated_at=updated_at,
            devices=devices,
            local_config=local_config,
            device_tokens=device_tokens,
            scenario_creation=scenario_creation,
        )


class CacheManager:
    """Serialized snapshot storage for one config entry (RFC v2 §3.3).

    One owner per entry: all saves go through the same asyncio.Lock, so an
    inventory update can not lose a concurrently received token or creation
    marker. Only a successful full inventory replaces the device catalog and
    refreshes updated_at.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        account_login: str | None = None,
        account_uid: str | None = None,
    ) -> None:
        if not hasattr(hass, "loop"):
            try:
                hass.loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
        self._hass = hass
        self._entry_id = entry_id
        self._account_login = account_login
        self._account_uid = account_uid
        self._store = Store(
            hass,
            SNAPSHOT_VERSION,
            key=f"{DOMAIN}.{entry_id}",
            private=True,
            atomic_writes=True,
        )
        self._lock = asyncio.Lock()
        self._snapshot: StationSnapshot | None = None
        self._untrusted: bool = False

    @property
    def is_untrusted(self) -> bool:
        """Return True if stored data on disk was invalid/untrusted."""
        return self._untrusted
    @property
    def snapshot(self) -> StationSnapshot | None:
        """Return the last known-good in-memory snapshot."""
        return self._snapshot

    @property
    def path(self) -> str:
        """Return the storage file path."""
        return self._store.path

    async def async_load(
        self, account_uid: str | None = None
    ) -> StationSnapshot | None:
        """Load and validate the stored snapshot.

        Returns None for a missing, corrupt, foreign-entry or foreign-account
        snapshot. A corrupt or unknown format is never rewritten here.
        """
        async with self._lock:
            try:
                data = await self._store.async_load()
            except (
                NotImplementedError,
                HomeAssistantError,
                KeyError,
                TypeError,
                ValueError,
            ):
                _LOGGER.warning(
                    "%s: unusable cache storage for entry %s",
                    DOMAIN,
                    self._entry_id,
                )
                return None
            if data is None:
                return None
            snapshot = StationSnapshot.from_dict(data)
            if snapshot is None:
                _LOGGER.warning(
                    "%s: invalid snapshot format for entry %s", DOMAIN, self._entry_id
                )
                self._untrusted = True
                return None
            self._untrusted = False
            if snapshot.entry_id != self._entry_id:
                _LOGGER.warning(
                    "%s: snapshot belongs to another entry %s",
                    DOMAIN,
                    snapshot.entry_id,
                )
                return None
            if (
                snapshot.account_uid is not None
                and account_uid is not None
                and snapshot.account_uid != account_uid
            ):
                _LOGGER.warning(
                    "%s: snapshot belongs to another account for entry %s",
                    DOMAIN,
                    self._entry_id,
                )
                return None
            self._snapshot = snapshot
            return snapshot

    async def async_save_devices(
        self,
        devices: list,
        account_uid: str | None = None,
        account_login: str | None = None,
    ) -> int:
        """Replace the device catalog after a successful full inventory.

        Corrupt device records are dropped instead of saved; tokens, markers
        and local_config collected in the same runtime survive the replace.
        Returns the new snapshot revision.
        """
        if not isinstance(devices, (list, tuple)):
            raise ValueError("devices must be a list")
        cleaned = [
            device
            for device in (_clean_device(device) for device in devices)
            if device is not None
        ]
        async with self._lock:
            snapshot = self._ensure_snapshot()
            snapshot.devices = cleaned
            if account_uid is not None:
                snapshot.account_uid = account_uid
            if account_login is not None:
                snapshot.account_login = account_login
            snapshot.updated_at = dt_util.utcnow().isoformat()
            return await self._async_save_locked()

    async def async_save_device_token(
        self,
        glagol_id: str,
        token: str,
        state: str = TOKEN_STATE_VALID,
    ) -> int:
        """Persist one device token immediately, without touching devices.

        The inventory freshness timestamp is not changed by token saves.
        Returns the new snapshot revision.
        """
        _require_id(glagol_id, "glagol_id")
        _require_id(token, "token")
        _require_id(state, "state")
        record = {
            "token": token,
            "obtained_at": dt_util.utcnow().isoformat(),
            "state": state,
        }
        async with self._lock:
            snapshot = self._ensure_snapshot()
            snapshot.device_tokens[glagol_id] = record
            return await self._async_save_locked()

    async def async_save_scenario_marker(
        self, device_id: str, trigger: str, state: str
    ) -> int:
        """Persist one scenario creation marker immediately (RFC v2 §5.2).

        Returns the new snapshot revision; pass it to
        async_verify_durable_marker before a non-idempotent POST.
        """
        _require_id(device_id, "device_id")
        _require_id(trigger, "trigger")
        if state not in SCENARIO_STATES:
            raise ValueError(f"unknown scenario marker state: {state}")
        record = {
            "trigger": trigger,
            "state": state,
            "updated_at": dt_util.utcnow().isoformat(),
        }
        async with self._lock:
            snapshot = self._ensure_snapshot()
            snapshot.scenario_creation[device_id] = record
            return await self._async_save_locked()

    async def async_save_local_config(
        self, glagol_id: str, host: str | None = None, port: int | None = None
    ) -> int:
        """Persist the last known local address of a speaker.

        Not a proof of availability; mDNS may update it at any time.
        Returns the new snapshot revision.
        """
        _require_id(glagol_id, "glagol_id")
        if host is not None and not isinstance(host, str):
            raise ValueError("host must be a string")
        if port is not None and (isinstance(port, bool) or not isinstance(port, int)):
            raise ValueError("port must be an integer")
        record = {}
        if host is not None:
            record["host"] = host
        if port is not None:
            record["port"] = port
        async with self._lock:
            snapshot = self._ensure_snapshot()
            if record:
                snapshot.local_config[glagol_id] = record
            return await self._async_save_locked()

    async def async_save_and_verify_scenario_marker(
        self, device_id: str, trigger: str, state: str
    ) -> bool:
        """Save scenario marker and verify on-disk readback under lock (RFC v2 §3.3.5)."""
        _require_id(device_id, "device_id")
        _require_id(trigger, "trigger")
        if state not in SCENARIO_STATES:
            raise ValueError(f"unknown scenario marker state: {state}")
        record = {
            "trigger": trigger,
            "state": state,
            "updated_at": dt_util.utcnow().isoformat(),
        }
        async with self._lock:
            snapshot = self._ensure_snapshot()
            snapshot.scenario_creation[device_id] = record
            rev = await self._async_save_locked()
            return await self._async_verify_durable_marker_locked(rev, (device_id, state))

    async def async_verify_durable_marker(
        self,
        revision: int,
        marker: tuple[str, str] | None = None,
    ) -> bool:
        """Confirm that the last save reached the disk (RFC v2 §3.3.5)."""
        async with self._lock:
            return await self._async_verify_durable_marker_locked(revision, marker)

    async def _async_verify_durable_marker_locked(
        self,
        revision: int,
        marker: tuple[str, str] | None = None,
    ) -> bool:
        data = await self._hass.async_add_executor_job(
            _load_json_file, self._store.path
        )
        if not isinstance(data, dict):
            return False
        payload = data.get("data")
        if not isinstance(payload, dict):
            return False
        durable_revision = payload.get("revision")
        if (
            isinstance(durable_revision, bool)
            or not isinstance(durable_revision, int)
            or durable_revision != revision
        ):
            return False
        if marker is not None:
            device_id, state = marker
            records = payload.get("scenario_creation")
            if not isinstance(records, dict):
                return False
            record = records.get(device_id)
            if not isinstance(record, dict) or record.get("state") != state:
                return False
        return True

    async def async_remove(self) -> None:
        """Delete the storage file after the entry is removed (§3.3.7)."""
        async with self._lock:
            self._snapshot = None
            await self._store.async_remove()

    def _ensure_snapshot(self) -> StationSnapshot:
        """Return the in-memory snapshot, creating an empty one if needed."""
        if self._snapshot is None:
            self._snapshot = StationSnapshot(entry_id=self._entry_id)
        return self._snapshot

    async def _async_save_locked(self) -> int:
        """Bump the revision and write the payload; the lock must be held."""
        snapshot = self._snapshot
        snapshot.revision += 1
        await self._store.async_save(snapshot.to_dict())
        return snapshot.revision
