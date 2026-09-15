from __future__ import annotations

DOMAIN = "gps_timeline"

CONF_ENTITY_ID = "entity_id"
CONF_PLACES_ENTITY = "places_entity"
CONF_ACTIVITY_ENTITY = "activity_entity"
CONF_ACCURACY_THRESHOLD = "accuracy_threshold"
CONF_SUBJECT_KIND = "subject_kind"
CONF_SUBJECT_NAME = "subject_name"
CONF_ATTACH_TRACKER_ID = "attach_tracker_id"

ATTACH_START_FRESH = "start_fresh"

DEFAULT_ACCURACY_THRESHOLD = 100
COORD_DECIMALS = 6

DB_DIR_NAME = "gps_timeline"
DB_FILE_NAME = "gps_timeline.db"

FLUSH_INTERVAL = 2.0
FLUSH_BATCH_SIZE = 200
MAX_PENDING_ROWS = 10_000
MAX_RETRY_DELAY = 300.0

WS_HISTORY_DURING_PERIOD = f"{DOMAIN}/history_during_period"

SERVICE_BACKFILL = "backfill"
SERVICE_PURGE = "purge"
