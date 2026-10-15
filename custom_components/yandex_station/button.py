from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import slugify

from .core.const import DOMAIN
from .core.lifecycle import get_lifecycle
from .core.entity import YandexCustomEntity, YandexEntity, extract_instance
from .core.yandex_quasar import YandexQuasar
from .hass import hass_utils

INCLUDE_CAPABILITIES = ("devices.capabilities.custom.button",)


async def async_setup_entry(hass, entry, async_add_entities):
    entities = []
    quasar = hass.data.get(DOMAIN, {}).get(entry.unique_id)
    if quasar is not None:
        entities.extend(
            YandexAccountButton(hass, quasar, entry, command)
            for command in ("check_cloud", "reload_account", "reload_code")
        )

    for quasar, device, config in hass_utils.incluce_devices(hass, entry):
        # get on_off capability from intercom
        if device["type"] == "devices.types.openable.intercom":
            for instance in device["capabilities"]:
                if instance["type"] == "devices.capabilities.on_off":
                    entities.append(YandexCustomButton(quasar, device, instance))

        if device["type"] == "devices.types.camera.yandex.mike":
            entities.append(YandexCameraButton(quasar, device, "down"))
            entities.append(YandexCameraButton(quasar, device, "up"))
            entities.append(YandexCameraButton(quasar, device, "left"))
            entities.append(YandexCameraButton(quasar, device, "right"))

        if instances := config.get("capabilities"):
            for instance in device["capabilities"]:
                if instance["type"] not in INCLUDE_CAPABILITIES:
                    continue
                if extract_instance(instance) in instances:
                    entities.append(YandexCustomButton(quasar, device, instance))

    await hass_utils.async_translate_entity_names(hass, entities, "button")
    async_add_entities(entities)


class YandexAccountButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, quasar: YandexQuasar, entry, command: str):
        self.quasar = quasar
        self.entry = entry
        self.command = command
        self._lifecycle = get_lifecycle(hass)
        self._attr_translation_key = command
        self._attr_unique_id = f"{entry.entry_id}_{command}"
        self.entity_id = f"button.yandex_station_{slugify(self._attr_unique_id)}"
        self._attr_device_info = hass_utils.account_device_info(entry)
        self._attr_device_class = (
            None if command == "check_cloud" else ButtonDeviceClass.RESTART
        )
        self._attr_icon = "mdi:cloud-check" if command == "check_cloud" else "mdi:reload"
        self._lifecycle.guard_entity(self, entry.entry_id)

    @property
    def available(self) -> bool:
        # The execution guard checks admission. Entities are published while the
        # reload gate is closed, and non-polling buttons cannot refresh that flag.
        return not self.quasar.stopping and self._lifecycle.entry_enabled(self.entry)

    async def async_press(self) -> None:
        if self.command == "check_cloud":
            result = await self.quasar.async_check_cloud()
            if result["status"] != "ok":
                raise HomeAssistantError("check_cloud: " + result["reason"])
            return
        data = {"entry_id": self.entry.entry_id} if self.command == "reload_account" else {}
        # Keep the original user context: an entity-control permission is not
        # permission to reload integration code or another account.
        await self.hass.services.async_call(
            DOMAIN, self.command, data, blocking=True, context=self._context
        )


# noinspection PyAbstractClass
class YandexCustomButton(ButtonEntity, YandexCustomEntity):
    async def async_press(self) -> None:
        await self.device_action(self.instance, True)


class YandexCameraButton(ButtonEntity, YandexEntity):
    def __init__(self, quasar: YandexQuasar, device: dict, direction: str):
        super().__init__(quasar, device)
        self._attr_name += " " + direction
        self._attr_unique_id += " " + direction

        if direction == "left":
            self.instance = "camera_pan"
            self.value = -1
        elif direction == "right":
            self.instance = "camera_pan"
            self.value = 1
        elif direction == "down":
            self.instance = "camera_tilt"
            self.value = -1
        elif direction == "up":
            self.instance = "camera_tilt"
            self.value = 1

    async def async_press(self) -> None:
        await self.device_action(self.instance, self.value, relative=True)
