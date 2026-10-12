import asyncio
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory

from custom_components.yandex_station.__init__ import SPEAKER_PLATFORMS
from custom_components.yandex_station.binary_sensor import (
    YandexStationLocalConnection,
    async_setup_entry as bs_setup_entry,
)
from custom_components.yandex_station.core.const import DOMAIN
from custom_components.yandex_station.core.policy import AuthState, DevicesState, EntryPolicy, Readiness
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
from custom_components.yandex_station.core.yandex_station import YandexStation
from custom_components.yandex_station.sensor import (
    YandexAliceStateSensor,
    YandexCloudHealthSensor,
    YandexInventorySourceSensor,
    YandexTransportSensor,
    async_setup_entry as sensor_setup_entry,
)

def test_speaker_platforms_includes_sensors():
    assert "binary_sensor" in SPEAKER_PLATFORMS
    assert "sensor" in SPEAKER_PLATFORMS


def test_station_local_connection_binary_sensor():
    async def scenario():
        session = MagicMock()
        quasar = YandexQuasar(session)
        did = "glagol_did_100"
        device = {
            "id": "cloud_id_100",
            "name": "Станция Зал",
            "host": "192.168.1.100",
            "port": 1961,
            "quasar_info": {"device_id": did, "platform": "yandexstation"},
            "capabilities": [{"type": "devices.capabilities.on_off"}],
        }

        station = YandexStation(quasar, device)
        device["entity"] = station

        sensor = YandexStationLocalConnection(quasar, device)
        sensor.hass = MagicMock()
        sensor.async_write_ha_state = MagicMock()

        # 1. Before connection
        assert sensor.is_on is False
        assert sensor.device_class == BinarySensorDeviceClass.CONNECTIVITY
        assert sensor.entity_category == EntityCategory.DIAGNOSTIC
        assert sensor.unique_id == f"{did}_local_connection"

        # 2. On confirmed Glagol status frame
        station.async_set_state({
            "state": {"aliceState": "IDLE", "volume": 0.5},
            "softwareVersion": "2.5.0",
        })
        assert sensor.is_on is True
        assert sensor.extra_state_attributes["host"] == "192.168.1.100"
        assert sensor.extra_state_attributes["software_version"] == "2.5.0"
        assert "last_heartbeat" in sensor.extra_state_attributes

        # 3. On Glagol disconnect (data=None)
        station.async_set_state(None)
        assert sensor.is_on is False

    asyncio.run(scenario())


def test_service_device_and_account_sensors():
    async def scenario():
        session = MagicMock()
        readiness = Readiness(
            auth_state=AuthState.VALID,
            devices_state=DevicesState.READY,
            inventory_source="cloud",
            last_snapshot_at="2026-10-13T12:00:00Z",
        )
        quasar = YandexQuasar(session, readiness=readiness)

        entry = MagicMock()
        entry.entry_id = "entry_uuid_1"
        entry.title = "my_yandex_account"
        entry.unique_id = "test_user"

        service_device = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Яндекс",
            entry_type=DeviceEntryType.SERVICE,
        )
        health_sensor = YandexCloudHealthSensor(quasar, entry, service_device)
        inv_sensor = YandexInventorySourceSensor(quasar, entry, service_device)
        dev_info = health_sensor.device_info
        assert dev_info["entry_type"] == DeviceEntryType.SERVICE
        assert dev_info["identifiers"] == {(DOMAIN, "entry_uuid_1")}

        # State and category checks
        assert health_sensor.entity_category == EntityCategory.DIAGNOSTIC
        assert health_sensor.native_value == "online"
        assert health_sensor.extra_state_attributes["auth_state"] == "valid"
        assert health_sensor.extra_state_attributes["inventory_source"] == "cloud"

        assert inv_sensor.entity_category == EntityCategory.DIAGNOSTIC
        assert inv_sensor.native_value == "cloud"
        assert inv_sensor.extra_state_attributes["last_snapshot_at"] == "2026-10-13T12:00:00Z"

    asyncio.run(scenario())
def test_speaker_transport_and_alice_sensors():
    async def scenario():
        session = MagicMock()
        readiness = Readiness(
            auth_state=AuthState.VALID,
            devices_state=DevicesState.READY,
        )
        quasar = YandexQuasar(session, readiness=readiness)
        did = "did_sp_200"
        device = {
            "id": "cloud_sp_200",
            "name": "Станция Спальня",
            "quasar_info": {"device_id": did, "platform": "yandexstation"},
            "capabilities": [{"type": "devices.capabilities.on_off"}],
            "scenario_id": "scen_valid_1",
        }
        station = YandexStation(quasar, device)
        device["entity"] = station

        trans_sensor = YandexTransportSensor(quasar, device)
        alice_sensor = YandexAliceStateSensor(quasar, device)

        trans_sensor.hass = MagicMock()
        trans_sensor.async_write_ha_state = MagicMock()
        alice_sensor.hass = MagicMock()
        alice_sensor.async_write_ha_state = MagicMock()
        # Initially without local Glagol message: cloud transport if cloud ready
        assert trans_sensor.native_value == "cloud"
        assert alice_sensor.native_value is None
        assert alice_sensor.entity_registry_enabled_default is False

        # When local Glagol message arrives: transitions to local and Alice state updates
        station.async_set_state({
            "state": {"aliceState": "LISTENING", "volume": 0.4},
        })
        assert trans_sensor.native_value == "local"
        alice_sensor._on_glagol_update({"state": {"aliceState": "LISTENING"}})
        assert alice_sensor.native_value == "LISTENING"

        # On Glagol disconnect: transport becomes cloud (or unavailable if offline)
        station.async_set_state(None)
        assert trans_sensor.native_value == "cloud"
        alice_sensor._on_glagol_update(None)
        assert alice_sensor.native_value is None

        # Test module device (no capabilities) -> unavailable when not local
        module_device = {
            "id": "mod_1",
            "name": "Модуль",
            "capabilities": [],
            "quasar_info": {"device_id": "mod_did", "platform": "yandexmodule"},
        }
        mod_sensor = YandexTransportSensor(quasar, module_device)
        assert mod_sensor.native_value == "unavailable"

        # Test speaker without scenario_id -> unavailable when not local
        device_no_scen = dict(device)
        device_no_scen.pop("entity", None)
        device_no_scen["scenario_id"] = None
        no_scen_sensor = YandexTransportSensor(quasar, device_no_scen)
        assert no_scen_sensor.native_value == "unavailable"

        # Verify station entity itself returns unavailable when scenario_id is None
        device["scenario_id"] = None
        assert station.transport == "unavailable"
    asyncio.run(scenario())


def test_diagnostic_sensors_disabled_by_policy():
    async def scenario():
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e1"
        entry.unique_id = "u1"
        entry.options = {"enable_diagnostic_sensors": False}

        session = MagicMock()
        policy = EntryPolicy(enable_diagnostic_sensors=False)
        quasar = YandexQuasar(session, policy=policy)
        quasar.devices = [{
            "id": "dev1",
            "name": "Speaker",
            "capabilities": [{"type": "devices.capabilities.on_off"}],
            "quasar_info": {"device_id": "did1", "platform": "yandexstation"},
        }]
        hass.data = {DOMAIN: {"u1": quasar, "config": {}}}

        added_bs = []
        await bs_setup_entry(hass, entry, lambda entities: added_bs.extend(entities))
        # No diagnostic binary sensors added
        assert not any(isinstance(e, YandexStationLocalConnection) for e in added_bs)

        added_sensors = []
        await sensor_setup_entry(hass, entry, lambda entities: added_sensors.extend(entities))
        # No service or diagnostic sensors added
        assert not any(isinstance(e, (YandexCloudHealthSensor, YandexInventorySourceSensor, YandexAliceStateSensor, YandexTransportSensor)) for e in added_sensors)

    asyncio.run(scenario())
