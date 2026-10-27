Smart speakers types:

```
devices.types.smart_speaker.yandex.station
devices.types.smart_speaker.yandex.station_2
devices.types.smart_speaker.yandex.station.mini
devices.types.smart_speaker.yandex.station.micro
devices.types.smart_speaker.yandex.station.mini_2
devices.types.smart_speaker.yandex.station.mini_2_no_clock
devices.types.smart_speaker.yandex.station.midi
devices.types.smart_speaker.yandex.station.quinglong
devices.types.smart_speaker.yandex.station.chiron
devices.types.smart_speaker.dexp.smartbox
devices.types.smart_speaker.irbis.a
devices.types.smart_speaker.elari.smartbeat
devices.types.smart_speaker.lg.xboom_wk7y
devices.types.smart_speaker.prestigio.smartmate
devices.types.smart_speaker.jbl.link_music
devices.types.smart_speaker.jbl.link_portable
devices.types.media_device.dongle.yandex.module
devices.types.media_device.dongle.yandex.module_2
devices.types.media_device.tv
devices.types.media_device.tv.yandex.goya
devices.types.media_device.tv.yandex.magritte
```

## Resilience ownership

- `policy.ROLE_BASE_FIELDS` defines the endpoint roles, option fields and default bases shared by session configuration and the options form.
- `YandexSession` owns request admission, authentication and single-flight CSRF acquisition. Configuration normalization does not replace destination checks before transport.
- `YandexQuasar` owns cloud inventory decoding, token candidates, compare-invalidation in RAM/Store and cancellation-safe single-flight acquisition. Local callers do not inspect the token map. Scenario automatic eligibility and explicit manual authorization remain separate; neither path retries an uncertain creation.
- `CacheManager.async_save_scenario_marker()` returns true only after disk read-back confirms the saved revision. A creation POST requires a durable pending marker.
- `YandexGlagol` owns the local connection and acceptance of a supplied token. It reports a confirmed current-connection rejection through one device-bound async callback, which returns a candidate without restarting the reader. Local disconnect is published before waiting for persistence/acquisition; a late result cannot replace a newer token or revive a stopped client. Start, restart, token adoption and stop serialize reader replacement. Quasar drains token acquisition/invalidation, entity-owned HDMI tasks and local readers before closing their session and cache.
- Cached startup and mDNS read candidates synchronously. No replacement means waiting, not a new cloud retry worker; an already-running post-publication recovery or a subsequent permitted setup/reload may supply the next candidate. Token Store writes do not use the scenario journal's disk read-back barrier.
- Rejected strings remain guarded for this entry runtime, including across different unaccepted candidates. Receiving a candidate cannot retire the guard. A new setup/reload creates a fresh runtime; there is no persisted or global blacklist.
- `YandexQuasar.cloud_control_ready()` supplies the shared cloud-route predicate; callers retain ownership, stopping and operation-specific admission checks.
- Account diagnostic sensors share readiness subscriptions, while keeping their individual state projections and registry identities.
- `LifecycleManager` owns the stable `StreamContext`, service dispatchers, discovery lifecycle and dev `reload_code` execution.

## Glagol token recovery logs

`glagol_token_rejected` (WARNING) records a confirmed peer CLOSE `4000 / Invalid token`.
`glagol_token_recovered` (INFO) closes that incident once a state message confirms the current connection's token. Its `elapsed` measures monotonic seconds from the first rejection, including intervening retries. Ordinary connections and subsequent state messages do not emit recovery INFO; stopping the client clears the pending incident.

Both events use the same device identifier and never include token values. Recovery messages require INFO or DEBUG logging; they are hidden at WARNING.