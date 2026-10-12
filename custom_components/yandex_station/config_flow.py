"""
1. User can enter login/pass from GUI
2. User can set login/pass in YAML
3. If the password requires updating, user need to configure another component
   with the same login.
4. Captcha will be requested if necessary
5. If authorization through YAML does not work, user can continue it through
   the GUI.
"""

import logging
from functools import lru_cache

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow

from .core import utils
from .core.const import DOMAIN
from .core.yandex_quasar import YandexQuasar
from .core.yandex_session import LoginResponse, YandexSession
from .core.cache import CacheManager

_LOGGER = logging.getLogger(__name__)


def generate_qr_code(data: str) -> str:
    try:
        from homeassistant.auth.mfa_modules import totp

        # noinspection PyProtectedMember
        return totp._generate_qr_code(data)
    except Exception as e:
        return repr(e)


# noinspection PyUnusedLocal
class YandexStationFlowHandler(ConfigFlow, domain=DOMAIN):
    @property
    @lru_cache()
    def yandex(self):
        session = utils.async_create_clientsession(self.hass)
        return YandexSession(session)

    async def async_step_import(self, data: dict):
        """Init by component setup. Forward YAML login/pass to auth."""
        await self.async_set_unique_id(data["username"])
        self._abort_if_unique_id_configured()

        if "x_token" in data:
            return self.async_create_entry(
                title=data["username"], data={"x_token": data["x_token"]}
            )

        else:
            return await self.async_step_user()
    async def async_step_reauth(self, entry_data=None):
        """Handle reauthorization request from Core (RFC v2 §6.3)."""
        entry_id = self.context.get("entry_id")
        if entry_id:
            self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id)
        return await self.async_step_user()
    async def async_step_user(self, user_input=None):
        """Init by user via GUI"""
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required("method", default="qr"): vol.In(
                            {
                                "qr": "QR-код",
                                "cookies": "Cookies",
                                "token": "Токен",
                            }
                        )
                    }
                ),
            )

        method = user_input["method"]
        if method == "qr":
            qr_url = await self.yandex.get_qr()
            return self.async_show_form(
                step_id="qr",
                description_placeholders={
                    "qr_url": qr_url,
                    "qr_data": generate_qr_code(qr_url),
                },
            )

        if method == "cookies":
            return self.async_show_form(
                step_id=method,
                data_schema=vol.Schema({vol.Required(method): str}),
                description_placeholders={
                    # hassfest prohibits the use of links in translation files
                    "ex_url": "https://chrome.google.com/webstore/detail/copy-cookies/jcbpglbplpblnagieibnemmkiamekcdg",
                    "ya_url": "https://passport.yandex.ru/profile",
                },
            )

        # cookies, token
        return self.async_show_form(
            step_id=method,
            data_schema=vol.Schema({vol.Required(method): str}),
        )

    async def async_step_qr(self, user_input):
        resp = await self.yandex.login_qr()
        if not resp:
            self.cur_step["errors"] = {"base": "unauthorised"}
            return self.cur_step
        return await self._check_yandex_response(resp)

    async def async_step_cookies(self, user_input):
        resp = await self.yandex.login_cookies(user_input["cookies"])
        return await self._check_yandex_response(resp)

    async def async_step_token(self, user_input):
        resp = await self.yandex.validate_token(user_input["token"])
        return await self._check_yandex_response(resp)

    async def _check_yandex_response(self, resp: LoginResponse):
        """Check Yandex response. Do not create entry for the same login. Show
        captcha form if captcha required. Show auth form with error if error.
        """
        if resp.ok:
            if hasattr(self, "_reauth_entry") and self._reauth_entry:
                if (
                    self._reauth_entry.unique_id
                    and resp.display_login != self._reauth_entry.unique_id
                ):
                    _LOGGER.warning(
                        "Reauth account mismatch: expected %s, got %s",
                        self._reauth_entry.unique_id,
                        resp.display_login,
                    )
                    return self.async_abort(reason="account_mismatch")

                self.hass.config_entries.async_update_entry(
                    self._reauth_entry, data={"x_token": resp.x_token}
                )
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                )
                return self.async_abort(reason="reauth_successful")

            # set unique_id or return existing entry
            entry = await self.async_set_unique_id(resp.display_login)
            if entry:
                # update existing entry with same login
                self.hass.config_entries.async_update_entry(
                    entry, data={"x_token": resp.x_token}
                )
                return self.async_abort(reason="account_updated")

            else:
                # create new entry for new login
                return self.async_create_entry(
                    title=resp.display_login, data={"x_token": resp.x_token}
                )
        elif resp.errors:
            _LOGGER.debug(f"Config error: {resp.error}")
            if self.cur_step:
                self.cur_step["errors"] = {"base": resp.error}
                return self.cur_step

        raise AbortFlow("not_implemented")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(OptionsFlow):
    def __init__(self, config_entry: ConfigEntry = None):
        self._config_entry = config_entry

    @property
    def config_entry(self) -> ConfigEntry:
        if self._config_entry is not None:
            return self._config_entry
        return self.hass.config_entries.async_get_entry(self.handler)

    async def async_step_init(self, user_input: dict = None):
        """Show main menu with options (RFC v2 §6.1)."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "devices", "reauth"],
        )

    async def async_step_settings(self, user_input: dict = None):
        """Configure integration parameters and endpoints."""
        if user_input is not None:
            return await self._async_save_options(user_input)

        defaults = dict(self.config_entry.options)
        quasar_url = defaults.get("quasar_base_url") or "https://iot.quasar.yandex.ru (по умолчанию)"
        account_url = defaults.get("account_base_url") or "https://quasar.yandex.ru (по умолчанию)"
        passport_url = defaults.get("passport_base_url") or "https://passport.yandex.ru (по умолчанию)"
        placeholders = {
            "current_quasar": quasar_url,
            "current_account": account_url,
            "current_passport": passport_url,
        }

        schema_dict = {
            vol.Optional(
                "force_offline_local",
                default=defaults.get("force_offline_local", False),
            ): cv.boolean,
            vol.Optional(
                "use_cached_devices",
                default=defaults.get("use_cached_devices", False),
            ): cv.boolean,
            vol.Optional(
                "enable_scenarios", default=defaults.get("enable_scenarios", True)
            ): cv.boolean,
            vol.Optional(
                "enable_diagnostic_sensors",
                default=defaults.get("enable_diagnostic_sensors", True),
            ): cv.boolean,
            vol.Optional(
                "quasar_base_url",
                default=defaults.get("quasar_base_url", ""),
            ): cv.string,
            vol.Optional(
                "account_base_url",
                default=defaults.get("account_base_url", ""),
            ): cv.string,
            vol.Optional(
                "passport_base_url",
                default=defaults.get("passport_base_url", ""),
            ): cv.string,
        }
        data = vol_schema(schema_dict, defaults)
        return self.async_show_form(
            step_id="settings",
            data_schema=data,
            description_placeholders=placeholders,
        )

    async def async_step_devices(self, user_input: dict = None):
        """Configure device inclusion."""
        if user_input is not None:
            return await self._async_save_options(user_input)

        devices = {}
        entry_login = self.config_entry.unique_id
        quasar: YandexQuasar | None = self.hass.data.get(DOMAIN, {}).get(entry_login)
        if quasar and quasar.devices:
            for i in quasar.devices:
                devices[i["id"]] = device_name(i)
        else:
            cache = CacheManager(
                self.hass, self.config_entry.entry_id, account_login=entry_login
            )
            snapshot = await cache.async_load()
            if snapshot and snapshot.devices:
                for i in snapshot.devices:
                    devices[i["id"]] = device_name(i)

        defaults = dict(self.config_entry.options)
        if include := defaults.get("include"):
            for inc in include:
                if isinstance(inc, str) and inc not in devices:
                    devices[inc] = f"{inc} (недоступно)"

        devices = dict(sorted(devices.items(), key=lambda x: x[1]))
        schema_dict = {
            vol.Optional("include"): cv.multi_select(devices),
        }
        data = vol_schema(schema_dict, defaults)
        return self.async_show_form(step_id="devices", data_schema=data)

    async def async_step_reauth(self, user_input: dict = None):
        """Start reauthorization flow."""
        self.config_entry.async_start_reauth(self.hass)
        return self.async_abort(reason="reauth_started")

    async def _async_save_options(self, user_input: dict):
        """Synchronously commit options to entry and schedule reload (RFC v2 §6.2)."""
        new_options = dict(self.config_entry.options)
        for k, v in user_input.items():
            if v == "":
                new_options.pop(k, None)
            elif v is not None:
                new_options[k] = v

        self.hass.config_entries.async_update_entry(
            self.config_entry, options=new_options
        )
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self.config_entry.entry_id)
        )
        return self.async_create_entry(title="", data=new_options)

def vol_schema(schema: dict, defaults: dict | None) -> vol.Schema:
    if defaults:
        for key in schema:
            if (value := defaults.get(key.schema)) is not None:
                key.default = vol.default_factory(value)
    return vol.Schema(schema)


def device_name(device: dict) -> str:
    if room := device.get("room_name"):
        return f"{device['house_name']} - {room} - {device['name']}"
    return f"{device['house_name']} - {device['name']}"
