import asyncio

from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import issue_registry

from custom_components.yandex_station.core.entity import YandexEntity
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
from custom_components.yandex_station.core.yandex_station import YandexStationBase

def core_hass(config_dir) -> HomeAssistant:
    """Real Core instance across supported constructor signatures."""
    try:
        hass = HomeAssistant(str(config_dir))
    except TypeError:
        hass = HomeAssistant()
    hass.config.config_dir = str(config_dir)
    return hass


async def seed_config_entries(hass, records) -> None:
    """Seed a real ConfigEntries store before Core initialization."""
    await issue_registry.async_load(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries._store.async_save({"entries": records})
    await hass.config_entries.async_initialize()


class FakeQuasar(YandexQuasar):
    def __init__(self, data: dict = None):
        super().__init__(None)
        self.data = data

    async def get_device(self, *args):
        return self.data

    def subscribe_update(self, *args):
        pass


class FakeYandexStation(YandexStationBase):
    def __init__(self):
        quasar = FakeQuasar()
        device = {
            "id": "",
            "name": "",
            "quasar_info": {"device_id": "", "platform": ""},
            "capabilities": [],
        }
        super().__init__(quasar, device)


def update_ha_state(cls, device: dict, **kwargs) -> State:
    device.setdefault("id", "ID")
    device.setdefault("name", "NAME")
    device.setdefault("capabilities", [])
    device.setdefault("properties", [])

    async def update():
        entity: YandexEntity = cls(FakeQuasar(device), device, **kwargs)
        try:
            entity.hass = HomeAssistant("")
        except TypeError:
            entity.hass = HomeAssistant()
        entity.entity_id = "domain.object_id"
        await entity.async_update_ha_state(True)
        return entity.hass.states.get(entity.entity_id)

    return asyncio.run(update())


# JSON => Python
true = True
false = False
null = None
