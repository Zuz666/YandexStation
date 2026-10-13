import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.components.binary_sensor import HomeAssistant  # important for tests
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MEDIA_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_DEVICES,
    CONF_DOMAIN,
    CONF_HOST,
    CONF_INCLUDE,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_TOKEN,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .core import stream, utils
from .core.const import CONF_MEDIA_PLAYERS, DATA_CONFIG, DATA_SPEAKERS, DOMAIN
from .core.yandex_glagol import YandexIOListener
from .core.yandex_quasar import YandexQuasar
from .core.yandex_session import YandexSession, safe_error_summary
from .core.yandex_station import YandexStationBase
from .hass import hass_utils
from .core.policy import AuthState, DevicesState, EntryPolicy, SessionConfig
from .core.lifecycle import StableStreamDispatcher, get_lifecycle
from .core.cache import CacheManager

_LOGGER = logging.getLogger(__name__)

# only for speakers
SPEAKER_PLATFORMS = [
    "binary_sensor",
    "calendar",
    "camera",
    "media_player",
    "select",
    "sensor",
]
# for import section
PLATFORMS = [
    "binary_sensor",
    "button",
    "calendar",
    "camera",
    "climate",
    "cover",
    "fan",
    "humidifier",
    "light",
    "lock",
    "media_player",
    "number",
    "remote",
    "select",
    "sensor",
    "switch",
    "vacuum",
    "water_heater",
]

CONF_TTS_NAME = "tts_service_name"
CONF_DEBUG = "debug"
CONF_RECOGNITION_LANG = "recognition_lang"
CONF_PROXY = "proxy"
CONF_SSL = "ssl"

CONF_CLOUD_UPDATES = "cloud_updates"
CONF_LOCAL_UPDATES = "local_updates"

def _validate_session_config(config):
    try:
        SessionConfig.from_configs(config)
    except ValueError as error:
        raise vol.Invalid(str(error)) from error
    return config


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(vol.Schema(
            {
                vol.Optional(CONF_USERNAME): cv.string,
                vol.Optional(CONF_PASSWORD): cv.string,
                vol.Optional(CONF_TOKEN): cv.string,
                vol.Optional(CONF_TTS_NAME): cv.string,
                vol.Optional(CONF_INCLUDE): cv.ensure_list,
                vol.Optional(CONF_DEVICES): {
                    cv.string: vol.Schema(
                        {
                            vol.Optional(CONF_HOST): cv.string,
                            vol.Optional(CONF_PORT, default=1961): cv.port,
                        },
                        extra=vol.ALLOW_EXTRA,
                    ),
                },
                vol.Optional(CONF_MEDIA_PLAYERS): vol.Any(dict, list),
                vol.Optional(CONF_RECOGNITION_LANG): cv.string,
                vol.Optional(CONF_DOMAIN): cv.string,
                vol.Optional(CONF_PROXY): cv.string,
                vol.Optional(CONF_SSL): cv.boolean,
                vol.Optional(CONF_CLOUD_UPDATES): cv.boolean,
                vol.Optional(CONF_LOCAL_UPDATES): cv.boolean,
                vol.Optional(CONF_DEBUG, default=False): cv.boolean,
                vol.Optional("enable_scenarios"): cv.boolean,
                vol.Optional("use_cached_devices"): cv.boolean,
                vol.Optional("force_offline_local"): cv.boolean,
                vol.Optional("quasar_base_url"): cv.string,
                vol.Optional("account_base_url"): cv.string,
                vol.Optional("passport_base_url"): cv.string,
                vol.Optional("mobileproxy_base_url"): cv.string,
                vol.Optional("glagol_base_url"): cv.string,
                vol.Optional("rpc_base_url"): cv.string,
                vol.Optional("cookie_storage_endpoints"): cv.ensure_list,
                vol.Optional("csrf_endpoints"): cv.ensure_list,
                vol.Optional("enable_diagnostic_sensors"): cv.boolean,
            },
            extra=vol.ALLOW_EXTRA,
        ), _validate_session_config),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, hass_config: dict):
    config: dict = hass_config.get(DOMAIN) or {}
    lifecycle = get_lifecycle(hass)
    hass.data[DOMAIN][DATA_CONFIG] = config
    hass.data[DOMAIN].setdefault(DATA_SPEAKERS, {})
    context = lifecycle.context
    try:
        from homeassistant.helpers import network
        context.hass_url = network.get_url(hass, allow_external=False)
        stream.StreamView.hass_url = context.hass_url
    except Exception as e:
        _LOGGER.warning(f"Ошибка получения локального адреса Home Assistant: {e}")

    stream.StreamView.hass = hass
    stream.StreamView.key = context.key
    context.implementation = stream.StreamImplementation(context)

    if lifecycle.stream_dispatcher is None:
        lifecycle.stream_dispatcher = StableStreamDispatcher(context)
        hass.http.register_view(lifecycle.stream_dispatcher)

    # if CONF_RECOGNITION_LANG in config:
    #     utils.fix_recognition_lang(
    #         hass, "frontend_latest", config[CONF_RECOGNITION_LANG]
    #     )

    await async_rebind_runtime(hass)
    await _setup_entry_from_config(hass)

    def import_conversation():
        try:
            from . import conversation

            SPEAKER_PLATFORMS.append("conversation")
            PLATFORMS.append("conversation")
        except ImportError as e:
            _LOGGER.warning(repr(e))

    # using executor, because bug with "Detected blocking call"
    await hass.async_add_executor_job(import_conversation)

    async def reload_code_service(call: ServiceCall):
        enable = hass.data[DOMAIN].get(DATA_CONFIG, {}).get("enable_code_reload", False)
        return await lifecycle.async_hot_reload(hass, enable_code_reload=enable)

    hass.services.async_register(DOMAIN, "reload_code", reload_code_service)

    return True


async def async_rebind_runtime(hass: HomeAssistant):
    """Rebind reloadable targets without replacing stable service dispatchers."""
    lifecycle = get_lifecycle(hass)
    await _init_services(hass)
    if hass.data[DOMAIN][DATA_CONFIG].get(CONF_LOCAL_UPDATES, True):
        await _init_local_discovery(hass)
    if lifecycle.stop_unsubscribe is None:
        lifecycle.stop_unsubscribe = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, lifecycle.async_stop
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    lifecycle = get_lifecycle(hass)
    async with lifecycle.entry_operation(entry.entry_id, "setup") as admitted:
        if not admitted:
            raise ConfigEntryNotReady("reload_in_progress")

        async def update_cookie_and_token(**kwargs):
            hass.config_entries.async_update_entry(entry, data={**entry.data, **kwargs})

        config = hass.data[DOMAIN][DATA_CONFIG]
        policy = EntryPolicy.from_configs(config, entry.options)
        session_config = SessionConfig.from_configs(config, entry.options)
        cache_manager = CacheManager(
            hass, entry.entry_id, account_login=entry.unique_id,
            account_uid=entry.data.get("account_uid"),
        )
        session = utils.async_create_clientsession(hass)
        yandex = YandexSession(session, config=session_config, policy=policy, **entry.data)
        yandex.add_update_listener(update_cookie_and_token)
        quasar = YandexQuasar(yandex, policy=policy, cache_manager=cache_manager)
        lifecycle.runtimes[entry.entry_id] = quasar
        try:
            try:
                await quasar.init()
            except Exception as error:
                if quasar.readiness.devices_state != DevicesState.READY:
                    if yandex.readiness.auth_state == AuthState.REAUTH_REQUIRED:
                        raise ConfigEntryAuthFailed("credential_rejected") from error
                    raise ConfigEntryNotReady(safe_error_summary(error)) from error
            finally:
                lifecycle.finish_bootstrap(entry.entry_id)
            if yandex.readiness.auth_state == AuthState.REAUTH_REQUIRED:
                entry.async_start_reauth(hass)
            await hass_utils.load_fake_devies(hass, quasar)
            # Keep the account-login lookup used by all platform setup functions.
            hass.data[DOMAIN][entry.unique_id] = quasar
            await hass_utils.async_resolve_speaker_owners(hass, entry, quasar)

            observations = hass.data[DOMAIN][DATA_SPEAKERS]
            for device in quasar.speakers + quasar.modules:
                did = device["quasar_info"]["device_id"]
                if observation := observations.get(did):
                    device["host"] = observation["host"]
                    device["port"] = observation["port"]
                    await cache_manager.async_save_local_config(
                        did, host=observation["host"], port=observation["port"]
                    )
            await _setup_devices(hass, quasar)

            if hass_utils.incluce_devices(hass, entry):
                quasar.platforms = platforms = PLATFORMS
                entry.async_on_unload(async_track_time_interval(
                    hass, quasar.devices_passive_update, timedelta(minutes=5)
                ))
            else:
                quasar.platforms = platforms = SPEAKER_PLATFORMS
            # сначала публикация всех платформ, затем cloud recovery/token-fetch
            await hass.config_entries.async_forward_entry_setups(entry, platforms)
            if policy.effective_cloud_allowed:
                quasar.start()
            return True
        except BaseException as error:
            lifecycle.setup_reasons[entry.entry_id] = safe_error_summary(error)
            await quasar.async_stop()
            lifecycle.runtimes.pop(entry.entry_id, None)
            if hass.data[DOMAIN].get(entry.unique_id) is quasar:
                hass.data[DOMAIN].pop(entry.unique_id, None)
            raise



async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    lifecycle = get_lifecycle(hass)
    async with lifecycle.entry_operation(entry.entry_id, "unload") as admitted:
        if not admitted:
            return False
        quasar = lifecycle.runtimes.get(entry.entry_id)
        if quasar is not None:
            await quasar.async_stop()
        platforms = getattr(quasar, "platforms", None) or PLATFORMS
        unloaded = await hass.config_entries.async_unload_platforms(entry, platforms)
        if unloaded:
            lifecycle.runtimes.pop(entry.entry_id, None)
            hass.data[DOMAIN].pop(entry.unique_id, None)
        return unloaded

async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry):
    lifecycle = get_lifecycle(hass)
    async with lifecycle.entry_operation(entry.entry_id, "remove") as admitted:
        if not admitted:
            raise RuntimeError("reload_in_progress")
        quasar = lifecycle.runtimes.get(entry.entry_id)
        if quasar is not None:
            await quasar.async_stop()
        cache = quasar.cache_manager if quasar is not None else CacheManager(
            hass, entry.entry_id, account_login=entry.unique_id,
            account_uid=entry.data.get("account_uid"),
        )
        await cache.async_remove()
        lifecycle.runtimes.pop(entry.entry_id, None)
        hass.data[DOMAIN].pop(entry.unique_id, None)
        # Keep the ownership reservation: removal is not an automatic claim by a peer.


async def _init_local_discovery(hass: HomeAssistant):
    """Own one browser and share address observations, never account dictionaries."""
    lifecycle = get_lifecycle(hass)
    await lifecycle.async_stop_discovery()
    speakers = hass.data[DOMAIN][DATA_SPEAKERS]

    async def found_local_speaker(info: dict):
        did, host, port = info.get("device_id"), info.get("host"), info.get("port")
        if (
            not isinstance(did, str) or not did
            or not isinstance(host, str) or not host
            or isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536
        ):
            return
        observation = {
            "device_id": did, "host": host, "port": port,
            "observed_at": dt_util.utcnow().isoformat(),
        }
        for key in ("platform", "model"):
            if isinstance(info.get(key), str):
                observation[key] = info[key]
        speakers[did] = observation
        for quasar in tuple(lifecycle.runtimes.values()):
            if speakers.get(did) is not observation:
                return
            if quasar.stopping:
                continue
            for device in quasar.speakers + quasar.modules:
                if device["quasar_info"]["device_id"] != did:
                    continue
                changed = (device.get("host"), device.get("port")) != (host, port)
                device["host"], device["port"] = host, port
                if quasar.cache_manager is not None:
                    try:
                        await quasar.cache_manager.async_save_local_config(
                            did, host=host, port=port
                        )
                    except RuntimeError:
                        if quasar.stopping:
                            continue
                        raise
                if speakers.get(did) is not observation:
                    return
                if (
                    quasar.stopping or did not in quasar.owned_glagol_ids
                    or not (quasar.policy.local_updates or did in quasar.yaml_glagol_hosts)
                ):
                    continue
                entity = device.get("entity")
                if entity is not None and entity.hass and (changed or not entity.local_state):
                    await entity.init_local_mode()
                    entity.async_write_ha_state()

    zeroconf = await utils.get_zeroconf_singleton(hass)
    listener = YandexIOListener(lambda info: hass.loop.call_soon_threadsafe(
        lifecycle.schedule_discovery, found_local_speaker, info
    ))
    lifecycle.discovery_listener = listener
    await hass.async_add_executor_job(listener.start, zeroconf)


async def _init_services(hass: HomeAssistant):
    """Init Yandex Station TTS service."""
    lifecycle = get_lifecycle(hass)

    def register(domain, name, target, **kwargs):
        key = f"{domain}.{name}"
        registered = key in lifecycle.service_targets
        handler = lifecycle.bind_service(key, target)
        if not registered:
            hass.services.async_register(domain, name, handler, **kwargs)

    def get_all_speakers():
        speakers = []
        for val in hass.data.get(DOMAIN, {}).values():
            if hasattr(val, "owned_speakers") and hasattr(val, "owned_modules"):
                speakers.extend(val.owned_speakers + val.owned_modules)
        return speakers

    try:
        # starting from Home Assistant 2023.7
        from homeassistant.core import ServiceResponse, SupportsResponse
        from homeassistant.helpers import service

        async def send_command(call: ServiceCall) -> ServiceResponse:
            try:
                # from HA 2025.10
                entity_ids = await service.async_extract_entity_ids(call)
            except TypeError:
                # for HA before 2025.10
                entity_ids = await service.async_extract_entity_ids(hass, call)
            for dev in get_all_speakers():
                entity: YandexStationBase = dev.get("entity")
                if (
                    not entity
                    or entity.entity_id not in entity_ids
                    or not entity.glagol
                ):
                    continue
                data = service.remove_entity_service_fields(call)
                data.setdefault("command", "sendText")
                if external := data.get("external"):
                    data = utils.external_command(**external)
                return await entity.glagol.send(data)
            return {"error": "Entity not found"}

        register(
            DOMAIN,
            "send_command",
            send_command,
            supports_response=SupportsResponse.OPTIONAL,
        )
    except ImportError as e:
        _LOGGER.warning(repr(e))

    async def yandex_station_say(call: ServiceCall):
        entity_ids = call.data.get(ATTR_ENTITY_ID) or utils.find_station(
            get_all_speakers()
        )
        _LOGGER.debug(f"Yandex say to: {entity_ids}")

        if not entity_ids:
            _LOGGER.error("Entity_id parameter required")
            return

        message = call.data.get("message")

        data = {
            ATTR_MEDIA_CONTENT_ID: message,
            ATTR_MEDIA_CONTENT_TYPE: "tts",
            ATTR_ENTITY_ID: entity_ids,
        }

        if "options" in call.data:
            data["extra"] = call.data["options"]

        await hass.services.async_call(
            MEDIA_DOMAIN, SERVICE_PLAY_MEDIA, data, blocking=True
        )

    config = hass.data[DOMAIN][DATA_CONFIG]
    service_name = config.get(CONF_TTS_NAME, "yandex_station_say")
    register("tts", service_name, yandex_station_say)


async def _setup_entry_from_config(hass: HomeAssistant):
    """Support legacy config from YAML."""
    config = hass.data[DOMAIN][DATA_CONFIG]
    if CONF_USERNAME not in config:
        return

    # check if already configured
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.unique_id == config[CONF_USERNAME]:
            return

    if x_token := utils.load_token_from_json(hass):
        config["x_token"] = x_token

    # need username and token or password
    if "x_token" not in config and CONF_PASSWORD not in config:
        return

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=config
        )
    )


async def _setup_devices(hass: HomeAssistant, quasar: YandexQuasar):
    """Set speakers additional config from YAML."""
    config = hass.data[DOMAIN][DATA_CONFIG]
    if CONF_DEVICES not in config:
        return

    confdevices = config[CONF_DEVICES]

    for device in quasar.speakers + quasar.modules:
        did = device["quasar_info"]["device_id"]
        if upd := confdevices.get(did) or confdevices.get(did.lower()):
            if upd.get(CONF_HOST):
                quasar.yaml_glagol_hosts.add(did)
            device.update(upd)
        if quasar.cache_manager and (device.get("host") or device.get("port")):
            await quasar.cache_manager.async_save_local_config(
                did, host=device.get("host"), port=device.get("port")
            )


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Supported from Hass v2022.3"""
    dr.async_get(hass).async_remove_device(device.id)
    return True
