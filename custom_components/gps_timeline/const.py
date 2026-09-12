from __future__ import annotations

DOMAIN = "gps_timeline"

CONF_ENTITY_ID = "entity_id"
CONF_PLACES_ENTITY = "places_entity"
CONF_ACTIVITY_ENTITY = "activity_entity"
CONF_ACCURACY_THRESHOLD = "accuracy_threshold"

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
