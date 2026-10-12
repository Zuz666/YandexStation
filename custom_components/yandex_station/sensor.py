import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    LIGHT_LUX,
    MAJOR_VERSION,
    MINOR_VERSION,
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfVolume,
)

from .core.entity import YandexCustomEntity
from .hass import hass_utils

from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.util import slugify
from .core.const import DOMAIN
from .core.policy import AuthState, DevicesState
_LOGGER = logging.getLogger(__name__)

if (MAJOR_VERSION, MINOR_VERSION) >= (2026, 7):
    from homeassistant.const import UnitOfDensity, UnitOfRatio

    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER = UnitOfDensity.MICROGRAMS_PER_CUBIC_METER
    CONCENTRATION_PARTS_PER_MILLION = UnitOfRatio.PARTS_PER_MILLION
else:
    from homeassistant.const import CONCENTRATION_MICROGRAMS_PER_CUBIC_METER
    from homeassistant.const import CONCENTRATION_PARTS_PER_MILLION

# https://yandex.ru/dev/dialogs/smart-home/doc/concepts/device-type-sensor.html
INCLUDE_TYPES = (
    "devices.types.sensor",
    "devices.types.sensor.button",
    "devices.types.sensor.climate",
    "devices.types.sensor.gas",
    "devices.types.sensor.illumination",
    "devices.types.sensor.motion",
    "devices.types.sensor.open",
    "devices.types.sensor.presence",
    "devices.types.sensor.smoke",
    "devices.types.sensor.vibration",
    "devices.types.sensor.water_leak",
    "devices.types.openable.valve",
    "devices.types.smart_meter",
    "devices.types.smart_meter.cold_water",
    "devices.types.smart_meter.electricity",
    "devices.types.smart_meter.gas",
    "devices.types.smart_meter.heat",
    "devices.types.smart_meter.hot_water",
    "devices.types.socket",
    "devices.types.remote_car",  # fuel_level, petrol_mileage
    "devices.types.remote.ir",  # temperature, humidity
    "devices.types.smart_speaker.yandex.station.pickle",  # co2_level, temp., hum.
    "devices.types.smart_speaker.yandex.station.plum",  # battery
    "devices.types.vacuum_cleaner",  # battery_level
)
INCLUDE_PROPERTIES = ("devices.properties.float", "devices.properties.event")

SENSOR = SensorDeviceClass  # just to reduce the code

ENTITY_DESCRIPTIONS: dict[str, dict] = {
    "temperature": {
        "class": SENSOR.TEMPERATURE,
        "state": SensorStateClass.MEASUREMENT,
        "units": UnitOfTemperature.CELSIUS,
    },
    "humidity": {
        "class": SENSOR.HUMIDITY,
        "state": SensorStateClass.MEASUREMENT,
        "units": PERCENTAGE,
    },
    "pm2.5_density": {
        "class": SENSOR.PM25,
        "state": SensorStateClass.MEASUREMENT,
        "units": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    },
    "pm10_density": {
        "class": SENSOR.PM10,
        "state": SensorStateClass.MEASUREMENT,
        "units": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    },
    "co2_level": {
        "class": SENSOR.CO2,
        "state": SensorStateClass.MEASUREMENT,
        "units": CONCENTRATION_PARTS_PER_MILLION,
    },
    "illumination": {
        "class": SENSOR.ILLUMINANCE,
        "state": SensorStateClass.MEASUREMENT,
        "units": LIGHT_LUX,
    },
    "battery_level": {
        "class": SENSOR.BATTERY,
        "state": SensorStateClass.MEASUREMENT,
        "units": PERCENTAGE,
    },
    "pressure": {
        "class": SENSOR.PRESSURE,
        "state": SensorStateClass.MEASUREMENT,
        "units": UnitOfPressure.MMHG,
    },
    "voltage": {
        "class": SENSOR.VOLTAGE,
        "state": SensorStateClass.MEASUREMENT,
        "units": UnitOfElectricPotential.VOLT,
    },
    "power": {
        "class": SENSOR.POWER,
        "state": SensorStateClass.MEASUREMENT,
        "units": UnitOfPower.WATT,
    },
    "amperage": {
        "class": SENSOR.CURRENT,
        "state": SensorStateClass.MEASUREMENT,
        "units": UnitOfElectricCurrent.AMPERE,
    },
    "vibration": {"class": SENSOR.ENUM},
    "open": {"class": SENSOR.ENUM},
    "button": {"class": SENSOR.ENUM},
    "motion": {"class": SENSOR.ENUM},
    "occupancy": {"units": "objects"},
    "smoke": {"class": SENSOR.ENUM},
    "gas": {"class": SENSOR.ENUM},
    "food_level": {"class": SENSOR.ENUM},
    "water_level": {"class": SENSOR.ENUM},
    "water_leak": {"class": SENSOR.ENUM},
    "electricity_meter": {
        "class": SENSOR.ENERGY,
        "state": SensorStateClass.TOTAL,
        "units": UnitOfEnergy.KILO_WATT_HOUR,
    },
    "gas_meter": {
        "class": SENSOR.GAS,
        "state": SensorStateClass.TOTAL,
        "units": UnitOfVolume.CUBIC_METERS,
    },
    "water_meter": {
        "class": SENSOR.WATER,
        "state": SensorStateClass.TOTAL,
        "units": UnitOfVolume.CUBIC_METERS,
    },
    # there is no better option than a battery for fuel_level
    "fuel_level": {
        "class": SENSOR.BATTERY,
        "state": SensorStateClass.MEASUREMENT,
        "units": PERCENTAGE,
    },
    "petrol_mileage": {
        "class": SENSOR.DISTANCE,
        "state": SensorStateClass.MEASUREMENT,
        "units": UnitOfLength.KILOMETERS,
    },
}


async def async_setup_entry(hass, entry, async_add_entities):
    entities = []
    quasar: YandexQuasar | None = hass.data.get(DOMAIN, {}).get(entry.unique_id)

    if quasar and quasar.policy.enable_diagnostic_sensors:
        service_device = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Яндекс ({entry.title or entry.unique_id})",
            manufacturer="Yandex",
            model="Account & Local Resilience Gateway",
            entry_type=DeviceEntryType.SERVICE,
        )
        entities.append(YandexCloudHealthSensor(quasar, entry, service_device))
        entities.append(YandexInventorySourceSensor(quasar, entry, service_device))

        for speaker in quasar.speakers + quasar.modules:
            entities.append(YandexTransportSensor(quasar, speaker))
            entities.append(YandexAliceStateSensor(quasar, speaker))
    for quasar, device, config in hass_utils.incluce_devices(hass, entry):
        if "properties" in config:
            instances = config["properties"]
        elif device["type"] in INCLUDE_TYPES:
            instances = ENTITY_DESCRIPTIONS.keys()  # all supported instances
        else:
            continue

        for instance in device["properties"]:
            if instance["type"] not in INCLUDE_PROPERTIES:
                continue
            if instance["parameters"]["instance"] in instances:
                entities.append(YandexCustomSensor(quasar, device, instance))

    async_add_entities(entities)


# noinspection PyAbstractClass
class YandexCustomSensor(SensorEntity, YandexCustomEntity):
    def internal_init(self, capabilities: dict, properties: dict):
        if desc := ENTITY_DESCRIPTIONS.get(self.instance):
            if "class" in desc:
                self._attr_device_class = desc["class"]
            if "state" in desc:
                self._attr_state_class = desc["state"]
            if "units" in desc:
                self._attr_native_unit_of_measurement = desc["units"]
        try:
            if self.config["parameters"]["range"]["precision"] == 1:
                self._attr_suggested_display_precision = 0
        except KeyError:
            pass

    def internal_update(self, capabilities: dict, properties: dict):
        if self.instance in properties:
            self._attr_native_value = properties[self.instance]


class YandexCloudHealthSensor(SensorEntity):
    """Diagnostic sensor reporting the computed cloud readiness state."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Статус связи с облаком"
    _attr_icon = "mdi:cloud-check"

    def __init__(self, quasar: YandexQuasar, entry, device_info: DeviceInfo):
        self.quasar = quasar
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cloud_health"
        self.entity_id = f"sensor.yandex_station_{slugify(self._attr_unique_id)}"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> str:
        return str(self.quasar.readiness.computed_cloud_state(self.quasar.policy))

    @property
    def extra_state_attributes(self) -> dict:
        r = self.quasar.readiness
        return {
            "auth_state": str(r.auth_state),
            "csrf_state": str(r.csrf_state),
            "devices_state": str(r.devices_state),
            "scenarios_state": str(r.scenarios_state),
            "inventory_source": str(r.inventory_source),
            "last_snapshot_at": r.last_snapshot_at,
        }


class YandexInventorySourceSensor(SensorEntity):
    """Diagnostic sensor reporting whether catalog is from cloud or cache."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_name = "Источник каталога"
    _attr_icon = "mdi:database-sync"

    def __init__(self, quasar: YandexQuasar, entry, device_info: DeviceInfo):
        self.quasar = quasar
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_inventory_source"
        self.entity_id = f"sensor.yandex_station_{slugify(self._attr_unique_id)}"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> str:
        return str(self.quasar.readiness.inventory_source)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "last_snapshot_at": self.quasar.readiness.last_snapshot_at,
        }


class YandexAliceStateSensor(SensorEntity):
    """Diagnostic sensor reporting speaker Alice voice state."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_has_entity_name = True
    _attr_name = "Состояние Алисы"
    _attr_icon = "mdi:microphone"

    def __init__(self, quasar: YandexQuasar, device: dict):
        self.quasar = quasar
        self.device = device
        did = device.get("quasar_info", {}).get("device_id") or device.get("id")
        self._did = did
        self._attr_unique_id = f"{did}_alice_state"
        self.entity_id = f"sensor.yandex_station_{slugify(self._attr_unique_id)}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, did)},
            name=device["name"],
        )
        self._state = None
        self._unsub = quasar.subscribe_update(f"{self._did}_glagol", self._on_glagol_update)

    async def async_added_to_hass(self):
        entity = self.device.get("entity")
        if entity and getattr(entity, "local_state", None):
            self._state = entity.local_state.get("aliceState", "IDLE")
        self.async_on_remove(self._unsub)

    def _on_glagol_update(self, data: dict | None):
        if data and "state" in data:
            self._state = data["state"].get("aliceState", "IDLE")
        else:
            self._state = None
        if self.hass:
            self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        return self._state


class YandexTransportSensor(SensorEntity):
    """Diagnostic sensor reporting current control transport for a speaker (local / cloud / unavailable)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_name = "Транспорт управления"
    _attr_icon = "mdi:transit-connection-variant"

    def __init__(self, quasar: YandexQuasar, device: dict):
        self.quasar = quasar
        self.device = device
        did = device.get("quasar_info", {}).get("device_id") or device.get("id")
        self._did = did
        self._attr_unique_id = f"{did}_transport"
        self.entity_id = f"sensor.yandex_station_{slugify(self._attr_unique_id)}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, did)},
            name=device["name"],
        )
        self._unsub = quasar.subscribe_update(f"{self._did}_glagol", lambda _: self._on_update())

    async def async_added_to_hass(self):
        self.async_on_remove(self._unsub)

    def _on_update(self):
        if self.hass:
            self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        entity = self.device.get("entity")
        if entity and hasattr(entity, "transport"):
            return entity.transport
        if entity and getattr(entity, "local_state", None) is not None:
            return "local"
        if not self.device.get("capabilities"):
            return "unavailable"
        if not getattr(self.quasar, "policy", None):
            return "unavailable"
        if not self.quasar.policy.effective_cloud_allowed:
            return "unavailable"
        if not self.quasar.policy.enable_scenarios:
            return "unavailable"
        if not self.device.get("scenario_id"):
            return "unavailable"
        readiness = getattr(self.quasar, "readiness", None)
        if readiness:
            if readiness.auth_state != AuthState.VALID:
                return "unavailable"
            if readiness.devices_state != DevicesState.READY:
                return "unavailable"
        return "cloud"
