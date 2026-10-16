import asyncio
import ipaddress
import json
import logging
import time
import uuid
from asyncio import Future
from typing import Callable, Dict, Optional

from aiohttp import (
    ClientConnectorError,
    ClientWebSocketResponse,
    ServerTimeoutError,
    WSMsgType,
)
from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf

from .policy import TokenState
from .yandex_session import YandexSession

_LOGGER = logging.getLogger(__name__)


class YandexGlagol:
    """Local Glagol WebSocket client for a single speaker.

    All mutable state (token, socket, waiters) belongs to the instance so
    speakers never share request waiters.
    """

    def __init__(
        self,
        session: YandexSession,
        device: dict,
        token: str | None = None,
        quasar=None,
    ):
        self.session = session
        self.device = device
        self.quasar = quasar
        self.device_token = token
        self.token_state = TokenState.CANDIDATE if token else TokenState.MISSING
        self._token_rejected_at: float | None = None
        self.url: Optional[str] = None
        self.ws: Optional[ClientWebSocketResponse] = None
        self.update_handler: Callable = None
        self.waiters: Dict[str, Future] = {}
        self._connect_task: Optional[asyncio.Task] = None
        self._stopped = False
        self._cancelled = False
        self._lifecycle_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None

    def debug(self, text: str):
        _LOGGER.debug(f"{self.device['name']} | {text}")

    def is_device(self, device: str):
        return (
            self.device["quasar_info"]["device_id"] == device
            or self.device["name"] == device
        )

    @property
    def name(self):
        return self.device["name"]

    async def start_or_restart(self):
        async with self._lifecycle_lock:
            if self._stopped:
                return
            endpoint = f"wss://{self.device['host']}:{self.device['port']}"
            if not self.url:
                self.url = endpoint
                self._connect_task = asyncio.create_task(self._connect(0))
            elif endpoint != self.url:
                self.debug("Updating local speaker endpoint")
                self.url = endpoint
                if self.ws and not self.ws.closed:
                    await self.ws.close()

    async def _cancel_connect_task(self):
        """Cancel and await the pending connect/reconnect task, if any."""
        task = self._connect_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.debug(f"Connection stop error: {type(e).__name__}")
        self._connect_task = None

    async def _close_connection(self):
        await self._cancel_connect_task()
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        self.ws = None
        for future in self.waiters.values():
            if not future.done():
                future.cancel()
        self.waiters.clear()
        if self.update_handler:
            self.update_handler(None)

    async def stop(self):
        """Terminal stop invalidates replacements before waiting for their lock."""
        self.debug("Stopping local connection")
        self._stopped = True
        self._stop_event.set()
        self._token_rejected_at = None
        self.url = None
        async with self._lifecycle_lock:
            await self._close_connection()

    async def restart(self, delay: float = 0.0):
        """Replace the reader without reviving a client stopped by unload."""
        async with self._lifecycle_lock:
            if self._stopped or not self.url:
                return
            self.debug("Restarting local connection")
            await self._close_connection()
            if delay:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), delay)
                except asyncio.TimeoutError:
                    pass
            if not self._stopped and self.url:
                self._connect_task = asyncio.create_task(self._connect(0))

    async def adopt_token(self, token: str) -> bool:
        """Use a fetched candidate without replacing an accepted live token."""
        async with self._lifecycle_lock:
            if self._stopped or not token or token == self.device_token:
                return False
            if (
                self.token_state is TokenState.ACCEPTED
                and self.ws is not None and not self.ws.closed
            ):
                return False
            self.device_token = token
            self.token_state = TokenState.CANDIDATE
            if self.url and (self.ws is None or self.ws.closed):
                await self._cancel_connect_task()
                if not self._stopped and self.url:
                    self._connect_task = asyncio.create_task(self._connect(0))
            return True

    async def _handle_token_rejection(self, rejected_token: str) -> int:
        """Handle peer CLOSE (4000, 'Invalid token') for this connection.

        Invalidates only this (device_id, platform) tuple in local state, RAM,
        and durable storage, then performs at most one single-flight token fetch
        when cloud access is allowed. Returns the initial failure count for backoff.
        """
        did = self.device.get("quasar_info", {}).get("device_id")
        if self._token_rejected_at is None:
            self._token_rejected_at = time.monotonic()
        _LOGGER.warning(
            "glagol_token_rejected: device=%s code=4000", did,
        )
        self.token_state = TokenState.REJECTED
        self.device_token = None
        if self.quasar is not None:
            info = self.device["quasar_info"]
            ram = self.quasar.device_tokens.get(info["device_id"], {})
            if ram.get(info["platform"]) == rejected_token:
                ram.pop(info["platform"], None)
            if self.quasar.cache_manager is not None:
                try:
                    await self.quasar.cache_manager.async_invalidate_device_token(
                        info["device_id"], info["platform"], rejected_token
                    )
                except Exception as e:
                    _LOGGER.debug(
                        "Token invalidation save failed: %s", type(e).__name__
                    )
            if not self.quasar.stopping and self.quasar.policy.effective_cloud_allowed:
                new_token = await self.quasar.async_fetch_device_token(self.device)
                if new_token and new_token != rejected_token:
                    self.device_token = new_token
                    self.token_state = TokenState.CANDIDATE
                    return 0
        # Without a replacement candidate, wait for standard reconnect backoff
        return 2

    async def _connect(self, fails: int):
        self.debug("Connecting to local speaker")

        fails += 1  # will be reset with first accepted msg from station
        connection_token = self.device_token
        ws: Optional[ClientWebSocketResponse] = None
        self._cancelled = False

        try:
            if not connection_token:
                # Token is fetched by post-publication recovery, not inside the reader
                self.debug("Device token is missing; local connection is waiting")
                return
            if self.token_state is not TokenState.ACCEPTED:
                self.token_state = TokenState.CANDIDATE

            connection_url = self.url
            ws = self.ws = await self.session.ws_connect(
                connection_url, role="local_ws", heartbeat=55, ssl=False
            )
            if connection_url != self.url:
                return
            await self.ping()

            while True:
                msg = await ws.receive()

                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        self.debug("Ignored malformed TEXT frame")
                        continue
                    if not isinstance(data, dict):
                        self.debug("Ignored non-object TEXT frame")
                        continue
                    fails = 0  # any decodable message - reset fails

                    # Confirmed state message marks the candidate token as accepted
                    if "state" in data and connection_token == self.device_token:
                        self.token_state = TokenState.ACCEPTED
                        if self._token_rejected_at is not None and ws is self.ws:
                            _LOGGER.info(
                                "glagol_token_recovered: device=%s elapsed=%.2fs",
                                self.device.get("quasar_info", {}).get("device_id"),
                                time.monotonic() - self._token_rejected_at,
                            )
                            self._token_rejected_at = None

                    request_id = data.get("requestId")
                    if request_id in self.waiters:
                        result = {"status": data.get("status")}

                        if vinsResponse := data.get("vinsResponse"):
                            try:
                                # payload only in yandex module
                                if payload := vinsResponse.get("payload"):
                                    response = payload["response"]
                                else:
                                    response = vinsResponse["response"]

                                if card := response.get("card"):
                                    result.update(card)
                                elif cards := response.get("cards"):
                                    result.update(cards[0])
                                elif is_streaming := response.get("is_streaming"):
                                    result["is_streaming"] = is_streaming
                                elif output_speech := response.get("output_speech"):
                                    result.update(output_speech)

                            except Exception as e:
                                _LOGGER.debug(f"Response error: {e}")

                        if not self.waiters[request_id].done():
                            self.waiters[request_id].set_result(result)

                    if self.update_handler:
                        self.update_handler(data)

                elif msg.type == WSMsgType.BINARY:
                    # Ignore unknown binary frames without resetting backoff or token state
                    self.debug("Ignored unknown BINARY frame")
                    continue

                elif msg.type == WSMsgType.CLOSE:
                    rejected = (
                        msg.data == 4000
                        and msg.extra == "Invalid token"
                        and connection_token == self.device_token
                        and ws is self.ws
                    )
                    if rejected:
                        fails = await self._handle_token_rejection(connection_token)
                    # Any CLOSE frame terminates the current reader loop
                    break

                elif msg.type in (WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break

                elif isinstance(msg.data, ServerTimeoutError):
                    raise msg.data

        except (ClientConnectorError, ConnectionResetError, ServerTimeoutError) as e:
            self.debug(f"Connection error: {repr(e)}")

        except asyncio.CancelledError:
            self.debug("Stopping connection: CancelledError")
            self._cancelled = True
            raise

        except RuntimeError as e:
            # Raised when the Home Assistant client session is closed during shutdown
            self.debug(f"Stopping connection: {repr(e)}")
            self._stopped = True

        except Exception as e:
            _LOGGER.error(f"{self.name} => local | {repr(e)}")

        finally:
            if ws is not None and not ws.closed:
                await ws.close()
            if self.ws is ws:
                self.ws = None
            # Notify the consumer on any disconnect, including task cancellation
            if self.update_handler:
                try:
                    self.update_handler(None)
                except Exception:
                    pass
            # Cancelled or stopped readers never schedule a reconnect
            if self._stopped or self._cancelled or not self.url:
                self._connect_task = None
            else:
                self._connect_task = asyncio.create_task(self._reconnect_later(fails))

    async def _reconnect_later(self, fails: int):
        if fails:
            # 0s, 30s, 60s, ... 5 min
            delay = 30 * min(fails - 1, 10)
            self.debug(f"Reconnect backoff: {delay}s")
            await asyncio.sleep(delay)
        if self._stopped or self._cancelled or not self.url:
            return
        self._connect_task = asyncio.create_task(self._connect(fails))

    async def ping(self, command="ping"):
        ws = self.ws
        if ws is None or ws.closed:
            return
        try:
            await ws.send_json(
                {
                    "conversationToken": self.device_token,
                    "id": str(uuid.uuid4()),
                    "payload": {"command": command},
                    "sentTime": int(round(time.time() * 1000)),
                }
            )
        except Exception:
            pass

    async def send(self, payload: dict) -> Optional[dict]:
        _LOGGER.debug(f"{self.name} => local | {payload}")

        ws = self.ws
        if ws is None or ws.closed:
            return {"error": "no_local_connection"}

        request_id = str(uuid.uuid4())

        try:
            await ws.send_json(
                {
                    "conversationToken": self.device_token,
                    "id": request_id,
                    "payload": payload,
                    "sentTime": int(round(time.time() * 1000)),
                }
            )

            self.waiters[request_id] = self.loop.create_future()

            # limit future wait time
            await asyncio.wait_for(self.waiters[request_id], 5)

            return self.waiters.pop(request_id).result()

        except asyncio.TimeoutError as e:
            _ = self.waiters.pop(request_id, None)
            return {"error": repr(e)}

        except Exception as e:
            _LOGGER.error(f"{self.name} => local | {repr(e)}")
            return {"error": repr(e)}

    async def reset_session(self):
        payload = {
            "command": "serverAction",
            "serverActionEventPayload": {
                "type": "server_action",
                "name": "on_reset_session",
            },
        }
        await self.send(payload)

    prev_msg = None

    def debug_msg(self, data: dict):
        data.pop("id")
        data.pop("sentTime")
        data["state"].pop("timeSinceLastVoiceActivity")
        if player := data["state"].get("playerState"):
            player.pop("progress")

        if data == self.prev_msg:
            return

        for k in sorted(data.keys()):
            if self.prev_msg and k in self.prev_msg and data[k] == self.prev_msg[k]:
                continue
            self.debug(f"{k}: {data[k]}")

        if vins := data.get("vinsResponse"):
            with open(f"{time.time()}.json", "w") as f:
                json.dump(vins, f, ensure_ascii=False, indent=2)

        self.prev_msg = data


class YandexIOListener:
    add_handler = None
    browser = None

    def __init__(self, add_handler: Callable):
        self.add_handler = add_handler

    def start(self, zeroconf: Zeroconf):
        self.browser = ServiceBrowser(
            zeroconf, "_yandexio._tcp.local.", handlers=[self._zeroconf_handler]
        )

    def stop(self, *args):
        if self.browser is not None:
            self.browser.cancel()
            self.browser = None

    def _zeroconf_handler(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ):
        try:
            info = zeroconf.get_service_info(service_type, name)
            if not info:
                return

            # If device has multiple local addresses - this is may be bug:
            # https://github.com/AlexxIT/YandexStation/issues/568
            if len(info.addresses) != 1:
                return

            properties = {
                k.decode(): v.decode() if isinstance(v, bytes) else v
                for k, v in info.properties.items()
            }

            self.add_handler(
                {
                    "device_id": properties["deviceId"],
                    "platform": properties["platform"],
                    "host": str(ipaddress.ip_address(info.addresses[0])),
                    "port": info.port,
                }
            )

        except Exception as e:
            _LOGGER.debug("Can't get zeroconf info", exc_info=e)


def debug(data: bytes):
    data: dict = json.loads(data)
    if experiments := data.get("experiments"):
        data["experiments"] = len(experiments)
    if extra := data.get("extra"):
        data["extra"] = {k: len(v) for k, v in extra.items()}
    if features := data.get("supported_features"):
        data["supported_features"] = len(features)
    _LOGGER.debug(json.dumps(data, ensure_ascii=False))
