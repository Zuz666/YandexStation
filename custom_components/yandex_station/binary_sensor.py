from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.util import dt as dt_util, slugify

from .core.const import DOMAIN
from .core.yandex_quasar import YandexQuasar
from .core.entity import YandexCustomEntity
from .hass import hass_utils

INCLUDE_CAPABILITIES = ("devices.capabilities.lock",)
INCLUDE_PROPERTIES = ("devices.properties.float",)

ENTITY_DESCRIPTIONS = {
    "lock": BinarySensorDeviceClass.LOCK,
    "occupancy": BinarySensorDeviceClass.OCCUPANCY,
}


async def async_setup_entry(hass, entry, async_add_entities):
    entities = []
    quasar: YandexQuasar | None = hass.data.get(DOMAIN, {}).get(entry.unique_id)

    if quasar and quasar.policy.enable_diagnostic_sensors:
        for speaker in quasar.owned_speakers + quasar.owned_modules:
            did = speaker.get("quasar_info", {}).get("device_id") or speaker.get("id")
            sensor = YandexStationLocalConnection(quasar, entry, speaker)
            if hass_utils.migrate_diagnostic_unique_id(
                hass, entry, "binary_sensor", f"{did}_local_connection", sensor.unique_id
            ):
                entities.append(sensor)

    for quasar, device, config in hass_utils.incluce_devices(hass, entry):
        for instance in device["capabilities"]:
            if instance["type"] in INCLUDE_CAPABILITIES:
                entities.append(YandexBinarySensor(quasar, device, instance))
        for instance in device["properties"]:
            if instance["type"] not in INCLUDE_PROPERTIES:
                continue
            if instance["parameters"]["instance"] not in ENTITY_DESCRIPTIONS:
                continue
            if (
                "properties" in config
                and instance["parameters"]["instance"] not in config["properties"]
            ):
                continue
            entities.append(YandexBinarySensor(quasar, device, instance))

    await hass_utils.async_translate_entity_names(hass, entities, "binary_sensor")
    async_add_entities(entities)


# noinspection PyAbstractClass
class YandexBinarySensor(BinarySensorEntity, YandexCustomEntity):

    def internal_init(self, capabilities: dict, properties: dict):
        # {'access_methods': None, 'instance': 'lock', 'retrievable': True, 'values': ['closed', 'open']}
        if desc := ENTITY_DESCRIPTIONS.get(self.instance):
            self._attr_device_class = desc

    def internal_update(self, capabilities: dict, properties: dict):
        if self.instance == "lock" and self.instance in capabilities:
            # On means open (unlocked), Off means closed (locked)
            self._attr_is_on = capabilities[self.instance] == "open"
            return

        if self.instance in properties:
            value = properties[self.instance]
            self._attr_is_on = value is not None and value > 0


class YandexStationLocalConnection(BinarySensorEntity):
    """Binary sensor for real-time Glagol local connectivity."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_translation_key = "local_connection"
    _attr_has_entity_name = True

    def __init__(self, quasar: YandexQuasar, entry, device: dict):
        self.quasar = quasar
        self.device = device
        did = device.get("quasar_info", {}).get("device_id") or device.get("id")
        self._did = did
        self._attr_unique_id = f"{entry.entry_id}_{did}_local_connection"
        self.entity_id = f"binary_sensor.yandex_station_{slugify(self._attr_unique_id)}"
        self._attr_is_on = False
        self._software_version = None
        self._last_heartbeat = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, did)},
            name=device["name"],
        )

    async def async_added_to_hass(self):
        entity = self.device.get("entity")
        if entity and getattr(entity, "local_state", None):
            self._attr_is_on = True
        self.async_on_remove(
            self.quasar.subscribe_update(f"{self._did}_glagol", self._on_glagol_update)
        )

    def _on_glagol_update(self, data: dict | None):
        if data and "state" in data:
            self._attr_is_on = True
            if sw := data.get("softwareVersion"):
                self._software_version = sw
            self._last_heartbeat = dt_util.utcnow().isoformat()
        else:
            self._attr_is_on = False
        if self.hass:
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {
            "host": self.device.get("host"),
            "port": self.device.get("port"),
        }
        if self._software_version:
            attrs["software_version"] = self._software_version
        if self._last_heartbeat:
            attrs["last_heartbeat"] = self._last_heartbeat
        return attrs
