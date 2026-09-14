import asyncio
import json
import sqlite3

from homeassistant.core import State
from homeassistant.util import dt as dt_util
import pytest

from custom_components.gps_timeline.store import (
    Store,
    StoreError,
    normalize_entity_state,
    normalize_point,
)


class FakeHass:
    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()

    def async_create_task(self, coro):
        return self._loop.create_task(coro)


def make_state(state="not_home", attrs=None, ts=1000.0, entity_id="device_tracker.phone"):
    return State(
        entity_id,
        state,
        attrs or {},
        last_updated=dt_util.utc_from_timestamp(ts),
    )


TRACKER_ATTRS = {
    "latitude": 50.1234567,
    "longitude": 8.9876543,
    "gps_accuracy": 10,
    "battery_level": 80,
    "speed": 3.5,
    "altitude": 120.0,
    "course": 90,
    "source_type": "gps",
}


def test_normalize_point_full():
    row = normalize_point(make_state(attrs=TRACKER_ATTRS))
    assert row is not None
    (
        ts,
        state,
        lat,
        lon,
        accuracy,
        battery,
        speed,
        altitude,
        heading,
        source_type,
        attributes,
    ) = row
    assert ts == 1000.0
    assert state == "not_home"
    assert lat == 50.123457
    assert lon == 8.987654
    assert accuracy == 10.0
    assert battery == 80.0
    assert speed == 3.5
    assert altitude == 120.0
    assert heading == 90.0
    assert source_type == "gps"
    stored = json.loads(attributes)
    assert stored["latitude"] == 50.123457
    assert stored["longitude"] == 8.987654
    assert stored["gps_accuracy"] == 10


def test_normalize_point_missing_coordinates():
    assert normalize_point(make_state(attrs={"battery_level": 50})) is None
    assert normalize_point(make_state(attrs={"latitude": "abc", "longitude": 1.0})) is None


def test_normalize_point_zero_coordinates():
    assert normalize_point(make_state(attrs={"latitude": 0, "longitude": 0})) is None


def test_normalize_point_accuracy_filter():
    attrs = {**TRACKER_ATTRS, "gps_accuracy": 250}
    assert normalize_point(make_state(attrs=attrs), accuracy_threshold=100) is None
    row = normalize_point(make_state(attrs=attrs), accuracy_threshold=300)
    assert row is not None
    row = normalize_point(make_state(attrs=attrs), accuracy_threshold=0)
    assert row is not None
    no_gps_accuracy = {k: v for k, v in TRACKER_ATTRS.items() if k != "gps_accuracy"}
    row = normalize_point(
        make_state(attrs={**no_gps_accuracy, "accuracy": 5}), accuracy_threshold=100
    )
    assert row is not None
    assert row[4] == 5.0


def test_normalize_entity_state():
    row = normalize_entity_state(
        make_state("Starbucks", {"place_name": "Starbucks"}, ts=2000.0)
    )
    ts, state, attributes = row
    assert ts == 2000.0
    assert state == "Starbucks"
    assert json.loads(attributes)["place_name"] == "Starbucks"


@pytest.fixture
async def store(tmp_path):
    store = Store(FakeHass(), str(tmp_path / "gps_timeline" / "gps_timeline.db"))
    await store.async_setup()
    yield store
    await store.async_close()


async def test_ensure_tracker_idempotent(store):
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
    assert await store.async_bind_tracker(None, "device_tracker.phone") == tracker_id
    assert await store.async_get_tracked_entity_ids() == ["device_tracker.phone"]


async def test_points_roundtrip(store):
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
    store.async_add_points(
        tracker_id,
        [
            normalize_point(make_state(attrs=TRACKER_ATTRS, ts=ts))
            for ts in (100.0, 200.0, 300.0)
        ],
    )
    await store.async_flush()

    result = await store.async_query_states(["device_tracker.phone"], 0, 1000)
    items = result["device_tracker.phone"]
    assert len(items) == 3
    assert [item["lu"] for item in items] == [100.0, 200.0, 300.0]
    assert items[0]["s"] == "not_home"
    assert items[0]["a"]["latitude"] == 50.123457


async def test_point_dedup_on_same_ts(store):
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
    store.async_add_point(
        tracker_id, normalize_point(make_state(attrs=TRACKER_ATTRS, ts=100.0))
    )
    store.async_add_point(
        tracker_id, normalize_point(make_state(attrs=TRACKER_ATTRS, ts=100.0))
    )
    await store.async_flush()
    result = await store.async_query_states(["device_tracker.phone"], 0, 1000)
    assert len(result["device_tracker.phone"]) == 1


async def test_query_no_attributes_and_minimal_response(store):
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
    store.async_add_points(
        tracker_id,
        [normalize_point(make_state(attrs=TRACKER_ATTRS, ts=ts)) for ts in (100.0, 200.0)],
    )
    await store.async_flush()

    result = await store.async_query_states(
        ["device_tracker.phone"], 0, 1000, no_attributes=True
    )
    assert result["device_tracker.phone"][0]["a"] == {}

    result = await store.async_query_states(
        ["device_tracker.phone"], 0, 1000, minimal_response=True
    )
    items = result["device_tracker.phone"]
    assert items[0]["a"]["latitude"] == 50.123457
    assert items[1]["a"] == {}


async def test_include_start_time_state(store):
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
    store.async_add_point(
        tracker_id, normalize_point(make_state(attrs=TRACKER_ATTRS, ts=50.0))
    )
    await store.async_flush()

    result = await store.async_query_states(["device_tracker.phone"], 100.0, 300.0)
    assert len(result["device_tracker.phone"]) == 1
    assert result["device_tracker.phone"][0]["lu"] == 50.0

    result = await store.async_query_states(
        ["device_tracker.phone"], 100.0, 300.0, include_start_time_state=False
    )
    assert "device_tracker.phone" not in result


async def test_significant_changes_only_filters_duplicates(store):
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
    moved = {**TRACKER_ATTRS, "latitude": 51.0, "longitude": 9.0}
    store.async_add_points(
        tracker_id,
        [
            normalize_point(make_state(attrs=TRACKER_ATTRS, ts=100.0)),
            normalize_point(make_state(attrs=TRACKER_ATTRS, ts=200.0)),
            normalize_point(make_state(attrs=moved, ts=300.0)),
            normalize_point(make_state(attrs=moved, ts=400.0)),
            normalize_point(make_state("home", attrs=moved, ts=500.0)),
        ],
    )
    await store.async_flush()

    result = await store.async_query_states(
        ["device_tracker.phone"], 0, 1000, significant_changes_only=True
    )
    assert [item["lu"] for item in result["device_tracker.phone"]] == [100.0, 300.0, 500.0]

    result = await store.async_query_states(["device_tracker.phone"], 0, 1000)
    assert len(result["device_tracker.phone"]) == 5


async def test_entity_states_roundtrip(store):
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
    store.async_add_entity_state(
        tracker_id,
        "sensor.places_phone",
        (100.0, "Starbucks", json.dumps({"place_name": "Starbucks"})),
    )
    store.async_add_entity_state(
        tracker_id, "sensor.places_phone", (200.0, "Home", json.dumps({}))
    )
    await store.async_flush()

    result = await store.async_query_states(["sensor.places_phone"], 0, 1000)
    items = result["sensor.places_phone"]
    assert len(items) == 2
    assert items[0]["s"] == "Starbucks"
    assert items[0]["a"]["place_name"] == "Starbucks"


async def test_get_last_point(store):
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
    store.async_add_point(
        tracker_id, normalize_point(make_state(attrs=TRACKER_ATTRS, ts=100.0))
    )
    store.async_add_point(
        tracker_id, normalize_point(make_state(attrs=TRACKER_ATTRS, ts=300.0))
    )
    await store.async_flush()

    point = await store.async_get_last_point("device_tracker.phone")
    assert point is not None
    assert point["ts"] == 300.0
    assert point["lat"] == 50.123457
    assert point["attributes"]["battery_level"] == 80
    assert await store.async_get_last_point("device_tracker.unknown") is None


async def test_write_backfill_dedup(store):
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
    rows = [normalize_point(make_state(attrs=TRACKER_ATTRS, ts=ts)) for ts in (100.0, 200.0)]
    inserted, total = await store.async_write_backfill([(tracker_id, row) for row in rows], [])
    assert inserted == 2
    assert total == 2
    inserted, _ = await store.async_write_backfill([(tracker_id, rows[0])], [])
    assert inserted == 0


async def test_close_is_idempotent(store):
    await store.async_close()
    await store.async_close()


async def test_close_flushes_pending_points(tmp_path):
    db_path = str(tmp_path / "gps_timeline" / "gps_timeline.db")
    store = Store(FakeHass(), db_path)
    await store.async_setup()
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
    store.async_add_point(
        tracker_id, normalize_point(make_state(attrs=TRACKER_ATTRS, ts=100.0))
    )
    await store.async_close()

    reopened = Store(FakeHass(), db_path)
    await reopened.async_setup()
    try:
        result = await reopened.async_query_states(["device_tracker.phone"], 0, 1000)
        items = result["device_tracker.phone"]
        assert len(items) == 1
        assert items[0]["lu"] == 100.0
    finally:
        await reopened.async_close()


async def test_close_flushes_pending_entity_states(tmp_path):
    db_path = str(tmp_path / "gps_timeline" / "gps_timeline.db")
    store = Store(FakeHass(), db_path)
    await store.async_setup()
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
    store.async_add_entity_state(
        tracker_id, "sensor.places_phone", (100.0, "Home", json.dumps({}))
    )
    await store.async_close()

    reopened = Store(FakeHass(), db_path)
    await reopened.async_setup()
    try:
        result = await reopened.async_query_states(["sensor.places_phone"], 0, 1000)
        items = result["sensor.places_phone"]
        assert len(items) == 1
        assert items[0]["s"] == "Home"
    finally:
        await reopened.async_close()


async def test_flush_failure_requeues_and_retries(tmp_path, monkeypatch):
    monkeypatch.setattr("custom_components.gps_timeline.store.FLUSH_INTERVAL", 0.01)
    db_path = str(tmp_path / "gps_timeline" / "gps_timeline.db")
    store = Store(FakeHass(), db_path)
    await store.async_setup()
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")

    calls = {"count": 0}
    original_write = store._write

    def flaky_write(points, states):
        calls["count"] += 1
        if calls["count"] == 1:
            raise sqlite3.OperationalError("database is locked")
        original_write(points, states)

    monkeypatch.setattr(store, "_write", flaky_write)

    store.async_add_point(
        tracker_id, normalize_point(make_state(attrs=TRACKER_ATTRS, ts=100.0))
    )
    await store.async_flush()
    assert calls["count"] == 1
    assert len(store._pending_points) == 1

    await asyncio.sleep(0.1)
    assert calls["count"] == 2
    assert not store._pending_points

    result = await store.async_query_states(["device_tracker.phone"], 0, 1000)
    assert [item["lu"] for item in result["device_tracker.phone"]] == [100.0]
    await store.async_close()


async def test_requeue_caps_pending_rows(tmp_path, monkeypatch):
    monkeypatch.setattr("custom_components.gps_timeline.store.MAX_PENDING_ROWS", 3)
    db_path = str(tmp_path / "gps_timeline" / "gps_timeline.db")
    store = Store(FakeHass(), db_path)
    await store.async_setup()
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")

    def broken_write(points, states):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_write", broken_write)

    store.async_add_points(
        tracker_id,
        [
            normalize_point(make_state(attrs=TRACKER_ATTRS, ts=ts))
            for ts in (100.0, 200.0, 300.0, 400.0, 500.0)
        ],
    )
    await store.async_flush()
    assert [row[1][0] for row in store._pending_points] == [300.0, 400.0, 500.0]

    await store.async_close()


async def test_close_with_failing_write_does_not_raise(tmp_path, monkeypatch):
    db_path = str(tmp_path / "gps_timeline" / "gps_timeline.db")
    store = Store(FakeHass(), db_path)
    await store.async_setup()
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")

    def broken_write(points, states):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_write", broken_write)
    store.async_add_point(
        tracker_id, normalize_point(make_state(attrs=TRACKER_ATTRS, ts=100.0))
    )
    await store.async_close()
    assert store._closed


async def test_corrupt_db_archived_and_recreated(tmp_path):
    db_path = tmp_path / "gps_timeline" / "gps_timeline.db"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"this is not a sqlite database")

    store = Store(FakeHass(), str(db_path))
    await store.async_setup()
    try:
        backup = store.corrupt_backup_path
        assert backup is not None
        assert backup.exists()
        assert backup.read_bytes() == b"this is not a sqlite database"
        assert list(db_path.parent.glob("*.corrupt-*")) == [backup]

        tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
        store.async_add_point(
            tracker_id, normalize_point(make_state(attrs=TRACKER_ATTRS, ts=100.0))
        )
        await store.async_flush()
        result = await store.async_query_states(["device_tracker.phone"], 0, 1000)
        assert len(result["device_tracker.phone"]) == 1
    finally:
        await store.async_close()

    assert db_path.exists()


async def test_healthy_db_not_archived(store, tmp_path):
    assert store.corrupt_backup_path is None
    assert not list(tmp_path.rglob("*.corrupt-*"))


async def test_downgrade_newer_schema_rejected(tmp_path):
    db_path = tmp_path / "gps_timeline" / "gps_timeline.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version=99")
    conn.commit()
    conn.close()

    store = Store(FakeHass(), str(db_path))
    with pytest.raises(StoreError, match="newer"):
        await store.async_setup()


async def test_missing_migration_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("custom_components.gps_timeline.store.SCHEMA_VERSION", 3)
    db_path = tmp_path / "gps_timeline" / "gps_timeline.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()

    store = Store(FakeHass(), str(db_path))
    with pytest.raises(StoreError, match="No migration"):
        await store.async_setup()


async def test_migration_runs_and_data_survives(tmp_path, monkeypatch):
    db_path = str(tmp_path / "gps_timeline" / "gps_timeline.db")
    store = Store(FakeHass(), db_path)
    await store.async_setup()
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
    store.async_add_point(
        tracker_id, normalize_point(make_state(attrs=TRACKER_ATTRS, ts=100.0))
    )
    await store.async_flush()
    await store.async_close()

    def fake_migration(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS migration_marker (done INTEGER)")
        conn.execute("INSERT INTO migration_marker (done) VALUES (1)")

    monkeypatch.setattr(
        "custom_components.gps_timeline.store._MIGRATIONS", {2: fake_migration}
    )
    monkeypatch.setattr("custom_components.gps_timeline.store.SCHEMA_VERSION", 3)

    reopened = Store(FakeHass(), db_path)
    await reopened.async_setup()
    try:
        result = await reopened.async_query_states(["device_tracker.phone"], 0, 1000)
        assert len(result["device_tracker.phone"]) == 1

        conn = sqlite3.connect(db_path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM migration_marker").fetchone()[0] == 1
        conn.close()
    finally:
        await reopened.async_close()


async def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    db_path = str(tmp_path / "gps_timeline" / "gps_timeline.db")
    store = Store(FakeHass(), db_path)
    await store.async_setup()
    await store.async_close()

    def broken_migration(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS migration_marker (done INTEGER)")
        conn.execute("INSERT INTO migration_marker (done) VALUES (1)")
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(
        "custom_components.gps_timeline.store._MIGRATIONS", {2: broken_migration}
    )
    monkeypatch.setattr("custom_components.gps_timeline.store.SCHEMA_VERSION", 3)

    reopened = Store(FakeHass(), db_path)
    with pytest.raises(StoreError, match="Migration from schema version 2 failed"):
        await reopened.async_setup()

    assert reopened.corrupt_backup_path is None
    assert not list(tmp_path.rglob("*.corrupt-*"))
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "migration_marker" not in tables
    conn.close()


V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS trackers (
    id INTEGER PRIMARY KEY,
    entity_id TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracker_id INTEGER NOT NULL REFERENCES trackers(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    state TEXT,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    accuracy REAL,
    battery REAL,
    speed REAL,
    altitude REAL,
    heading REAL,
    source_type TEXT,
    attributes TEXT NOT NULL,
    UNIQUE (tracker_id, ts)
);

CREATE TABLE IF NOT EXISTS entity_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracker_id INTEGER NOT NULL REFERENCES trackers(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL,
    ts REAL NOT NULL,
    state TEXT NOT NULL,
    attributes TEXT,
    UNIQUE (entity_id, ts)
);
"""


def _make_v1_db(db_path):
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version=1")
    conn.executescript(V1_SCHEMA)
    conn.execute(
        "INSERT INTO trackers (entity_id, created_at) VALUES ('device_tracker.phone', 100.0)"
    )
    conn.execute(
        "INSERT INTO points (tracker_id, ts, state, lat, lon, attributes)"
        " VALUES (1, 500.0, 'not_home', 50.0, 9.0, '{}')"
    )
    conn.execute(
        "INSERT INTO entity_states (tracker_id, entity_id, ts, state, attributes)"
        " VALUES (1, 'sensor.places_phone', 500.0, 'Home', '{}')"
    )
    conn.commit()
    conn.close()


async def test_migration_v1_to_v2_preserves_data(tmp_path):
    db_path = tmp_path / "gps_timeline" / "gps_timeline.db"
    _make_v1_db(db_path)

    store = Store(FakeHass(), str(db_path))
    await store.async_setup()
    try:
        result = await store.async_query_states(
            ["device_tracker.phone", "sensor.places_phone"], 0, 1000
        )
        assert result["device_tracker.phone"][0]["lu"] == 500.0
        assert result["sensor.places_phone"][0]["lu"] == 500.0

        conn = sqlite3.connect(str(db_path))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {row[1] for row in conn.execute("PRAGMA table_info(trackers)")}
        assert {"entry_id", "subject_kind", "subject_name"} <= columns
        assert conn.execute(
            "SELECT entry_id, subject_kind, subject_name FROM trackers"
        ).fetchone() == (None, None, None)
        conn.close()

        reopened = Store(FakeHass(), str(db_path))
        await reopened.async_setup()
        await reopened.async_close()
    finally:
        await store.async_close()


async def test_migration_tolerates_v1_db_with_drifted_columns(tmp_path):
    """A v1-stamped DB that already carries v2 columns (e.g. from a beta test
    or a restored backup) must migrate cleanly instead of crashing."""
    db_path = tmp_path / "gps_timeline" / "gps_timeline.db"
    _make_v1_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("ALTER TABLE trackers ADD COLUMN entry_id TEXT")
    conn.execute("ALTER TABLE trackers ADD COLUMN subject_kind TEXT")
    conn.execute("ALTER TABLE trackers ADD COLUMN subject_name TEXT")
    conn.commit()
    conn.close()

    store = Store(FakeHass(), str(db_path))
    await store.async_setup()
    try:
        result = await store.async_query_states(["device_tracker.phone"], 0, 1000)
        assert result["device_tracker.phone"][0]["lu"] == 500.0

        conn = sqlite3.connect(str(db_path))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {row[1] for row in conn.execute("PRAGMA table_info(trackers)")}
        assert {"entry_id", "subject_kind", "subject_name"} <= columns
        conn.close()

        tracker_id = await store.async_bind_tracker("entry-1", "device_tracker.phone")
        assert tracker_id == 1
    finally:
        await store.async_close()


async def test_migration_index_exists_after_v1_upgrade(tmp_path):
    db_path = tmp_path / "gps_timeline" / "gps_timeline.db"
    _make_v1_db(db_path)

    store = Store(FakeHass(), str(db_path))
    await store.async_setup()
    try:
        tracker_id = await store.async_bind_tracker("entry-1", "device_tracker.phone")
        assert tracker_id == 1
        with pytest.raises(StoreError, match="owned by another"):
            await store.async_bind_tracker("entry-2", "device_tracker.phone")
    finally:
        await store.async_close()


async def test_bind_tracker_legacy_row_stamped_by_entity_id(tmp_path):
    db_path = tmp_path / "gps_timeline" / "gps_timeline.db"
    _make_v1_db(db_path)

    store = Store(FakeHass(), str(db_path))
    await store.async_setup()
    try:
        tracker_id = await store.async_bind_tracker("entry-1", "device_tracker.phone")
        assert tracker_id == 1
        assert await store.async_bind_tracker("entry-1", "device_tracker.phone") == 1

        conn = sqlite3.connect(str(db_path))
        assert conn.execute("SELECT entry_id FROM trackers").fetchone()[0] == "entry-1"
        conn.close()
    finally:
        await store.async_close()


async def test_bind_tracker_updates_entity_id_in_place(store):
    tracker_id = await store.async_bind_tracker("entry-1", "device_tracker.phone")
    store.async_add_point(
        tracker_id, normalize_point(make_state(attrs=TRACKER_ATTRS, ts=100.0))
    )
    await store.async_flush()

    renamed = await store.async_bind_tracker("entry-1", "device_tracker.phone_new")
    assert renamed == tracker_id

    result = await store.async_query_states(["device_tracker.phone_new"], 0, 1000)
    assert len(result["device_tracker.phone_new"]) == 1
    assert await store.async_get_tracked_entity_ids() == ["device_tracker.phone_new"]


async def test_bind_tracker_insert_conflict_raises_store_error(store):
    await store.async_bind_tracker("entry-1", "device_tracker.phone")
    with pytest.raises(StoreError, match="owned by another"):
        await store.async_bind_tracker("entry-2", "device_tracker.phone")


async def test_bind_tracker_update_conflict_skips_and_warns(store, caplog):
    await store.async_bind_tracker("entry-1", "device_tracker.phone")
    other_id = await store.async_bind_tracker(None, "device_tracker.other")
    tracker_id = await store.async_bind_tracker("entry-1", "device_tracker.other")
    assert tracker_id != other_id
    assert "already owned by tracker" in caplog.text

    conn = sqlite3.connect(store._path)
    rows = {row[0]: row[1] for row in conn.execute("SELECT id, entity_id FROM trackers")}
    conn.close()
    assert rows[tracker_id] == "device_tracker.phone"
    assert rows[other_id] == "device_tracker.other"


async def test_rename_entity_moves_companion_rows(store):
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
    store.async_add_entity_state(
        tracker_id, "sensor.places_phone", (100.0, "Home", json.dumps({}))
    )
    store.async_add_entity_state(
        tracker_id, "sensor.places_phone", (200.0, "Work", json.dumps({}))
    )
    await store.async_flush()

    await store.async_rename_entity("sensor.places_phone", "sensor.places_renamed")

    result = await store.async_query_states(["sensor.places_renamed"], 0, 1000)
    assert len(result["sensor.places_renamed"]) == 2
    assert await store.async_query_states(["sensor.places_phone"], 0, 1000) == {}


async def test_rename_entity_update_or_ignore_keeps_target_rows(store, caplog):
    tracker_id = await store.async_bind_tracker(None, "device_tracker.phone")
    store.async_add_entity_state(
        tracker_id, "sensor.places_old", (100.0, "Home", json.dumps({}))
    )
    store.async_add_entity_state(
        tracker_id, "sensor.places_new", (100.0, "Existing", json.dumps({}))
    )
    await store.async_flush()

    await store.async_rename_entity("sensor.places_old", "sensor.places_new")

    result = await store.async_query_states(["sensor.places_new"], 0, 1000)
    assert len(result["sensor.places_new"]) == 1
    assert result["sensor.places_new"][0]["s"] == "Existing"


async def test_rename_source_entity_with_conflict_warns(store, caplog):
    old_id = await store.async_bind_tracker(None, "device_tracker.phone")
    await store.async_bind_tracker(None, "device_tracker.phone_new")
    store.async_add_point(old_id, normalize_point(make_state(attrs=TRACKER_ATTRS, ts=100.0)))
    await store.async_flush()

    await store.async_rename_entity("device_tracker.phone", "device_tracker.phone_new")
    assert "already owned by tracker" in caplog.text

    result = await store.async_query_states(["device_tracker.phone"], 0, 1000)
    assert len(result["device_tracker.phone"]) == 1
    assert await store.async_query_states(["device_tracker.phone_new"], 0, 1000) == {}
    assert await store.async_get_tracked_entity_ids() == [
        "device_tracker.phone",
        "device_tracker.phone_new",
    ]


async def test_set_subject_set_update_clear(store):
    await store.async_bind_tracker("entry-1", "device_tracker.phone")

    await store.async_set_subject("entry-1", "person", "Yacin")
    conn = sqlite3.connect(store._path)
    assert conn.execute("SELECT subject_kind, subject_name FROM trackers").fetchone() == (
        "person",
        "Yacin",
    )

    await store.async_set_subject("entry-1", "object", "Car")
    assert conn.execute("SELECT subject_kind, subject_name FROM trackers").fetchone() == (
        "object",
        "Car",
    )

    await store.async_set_subject("entry-1", None, None)
    assert conn.execute("SELECT subject_kind, subject_name FROM trackers").fetchone() == (
        None,
        None,
    )

    await store.async_set_subject("entry-1", "person", "")
    assert conn.execute("SELECT subject_kind, subject_name FROM trackers").fetchone() == (
        None,
        None,
    )
    conn.close()


async def test_set_subject_with_no_matching_row_is_noop(store):
    await store.async_set_subject("missing", "person", "Nobody")
    assert await store.async_get_tracked_entity_ids() == []
