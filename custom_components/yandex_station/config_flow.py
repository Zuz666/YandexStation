"""
1. User can enter login/pass from GUI
2. User can set login/pass in YAML
3. If the password requires updating, user need to configure another component
   with the same login.
4. Captcha will be requested if necessary
5. If authorization through YAML does not work, user can continue it through
   the GUI.
"""

import json
import logging

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.exceptions import HomeAssistantError

from .core import utils
from .core.const import DATA_CONFIG, DOMAIN
from .core.lifecycle import get_lifecycle
from .core.policy import EntryPolicy, SessionConfig
from .core.yandex_quasar import YandexQuasar
from .core.yandex_session import LoginResponse, YandexSession
from .core.cache import CacheManager
from .hass import hass_utils
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
    def yandex(self):
        if not hasattr(self, "_yandex"):
            entry = self.hass.config_entries.async_get_entry(
                self.context.get("entry_id")
            ) if self.context.get("entry_id") else None
            yaml_config = self.hass.data.get(DOMAIN, {}).get(DATA_CONFIG, {})
            options = entry.options if entry else {}
            config = SessionConfig.from_configs(yaml_config, options)
            policy = EntryPolicy.from_configs(yaml_config, options)
            session = utils.async_create_clientsession(self.hass)
            self._yandex = YandexSession(
                session, config=config, policy=policy,
                account_uid=entry.data.get("account_uid") if entry else None,
            )
            self._session_stop_unsub = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP,
                lambda _: self.hass.async_create_task(session.close()),
            )
        return self._yandex

    async def _close_flow_session(self):
        if hasattr(self, "_yandex"):
            session = self._yandex.client_session
            del self._yandex
            self._session_stop_unsub()
            await session.close()

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
        """Reauthenticate only the entry selected by Core."""
        entry_id = self.context.get("entry_id")
        self._reauth_entry = (
            self.hass.config_entries.async_get_entry(entry_id) if entry_id else None
        )
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_target_missing")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({
                    vol.Required("confirm_reauth", default=False): cv.boolean,
                }),
            )
        if not user_input["confirm_reauth"]:
            return self.async_abort(reason="reauth_cancelled")
        return await self.async_step_user()

    def _reauth_block_reason(self):
        entry = getattr(self, "_reauth_entry", None)
        if entry is None:
            return None
        if not get_lifecycle(self.hass).entry_admitted(entry.entry_id):
            return "reload_in_progress"
        if not get_lifecycle(self.hass).entry_enabled(entry):
            return "entry_disabled"
        yaml_config = self.hass.data.get(DOMAIN, {}).get(DATA_CONFIG, {})
        if EntryPolicy.from_configs(yaml_config, entry.options).force_offline_local:
            return "offline_reauth_blocked"
        return None

    async def _reauth_network_gate(self):
        reason = self._reauth_block_reason()
        if reason is None:
            return None
        await self._close_flow_session()
        form = await self.async_step_user()
        form["errors"] = {"base": reason}
        return form
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
        if method == "qr" and (blocked := await self._reauth_network_gate()):
            return blocked
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
        if blocked := await self._reauth_network_gate():
            return blocked
        resp = await self.yandex.login_qr()
        if not resp.raw:  # QR not scanned: login_qr returns LoginResponse({})
            self.cur_step["errors"] = {"base": "unauthorised"}
            return self.cur_step
        return await self._check_yandex_response(resp)

    async def async_step_cookies(self, user_input):
        if blocked := await self._reauth_network_gate():
            return blocked
        resp = await self.yandex.login_cookies(user_input["cookies"])
        return await self._check_yandex_response(resp)

    async def async_step_token(self, user_input):
        if blocked := await self._reauth_network_gate():
            return blocked
        resp = await self.yandex.validate_token(user_input["token"])
        return await self._check_yandex_response(resp)

    async def _check_yandex_response(self, resp: LoginResponse):
        """Check Yandex response. Do not create entry for the same login. Show
        captcha form if captcha required. Show auth form with error if error.
        """
        if resp.ok:
            reauth_entry = getattr(self, "_reauth_entry", None)
            uid = resp.raw.get("uid")
            if uid is not None:
                uid = str(uid).strip()
            if reauth_entry is not None:
                if (
                    resp.display_login != reauth_entry.unique_id
                    or (uid and reauth_entry.data.get("account_uid") is not None
                        and uid != str(reauth_entry.data["account_uid"]))
                ):
                    _LOGGER.warning("Reauth account mismatch")
                    await self._close_flow_session()
                    return self.async_abort(reason="account_mismatch")
                entry = reauth_entry
                reason = "reauth_successful"
            else:
                entry = await self.async_set_unique_id(resp.display_login)
                reason = "account_updated"
                if entry is None:
                    data = {"x_token": resp.x_token}
                    if uid:
                        data["account_uid"] = uid
                    await self._close_flow_session()
                    return self.async_create_entry(title=resp.display_login, data=data)

            if reauth_entry is not None and (blocked := self._reauth_block_reason()):
                await self._close_flow_session()
                return self.async_abort(reason=blocked)
            if not get_lifecycle(self.hass).entry_admitted(entry.entry_id):
                await self._close_flow_session()
                return self.async_abort(reason="reload_in_progress")
            if not get_lifecycle(self.hass).entry_enabled(entry):
                await self._close_flow_session()
                return self.async_abort(reason="entry_disabled")
            if uid and entry.data.get("account_uid") is not None and (
                uid != str(entry.data["account_uid"])
            ):
                _LOGGER.warning("Login account UID mismatch")
                await self._close_flow_session()
                return self.async_abort(reason="account_mismatch")
            data = dict(entry.data)
            data["x_token"] = resp.x_token
            if uid:
                data["account_uid"] = uid
            self.hass.config_entries.async_update_entry(entry, data=data)
            get_lifecycle(self.hass).request_entry_reload(entry.entry_id)
            await self._close_flow_session()
            return self.async_abort(reason=reason)
        elif resp.errors:
            known = {
                "account.not_found", "password.not_matched", "unauthorised",
                "email.unsupported", "redirect.unsupported",
            }
            reason = resp.error if resp.error in known else "unauthorised"
            _LOGGER.debug("Config auth error: %s", reason)
            if self.cur_step:
                self.cur_step["errors"] = {"base": reason}
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

    def _abort_other_options_flows(self) -> None:
        options = getattr(self.hass.config_entries, "options", None)
        if options is not None:
            for flow in tuple(options.async_progress(include_uninitialized=True)):
                fid = flow.get("flow_id")
                if (
                    flow.get("handler") == self.config_entry.entry_id
                    and fid
                    and fid != getattr(self, "flow_id", None)
                ):
                    options.async_abort(fid)

    async def async_step_init(self, user_input: dict = None):
        """Show main options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "devices", "recovery", "reauth"],
        )

    async def async_step_recovery(self, user_input: dict = None):
        lifecycle = get_lifecycle(self.hass)
        runtime = lifecycle.runtimes.get(self.config_entry.entry_id)
        cache = runtime.cache_manager if runtime else None
        own_cache = cache is None
        if own_cache:
            cache = CacheManager(
                self.hass, self.config_entry.entry_id,
                account_login=self.config_entry.unique_id,
                account_uid=self.config_entry.data.get("account_uid"),
            )
        try:
            if cache.snapshot is None:
                await cache.async_load()
            cache_reason = cache.cache_status
        finally:
            if own_cache:
                await cache.async_close()
        readiness = runtime.readiness if runtime else None
        # Setup reasons are safe_error_summary codes/classes; no URL or body.
        reason = lifecycle.setup_reasons.get(self.config_entry.entry_id, "none")
        placeholders = {
            "cache_reason": cache_reason,
            "setup_reason": reason,
            "auth_reason": str(readiness.auth_state) if readiness else "unknown",
            "cloud_reason": (
                str(readiness.computed_cloud_state(runtime.policy))
                if readiness else "unknown"
            ),
            "scenario_reason": str(readiness.scenarios_state) if readiness else "unknown",
            "ownership_reason": "ownership_conflict" if lifecycle.ownership_conflicts.get(
                self.config_entry.entry_id
            ) else "none",
        }
        return self.async_show_menu(
            step_id="recovery",
            menu_options=["retry_setup", "retry_scenario_creation", "claim_speaker"],
            description_placeholders=placeholders,
        )

    async def async_step_retry_setup(self, user_input: dict = None):
        errors = {}
        if user_input is not None and user_input.get("confirm_retry"):
            entry = self.config_entry
            lifecycle = get_lifecycle(self.hass)
            if not lifecycle.entry_enabled(entry):
                errors["base"] = "entry_disabled"
            elif not lifecycle.entry_admitted(entry.entry_id):
                errors["base"] = "reload_in_progress"
            elif lifecycle.request_entry_reload(entry.entry_id):
                self._abort_other_options_flows()
                return self.async_abort(reason="retry_started")
            else:
                errors["base"] = "reload_in_progress"
        return self.async_show_form(
            step_id="retry_setup",
            data_schema=vol.Schema({
                vol.Required("confirm_retry", default=False): cv.boolean,
            }),
            errors=errors,
        )

    async def async_step_retry_scenario_creation(self, user_input: dict = None):
        lifecycle = get_lifecycle(self.hass)
        entry = self.config_entry
        runtime = lifecycle.runtimes.get(entry.entry_id)
        if runtime is None or runtime.cache_manager is None:
            return self.async_abort(reason="recovery_runtime_missing")
        snapshot = runtime.cache_manager.snapshot
        if snapshot is None:
            snapshot = await runtime.cache_manager.async_load()
        journal = snapshot.scenario_creation if snapshot else {}
        choices = {
            speaker["id"]: speaker.get("name", speaker["id"])
            for speaker in runtime.owned_speakers
            if speaker.get("id") in journal
            and journal[speaker["id"]].get("state") in ("pending_unknown", "confirmed")
            and not speaker.get("scenario_id")
        }
        if not choices:
            return self.async_abort(reason="scenario_retry_unavailable")
        errors = {}
        if user_input is not None and user_input.get("confirm_risk"):
            if not lifecycle.entry_admitted(entry.entry_id):
                errors["base"] = "reload_in_progress"
            elif not lifecycle.entry_enabled(entry):
                errors["base"] = "entry_disabled"
            elif user_input.get("device_id") not in choices:
                errors["base"] = "scenario_retry_unavailable"
            else:
                try:
                    result = await runtime.retry_scenario_creation(
                        user_input["device_id"], confirm_risk=True
                    )
                except (ValueError, RuntimeError, HomeAssistantError) as error:
                    reason = str(error)
                    errors["base"] = reason if reason in {
                        "unknown_device", "scenarios_disabled", "scenarios_unavailable",
                        "scenario_conflict", "csrf_unavailable", "marker_unavailable",
                        "marker_verification_failed", "confirm_required",
                    } else "scenario_retry_failed"
                except Exception as error:
                    _LOGGER.warning(
                        "Manual scenario retry failed: %s", type(error).__name__
                    )
                    errors["base"] = "scenario_retry_failed"
                else:
                    return self.async_abort(reason=(
                        "scenario_bound" if result == "bound" else "scenario_created"
                    ))
        return self.async_show_form(
            step_id="retry_scenario_creation",
            data_schema=vol.Schema({
                vol.Required("device_id"): vol.In(choices),
                vol.Required("confirm_risk", default=False): cv.boolean,
            }),
            errors=errors,
        )

    async def async_step_claim_speaker(self, user_input: dict = None):
        choices = await hass_utils.async_claimable_speakers(self.hass, self.config_entry)
        if not choices:
            return self.async_abort(reason="claim_unavailable")
        errors = {}
        if user_input is not None:
            if not user_input.get("confirm_claim"):
                return await self.async_step_recovery()
            try:
                await hass_utils.async_claim_speaker(
                    self.hass, self.config_entry, user_input.get("glagol_id")
                )
            except (ValueError, RuntimeError, HomeAssistantError) as error:
                reason = str(error)
                errors["base"] = (
                    reason if reason in {
                        "ownership_conflict", "reload_in_progress", "entry_disabled"
                    } else "ownership_conflict"
                )
            else:
                return self.async_abort(reason="speaker_claimed")
        return self.async_show_form(
            step_id="claim_speaker",
            data_schema=vol.Schema({
                vol.Required("glagol_id"): vol.In(choices),
                vol.Required("confirm_claim", default=False): cv.boolean,
            }),
            errors=errors,
        )

    async def async_step_settings(self, user_input: dict = None):
        """Configure integration parameters and endpoints."""
        if user_input is not None:
            return await self._async_save_options(user_input)

        yaml_config = self.hass.data.get(DOMAIN, {}).get(DATA_CONFIG, {})
        policy = EntryPolicy.from_configs(yaml_config, self.config_entry.options)
        try:
            session_config = SessionConfig.from_configs(
                yaml_config, self.config_entry.options
            )
        except ValueError:
            # A manually edited or obsolete override must not hide the form
            # that lets the user repair it; do not display defaults as effective.
            session_config = SessionConfig()
            invalid_endpoints = True
        else:
            invalid_endpoints = False

        def format_effective(default_url: str, custom_url: str, role: str) -> str:
            if invalid_endpoints:
                return "некорректная конфигурация адресов (проверьте YAML и параметры)"
            resolved = session_config.resolve_url(custom_url, role)
            if custom_url == default_url and not session_config.domain:
                return f"{resolved} (по умолчанию)"
            if custom_url in self.config_entry.options.values():
                return f"{resolved} (GUI override)"
            return f"{resolved} (из YAML)"

        placeholders = {
            "current_quasar": format_effective(
                "https://iot.quasar.yandex.ru", session_config.quasar_base_url, "quasar"
            ),
            "current_account": format_effective(
                "https://quasar.yandex.ru", session_config.account_base_url, "account"
            ),
            "current_passport": format_effective(
                "https://passport.yandex.ru", session_config.passport_base_url, "passport"
            ),
            "current_mobileproxy": format_effective(
                "https://mobileproxy.passport.yandex.net",
                session_config.mobileproxy_base_url, "mobileproxy",
            ),
            "current_glagol": format_effective(
                "https://quasar.yandex.net/glagol",
                session_config.glagol_base_url, "glagol_token",
            ),
            "current_rpc": format_effective(
                "https://rpc.alice.yandex.ru", session_config.rpc_base_url, "rpc",
            ),
            "current_cookie": (
                "некорректная конфигурация" if invalid_endpoints
                else json.dumps(session_config.cookie_storage_endpoints, ensure_ascii=False)
            ),
            "current_csrf": (
                "некорректная конфигурация" if invalid_endpoints
                else json.dumps(session_config.csrf_endpoints, ensure_ascii=False)
            ),
        }

        schema_dict = {
            vol.Optional(
                "force_offline_local",
                default=policy.force_offline_local,
            ): cv.boolean,
            vol.Optional(
                "use_cached_devices",
                default=policy.use_cached_devices,
            ): cv.boolean,
            vol.Optional(
                "enable_scenarios",
                default=policy.enable_scenarios,
            ): cv.boolean,
            vol.Optional(
                "enable_diagnostic_sensors",
                default=policy.enable_diagnostic_sensors,
            ): cv.boolean,
        }
        for key in (
            "quasar_base_url", "account_base_url", "passport_base_url",
            "mobileproxy_base_url", "glagol_base_url", "rpc_base_url",
        ):
            schema_dict[vol.Optional(
                key, default=self.config_entry.options.get(key, "")
            )] = cv.string
        for key in ("cookie_storage_endpoints", "csrf_endpoints"):
            override = self.config_entry.options.get(key)
            schema_dict[vol.Optional(
                key, default=json.dumps(override, ensure_ascii=False) if override else ""
            )] = cv.string
        data = vol.Schema(schema_dict)
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
        runtime = get_lifecycle(self.hass).runtimes.get(self.config_entry.entry_id)
        if runtime is not None:
            for device in runtime.devices:
                devices[device["id"]] = device_name(device)
        else:
            cache = CacheManager(
                self.hass, self.config_entry.entry_id, account_login=entry_login,
                account_uid=self.config_entry.data.get("account_uid"),
            )
            try:
                snapshot = await cache.async_load()
                if snapshot is not None:
                    for device in snapshot.devices:
                        devices[device["id"]] = device_name(device)
            finally:
                await cache.async_close()
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
        """Confirm and start reauthorization flow."""
        if user_input is not None:
            lifecycle = get_lifecycle(self.hass)
            if not lifecycle.entry_admitted(self.config_entry.entry_id):
                result = await self.async_step_reauth()
                result["errors"] = {"base": "reload_in_progress"}
                return result
            if user_input.get("confirm_reauth"):
                self.config_entry.async_start_reauth(self.hass)
                return self.async_abort(reason="reauth_started")
            return await self.async_step_init()

        return self.async_show_form(
            step_id="reauth",
            data_schema=vol.Schema({
                vol.Required("confirm_reauth", default=False): cv.boolean,
            }),
        )
    async def _async_save_options(self, user_input: dict):
        """Synchronously commit options to entry and schedule reload (RFC v2 §6.2)."""
        lifecycle = get_lifecycle(self.hass)
        if not lifecycle.entry_admitted(self.config_entry.entry_id):
            result = await self.async_step_settings()
            result["errors"] = {"base": "reload_in_progress"}
            return result
        new_options = dict(self.config_entry.options)
        endpoint_fields = {
            "quasar_base_url", "account_base_url", "passport_base_url",
            "mobileproxy_base_url", "glagol_base_url", "rpc_base_url",
            "cookie_storage_endpoints", "csrf_endpoints",
        }
        yaml_config = self.hass.data.get(DOMAIN, {}).get(DATA_CONFIG, {})
        inherited_policy = EntryPolicy.from_configs(yaml_config)
        policy_fields = {
            "force_offline_local", "use_cached_devices", "enable_scenarios",
            "enable_diagnostic_sensors",
        }
        try:
            for key, value in user_input.items():
                if key in endpoint_fields and value == "":
                    new_options.pop(key, None)
                elif key in ("cookie_storage_endpoints", "csrf_endpoints") and isinstance(value, str):
                    parsed = json.loads(value)
                    if not isinstance(parsed, list) or not parsed:
                        raise ValueError("invalid_endpoint")
                    new_options[key] = parsed
                elif key in policy_fields and key not in new_options and (
                    value == getattr(inherited_policy, key)
                ):
                    continue
                elif value is not None:
                    new_options[key] = value
            SessionConfig.from_configs(yaml_config, new_options)
            EntryPolicy.from_configs(yaml_config, new_options)
        except (ValueError, TypeError):
            result = await self.async_step_settings()
            result["errors"] = {"base": "invalid_endpoint"}
            return result

        self._abort_other_options_flows()
        if new_options != self.config_entry.options:
            self.hass.config_entries.async_update_entry(
                self.config_entry, options=new_options
            )
            if lifecycle.entry_enabled(self.config_entry):
                lifecycle.request_entry_reload(self.config_entry.entry_id)
        return self.async_create_entry(title="", data=None)

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
