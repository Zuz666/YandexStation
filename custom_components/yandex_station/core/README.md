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
- `YandexQuasar` owns cloud inventory decoding, token acquisition and scenario creation. Automatic eligibility and explicit manual authorization remain separate; neither path retries an uncertain creation.
- `CacheManager.async_save_scenario_marker()` returns true only after disk read-back confirms the saved revision. A creation POST requires a durable pending marker.
- `YandexGlagol` owns the local connection and acceptance of the supplied token, not a second cloud token-fetch path.
- `YandexQuasar.cloud_control_ready()` supplies the shared cloud-route predicate; callers retain ownership, stopping and operation-specific admission checks.
- Account diagnostic sensors share readiness subscriptions, while keeping their individual state projections and registry identities.
- `LifecycleManager` keeps reload qualification at both admission and execution. Changes to the stable lifecycle module or removed module bindings require an ordinary Core restart.