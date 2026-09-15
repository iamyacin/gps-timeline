# GPS Timeline

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/iamyacin/gps-timeline/main/custom_components/gps_timeline/brand/dark_logo.png">
  <img src="https://raw.githubusercontent.com/iamyacin/gps-timeline/main/custom_components/gps_timeline/brand/logo.png" alt="GPS Timeline" width="200">
</picture>

**GPS Timeline** is a Home Assistant integration that archives the location history of your trackers into a dedicated SQLite database that is **only deleted when you confirm it**, and serves that history back to dashboards through a WebSocket API — unlocking unlimited history depth for the [Location Timeline Card](https://community.home-assistant.io/t/location-timeline-card-to-easily-show-location-history/989513).

By default, Home Assistant's recorder keeps entity history for only 10 days (`purge_keep_days`), and there is no per-entity retention option. GPS Timeline fixes that for location data: every GPS fix is stored in its own SQLite database with full attribute fidelity and stays there forever.

## Features

- **Permanent GPS history** — points are stored in a dedicated SQLite database (`<config>/gps_timeline/gps_timeline.db`) and only deleted when you confirm it (removing a tracker asks whether to also delete its archived data; orphaned history can be re-attached when the subject is added again).
- **Any entity with coordinates** — track `device_tracker.*`, `person.*` or any entity exposing `latitude`/`longitude` attributes.
- **Maximum fidelity** — every accepted fix is stored with all its attributes (accuracy, battery, speed, altitude, course, and anything else the source reports) as a full snapshot.
- **Multiple users and devices** — add one configuration entry per tracked device; every entry can also archive its Places and activity sensors.
- **Companion archive** — Places integration labels and activity sensor states are archived alongside the GPS points, so old timelines keep their place names even after the recorder purges them.
- **Subjects** — optionally label what a device tracks (`person`, `topic` or `object`, plus a free-text name, e.g. "Yacin" or "Car"); the subject is exposed as attributes on the tracker entity.
- **Rename-safe** — renaming or reconfiguring the tracked entity (or its companion sensors) in the entity registry keeps the full archived timeline; historical rows follow the new entity id and archiving continues without a gap.
- **Backfill** — import up to 10 days of existing recorder history with one service call, so there is no gap when you start.
- **Junk filtering** — fixes whose GPS accuracy is worse than a configurable threshold are skipped; coordinates are stored rounded to 6 decimals (~11 cm precision).
- **Exposed entity** — each tracked device gets a `device_tracker.*` entity mirroring the latest archived point, usable anywhere in Home Assistant.
- **Zero dependencies** — pure Python standard library (`sqlite3`), no external databases, add-ons or containers.

## Installation

### HACS (recommended)

Ensure that [HACS](https://hacs.xyz/) is installed

<a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=custom-components&repository=places" target="_blank" rel="noreferrer noopener"><img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open your Home Assistant instance to download the places integration." /></a>

1. Open **HACS → Integrations → ⋮ → Custom repositories**.
2. Add this repository URL and set the category to **Integration**.
3. Install **GPS Timeline**, then restart Home Assistant.

### Manual

<a href="https://my.home-assistant.io/redirect/config_flow_start/?domain=places" target="_blank" rel="noreferrer noopener"><img src="https://my.home-assistant.io/badges/config_flow_start.svg" alt="Open your Home Assistant instance to create a places entry." /></a>

Copy `custom_components/gps_timeline` into the `custom_components` directory of your Home Assistant configuration, then restart Home Assistant.

## Configuration

1. Go to **Settings → Devices & services → Add integration** and search for **GPS Timeline**.
2. Pick the entity to track (one entry per device) and optionally select a **Places sensor** and an **activity sensor** to archive alongside it.
3. Optionally set a **subject**: what the device tracks (`person`, `topic` or `object`) and its name. Both fields are also available later from the entry's options; the subject appears as attributes on the exposed tracker entity.
4. Set the **maximum GPS accuracy** (default 100 m): fixes with worse accuracy are ignored. Set it to 0 to keep every fix.

Repeat for each additional person or device. Deleting an entry removes the listener and the exposed entity but **keeps the archived data**.

### Renaming and reconfiguring

- **Rename in the entity registry** (Settings → Devices & services → Entities → pencil): GPS Timeline follows the rename automatically — the archived history moves to the new entity id and live archiving keeps working. This applies to the tracked entity and to companion sensors.
- **Reconfigure** (entry → ⋮ → Reconfigure): switch the tracked entity or companion sensors without deleting the entry; the archive and its history are preserved and moved to the new entity ids.

Existing databases from earlier versions migrate transparently on upgrade — no data is lost and no action is needed.

## Use with the Location Timeline Card

The card reads history through Home Assistant's `history/history_during_period` WebSocket command, which is served from the purged recorder database. A `history_source` option in the card redirects that query to GPS Timeline:

```yaml
type: custom:location-timeline-card
entity:
  - device_tracker.my_phone
history_source: gps_timeline
places_entity:
  - sensor.places_my_phone
```

- The card keeps pointing at the same entity IDs — GPS Timeline serves the data transparently.
- `history_source: gps_timeline` activates the backend; when the flag is not set (or the integration is not installed), the card behaves exactly as before.
- Companion `places_entity`/`activity_entity` queries are served from the archive too, so old days still show place names and activities.

You can point the card at either the original tracker entity or the exposed `device_tracker.*` entity — both work.

### About the card fork

The `history_source` option is **not** part of the official [timeline_card](https://github.com/konewka17/timeline_card). I opened a pull request upstream ([konewka17/timeline_card#108](https://github.com/konewka17/timeline_card/pull/108)) to add it as an opt-in, non-breaking option that unlocks unlimited history depth instead of the recorder's 10-day window, but the card owner declined it for now. So I maintain a fully tested fork of the card with the GPS Timeline support included:

- **Fork (use this one):** [iamyacin/timeline_card](https://github.com/iamyacin/timeline_card)
- **Upstream PR (declined):** [konewka17/timeline_card#108](https://github.com/konewka17/timeline_card/pull/108)

To use GPS Timeline with the card, install the fork via HACS (add `iamyacin/timeline_card` as a custom repository with the category **Lovelace**) and configure it as shown above. The fork behaves identically to the upstream card — everything is opt-in and nothing changes unless `history_source: gps_timeline` is set.

## Services

### `gps_timeline.backfill`

Imports existing history from the recorder database into the archive.

| Field | Description |
|---|---|
| `entity_id` | A tracker entity configured in GPS Timeline. Its companion sensors are imported as well. |
| `start_time` | Earliest point to import (default: 10 days before `end_time`). |
| `end_time` | Latest point to import (default: now). |

Run it once after installing to backfill the recorder's existing window — after 10 days that data would be gone forever.

```yaml
action: gps_timeline.backfill
data:
  entity_id: device_tracker.my_phone
```

### `gps_timeline.purge`

Permanently deletes the archived location history of an entity — its points and companion states are removed from the SQLite database and this **cannot be undone**.

| Field | Description |
|---|---|
| `entity_id` | The entity whose archived history should be deleted. |

- If the entity belongs to a live tracker entry, its history is wiped and the entry is reloaded with an empty timeline; archiving then continues from scratch.
- If the entity is no longer configured (orphaned archive data), only its archived rows are deleted.
- The service fails with an error if there is no archived data for the entity or if the store is not running (no tracker is loaded).

This is the manual counterpart of the confirmations in the UI: when you remove a tracker entry, a repair issue asks whether to also delete its archived data (kept by default), so the service is mainly useful for scripted cleanups in automations.

```yaml
action: gps_timeline.purge
data:
  entity_id: device_tracker.my_phone
```

## Data and storage

- Database location: `<config>/gps_timeline/gps_timeline.db` (WAL mode) — inside the configuration directory, so it is included in Home Assistant backups.
- Three tables: `trackers` (registered entities, with their config entry and optional subject), `points` (one row per accepted GPS fix, with a full attribute snapshot), `entity_states` (companion sensor states).
- Coordinates are rounded to 6 decimals (~11 cm) before storage; the same rounded values are served back.
- Nothing is ever deleted automatically. Storage use is roughly 50–150 MB per year per actively moving device.
- The integration never talks to the internet.

## Frequently asked questions

**Why is there no per-entity retention in the recorder?** Home Assistant's recorder only supports a global `purge_keep_days`. GPS Timeline sidesteps the recorder for location data instead of storing everything longer.

**What happens if Home Assistant is off?** GPS fixes reported while the integration is not running are missed for periods beyond the recorder window; run `gps_timeline.backfill` afterwards to recover anything the recorder still holds.

**Can I use the archive for automations?** Yes — the exposed `device_tracker.*` entity reflects the latest archived fix and can be used in automations, scripts and dashboards.

## Roadmap

- GPX/CSV export service
- Google Takeout (Maps Timeline) import
- Daily statistics sensors (distance travelled, time at home, trips)
- Database compaction service and diagnostics
- Optional distance/heartbeat recording mode
- Deliberate, ranged data deletion tooling

## License

MIT — see [LICENSE](LICENSE).
