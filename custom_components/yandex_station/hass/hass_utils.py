import json
import logging
import os

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_INCLUDE, MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import DeviceInfo

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


def device_config_entry_ids(device):
    """Read singular Core ownership while preserving legacy/composite owners."""
    if hasattr(type(device), "config_entry_id") and not getattr(
        device, "is_composite_device", False
    ):
        return (device.config_entry_id,)
    return device.config_entries


def account_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Keep commands and optional diagnostics on the same account service device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Яндекс ({entry.title or entry.unique_id})",
        manufacturer="Yandex",
        model="Account & Local Resilience Gateway",
        entry_type=dr.DeviceEntryType.SERVICE,
        configuration_url="homeassistant://config/repairs?dialog=system-health",
    )


async def async_translate_entity_names(hass, entities, platform: str):
    """Supply translated names only on Core predating native entity naming."""
    if (MAJOR_VERSION, MINOR_VERSION) >= (2023, 4):
        return
    from homeassistant.helpers.translation import async_get_translations

    translated = [
        entity for entity in entities
        if entity.has_entity_name and entity.translation_key is not None
    ]
    if not translated:
        return
    resources = await async_get_translations(
        hass, hass.config.language, "entity", {DOMAIN}
    )
    for entity in translated:
        key = f"component.{DOMAIN}.entity.{platform}.{entity.translation_key}.name"
        entity._attr_name = resources[key]


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


async def async_resolve_speaker_owners(hass, entry, quasar) -> set[str]:
    """Assign speaker ownership via entity_registry; auto-rebind if previous owner is disabled/removed."""
    lifecycle = get_lifecycle(hass)
    try:
        registry = er.async_get(hass)
        devices = dr.async_get(hass)
    except (KeyError, RuntimeError):
        registry = devices = None
    owned = set()
    async with lifecycle.ownership_lock:
        for device in quasar.speakers + quasar.modules:
            did = device["quasar_info"]["device_id"]
            media = (
                next(
                    (
                        item for item in registry.entities.values()
                        if item.platform == DOMAIN
                        and item.domain == "media_player"
                        and item.unique_id == did
                    ),
                    None,
                )
                if registry is not None else None
            )
            owner = (
                media.config_entry_id
                if media is not None and media.config_entry_id
                else lifecycle.speaker_owners.get(did)
            )
            if owner and owner != entry.entry_id:
                old_entry = hass.config_entries.async_get_entry(owner)
                old_runtime = lifecycle.runtimes.get(owner)
                if lifecycle.entry_enabled(old_entry) or (
                    old_runtime is not None and not old_runtime.stopping
                ):
                    continue
                if media is not None:
                    dev_id = media.device_id
                    for item in list(registry.entities.values()):
                        if item.platform == DOMAIN and (
                            item.entity_id == media.entity_id
                            or (dev_id and item.device_id == dev_id)
                        ):
                            registry.async_update_entity(
                                item.entity_id, config_entry_id=entry.entry_id
                            )
                    if dev_id and (dev := devices.async_get(dev_id)) is not None:
                        if hasattr(type(dev), "config_entry_id"):
                            devices.async_update_device(
                                dev.id, new_config_entry_id=entry.entry_id
                            )
                        else:
                            devices.async_update_device(
                                dev.id,
                                add_config_entry_id=entry.entry_id,
                                remove_config_entry_id=owner,
                            )
            lifecycle.speaker_owners[did] = entry.entry_id
            owned.add(did)
        quasar.owned_glagol_ids = owned
        return owned
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
