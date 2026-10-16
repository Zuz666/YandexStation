import asyncio
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers import area_registry, entity_registry
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from custom_components.yandex_station.binary_sensor import (
    YandexStationLocalConnection,
    async_setup_entry as bs_setup_entry,
)
from custom_components.yandex_station.core.const import DATA_CONFIG, DOMAIN
from custom_components.yandex_station.core.policy import (
    AuthState,
    DevicesState,
    EntryPolicy,
    Readiness,
    ScenariosState,
)
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
from custom_components.yandex_station.core.yandex_station import YandexStation
from custom_components.yandex_station.sensor import (
    YandexAliceStateSensor,
    YandexCloudHealthSensor,
    YandexInventorySourceSensor,
    YandexCustomSensor,
    YandexTransportSensor,
    async_setup_entry as sensor_setup_entry,
)
from .test_cache import device_record
from .test_ownership import ownership_runtime

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

        entry = MagicMock(entry_id="entry-1")
        sensor = YandexStationLocalConnection(quasar, entry, device)
        sensor.hass = MagicMock()
        sensor.async_write_ha_state = MagicMock()

        # 1. Before connection
        assert sensor.is_on is False
        assert sensor.device_class == BinarySensorDeviceClass.CONNECTIVITY
        assert sensor.entity_category == EntityCategory.DIAGNOSTIC
        assert f"{did}_glagol" not in quasar.dispatcher

        await sensor.async_added_to_hass()
        assert not sensor.should_poll
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
def test_quasar_sync_transitions_readiness_to_online():
    async def scenario():
        session = MagicMock()
        policy = EntryPolicy()
        readiness = Readiness()
        quasar = YandexQuasar(session, policy=policy, readiness=readiness)

        mock_resp_devices = AsyncMock()
        mock_resp_devices.json.return_value = {
            "status": "ok",
            "households": [{"name": "Home", "all": []}],
        }

        async def fake_get(url, **kwargs):
            if "scenarios" in url:
                raise AssertionError("No speakers require scenario inventory")
            return mock_resp_devices

        session.get = AsyncMock(side_effect=fake_get)

        assert readiness.auth_state == AuthState.UNKNOWN
        assert readiness.devices_state == DevicesState.UNKNOWN
        # bounded bootstrap does inventory only: scenarios stay untouched (§5)
        assert readiness.scenarios_state == ScenariosState.UNKNOWN

        await quasar.init()

        assert readiness.auth_state == AuthState.VALID
        assert readiness.devices_state == DevicesState.READY
        assert readiness.inventory_source == "cloud"
        assert readiness.scenarios_state == ScenariosState.UNKNOWN

        entry = MagicMock()
        entry.entry_id = "test_e1"
        service_device = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Яндекс",
            entry_type=DeviceEntryType.SERVICE,
        )
        health_sensor = YandexCloudHealthSensor(quasar, entry, service_device)
        assert health_sensor.native_value == "online"

    asyncio.run(scenario())
def test_sensor_subscribes_to_readiness_updates_and_writes_state():
    async def scenario():
        session = MagicMock()
        policy = EntryPolicy()
        readiness = Readiness()
        quasar = YandexQuasar(session, policy=policy, readiness=readiness)

        entry = MagicMock()
        entry.entry_id = "test_e2"
        service_device = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Яндекс",
            entry_type=DeviceEntryType.SERVICE,
        )
        health_sensor = YandexCloudHealthSensor(quasar, entry, service_device)
        health_sensor.hass = MagicMock()
        health_sensor.async_write_ha_state = MagicMock()

        await health_sensor.async_added_to_hass()

        # Dispatching readiness update must trigger async_write_ha_state
        readiness.auth_state = AuthState.VALID
        readiness.devices_state = DevicesState.READY
        readiness.scenarios_state = ScenariosState.READY
        quasar.dispatch_update("readiness", {})

        health_sensor.async_write_ha_state.assert_called_once()
        assert health_sensor.native_value == "online"

    asyncio.run(scenario())


def test_quasar_sync_scenarios_failure_degrades_readiness():
    async def scenario():
        session = MagicMock()
        policy = EntryPolicy()
        readiness = Readiness()
        quasar = YandexQuasar(session, policy=policy, readiness=readiness)
        quasar.owned_glagol_ids = {"sp_1"}

        speaker_device = {
            "id": "speaker_dev_1",
            "name": "Станция Спальня",
            "type": "devices.types.smart_speaker.yandex.station",
            "quasar_info": {"device_id": "sp_1", "platform": "yandexstation"},
            "capabilities": [{
                "type": "devices.capabilities.quasar.server_action",
                "parameters": {"instance": "text_action"},
                "state": {"instance": "text_action", "value": "ничего не делай"},
            }],
        }
        mock_resp_devices = AsyncMock()
        mock_resp_devices.json.return_value = {
            "status": "ok",
            "households": [{"name": "Home", "all": [speaker_device]}],
        }
        quasar.add_scenario = AsyncMock()

        async def fake_get(url, **kwargs):
            if "scenarios" in url:
                raise RuntimeError("500 Internal Server Error")
            return mock_resp_devices

        session.get = AsyncMock(side_effect=fake_get)

        await quasar.init()

        assert readiness.auth_state == AuthState.VALID
        assert readiness.devices_state == DevicesState.READY
        # scenario recovery is an explicit pass after publication, not part of init
        assert readiness.scenarios_state == ScenariosState.UNKNOWN
        await quasar.load_scenarios()
        assert readiness.scenarios_state == ScenariosState.UNAVAILABLE
        # Assert that zero scenario creation POSTs were attempted when catalog is unavailable
        quasar.add_scenario.assert_not_called()
        assert speaker_device.get("scenario_id") is None
        entry = MagicMock()
        entry.entry_id = "test_e3"
        service_device = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Яндекс",
            entry_type=DeviceEntryType.SERVICE,
        )
        health_sensor = YandexCloudHealthSensor(quasar, entry, service_device)
        assert health_sensor.native_value == "degraded"

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
        quasar.owned_glagol_ids = {did}
        device = {
            "id": "cloud_sp_200",
            "name": "Станция Спальня",
            "quasar_info": {"device_id": did, "platform": "yandexstation"},
            "capabilities": [{"type": "devices.capabilities.on_off"}],
            "scenario_id": "scen_valid_1",
        }
        station = YandexStation(quasar, device)
        device["entity"] = station

        entry = MagicMock(entry_id="entry-1")
        trans_sensor = YandexTransportSensor(quasar, entry, device)
        alice_sensor = YandexAliceStateSensor(quasar, entry, device)

        trans_sensor.hass = MagicMock()
        trans_sensor.async_write_ha_state = MagicMock()
        alice_sensor.hass = MagicMock()
        alice_sensor.async_write_ha_state = MagicMock()
        assert f"{did}_glagol" not in quasar.dispatcher
        await trans_sensor.async_added_to_hass()
        await alice_sensor.async_added_to_hass()
        assert not trans_sensor.should_poll
        assert not alice_sensor.should_poll
        # Initially without local Glagol message: cloud transport if cloud ready
        assert trans_sensor.native_value == "cloud"
        assert alice_sensor.native_value is None
        assert alice_sensor.entity_registry_enabled_default is False

        readiness.devices_state = DevicesState.UNAVAILABLE
        quasar.dispatch_update("readiness", readiness)
        assert trans_sensor.native_value == "unavailable"
        assert trans_sensor.async_write_ha_state.call_count == 1
        readiness.devices_state = DevicesState.READY
        quasar.dispatch_update("readiness", readiness)
        assert trans_sensor.native_value == "cloud"
        assert trans_sensor.async_write_ha_state.call_count == 2

        # When local Glagol message arrives: transitions to local and Alice state updates
        station.async_set_state({
            "state": {"aliceState": "LISTENING", "volume": 0.4},
        })
        assert trans_sensor.native_value == "local"
        assert alice_sensor.native_value == "listening"

        # On Glagol disconnect: transport becomes cloud (or unavailable if offline)
        station.async_set_state(None)
        assert trans_sensor.native_value == "cloud"
        assert alice_sensor.native_value is None

        # Test module device (no capabilities) -> unavailable when not local
        module_device = {
            "id": "mod_1",
            "name": "Модуль",
            "capabilities": [],
            "quasar_info": {"device_id": "mod_did", "platform": "yandexmodule"},
        }
        mod_sensor = YandexTransportSensor(quasar, entry, module_device)
        assert mod_sensor.native_value == "unavailable"

        # Test speaker without scenario_id -> unavailable when not local
        device_no_scen = dict(device)
        device_no_scen.pop("entity", None)
        device_no_scen["scenario_id"] = None
        no_scen_sensor = YandexTransportSensor(quasar, entry, device_no_scen)
        assert no_scen_sensor.native_value == "unavailable"

        # Verify station entity itself returns unavailable when scenario_id is None
        device["scenario_id"] = None
        assert station.transport == "unavailable"
    asyncio.run(scenario())


def test_diagnostic_sensors_disabled_by_policy():
    async def scenario():
        hass = MagicMock()
        entry = MagicMock()
        assert not EntryPolicy.from_configs({}, {}).enable_diagnostic_sensors
        assert EntryPolicy.from_configs(
            {"enable_diagnostic_sensors": True}, {}
        ).enable_diagnostic_sensors
        assert not EntryPolicy.from_configs(
            {"enable_diagnostic_sensors": True},
            {"enable_diagnostic_sensors": False},
        ).enable_diagnostic_sensors
        entry.entry_id = "e1"
        entry.unique_id = "u1"
        entry.options = {}

        session = MagicMock()
        policy = EntryPolicy()
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


def test_diagnostics_opt_in_preserves_ordinary_sensor_and_owned_registry(tmp_path):
    async def scenario():
        async with ownership_runtime(tmp_path) as (hass, entries, _, _):
            entry = entries["a"]
            speaker = device_record("cloud-speaker", "shared-local")
            ordinary = {
                "id": "ordinary-cloud", "name": "Ordinary sensor",
                "type": "devices.types.sensor.climate", "capabilities": [],
                "properties": [{
                    "type": "devices.properties.float", "retrievable": True,
                    "parameters": {"instance": "temperature"},
                    "state": {"instance": "temperature", "value": 23},
                }],
            }
            quasar = YandexQuasar(MagicMock(), policy=EntryPolicy())
            quasar.devices = [speaker, ordinary]
            quasar.owned_glagol_ids = {"shared-local"}
            hass.data[DOMAIN][entry.unique_id] = quasar
            hass.data[DOMAIN][DATA_CONFIG]["include"] = ["ordinary-cloud"]
            sensors = []
            await sensor_setup_entry(hass, entry, sensors.extend)
            assert len(sensors) == 1
            assert isinstance(sensors[0], YandexCustomSensor)
            assert sensors[0].native_value == 23
            quasar.policy.enable_diagnostic_sensors = True

            registry = entity_registry.async_get(hass)
            area = area_registry.async_get(hass).async_create("Preserved area")
            previous = {}
            for domain, suffix in (
                ("sensor", "transport"), ("sensor", "alice_state"),
                ("binary_sensor", "local_connection"),
            ):
                old = registry.async_get_or_create(
                    domain, DOMAIN, f"shared-local_{suffix}",
                    config_entry=entry, suggested_object_id=f"preserved_{suffix}",
                )
                previous[suffix] = registry.async_update_entity(
                    old.entity_id, name=f"User {suffix}", area_id=area.id,
                    disabled_by=entity_registry.RegistryEntryDisabler.USER,
                )
            sensor_entities = []
            binary_entities = []
            await sensor_setup_entry(hass, entry, sensor_entities.extend)
            await bs_setup_entry(hass, entry, binary_entities.extend)
            assert len(sensor_entities) == 5
            assert len(binary_entities) == 1
            assert any(isinstance(e, YandexCustomSensor) for e in sensor_entities)
            for entity in sensor_entities + binary_entities:
                if isinstance(entity, YandexCustomSensor):
                    continue
                assert not entity.should_poll
                assert entity.has_entity_name
            for suffix, original in previous.items():
                domain = "binary_sensor" if suffix == "local_connection" else "sensor"
                record = registry.async_get(original.entity_id)
                assert record is not None
                assert record.unique_id == f"{entry.entry_id}_shared-local_{suffix}"
                assert record.config_entry_id == entry.entry_id
                assert record.name == f"User {suffix}"
                assert record.area_id == area.id
                assert record.disabled_by == entity_registry.RegistryEntryDisabler.USER
                assert registry.async_get_entity_id(
                    domain, DOMAIN, f"shared-local_{suffix}"
                ) is None

            registry.async_update_entity(
                previous["transport"].entity_id,
                config_entry_id=entries["b"].entry_id,
            )
            after_conflict = []
            await sensor_setup_entry(hass, entry, after_conflict.extend)
            assert not any(
                isinstance(e, YandexTransportSensor)
                for e in after_conflict
            )
            assert registry.async_get(
                previous["transport"].entity_id
            ).config_entry_id == entries["b"].entry_id

            foreign_legacy = registry.async_get_or_create(
                "binary_sensor", DOMAIN, "foreign-local_local_connection",
                config_entry=entries["b"],
                suggested_object_id="foreign_local_connection",
            )
            quasar.devices.append(device_record("cloud-other", "foreign-local"))
            quasar.owned_glagol_ids.add("foreign-local")
            after_foreign_legacy = []
            await bs_setup_entry(hass, entry, after_foreign_legacy.extend)
            assert any(
                e.unique_id == f"{entry.entry_id}_foreign-local_local_connection"
                for e in after_foreign_legacy
            )
            assert registry.async_get(foreign_legacy.entity_id).config_entry_id == entries["b"].entry_id
            assert registry.async_get(foreign_legacy.entity_id).unique_id == "foreign-local_local_connection"

            diagnostic_entities = [
                e for e in sensor_entities + binary_entities
                if not isinstance(e, YandexCustomSensor)
            ]
            signal = "shared-local_glagol"
            assert not quasar.dispatcher.get(signal)
            for entity in diagnostic_entities:
                entity.hass = hass
                entity.async_write_ha_state = MagicMock()
                await entity.async_added_to_hass()
            assert len(quasar.dispatcher[signal]) == 3
            assert len(quasar.dispatcher["readiness"]) == 3
            speaker["entity"] = YandexStation(quasar, speaker)
            speaker["entity"].async_set_state(
                {"state": {"aliceState": "SPEAKING", "volume": 0.4}}
            )
            local = binary_entities[0]
            assert local.is_on
            assert next(
                e for e in sensor_entities if isinstance(e, YandexAliceStateSensor)
            ).native_value == "speaking"
            for entity in diagnostic_entities:
                await entity.async_remove(force_remove=True)
            assert not quasar.dispatcher[signal]
            assert not quasar.dispatcher["readiness"]
    asyncio.run(scenario())
