# GPS Timeline

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="custom_components/gps_timeline/brand/dark_logo.png">
  <img src="custom_components/gps_timeline/brand/logo.png" alt="GPS Timeline" width="200">
</picture>

**GPS Timeline** is a Home Assistant integration that archives the location history of your trackers into a dedicated SQLite database that is **never purged**, and serves that history back to dashboards through a WebSocket API — unlocking unlimited history depth for the [Location Timeline Card](https://community.home-assistant.io/t/location-timeline-card-to-easily-show-location-history/989513).

By default, Home Assistant's recorder keeps entity history for only 10 days (`purge_keep_days`), and there is no per-entity retention option. GPS Timeline fixes that for location data: every GPS fix is stored in its own SQLite database with full attribute fidelity and stays there forever.

## Features

- **Permanent GPS history** — points are stored in a dedicated SQLite database (`<config>/gps_timeline/gps_timeline.db`) and never purged.
- **Any entity with coordinates** — track `device_tracker.*`, `person.*` or any entity exposing `latitude`/`longitude` attributes.
- **Maximum fidelity** — every accepted fix is stored with all its attributes (accuracy, battery, speed, altitude, course, and anything else the source reports) as a full snapshot.
- **Multiple users and devices** — add one configuration entry per tracked device; every entry can also archive its Places and activity sensors.
- **Companion archive** — Places integration labels and activity sensor states are archived alongside the GPS points, so old timelines keep their place names even after the recorder purges them.
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
3. Set the **maximum GPS accuracy** (default 100 m): fixes with worse accuracy are ignored. Set it to 0 to keep every fix.

Repeat for each additional person or device. Deleting an entry removes the listener and the exposed entity but **keeps the archived data**.

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

## Data and storage

- Database location: `<config>/gps_timeline/gps_timeline.db` (WAL mode) — inside the configuration directory, so it is included in Home Assistant backups.
- Three tables: `trackers` (registered entities), `points` (one row per accepted GPS fix, with a full attribute snapshot), `entity_states` (companion sensor states).
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
