import asyncio
import logging
import socket
import ssl
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie

import pytest
from aiohttp import ClientConnectorCertificateError, ClientSession, WSMsgType, web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from yarl import URL

from custom_components.yandex_station.core.policy import SessionConfig
from custom_components.yandex_station.core.yandex_session import YandexSession


def test_ssl_false_allows_https_and_wss_through_diagnostic_proxy(tmp_path, caplog):
    async def scenario():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic diagnostic peer")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("iot.quasar.yandex.ru"),
            ]), critical=False)
            .sign(key, hashes.SHA256())
        )
        cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert_path, key_path)
        observed = []

        async def peer(request):
            assert request.cookies["Session_id"] == "PRIVATE_COOKIE"
            if request.path == "/ws":
                observed.append("websocket")
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                await ws.send_str("diagnostic-peer-ready")
                await ws.close()
                return ws
            assert request.headers["Authorization"] == "OAuth PRIVATE_TOKEN"
            observed.append("http")
            return web.json_response({"status": "ok", "source": "diagnostic-peer"})

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", peer)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        peer_port = listener.getsockname()[1]
        await web.SockSite(runner, listener, ssl_context=tls).start()
        proxy_tasks = set()

        async def relay(reader, writer):
            try:
                while data := await reader.read(8192):
                    writer.write(data)
                    await writer.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                writer.close()

        async def proxy_handler(reader, writer):
            task = asyncio.current_task()
            proxy_tasks.add(task)
            upstream_writer = None
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                assert header.split(b"\r\n", 1)[0] == b"CONNECT iot.quasar.yandex.ru:443 HTTP/1.1"
                upstream_reader, upstream_writer = await asyncio.open_connection(
                    "127.0.0.1", peer_port
                )
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await asyncio.gather(
                    relay(reader, upstream_writer), relay(upstream_reader, writer)
                )
            finally:
                writer.close()
                if upstream_writer is not None:
                    upstream_writer.close()
                proxy_tasks.discard(task)

        proxy = await asyncio.start_server(proxy_handler, "127.0.0.1", 0)
        proxy_url = f"http://127.0.0.1:{proxy.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession() as client:
                cookie = SimpleCookie("Session_id=PRIVATE_COOKIE; Domain=.yandex.ru; Path=/; Secure")
                client.cookie_jar.update_cookies(cookie, response_url=URL("https://yandex.ru"))
                secure = YandexSession(client, config=SessionConfig(proxy=proxy_url))
                with pytest.raises(ClientConnectorCertificateError):
                    await secure.get(path="probe", role="quasar", headers={"Authorization": "OAuth PRIVATE_TOKEN"},)
                assert observed == []

                with caplog.at_level(logging.WARNING):
                    diagnostic = YandexSession(
                        client, config=SessionConfig(proxy=proxy_url, ssl=False)
                    )
                    response = await diagnostic.get(path="probe", role="quasar", headers={"Authorization": "OAuth PRIVATE_TOKEN"},)
                    assert await response.json() == {"status": "ok", "source": "diagnostic-peer"}
                    ws = await diagnostic.ws_connect(url="wss://iot.quasar.yandex.ru/ws", role="cloud_ws")
                    message = await ws.receive()
                    assert message.type == WSMsgType.TEXT
                    assert message.data == "diagnostic-peer-ready"
                    await ws.close()
                assert observed == ["http", "websocket"]
                warnings = [record.getMessage() for record in caplog.records if "ssl=false" in record.getMessage()]
                assert len(warnings) == 1
                assert "PRIVATE_COOKIE" not in caplog.text
                assert "PRIVATE_TOKEN" not in caplog.text
                await diagnostic.async_stop()
                await secure.async_stop()
        finally:
            proxy.close()
            await proxy.wait_closed()
            pending = list(proxy_tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await runner.cleanup()

    asyncio.run(scenario())
