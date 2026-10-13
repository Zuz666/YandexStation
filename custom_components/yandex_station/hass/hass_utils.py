import json
import logging
import os

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_INCLUDE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..core.cache import CacheManager
from ..core.const import (
    CLIMATE_TYPES, COVER_TYPES, DATA_CONFIG, DOMAIN, HUMIDIFIER_TYPES,
    LIGHT_TYPES, MEDIA_PLAYER_TYPES, VACUUM_TYPES, WATER_HEATER_TYPES,
)
from ..core.entity import extract_instance
from ..core.lifecycle import get_lifecycle
from ..core.quasar_info import has_quasar
from ..core.yandex_quasar import YandexQuasar

INCLUDE_KEYS = ("id", "name", "type", "room_name", "skill_id", "house_name")
_LOGGER = logging.getLogger(__name__)


INCLUDE_TYPES_UNKNOWN = (
    "devices.types.camera",
    "devices.types.cooking",
    "devices.types.cooking.coffee_maker",
    "devices.types.cooking.multicooker",
    "devices.types.dishwasher",
    "devices.types.iron",
    "devices.types.openable",
    "devices.types.other",
    "devices.types.pet_drinking_fountain",
    "devices.types.pet_feeder",
    "devices.types.washing_machine",
)
INCLUDE_SKIP_INSTANCES = {
    CLIMATE_TYPES: [
        "on",
        "thermostat",
        "program",
        "heat",
        "work_speed",
        "temperature",
        "humidity",
        "fan_speed",
    ],
    COVER_TYPES: ["on", "open", "pause"],
    HUMIDIFIER_TYPES: ["on", "fan_speed", "work_speed", "humidity"],
    LIGHT_TYPES: ["on", "brightness", "color"],
    MEDIA_PLAYER_TYPES: ["on", "pause", "volume", "mute", "channel", "input_source"],
    VACUUM_TYPES: ["on", "pause", "work_speed"],
    WATER_HEATER_TYPES: ["on", "tea_mode", "temperature"],
    INCLUDE_TYPES_UNKNOWN: [],
}


def incluce_devices(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> list[tuple[YandexQuasar, dict, dict]]:
    if DOMAIN not in hass.data:
        return []
    quasar: YandexQuasar | None = hass.data[DOMAIN].get(config_entry.unique_id)
    if not quasar or not getattr(quasar, "devices", None):
        return []
    config: dict = hass.data[DOMAIN].get(DATA_CONFIG, {})
    # config_entry has more priority
    includes = config_entry.options.get(CONF_INCLUDE, []) + config.get(CONF_INCLUDE, [])
    devices = []

    # Iterate over devices first, then includes, so duplicate include names match
    # each corresponding device
    for device in quasar.devices:
        if (
            has_quasar(device)
            and device["quasar_info"]["device_id"] not in quasar.owned_glagol_ids
        ):
            continue
        for conf in includes:
            if isinstance(conf, str):
                if conf == device["id"] or conf == device["name"]:
                    conf = build_include_config(device)
                    devices.append((quasar, device, conf))
                    break
            elif isinstance(conf, dict):
                if all(conf[k] == device.get(k) for k in INCLUDE_KEYS if k in conf):
                    devices.append((quasar, device, conf))
                    break

    return devices


def build_include_config(device: dict) -> dict:
    for include_types, include_skip in INCLUDE_SKIP_INSTANCES.items():
        if device["type"] in include_types:
            break
    else:
        return {}

    caps = [extract_instance(i) for i in device["capabilities"]]
    props = [i["parameters"]["instance"] for i in device["properties"]]

    return {
        "capabilities": [i for i in caps if i not in include_skip],
        "properties": [i for i in props if i not in include_skip],
    }


async def load_fake_devies(hass: HomeAssistant, quasar: YandexQuasar):
    path = hass.config.path(DOMAIN + ".json")
    if not os.path.isfile(path):
        return

    def job():
        with open(path, "rb") as f:
            quasar.devices = json.load(f)

    await hass.async_add_executor_job(job)


async def _async_account_speakers(hass, entry, *, snapshot_only=False):
    """Read this account's records only; no discovery or foreign credentials."""
    lifecycle = get_lifecycle(hass)
    runtime = lifecycle.runtimes.get(entry.entry_id)
    if runtime is None:
        runtime = hass.data[DOMAIN].get(entry.unique_id)
    if runtime is not None and not runtime.stopping and not snapshot_only:
        records = {
            device["quasar_info"]["device_id"]: device
            for device in runtime.devices if has_quasar(device)
        }
        if records:
            return records, runtime.cached_glagol_ids & records.keys()
    cache = runtime.cache_manager if runtime is not None else None
    own_cache = cache is None or cache.closed or runtime.stopping
    if own_cache:
        cache = CacheManager(
            hass, entry.entry_id, account_login=entry.unique_id,
            account_uid=entry.data.get("account_uid"),
        )
    try:
        snapshot = cache.snapshot or await cache.async_load()
        if (
            snapshot is None or snapshot.entry_id != entry.entry_id
            or snapshot.account_login != entry.unique_id
            or (
                entry.data.get("account_uid") is not None
                and snapshot.account_uid is not None
                and snapshot.account_uid != entry.data["account_uid"]
            )
        ):
            return {}, set()
        records = {
            device["quasar_info"]["device_id"]: device
            for device in snapshot.devices if has_quasar(device)
        }
        cached_ids = (
            runtime.cached_glagol_ids & records.keys()
            if runtime is not None and not runtime.stopping else set(records)
        )
        return records, cached_ids
    finally:
        if own_cache:
            await cache.async_close()


def _speaker_registry_entries(hass, glagol_id):
    """The media identity is canonical; companion identities share its device."""
    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    if "async_get_devices" in getattr(type(device_registry), "__dict__", {}):
        matches = list(
            device_registry.async_get_devices(identifiers={(DOMAIN, glagol_id)})
        )
    else:
        one = device_registry.async_get_device(identifiers={(DOMAIN, glagol_id)})
        matches = [one] if one is not None else []
    by_id = {item.id: item for item in matches if item is not None}
    suffixes = {
        "camera": ("lyrics",), "calendar": ("calendar",),
        "select": ("equalizer",), "conversation": ("conversation",),
        "sensor": ("transport", "alice_state"),
        "binary_sensor": ("local_connection",),
    }
    records = []
    media_device = None
    for item in registry.entities.values():
        if item.platform != DOMAIN:
            continue
        if item.domain == "media_player" and item.unique_id == glagol_id:
            records.append(item)
            if item.device_id:
                media_device = by_id.get(item.device_id) or device_registry.async_get(
                    item.device_id
                )
        elif item.device_id in by_id:
            records.append(item)
        elif any(
            item.unique_id in (
                f"{glagol_id}_{suffix}",
                f"{item.config_entry_id}_{glagol_id}_{suffix}",
            )
            for suffix in suffixes.get(item.domain, ())
        ):
            records.append(item)
    device = media_device
    if device is None:
        record_devices = [
            by_id[item.device_id]
            for item in records
            if item.device_id in by_id
        ]
        device = (
            record_devices[0]
            if record_devices
            else (matches[0] if matches else None)
        )
    return device, records


def _registry_speaker_owner(hass, glagol_id):
    """Return (owner, conflict); an orphan record is not permission to take over."""
    _, records = _speaker_registry_entries(hass, glagol_id)
    for item in records:
        if item.domain == "media_player" and item.unique_id == glagol_id:
            return item.config_entry_id or "", False
    device_registry = dr.async_get(hass)
    if "async_get_devices" in getattr(type(device_registry), "__dict__", {}):
        matches = list(
            device_registry.async_get_devices(identifiers={(DOMAIN, glagol_id)})
        )
    else:
        one = device_registry.async_get_device(identifiers={(DOMAIN, glagol_id)})
        matches = [one] if one is not None else []
    device_owners = {
        cid for match in matches if match is not None for cid in match.config_entries if cid
    }
    if not records:
        return None, len(device_owners) > 1
    owners = {item.config_entry_id for item in records} | device_owners
    if len(owners) == 1 and None not in owners:
        return owners.pop(), False
    return None, True


async def async_resolve_speaker_owners(hass, entry, quasar) -> set[str]:
    """Choose one local owner before publishing entities or starting producers."""
    lifecycle = get_lifecycle(hass)
    own_ids = {
        device["quasar_info"]["device_id"]
        for device in quasar.speakers + quasar.modules
    }
    owned, conflicts = set(), set()
    async with lifecycle.ownership_lock:
        candidates = [
            candidate for candidate in hass.config_entries.async_entries(DOMAIN)
            if lifecycle.entry_enabled(candidate)
        ]
        catalogs = {}
        cached_ids = set()
        for candidate in candidates:
            records, cached = await _async_account_speakers(hass, candidate)
            catalogs[candidate.entry_id] = set(records)
            cached_ids.update(cached)

        unknown = {
            did for did in own_ids
            if _registry_speaker_owner(hass, did) == (None, False)
            and did not in lifecycle.speaker_owners and did not in cached_ids
        }
        if unknown:
            await lifecycle.wait_initial_bootstraps(entry.entry_id)
            for candidate in candidates:
                records, _ = await _async_account_speakers(hass, candidate)
                catalogs[candidate.entry_id] = set(records)

        for did in own_ids:
            owner, conflict = _registry_speaker_owner(hass, did)
            if conflict:
                conflicts.add(did)
                continue
            if owner is None:
                owner = lifecycle.speaker_owners.get(did)
            if owner is None:
                eligible = [
                    candidate.entry_id for candidate in candidates
                    if did in catalogs[candidate.entry_id]
                    and lifecycle.entry_enabled(
                        hass.config_entries.async_get_entry(candidate.entry_id)
                    )
                ]
                owner = min(eligible) if eligible else None
            if owner is not None:
                lifecycle.speaker_owners[did] = owner
            if owner == entry.entry_id:
                owned.add(did)
            elif owner is not None and not lifecycle.entry_enabled(
                hass.config_entries.async_get_entry(owner)
            ):
                conflicts.add(did)
        lifecycle.ownership_conflicts[entry.entry_id] = conflicts
        if conflicts:
            lifecycle.setup_reasons[entry.entry_id] = "ownership_conflict"
        quasar.owned_glagol_ids = owned
        return owned


async def async_claimable_speakers(hass, entry) -> dict[str, str]:
    """Recovery labels come from this entry's durable account-bound catalog."""
    records, _ = await _async_account_speakers(hass, entry, snapshot_only=True)
    return {did: device["name"] for did, device in records.items()}


async def async_claim_speaker(hass, entry, glagol_id: str) -> None:
    """Explicitly transfer registry ownership without copying account state."""
    lifecycle = get_lifecycle(hass)
    async with lifecycle.consumer():
        async with lifecycle.ownership_lock:
            records, _ = await _async_account_speakers(hass, entry, snapshot_only=True)
            if not lifecycle.entry_admitted(entry.entry_id):
                raise RuntimeError("reload_in_progress")
            if not lifecycle.entry_enabled(entry):
                raise ValueError("entry_disabled")
            if not isinstance(glagol_id, str) or glagol_id not in records:
                raise ValueError("ownership_conflict")
            device, entities = _speaker_registry_entries(hass, glagol_id)
            devices = dr.async_get(hass)
            if "async_get_devices" in getattr(type(devices), "__dict__", {}):
                matches = list(
                    devices.async_get_devices(identifiers={(DOMAIN, glagol_id)})
                )
            else:
                matches = [device] if device is not None else []
            if device is not None and all(m.id != device.id for m in matches):
                matches.append(device)
            owners = {item.config_entry_id for item in entities if item.config_entry_id}
            for match in matches:
                if match is not None:
                    owners.update(match.config_entries)
            if previous := lifecycle.speaker_owners.get(glagol_id):
                owners.add(previous)
            for owner in owners - {entry.entry_id}:
                old_entry = hass.config_entries.async_get_entry(owner)
                if old_entry is not None:
                    state = getattr(old_entry.state, "value", old_entry.state)
                    if old_entry.disabled_by is None or state == "loaded":
                        raise ValueError("ownership_conflict")
                old_runtime = lifecycle.runtimes.get(owner)
                if owner in lifecycle.entry_ids or (
                    old_runtime is not None and not old_runtime.stopping
                ):
                    raise ValueError("ownership_conflict")
            if not lifecycle.request_entry_reload(entry.entry_id):
                raise RuntimeError("reload_in_progress")
            registry = er.async_get(hass)
            for entity in entities:
                updates = {}
                if entity.config_entry_id != entry.entry_id:
                    updates["config_entry_id"] = entry.entry_id
                if device is not None and entity.device_id != device.id:
                    updates["device_id"] = device.id
                if updates:
                    registry.async_update_entity(entity.entity_id, **updates)
            if device is not None:
                if "async_get_devices" in getattr(type(devices), "__dict__", {}):
                    for match in matches:
                        if match is not None and match.id != device.id:
                            devices.async_remove_device(match.id)
                    if entry.entry_id not in device.config_entries:
                        devices.async_update_device(
                            device.id, new_config_entry_id=entry.entry_id
                        )
                else:
                    devices.async_update_device(device.id, add_config_entry_id=entry.entry_id)
                    for owner in owners - {entry.entry_id}:
                        devices.async_update_device(device.id, remove_config_entry_id=owner)
            lifecycle.speaker_owners[glagol_id] = entry.entry_id
            lifecycle.ownership_conflicts.get(entry.entry_id, set()).discard(glagol_id)


def migrate_diagnostic_unique_id(
    hass: HomeAssistant, entry: ConfigEntry, platform: str,
    legacy_id: str, target_id: str,
) -> bool:
    """Preserve owned registry metadata without claiming another entry's ID.

    Return False when the target belongs to a different entry: registering a
    new entity there would attach it to the foreign registry record.
    """
    registry = er.async_get(hass)
    target_entity_id = registry.async_get_entity_id(platform, DOMAIN, target_id)
    legacy_entity_id = registry.async_get_entity_id(platform, DOMAIN, legacy_id)
    legacy = registry.async_get(legacy_entity_id) if legacy_entity_id else None
    if target_entity_id:
        target = registry.async_get(target_entity_id)
        if target.config_entry_id != entry.entry_id:
            _LOGGER.warning("diagnostic_id_conflict")
            return False
        if legacy is not None and legacy.config_entry_id == entry.entry_id:
            _LOGGER.warning("diagnostic_id_conflict")
        return True
    if legacy is not None and legacy.config_entry_id == entry.entry_id:
        registry.async_update_entity(legacy_entity_id, new_unique_id=target_id)
    return True
