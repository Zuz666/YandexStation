import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.yandex_station.core.cache import (
    CacheManager,
    SCENARIO_STATE_CONFIRMED,
    SCENARIO_STATE_PENDING,
    StationSnapshot,
)
from custom_components.yandex_station.core.policy import EntryPolicy
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar, encode


def test_scenario_match_binds_and_saves_confirmed(tmp_path):
    async def scenario():
        session = MagicMock()
        cache = MagicMock(spec=CacheManager)
        cache.snapshot = StationSnapshot(entry_id="e1", account_login="u1")
        cache.async_save_scenario_marker = AsyncMock()

        quasar = YandexQuasar(session, cache_manager=cache)
        device_id = "dev_123"
        hash_code = encode(device_id)

        speaker = {
            "id": device_id,
            "name": "Колонка",
            "type": "devices.types.smart_speaker.yandex.station",
            "capabilities": [{"type": "devices.capabilities.on_off"}],
            "quasar_info": {"device_id": "did1", "platform": "yandexstation"},
        }
        quasar.devices = [speaker]
        quasar.scenarios = [
            {"id": "scen_existing_id", "triggers": [{"value": hash_code}]}
        ]

        quasar.add_scenario = AsyncMock()

        await quasar.load_speakers()

        assert speaker["scenario_id"] == "scen_existing_id"
        quasar.add_scenario.assert_not_called()
        cache.async_save_scenario_marker.assert_called_once_with(
            device_id, hash_code, SCENARIO_STATE_CONFIRMED
        )

    asyncio.run(scenario())


def test_scenario_creation_blocked_when_marker_exists(tmp_path):
    async def scenario():
        session = MagicMock()
        cache = MagicMock(spec=CacheManager)
        # Snapshot already has pending_unknown marker
        snapshot = StationSnapshot(
            entry_id="e1",
            account_login="u1",
            scenario_creation={"dev_123": {"trigger": "hash1", "state": SCENARIO_STATE_PENDING}},
        )
        cache.snapshot = snapshot
        cache.async_load = AsyncMock(return_value=snapshot)
        cache.async_save_scenario_marker = AsyncMock()

        quasar = YandexQuasar(session, cache_manager=cache)
        device_id = "dev_123"

        speaker = {
            "id": device_id,
            "name": "Колонка",
            "type": "devices.types.smart_speaker.yandex.station",
            "capabilities": [{"type": "devices.capabilities.on_off"}],
            "quasar_info": {"device_id": "did1", "platform": "yandexstation"},
        }
        quasar.devices = [speaker]
        quasar.scenarios = []  # No match in scenarios list!

        quasar.add_scenario = AsyncMock()

        await quasar.load_speakers()

        # Because marker exists, add_scenario MUST NOT be called!
        quasar.add_scenario.assert_not_called()
        assert "scenario_id" not in speaker or speaker["scenario_id"] is None

    asyncio.run(scenario())


def test_send_serialized_per_device():
    async def scenario():
        session = MagicMock()
        quasar = YandexQuasar(session)

        device = {"id": "dev1", "name": "Колонка", "scenario_id": "scen1"}

        order = []

        async def mock_put(url, **kwargs):
            text = kwargs.get("json", {}).get("steps", [{}])[0].get("parameters", {}).get("items", [{}])[0].get("value", {}).get("capabilities", [{}])[0].get("state", {}).get("value", {}).get("text", "")
            order.append(f"put_{text}")
            await asyncio.sleep(0.02)
            resp = MagicMock()
            resp.json = AsyncMock(return_value={"status": "ok"})
            return resp

        async def mock_post(url, **kwargs):
            order.append("post_action")
            await asyncio.sleep(0.01)
            resp = MagicMock()
            resp.json = AsyncMock(return_value={"status": "ok"})
            return resp

        session.put = AsyncMock(side_effect=mock_put)
        session.post = AsyncMock(side_effect=mock_post)

        # Run 2 concurrent send calls on the same device
        res1, res2 = await asyncio.gather(
            quasar.send(device, "cmd1", is_tts=True),
            quasar.send(device, "cmd2", is_tts=True),
        )

        assert res1 is True
        assert res2 is True
        # Verify strict order without interleaving PUTs and POSTs:
        # Either [put_cmd1, post_action, put_cmd2, post_action]
        # or [put_cmd2, post_action, put_cmd1, post_action]
        assert order in (
            ["put_cmd1", "post_action", "put_cmd2", "post_action"],
            ["put_cmd2", "post_action", "put_cmd1", "post_action"],
        )

    asyncio.run(scenario())


def test_send_missing_scenario_id_fails_cleanly():
    async def scenario():
        session = MagicMock()
        session.put = AsyncMock()
        session.post = AsyncMock()
        quasar = YandexQuasar(session)

        # Device has no scenario_id or scenario_id is None
        device = {"id": "dev1", "name": "Колонка", "scenario_id": None}

        ok = await quasar.send(device, "продолжить")
        assert ok is False
        session.put.assert_not_called()
        session.post.assert_not_called()

    asyncio.run(scenario())


def test_failed_durable_marker_verification_aborts_post():
    async def scenario():
        session = MagicMock()
        cache = MagicMock(spec=CacheManager)
        cache.snapshot = StationSnapshot(entry_id="e1", account_login="u1")
        # Verification returns False (e.g. disk write failed)
        cache.async_save_and_verify_scenario_marker = AsyncMock(return_value=False)

        quasar = YandexQuasar(session, cache_manager=cache)
        quasar.devices = [{
            "id": "dev_fail_1",
            "name": "Колонка",
            "capabilities": [{"type": "devices.capabilities.on_off"}],
            "quasar_info": {"device_id": "did1", "platform": "yandexstation"},
        }]
        quasar.scenarios = []
        quasar.add_scenario = AsyncMock()

        await quasar.load_speakers()

        # Because durable verification failed, POST MUST NOT be called!
        quasar.add_scenario.assert_not_called()
        assert quasar.devices[0]["scenario_id"] is None

    asyncio.run(scenario())


def test_concurrent_load_speakers_calls_are_serialized():
    async def scenario():
        session = MagicMock()
        cache = MagicMock(spec=CacheManager)
        snapshot = StationSnapshot(entry_id="e1", account_login="u1")
        cache.snapshot = snapshot

        async def mock_save_and_verify(dev_id, trigger, state):
            snapshot.scenario_creation[dev_id] = {"trigger": trigger, "state": state}
            await asyncio.sleep(0.01)
            return True

        cache.async_save_and_verify_scenario_marker = AsyncMock(side_effect=mock_save_and_verify)
        cache.async_save_scenario_marker = AsyncMock()

        quasar = YandexQuasar(session, cache_manager=cache)
        speaker = {
            "id": "dev_concurrent_1",
            "name": "Колонка",
            "capabilities": [{"type": "devices.capabilities.on_off"}],
            "quasar_info": {"device_id": "did1", "platform": "yandexstation"},
        }
        quasar.devices = [speaker]
        quasar.scenarios = []

        add_count = 0

        async def mock_add(dev_id, hash_code):
            nonlocal add_count
            add_count += 1
            await asyncio.sleep(0.01)
            return f"scen_{add_count}"

        quasar.add_scenario = AsyncMock(side_effect=mock_add)

        # Run two concurrent load_speakers calls
        await asyncio.gather(
            quasar.load_speakers(),
            quasar.load_speakers(),
        )

        # Reconciliation lock ensures exactly 1 scenario creation POST occurred
        assert add_count == 1
        assert speaker["scenario_id"] == "scen_1"

    asyncio.run(scenario())


def test_untrusted_journal_aborts_creation():
    async def scenario():
        session = MagicMock()
        cache = MagicMock(spec=CacheManager)
        cache.is_untrusted = True
        cache.snapshot = None

        quasar = YandexQuasar(session, cache_manager=cache)
        quasar.devices = [{
            "id": "dev_untrusted_1",
            "name": "Колонка",
            "capabilities": [{"type": "devices.capabilities.on_off"}],
            "quasar_info": {"device_id": "did1", "platform": "yandexstation"},
        }]
        quasar.scenarios = []
        quasar.add_scenario = AsyncMock()

        await quasar.load_speakers()

        # Untrusted journal forbids automatic creation
        quasar.add_scenario.assert_not_called()
        assert quasar.devices[0]["scenario_id"] is None

    asyncio.run(scenario())
