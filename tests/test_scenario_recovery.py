import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.yandex_station.core.cache import (
    CacheManager,
    SCENARIO_STATE_CONFIRMED,
    SCENARIO_STATE_PENDING,
    StationSnapshot,
)
from custom_components.yandex_station.core.policy import EntryPolicy, ScenariosState
from custom_components.yandex_station.core.yandex_quasar import (
    YandexQuasar, encode, scenario_speaker_tts,
)
from .test_cache import device_record


def wire_scenario(sid, device_id="dev_123", trigger=None):
    payload = scenario_speaker_tts(
        f"ХА {device_id}", trigger or encode(device_id), device_id, "пустышка"
    )
    return {"id": sid, **payload}



def quasar_with(session=None, *, scenarios=None, cache=None):
    session = session or MagicMock()
    cache = cache or MagicMock(spec=CacheManager)
    cache.snapshot = StationSnapshot(entry_id="e1", account_login="u1")
    cache.async_save_scenario_marker = AsyncMock()
    cache.async_save_and_verify_scenario_marker = AsyncMock(return_value=True)
    quasar = YandexQuasar(session, policy=EntryPolicy(), cache_manager=cache)
    quasar.owned_glagol_ids = {"did1"}
    speaker = {
        "id": "dev_123",
        "name": "Колонка",
        "type": "devices.types.smart_speaker.yandex.station",
        "capabilities": [{"type": "devices.capabilities.on_off"}],
        "quasar_info": {"device_id": "did1", "platform": "yandexstation"},
    }
    quasar.devices = [speaker]
    quasar.scenarios = scenarios if scenarios is not None else []
    quasar.add_scenario = AsyncMock()
    quasar.readiness.scenarios_state = ScenariosState.READY
    return quasar, speaker, cache, session


def test_created_scenario_binds_after_catalog_detail_and_restart_without_post():
    async def scenario():
        quasar, speaker, _, session = quasar_with()
        session.ensure_csrf = AsyncMock(return_value="csrf")
        response = AsyncMock()
        response.json.return_value = {"status": "ok", "scenario_id": "scen_created"}
        session.post = AsyncMock(return_value=response)
        quasar.add_scenario = YandexQuasar.add_scenario.__get__(quasar)
        await quasar.load_speakers()
        assert speaker["scenario_id"] == "scen_created"
        created = quasar.scenarios[0]
        assert created["steps"] == wire_scenario("scen_created")["steps"]
        assert {**session.post.await_args.kwargs["json"], "id": "scen_created"} == created

        restarted, restored, _, network = quasar_with()
        catalog = AsyncMock()
        catalog.json.return_value = {
            "status": "ok",
            "scenarios": [{"id": created["id"], "triggers": created["triggers"]}],
        }
        detail = AsyncMock()
        detail.json.return_value = {"status": "ok", "scenario": created}

        async def fetch(url, **kwargs):
            return detail if url.endswith("/edit") else catalog

        network.get = AsyncMock(side_effect=fetch)
        await restarted.load_scenarios()
        await restarted.load_speakers()
        assert restored["scenario_id"] == "scen_created"
        restarted.add_scenario.assert_not_called()
        assert network.get.await_count == 2

    asyncio.run(scenario())


def test_inconsistent_or_additional_device_target_never_binds():
    async def scenario():
        for mutate in (
            lambda item: item["value"].update(id="other-device"),
            lambda item: item.update(id="other-device"),
        ):
            record = wire_scenario("scen_bad")
            mutate(record["steps"][0]["parameters"]["items"][0])
            quasar, speaker, _, _ = quasar_with(scenarios=[record])
            await quasar.load_speakers()
            assert speaker["scenario_id"] is None
            assert speaker["scenario_unresolved"]
            quasar.add_scenario.assert_not_called()

        record = wire_scenario("scen_extra")
        extra = wire_scenario("other", "other-device")["steps"][0]["parameters"]["items"][0]
        record["steps"][0]["parameters"]["items"].append(extra)
        quasar, speaker, _, _ = quasar_with(scenarios=[record])
        await quasar.load_speakers()
        assert speaker["scenario_id"] is None
        assert speaker["scenario_unresolved"]
        quasar.add_scenario.assert_not_called()

    asyncio.run(scenario())

def test_creation_without_durable_store_never_posts():
    async def scenario():
        quasar, speaker, _, session = quasar_with()
        quasar.cache_manager = None
        session.ensure_csrf = AsyncMock(return_value="csrf")
        await quasar.load_speakers()
        assert speaker["scenario_id"] is None
        quasar.add_scenario.assert_not_called()
        session.ensure_csrf.assert_not_awaited()

        quasar.load_scenarios = AsyncMock()
        try:
            await quasar.retry_scenario_creation("dev_123", confirm_risk=True)
            assert False, "durable marker required"
        except RuntimeError as error:
            assert str(error) == "marker_unavailable"
        quasar.add_scenario.assert_not_called()

    asyncio.run(scenario())


def test_multiple_trigger_matches_leave_binding_unresolved_and_ban_post():
    async def scenario():
        hash_code = encode("dev_123")
        scenarios = [
            wire_scenario("scen_a"),
            {"id": "scen_b",
             "triggers": [{"type": "other"}, {"value": hash_code}]},
        ]
        quasar, speaker, cache, session = quasar_with(scenarios=scenarios)
        await quasar.load_speakers()
        assert speaker["scenario_id"] is None
        assert speaker["scenario_unresolved"] is True
        quasar.add_scenario.assert_not_called()
        cache.async_save_and_verify_scenario_marker.assert_not_called()

    asyncio.run(scenario())


def test_foreign_target_is_not_proof_of_absence():
    async def scenario():
        hash_code = encode("dev_123")
        scenarios = [wire_scenario("scen_foreign", "someone-else-device", hash_code)]
        quasar, speaker, cache, session = quasar_with(scenarios=scenarios)
        await quasar.load_speakers()
        assert speaker["scenario_id"] is None
        assert speaker["scenario_unresolved"] is True
        quasar.add_scenario.assert_not_called()

    asyncio.run(scenario())


def test_nested_and_typed_triggers_are_all_matched():
    async def scenario():
        hash_code = encode("dev_123")
        scenarios = [
            # Typed trigger is second: all triggers are inspected, not only the first
            {"id": "scen_typed",
             "triggers": [
                 {"type": "scenario.trigger.other", "value": "noise"},
                 {"type": "scenario.trigger.voice", "value": hash_code},
             ],
             "steps": wire_scenario("scen_typed")["steps"]},
            wire_scenario("scen_nested"),
        ]
        quasar, speaker, _, _ = quasar_with()
        # Verify each scenario format (typed and nested) in turn
        quasar.scenarios = [scenarios[0]]
        await quasar.load_speakers()
        assert speaker["scenario_id"] == "scen_typed"
        quasar.scenarios = [scenarios[1]]
        speaker.pop("scenario_id", None)
        await quasar.load_speakers()
        assert speaker["scenario_id"] == "scen_nested"

    asyncio.run(scenario())


def test_nested_nonvoice_trigger_does_not_bind():
    async def scenario():
        record = wire_scenario("scen_wrong_type")
        record["triggers"][0]["trigger"]["type"] = "scenario.trigger.other"
        quasar, speaker, cache, _ = quasar_with(scenarios=[record])
        cache.snapshot.scenario_creation["dev_123"] = {
            "trigger": encode("dev_123"), "state": SCENARIO_STATE_PENDING,
        }
        await quasar.load_speakers()
        assert speaker["scenario_id"] is None
        quasar.add_scenario.assert_not_called()

    asyncio.run(scenario())

def test_incomplete_candidate_resolves_via_detail_get():
    async def scenario():
        hash_code = encode("dev_123")
        scenarios = [{"id": "scen_detail", "triggers": [{"value": hash_code}]}]
        quasar, speaker, _, session = quasar_with(scenarios=scenarios)
        detail = AsyncMock()
        detail.json.return_value = {
            "status": "ok",
            "scenario": wire_scenario("scen_detail"),
        }

        async def fake_get(url, **kwargs):
            assert url.endswith("/scenarios/scen_detail/edit"), url
            return detail

        session.get = AsyncMock(side_effect=fake_get)
        await quasar.load_speakers()
        assert speaker["scenario_id"] == "scen_detail"
        quasar.add_scenario.assert_not_called()

    asyncio.run(scenario())


def test_unavailable_detail_leaves_binding_unresolved():
    async def scenario():
        hash_code = encode("dev_123")
        scenarios = [{"id": "scen_detail", "triggers": [{"value": hash_code}]}]
        quasar, speaker, _, session = quasar_with(scenarios=scenarios)

        async def fake_get(url, **kwargs):
            raise RuntimeError("detail unavailable")

        session.get = AsyncMock(side_effect=fake_get)
        await quasar.load_speakers()
        assert speaker["scenario_id"] is None
        assert speaker["scenario_unresolved"] is True
        quasar.add_scenario.assert_not_called()

    asyncio.run(scenario())


def test_failed_catalog_get_clears_executable_bindings():
    async def scenario():
        quasar, speaker, _, session = quasar_with()
        speaker["scenario_id"] = "scen_stale"

        async def fake_get(url, **kwargs):
            raise RuntimeError("catalog down")

        session.get = AsyncMock(side_effect=fake_get)
        await quasar.load_scenarios()
        assert quasar.readiness.scenarios_state == ScenariosState.UNAVAILABLE
        assert quasar.scenarios is None
        assert speaker["scenario_id"] is None

    asyncio.run(scenario())


def test_retry_scenario_creation_binds_existing_match_without_post():
    async def scenario():
        hash_code = encode("dev_123")
        quasar, speaker, cache, session = quasar_with()
        quasar.load_scenarios = AsyncMock(
            side_effect=lambda: setattr(
                quasar, "scenarios",
                [wire_scenario("scen_late")],
            )
        )
        result = await quasar.retry_scenario_creation("dev_123", confirm_risk=True)
        assert result == "bound"
        assert speaker["scenario_id"] == "scen_late"
        quasar.add_scenario.assert_not_called()

    asyncio.run(scenario())


def test_retry_scenario_creation_requires_confirm_and_posts_once():
    async def scenario():
        quasar, speaker, cache, session = quasar_with()
        quasar.load_scenarios = AsyncMock()  # Fresh successful catalog GET
        cache.snapshot.scenario_creation["dev_123"] = {
            "trigger": "old", "state": SCENARIO_STATE_PENDING,
        }
        session.ensure_csrf = AsyncMock(return_value=True)
        quasar.add_scenario = AsyncMock(return_value="scen_new")

        try:
            await quasar.retry_scenario_creation("dev_123", confirm_risk=False)
            assert False, "confirm_required expected"
        except RuntimeError as error:
            assert str(error) == "confirm_required"
        quasar.add_scenario.assert_not_called()

        result = await quasar.retry_scenario_creation("dev_123", confirm_risk=True)
        assert result == "created"
        assert speaker["scenario_id"] == "scen_new"
        quasar.add_scenario.assert_called_once()
        # Marker is replaced with a new revision without intermediate deletion
        cache.async_save_and_verify_scenario_marker.assert_called_once_with(
            "dev_123", encode("dev_123"), SCENARIO_STATE_PENDING
        )
        cache.async_save_scenario_marker.assert_called_once_with(
            "dev_123", encode("dev_123"), SCENARIO_STATE_CONFIRMED
        )

    asyncio.run(scenario())


def test_retry_scenario_creation_refuses_conflict_and_unavailable():
    async def scenario():
        hash_code = encode("dev_123")
        quasar, speaker, _, _ = quasar_with(
            scenarios=[wire_scenario("scen_a"), wire_scenario("scen_b")]
        )
        quasar.load_scenarios = AsyncMock()  # Conflict is checked on the loaded catalog
        try:
            await quasar.retry_scenario_creation("dev_123", confirm_risk=True)
            assert False, "scenario_conflict expected"
        except RuntimeError as error:
            assert str(error) == "scenario_conflict"
        quasar.add_scenario.assert_not_called()

        quasar2, _, _, _ = quasar_with()
        quasar2.readiness.scenarios_state = ScenariosState.UNAVAILABLE
        quasar2.load_scenarios = AsyncMock()
        try:
            await quasar2.retry_scenario_creation("dev_123", confirm_risk=True)
            assert False, "scenarios_unavailable expected"
        except RuntimeError as error:
            assert str(error) == "scenarios_unavailable"

    asyncio.run(scenario())


def test_scenario_worker_retries_until_ready_and_finishes():
    async def scenario():
        quasar, speaker, cache, session = quasar_with()
        attempts = []

        async def flaky_load():
            attempts.append(1)
            if len(attempts) < 2:
                quasar.readiness.scenarios_state = ScenariosState.UNAVAILABLE
                return
            quasar.readiness.scenarios_state = ScenariosState.READY
            quasar.scenarios = []

        # Clamp backoff to a single event-loop turn
        from custom_components.yandex_station.core.policy import RecoveryBackoff
        original = RecoveryBackoff.next_delay
        RecoveryBackoff.next_delay = lambda self, retry_after=None: 0
        quasar.load_scenarios = flaky_load
        speaker_resolved = MagicMock(return_value=None)
        quasar._resolve_speaker_scenario = speaker_resolved
        try:
            await quasar._scenario_worker()
        finally:
            RecoveryBackoff.next_delay = original
        assert len(attempts) == 2
        speaker_resolved.assert_called_once_with(speaker)
        assert quasar._scenario_worker_task is None

    asyncio.run(scenario())
