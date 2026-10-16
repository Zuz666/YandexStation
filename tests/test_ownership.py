import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import patch
import aiohttp

import pytest
from homeassistant.components import network
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry, device_registry, entity_registry

from custom_components.yandex_station.core import utils
from custom_components.yandex_station.core.cache import CacheManager
from custom_components.yandex_station.core.const import DATA_CONFIG, DATA_SPEAKERS, DOMAIN
from custom_components.yandex_station.core.lifecycle import get_lifecycle
from custom_components.yandex_station.core.policy import EntryPolicy, ScenariosState, SendOutcome
from custom_components.yandex_station.core.yandex_quasar import YandexQuasar
from custom_components.yandex_station.core.yandex_session import YandexSession
from custom_components.yandex_station.core.yandex_station import YandexStation
from custom_components.yandex_station.hass.hass_utils import (
    async_resolve_speaker_owners, incluce_devices,
)
from . import core_hass, seed_config_entries
from .test_cache import config_entry, device_record


@asynccontextmanager
async def ownership_runtime(tmp_path, *, disabled=()):
    hass = core_hass(tmp_path)
    entries = []
    for suffix in ('a', 'b'):
        record = config_entry('entry-' + suffix, 'login-' + suffix).as_dict()
        record['data'] = {'account_uid': 'uid-' + suffix}
        record['options'] = {'force_offline_local': True, 'enable_diagnostic_sensors': False}
        if suffix in disabled:
            record['disabled_by'] = 'user'
        entries.append(record)
    # Fixture seed only: consumers use real Core entry lookup and registries.
    await seed_config_entries(hass, entries)
    if setup_registry := getattr(device_registry, 'async_setup', None):
        setup_registry(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    await area_registry.async_load(hass)
    hass.data[DOMAIN] = {DATA_CONFIG: {}, DATA_SPEAKERS: {}}
    lifecycle = get_lifecycle(hass)
    try:
        # Containers have no multicast interface: use the threaded resolver.
        hass.data["aiohttp_resolver"] = aiohttp.DefaultResolver()
        client = utils.async_create_clientsession(hass)
        real_entries = {
            suffix: hass.config_entries.async_get_entry('entry-' + suffix)
            for suffix in ('a', 'b')
        }
        yield hass, real_entries, client, lifecycle
    finally:
        await lifecycle.async_stop()
        await aiohttp.ClientSession.close(client)
        await hass.async_stop()


async def prepare_account(hass, entry, *, host='127.0.0.1', devices=None):
    devices = devices if devices is not None else [
        device_record('cloud-' + entry.entry_id, 'shared-local', 'yandexstation')
    ]
    cache = CacheManager(
        hass, entry.entry_id, account_login=entry.unique_id,
        account_uid=entry.data['account_uid'],
    )
    await cache.async_save_devices(devices)
    for device in devices:
        if info := device.get('quasar_info'):
            await cache.async_save_device_token(
                info['device_id'], info['platform'], 'candidate-' + entry.entry_id
            )
            await cache.async_save_scenario_marker(
                device['id'], 'trigger-' + entry.entry_id, 'confirmed'
            )
            await cache.async_save_local_config(info['device_id'], host=host, port=1905)
    await cache.async_close()


async def restore_account(hass, entry, client):
    policy = EntryPolicy(force_offline_local=True)
    cache = CacheManager(
        hass, entry.entry_id, account_login=entry.unique_id,
        account_uid=entry.data['account_uid'],
    )
    quasar = YandexQuasar(
        YandexSession(client, policy=policy), policy=policy, cache_manager=cache,
    )
    await quasar.init()
    get_lifecycle(hass).runtimes[entry.entry_id] = quasar
    hass.data[DOMAIN][entry.unique_id] = quasar
    return quasar


def register_speaker(hass, entry, *, domain='media_player', suffix=''):
    device = device_registry.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, 'shared-local')},
        name='Physical speaker',
    )
    entity = entity_registry.async_get(hass).async_get_or_create(
        domain, DOMAIN, 'shared-local' + suffix,
        config_entry=entry, device_id=device.id,
        suggested_object_id='preserved_speaker' + suffix,
    )
    return device, entity


def test_first_active_entry_owns_shared_speaker_and_account_state_stays_separate(tmp_path):
    async def scenario():
        async with ownership_runtime(tmp_path) as (hass, entries, client, lifecycle):
            for entry in entries.values():
                await prepare_account(hass, entry)
            runtimes = {}
            for suffix in ('a', 'b'):
                quasar = runtimes[suffix] = await restore_account(hass, entries[suffix], client)
                await async_resolve_speaker_owners(hass, entries[suffix], quasar)
            assert runtimes['a'].owned_glagol_ids == {'shared-local'}
            assert runtimes['b'].owned_glagol_ids == set()
            for suffix, quasar in runtimes.items():
                assert quasar.devices[0]['id'] == 'cloud-entry-' + suffix
                assert quasar.device_tokens == {
                    'shared-local': {'yandexstation': 'candidate-entry-' + suffix}
                }
                assert set(quasar.cache_manager.snapshot.scenario_creation) == {
                    'cloud-entry-' + suffix
                }
            await runtimes['a'].async_stop()
            assert not client.closed
            assert not runtimes['b'].stopping
            assert await runtimes['b'].cache_manager.async_load() is not None
            assert lifecycle.speaker_owners['shared-local'] == 'entry-a'
    asyncio.run(scenario())


def test_media_registry_owner_wins_over_setup_order(tmp_path):
    async def scenario():
        async with ownership_runtime(tmp_path) as (hass, entries, client, _):
            _, media = register_speaker(hass, entries['b'])
            for entry in entries.values():
                await prepare_account(hass, entry)
            a = await restore_account(hass, entries['a'], client)
            b = await restore_account(hass, entries['b'], client)
            assert await async_resolve_speaker_owners(hass, entries['a'], a) == set()
            assert await async_resolve_speaker_owners(hass, entries['b'], b) == {'shared-local'}
            assert entity_registry.async_get(hass).async_get(media.entity_id).config_entry_id == 'entry-b'
    asyncio.run(scenario())


def test_disabled_owner_is_taken_over_automatically_preserving_entity_id_and_metadata(tmp_path):
    async def scenario():
        async with ownership_runtime(tmp_path, disabled=('a',)) as (hass, entries, client, _):
            device, media = register_speaker(hass, entries['a'])
            _, lyrics = register_speaker(hass, entries['a'], domain='camera', suffix='_lyrics')
            area = area_registry.async_get(hass).async_create('Preserved area')
            registry = entity_registry.async_get(hass)
            registry.async_update_entity(
                media.entity_id, name='User speaker name', area_id=area.id,
                disabled_by=entity_registry.RegistryEntryDisabler.USER,
            )
            device_registry.async_get(hass).async_update_device(
                device.id, name_by_user='User device name', area_id=area.id,
            )
            for entry in entries.values():
                await prepare_account(hass, entry)
            b = await restore_account(hass, entries['b'], client)
            before = deepcopy(b.cache_manager.snapshot.to_dict())
            assert await async_resolve_speaker_owners(hass, entries['b'], b) == {'shared-local'}

            updated = registry.async_get(media.entity_id)
            assert updated.unique_id == media.unique_id
            assert updated.config_entry_id == 'entry-b'
            assert updated.name == 'User speaker name' and updated.area_id == area.id
            assert updated.disabled_by == entity_registry.RegistryEntryDisabler.USER
            assert registry.async_get(lyrics.entity_id).unique_id == lyrics.unique_id
            assert registry.async_get(lyrics.entity_id).config_entry_id == 'entry-b'
            updated_device = device_registry.async_get(hass).async_get(device.id)
            assert updated_device.name_by_user == 'User device name'
            assert updated_device.area_id == area.id
            if hasattr(type(updated_device), 'config_entry_id'):
                assert updated_device.config_entry_id == 'entry-b'
            else:
                assert updated_device.config_entries == {'entry-b'}
            assert b.cache_manager.snapshot.to_dict() == before
            assert entries['a'].disabled_by is not None
    asyncio.run(scenario())


def test_unassigned_owner_cannot_connect_fetch_tokens_send_or_bypass_yaml_include(tmp_path):
    async def scenario():
        async with ownership_runtime(tmp_path) as (hass, entries, client, _):
            await prepare_account(hass, entries['b'])
            quasar = await restore_account(hass, entries['b'], client)
            device = quasar.devices[0]
            station = YandexStation(quasar, device)
            await station.init_local_mode()
            assert station.glagol is None and not station.available
            with pytest.raises(RuntimeError, match='speaker_not_owned'):
                await quasar.async_fetch_device_token(device)
            quasar.policy.force_offline_local = False
            quasar.readiness.scenarios_state = ScenariosState.READY
            device['scenario_id'] = 'synthetic-scenario'
            assert await quasar.send(device, 'must-not-be-spoken') == SendOutcome.UNAVAILABLE
            ordinary = {'id': 'ordinary-cloud', 'name': 'Ordinary switch', 'type': 'devices.types.switch', 'capabilities': [], 'properties': []}
            quasar.devices.append(ordinary)
            hass.data[DOMAIN][DATA_CONFIG]['include'] = [device['id'], ordinary['id']]
            assert [record['id'] for _, record, _ in incluce_devices(hass, entries['b'])] == ['ordinary-cloud']
    asyncio.run(scenario())


def test_reload_is_coalesced_after_requester_returns_and_disabled_entry_stays_disabled(tmp_path):
    async def scenario():
        async with ownership_runtime(tmp_path, disabled=('b',)) as (hass, entries, _, lifecycle):
            returned = False
            observed = []
            reloaded = asyncio.Event()
            async def reload_entry(entry_id):
                observed.append((entry_id, returned))
                reloaded.set()
                return True
            async def request():
                nonlocal returned
                assert lifecycle.request_entry_reload(entries['a'].entry_id)
                assert lifecycle.request_entry_reload(entries['a'].entry_id)
                assert not lifecycle.request_entry_reload(entries['b'].entry_id)
                assert observed == []
                returned = True
            with patch.object(hass.config_entries, 'async_reload', side_effect=reload_entry):
                await asyncio.create_task(request())
                await asyncio.wait_for(reloaded.wait(), 1)
                await hass.async_block_till_done()
            assert observed == [('entry-a', True)]
            assert entries['b'].disabled_by is not None
    asyncio.run(scenario())


def test_closed_gate_rejects_external_entry_and_service_but_allows_manager_capability(tmp_path):
    async def scenario():
        async with ownership_runtime(tmp_path) as (hass, entries, _, lifecycle):
            lifecycle.context.admission_open = False
            async with lifecycle.entry_operation(entries['a'].entry_id, 'setup') as admitted:
                assert admitted is False
            with pytest.raises(HomeAssistantError, match='reload_in_progress'):
                async with lifecycle.consumer():
                    pytest.fail('closed gate admitted a service')
            async def setup_entry(entry_id):
                async with lifecycle.entry_operation(entry_id, 'setup') as admitted:
                    assert admitted and lifecycle.entry_ids == {entry_id}
                    return True
            with patch.object(hass.config_entries, 'async_setup', side_effect=setup_entry):
                assert await lifecycle._async_core_entry_operation('async_setup', entries['a'].entry_id)
            assert lifecycle.entry_ids == set()
            async with lifecycle.entry_operation(entries['a'].entry_id, 'setup') as admitted:
                assert admitted is False
    asyncio.run(scenario())
