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
from .core.yandex_quasar import YandexQuasar
from .hass import hass_utils

from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.util import slugify
from .core.const import DOMAIN
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
        service_device = hass_utils.account_device_info(entry)
        entities.append(YandexCloudHealthSensor(quasar, entry, service_device))
        entities.append(YandexInventorySourceSensor(quasar, entry, service_device))

        for speaker in quasar.owned_speakers + quasar.owned_modules:
            did = speaker.get("quasar_info", {}).get("device_id") or speaker.get("id")
            transport = YandexTransportSensor(quasar, entry, speaker)
            if hass_utils.migrate_diagnostic_unique_id(
                hass, entry, "sensor", f"{did}_transport", transport.unique_id
            ):
                entities.append(transport)
            alice = YandexAliceStateSensor(quasar, entry, speaker)
            if hass_utils.migrate_diagnostic_unique_id(
                hass, entry, "sensor", f"{did}_alice_state", alice.unique_id
            ):
                entities.append(alice)
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

    await hass_utils.async_translate_entity_names(hass, entities, "sensor")
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


class YandexAccountReadinessSensor(SensorEntity):
    """Account diagnostic sensor republishing readiness without polling."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_has_entity_name = True
    _unique_id_suffix: str

    def __init__(self, quasar: YandexQuasar, entry, device_info: DeviceInfo):
        self.quasar = quasar
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{self._unique_id_suffix}"
        self.entity_id = f"sensor.yandex_station_{slugify(self._attr_unique_id)}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self):
        self.async_on_remove(
            self.quasar.subscribe_update("readiness", self._handle_readiness_update)
        )

    def _handle_readiness_update(self, message):
        if self.hass:
            self.async_write_ha_state()


class YandexCloudHealthSensor(YandexAccountReadinessSensor):
    """Diagnostic sensor reporting the computed cloud readiness state."""

    _attr_translation_key = "cloud_health"
    _attr_icon = "mdi:cloud-check"
    _unique_id_suffix = "cloud_health"

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


class YandexInventorySourceSensor(YandexAccountReadinessSensor):
    """Diagnostic sensor reporting whether catalog is from cloud or cache."""

    _attr_translation_key = "inventory_source"
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:database-sync"
    _unique_id_suffix = "inventory_source"

    @property
    def native_value(self) -> str:
        return str(self.quasar.readiness.inventory_source)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "last_snapshot_at": self.quasar.readiness.last_snapshot_at,
        }


class YandexSpeakerDiagnosticSensor(SensorEntity):
    """Base class for non-polling speaker diagnostic sensors."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_has_entity_name = True
    _unique_id_suffix: str

    def __init__(self, quasar: YandexQuasar, entry, device: dict):
        self.quasar = quasar
        self.device = device
        did = device.get("quasar_info", {}).get("device_id") or device.get("id")
        self._did = did
        self._attr_unique_id = f"{entry.entry_id}_{did}_{self._unique_id_suffix}"
        self.entity_id = f"sensor.yandex_station_{slugify(self._attr_unique_id)}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, did)},
            name=device["name"],
        )


class YandexAliceStateSensor(YandexSpeakerDiagnosticSensor):
    """Diagnostic sensor reporting speaker Alice voice state."""

    _attr_translation_key = "alice_state"
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:microphone"
    _unique_id_suffix = "alice_state"

    def __init__(self, quasar: YandexQuasar, entry, device: dict):
        super().__init__(quasar, entry, device)
        self._state = None

    async def async_added_to_hass(self):
        entity = self.device.get("entity")
        if entity and getattr(entity, "local_state", None):
            self._state = entity.local_state.get("aliceState", "IDLE")
        self.async_on_remove(
            self.quasar.subscribe_update(f"{self._did}_glagol", self._on_glagol_update)
        )

    def _on_glagol_update(self, data: dict | None):
        if data and "state" in data:
            self._state = data["state"].get("aliceState", "IDLE")
        else:
            self._state = None
        if self.hass:
            self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        return self._state.lower() if isinstance(self._state, str) else None


class YandexTransportSensor(YandexSpeakerDiagnosticSensor):
    """Diagnostic sensor reporting current control transport for a speaker (local / cloud / unavailable)."""

    _attr_translation_key = "transport"
    _attr_icon = "mdi:transit-connection-variant"
    _unique_id_suffix = "transport"
    async def async_added_to_hass(self):
        self.async_on_remove(
            self.quasar.subscribe_update(f"{self._did}_glagol", lambda _: self._on_update())
        )
        self.async_on_remove(
            self.quasar.subscribe_update("readiness", lambda _: self._on_update())
        )

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
        if not self.quasar.cloud_control_ready(self.device):
            return "unavailable"
        return "cloud"
