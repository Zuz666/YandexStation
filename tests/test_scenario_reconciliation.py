import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.media_player import MediaPlayerState
from homeassistant.exceptions import HomeAssistantError

from custom_components.yandex_station.core.cache import (
    CacheManager,
    SCENARIO_STATE_CONFIRMED,
    SCENARIO_STATE_PENDING,
    StationSnapshot,
)
from custom_components.yandex_station.core.policy import (
    EntryPolicy, Readiness, ScenariosState, SendOutcome,
)
from custom_components.yandex_station.core.yandex_quasar import (
    YandexQuasar, encode, scenario_speaker_tts,
)
from custom_components.yandex_station.core.yandex_session import CloudRequestError
from custom_components.yandex_station.core.yandex_station import YandexStation
from .test_cache import device_record



class ScenarioCommandPeer:
    """A stateful scenario endpoint: actions execute the last stored configuration."""

    def __init__(self):
        self.policy = EntryPolicy()
        self.readiness = Readiness(scenarios_state=ScenariosState.READY)
        self.configured = None
        self.executed = []
        self.put_failure = False
        self.put_status = None
        self.put_calls = []
        self.lose_action_response = False
        self.action_started = asyncio.Event()
        self.release_action = None
        self.retry_after = {}
        self.catalog = None

    async def ensure_csrf(self):
        return "synthetic-csrf"

    async def json(self):
        return {"status": "ok"}

    async def put(self, *, path, role, json):
        if self.put_failure:
            raise CloudRequestError(role, 403)
        if self.put_status is not None:
            self.put_calls.append(self.put_status)
            raise CloudRequestError(role, self.put_status)
        value = json["steps"][0]["parameters"]["items"][0]["value"]
        self.configured = value["capabilities"][0]["state"]["value"]
        await asyncio.sleep(0)
        return self

    async def get(self, *, path, role):
        if self.catalog is None:
            raise CloudRequestError(role, 404)
        return self.catalog

    async def post(self, *, path, role):
        self.executed.append(self.configured)
        self.action_started.set()
        if self.release_action:
            await self.release_action.wait()
        if self.lose_action_response:
            raise asyncio.TimeoutError
        await asyncio.sleep(0)
        return self


def command_runtime(peer):
    quasar = YandexQuasar(peer)
    quasar.owned_glagol_ids = {"glagol1"}
    device = device_record("dev1", "glagol1")
    device["scenario_id"] = "scen1"
    quasar.devices = [device]
    return quasar, device


def test_scenario_match_binds_and_saves_confirmed(tmp_path):
    async def scenario():
        session = MagicMock()
        cache = MagicMock(spec=CacheManager)
        cache.snapshot = StationSnapshot(entry_id="e1", account_login="u1")
        cache.async_save_scenario_marker = AsyncMock()

        quasar = YandexQuasar(session, cache_manager=cache)
        quasar.owned_glagol_ids = {"did1"}
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
            {"id": "scen_existing_id", "triggers": [{"value": hash_code}],
             "steps": scenario_speaker_tts("ХА dev_123", hash_code, device_id, "пустышка")["steps"]}
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
        quasar.owned_glagol_ids = {"did1"}
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
        peer = ScenarioCommandPeer()
        quasar, device = command_runtime(peer)
        outcomes = await asyncio.gather(
            quasar.send(device, "cmd1", is_tts=True),
            quasar.send(device, "cmd2", is_tts=True),
        )
        assert outcomes == [SendOutcome.SENT, SendOutcome.SENT]
        assert peer.executed == [{"text": "cmd1"}, {"text": "cmd2"}]

    asyncio.run(scenario())


def test_send_missing_scenario_id_fails_cleanly():
    async def scenario():
        peer = ScenarioCommandPeer()
        quasar, device = command_runtime(peer)
        device["scenario_id"] = None

        ok = await quasar.send(device, "продолжить")
        assert ok == SendOutcome.UNAVAILABLE
        assert peer.configured is None
        assert peer.executed == []

    asyncio.run(scenario())


def test_failed_put_and_lost_action_response_never_replay(caplog):
    async def scenario():
        peer = ScenarioCommandPeer()
        quasar, device = command_runtime(peer)
        secret = "synthetic-secret-spoken-phrase"
        peer.put_failure = True
        assert await quasar.send(device, secret) == SendOutcome.UNAVAILABLE
        assert peer.executed == []
        assert "scenario_update" in caplog.text
        assert "quasar: HTTP 403" in caplog.text
        peer.put_failure = False
        peer.lose_action_response = True
        assert await quasar.send(device, secret) == SendOutcome.UNKNOWN_OUTCOME
        assert peer.executed == [secret]
        assert device["last_command_outcome"] == "unknown_outcome"
        assert "scenario_action" in caplog.text
        assert "TimeoutError" in caplog.text
        assert secret not in caplog.text

    asyncio.run(scenario())


def test_command_deadline_includes_lock_wait(monkeypatch):
    async def scenario():
        from custom_components.yandex_station.core import yandex_quasar

        monkeypatch.setattr(yandex_quasar, "COMMAND_TIMEOUT", 0.01)
        peer = ScenarioCommandPeer()
        quasar, device = command_runtime(peer)
        lock = asyncio.Lock()
        await lock.acquire()
        quasar._device_command_locks["dev1"] = lock
        try:
            assert await quasar.send(device, "expired") == SendOutcome.UNAVAILABLE
        finally:
            lock.release()
        await asyncio.sleep(0)
        assert peer.configured is None
        assert peer.executed == []

    asyncio.run(scenario())


def test_cancelled_action_records_unknown_outcome_without_replay():
    async def scenario():
        peer = ScenarioCommandPeer()
        peer.release_action = asyncio.Event()
        quasar, device = command_runtime(peer)
        task = asyncio.create_task(quasar.send(device, "one-effect"))
        await asyncio.wait_for(peer.action_started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert peer.executed == ["one-effect"]
        assert device["last_command_outcome"] == "unknown_outcome"

    asyncio.run(scenario())


@pytest.mark.parametrize("lost_action", [False, True])
@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("async_set_volume_level", (0.7,)),
        ("async_media_play", ()),
        ("async_media_pause", ()),
        ("async_media_stop", ()),
        ("async_media_previous_track", ()),
        ("async_media_next_track", ()),
        ("async_play_media", ("text", "synthetic-secret-phrase")),
        ("async_play_media", ("command", "synthetic-secret-phrase")),
        ("async_play_media", ("text:synthetic-skill", "synthetic-secret-phrase")),
    ],
)
def test_station_failed_cloud_commands_preserve_state(method, args, lost_action, caplog):
    async def scenario():
        peer = ScenarioCommandPeer()
        peer.put_failure = not lost_action
        peer.lose_action_response = lost_action
        quasar, device = command_runtime(peer)
        station = YandexStation(quasar, device)
        station._attr_state = MediaPlayerState.PAUSED
        station._attr_volume_level = 0.4
        with pytest.raises(HomeAssistantError):
            await getattr(station, method)(*args)
        assert station.state == MediaPlayerState.PAUSED
        assert station.volume_level == 0.4
        assert len(peer.executed) == int(lost_action)
        assert device["last_command_outcome"] == (
            "unknown_outcome" if lost_action else "unavailable"
        )
        assert "synthetic-secret-phrase" not in caplog.text

    asyncio.run(scenario())


def test_failed_durable_marker_verification_aborts_post():
    async def scenario():
        session = MagicMock()
        session.ensure_csrf = AsyncMock(return_value=True)
        cache = MagicMock(spec=CacheManager)
        cache.snapshot = StationSnapshot(entry_id="e1", account_login="u1")
        # Verification returns False (e.g. disk write failed)
        cache.async_save_scenario_marker = AsyncMock(return_value=False)

        quasar = YandexQuasar(session, cache_manager=cache)
        quasar.owned_glagol_ids = {"did1"}
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
        session.ensure_csrf = AsyncMock(return_value=True)
        cache = MagicMock(spec=CacheManager)
        snapshot = StationSnapshot(entry_id="e1", account_login="u1")
        cache.snapshot = snapshot

        async def mock_save(dev_id, trigger, state):
            snapshot.scenario_creation[dev_id] = {"trigger": trigger, "state": state}
            await asyncio.sleep(0.01)
            return True

        cache.async_save_scenario_marker = AsyncMock(side_effect=mock_save)

        quasar = YandexQuasar(session, cache_manager=cache)
        quasar.owned_glagol_ids = {"did1"}
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
        cache.scenario_creation_trusted = False
        cache.snapshot = None

        quasar = YandexQuasar(session, cache_manager=cache)
        quasar.owned_glagol_ids = {"did1"}
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


def test_command_404_rearms_worker_and_rebinds_without_command_replay():
    """Finding 9: a 404 clears the binding and must re-arm reconciliation."""
    async def scenario():
        payload = scenario_speaker_tts("ХА dev1", encode("dev1"), "dev1", "пустышка")
        candidate = {
            "id": "scen1",
            "triggers": payload["triggers"],
            "steps": payload["steps"],
        }
        peer = ScenarioCommandPeer()
        catalog = AsyncMock()
        catalog.json = AsyncMock(return_value={
            "status": "ok", "scenarios": [candidate],
        })
        peer.catalog = catalog
        quasar, device = command_runtime(peer)
        # Successful startup already reconciled; the worker has finished
        device["scenario_id"] = "scen1"

        peer.put_status = 404
        outcome = await quasar.send(device, "привет")

        assert outcome is SendOutcome.UNAVAILABLE
        assert "scenario_id" not in device
        assert quasar.readiness.scenarios_state is ScenariosState.UNAVAILABLE

        worker = quasar._scenario_worker_task
        assert worker is not None
        await asyncio.wait_for(asyncio.shield(worker), 5)

        # Reconciliation re-bound the speaker via GET; the command never replayed
        assert device["scenario_id"] == "scen1"
        assert peer.executed == []
        assert peer.put_calls == [404]

    asyncio.run(scenario())
