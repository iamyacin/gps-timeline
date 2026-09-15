from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
    COORD_DECIMALS,
    DEFAULT_ACCURACY_THRESHOLD,
    FLUSH_BATCH_SIZE,
    FLUSH_INTERVAL,
    MAX_PENDING_ROWS,
    MAX_RETRY_DELAY,
)

SCHEMA_VERSION = 2

_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}

_LOGGER = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trackers (
    id INTEGER PRIMARY KEY,
    entity_id TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL,
    entry_id TEXT,
    subject_kind TEXT,
    subject_name TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_trackers_entry
    ON trackers(entry_id) WHERE entry_id IS NOT NULL;

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

CREATE INDEX IF NOT EXISTS idx_points_tracker_ts ON points (tracker_id, ts);

CREATE TABLE IF NOT EXISTS entity_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracker_id INTEGER NOT NULL REFERENCES trackers(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL,
    ts REAL NOT NULL,
    state TEXT NOT NULL,
    attributes TEXT,
    UNIQUE (entity_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_entity_states_entity_ts ON entity_states (entity_id, ts);
"""


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(trackers)")}
    for column in ("entry_id", "subject_kind", "subject_name"):
        if column not in columns:
            conn.execute(f"ALTER TABLE trackers ADD COLUMN {column} TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_trackers_entry"
        " ON trackers(entry_id) WHERE entry_id IS NOT NULL"
    )


_MIGRATIONS[1] = _migrate_v1_to_v2

_INSERT_POINT_SQL = (
    "INSERT OR IGNORE INTO points"
    " (tracker_id, ts, state, lat, lon, accuracy, battery, speed,"
    " altitude, heading, source_type, attributes)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_INSERT_ENTITY_STATE_SQL = (
    "INSERT OR IGNORE INTO entity_states"
    " (tracker_id, entity_id, ts, state, attributes)"
    " VALUES (?, ?, ?, ?, ?)"
)

_SELECT_POINT_SQL = (
    "SELECT ts, state, attributes FROM points"
    " WHERE tracker_id = ? AND ts >= ? AND ts <= ?"
    " ORDER BY ts ASC"
)

_SELECT_POINT_BEFORE_SQL = (
    "SELECT ts, state, attributes FROM points"
    " WHERE tracker_id = ? AND ts < ?"
    " ORDER BY ts DESC LIMIT 1"
)

_SELECT_ENTITY_STATE_SQL = (
    "SELECT ts, state, attributes FROM entity_states"
    " WHERE entity_id = ? AND ts >= ? AND ts <= ?"
    " ORDER BY ts ASC"
)

_SELECT_ENTITY_STATE_BEFORE_SQL = (
    "SELECT ts, state, attributes FROM entity_states"
    " WHERE entity_id = ? AND ts < ?"
    " ORDER BY ts DESC LIMIT 1"
)

_SELECT_LAST_POINT_SQL = (
    "SELECT p.ts, p.state, p.lat, p.lon, p.accuracy, p.battery,"
    " p.speed, p.altitude, p.heading, p.source_type, p.attributes"
    " FROM points p JOIN trackers t ON t.id = p.tracker_id"
    " WHERE t.entity_id = ?"
    " ORDER BY p.ts DESC LIMIT 1"
)


class StoreError(HomeAssistantError):
    """Raised on storage failures."""


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _is_lock_error(err: Exception) -> bool:
    message = str(err).lower()
    return isinstance(err, sqlite3.OperationalError) and (
        "locked" in message or "busy" in message
    )


def normalize_point(
    state: State, accuracy_threshold: float = DEFAULT_ACCURACY_THRESHOLD
) -> tuple | None:
    """Convert a tracker state into a point row, or None if it must not be stored."""
    attributes = state.attributes
    latitude = _as_number(attributes.get("latitude"))
    longitude = _as_number(attributes.get("longitude"))
    if latitude is None or longitude is None:
        return None
    if latitude == 0.0 and longitude == 0.0:
        return None

    accuracy = _as_number(attributes.get("gps_accuracy"))
    if accuracy is None:
        accuracy = _as_number(attributes.get("accuracy"))
    if accuracy_threshold > 0 and accuracy is not None and accuracy > accuracy_threshold:
        return None

    latitude = round(latitude, COORD_DECIMALS)
    longitude = round(longitude, COORD_DECIMALS)

    stored_attributes = dict(attributes)
    stored_attributes["latitude"] = latitude
    stored_attributes["longitude"] = longitude

    ts = getattr(state, "last_updated_ts", None)
    if ts is None:
        ts = state.last_updated.timestamp()

    battery = _as_number(attributes.get("battery_level"))
    if battery is None:
        battery = _as_number(attributes.get("battery"))
    heading = _as_number(attributes.get("course"))
    if heading is None:
        heading = _as_number(attributes.get("heading"))
    speed = _as_number(attributes.get("speed"))
    altitude = _as_number(attributes.get("altitude"))
    source_type = attributes.get("source_type")
    if not isinstance(source_type, str):
        source_type = None

    return (
        ts,
        state.state,
        latitude,
        longitude,
        accuracy,
        battery,
        speed,
        altitude,
        heading,
        source_type,
        _dumps(stored_attributes),
    )


def normalize_entity_state(state: State) -> tuple:
    """Convert a companion entity state (Places/activity sensors) into a row."""
    ts = getattr(state, "last_updated_ts", None)
    if ts is None:
        ts = state.last_updated.timestamp()
    return (ts, state.state, _dumps(state.attributes))


def _filter_significant_changes(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop consecutive rows whose state and attributes are unchanged."""
    significant: list[dict[str, Any]] = []
    for item in items:
        last = significant[-1] if significant else None
        if last is not None and item["s"] == last["s"] and item["a"] == last["a"]:
            continue
        significant.append(item)
    return significant


class Store:
    """SQLite storage for GPS timeline points.

    Rows are kept forever until explicitly purged (repair confirmation flow or
    the ``gps_timeline.purge`` service); nothing is ever deleted automatically.
    """

    def __init__(self, hass: HomeAssistant, db_path: str) -> None:
        self._hass = hass
        self._path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._conn_lock = threading.Lock()
        self._pending_points: list[tuple[int, tuple]] = []
        self._pending_states: list[tuple[int, tuple]] = []
        self._dropped_trackers: set[int] = set()
        self._flush_task: asyncio.Task | None = None
        self._retry_tasks: set[asyncio.Task] = set()
        self._flush_failure_count = 0
        self._corrupt_backup_path: Path | None = None
        self._closed = False

    @property
    def corrupt_backup_path(self) -> Path | None:
        """Path of the archived corrupt database, if one was recovered from."""
        return self._corrupt_backup_path

    async def async_setup(self) -> None:
        await asyncio.to_thread(self._setup)

    def _setup(self) -> None:
        try:
            self._open()
            return
        except sqlite3.DatabaseError as err:
            if _is_lock_error(err):
                raise
            _LOGGER.error(
                "GPS Timeline database %s is corrupt (%s); archiving it and starting fresh",
                self._path,
                err,
            )
        self._corrupt_backup_path = self._archive_corrupt_db()
        try:
            self._open()
        except sqlite3.DatabaseError as err:
            raise StoreError(f"Could not initialize database: {err}") from err

    def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), timeout=30, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._migrate(conn)
        except (sqlite3.DatabaseError, StoreError):
            conn.close()
            raise
        with self._conn_lock:
            self._conn = conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise StoreError(
                f"Database schema version {version} is newer than the supported"
                f" version {SCHEMA_VERSION}; upgrade the integration"
            )
        if version > 0:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for from_version in range(version, SCHEMA_VERSION):
                    migration = _MIGRATIONS.get(from_version)
                    if migration is None:
                        raise StoreError(f"No migration from schema version {from_version}")
                    _LOGGER.info("Migrating database from schema version %s", from_version)
                    migration(conn)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            except sqlite3.DatabaseError as err:
                conn.rollback()
                if _is_lock_error(err):
                    raise
                raise StoreError(
                    f"Migration from schema version {version} failed: {err}"
                ) from err
            except Exception:
                conn.rollback()
                raise
            conn.commit()
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()

    def _archive_corrupt_db(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        self._path.rename(backup)
        for suffix in ("-wal", "-shm"):
            sidecar = self._path.with_name(f"{self._path.name}{suffix}")
            if sidecar.exists():
                sidecar.rename(backup.with_name(f"{backup.name}{suffix}"))
        return backup

    @callback
    def async_add_point(self, tracker_id: int, row: tuple) -> None:
        self._pending_points.append((tracker_id, row))
        self._schedule_flush()

    @callback
    def async_add_points(self, tracker_id: int, rows: list[tuple]) -> None:
        self._pending_points.extend((tracker_id, row) for row in rows)
        self._schedule_flush()

    @callback
    def async_add_entity_state(self, tracker_id: int, entity_id: str, row: tuple) -> None:
        self._pending_states.append((tracker_id, entity_id, row))
        self._schedule_flush()

    @callback
    def _schedule_flush(self) -> None:
        if self._closed:
            return
        if len(self._pending_points) + len(self._pending_states) >= FLUSH_BATCH_SIZE:
            self._hass.async_create_task(self.async_flush())
            return
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = self._hass.async_create_task(self._async_delayed_flush())

    async def _async_delayed_flush(self, delay: float = FLUSH_INTERVAL) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        await self.async_flush()

    @callback
    def _schedule_retry(self) -> None:
        if self._closed:
            return
        delay = min(FLUSH_INTERVAL * 2 ** (self._flush_failure_count - 1), MAX_RETRY_DELAY)
        task = self._hass.async_create_task(self._async_delayed_flush(delay))
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    @callback
    def _drop_pending_rows(self, tracker_id: int) -> None:
        """Forget pending queued rows for a purged tracker.

        ``INSERT OR IGNORE`` does not suppress FK violations, so queued rows for
        a deleted tracker would fail on flush, requeue, and retry forever.
        Safe: the tracker's listener was unsubscribed before its entry was
        removed, so nothing new can arrive for it.
        """
        self._dropped_trackers.add(tracker_id)
        self._pending_points = [
            (tid, row) for tid, row in self._pending_points if tid != tracker_id
        ]
        self._pending_states = [
            (tid, entity_id, row)
            for tid, entity_id, row in self._pending_states
            if tid != tracker_id
        ]

    def _filter_dropped_rows(
        self,
        points: list[tuple[int, tuple]],
        states: list[tuple[int, tuple]],
    ) -> tuple[list[tuple[int, tuple]], list[tuple[int, tuple]]]:
        """Skip rows that reference a tracker purged mid-flight."""
        if self._dropped_trackers:
            points = [(tid, row) for tid, row in points if tid not in self._dropped_trackers]
            states = [
                (tid, entity_id, row)
                for tid, entity_id, row in states
                if tid not in self._dropped_trackers
            ]
        return points, states

    async def async_flush(self, *, final: bool = False) -> None:
        if self._closed and not final:
            return
        points = self._pending_points
        states = self._pending_states
        self._pending_points = []
        self._pending_states = []
        if not points and not states:
            return
        try:
            await asyncio.to_thread(self._write, points, states)
        except Exception:
            self._flush_failure_count += 1
            _LOGGER.exception(
                "Failed to write %s points and %s entity states; re-queueing for retry",
                len(points),
                len(states),
            )
            self._requeue(points, states)
            if not final:
                self._schedule_retry()
        else:
            self._flush_failure_count = 0

    def _requeue(
        self, points: list[tuple[int, tuple]], states: list[tuple[int, tuple]]
    ) -> None:
        points, states = self._filter_dropped_rows(points, states)
        self._pending_points[:0] = points
        self._pending_states[:0] = states
        for name, pending in (
            ("points", self._pending_points),
            ("entity states", self._pending_states),
        ):
            overflow = len(pending) - MAX_PENDING_ROWS
            if overflow > 0:
                del pending[:overflow]
                _LOGGER.warning(
                    "Pending %s queue overflow; dropped %s oldest rows", name, overflow
                )

    def _write(self, points: list[tuple[int, tuple]], states: list[tuple[int, tuple]]) -> None:
        points, states = self._filter_dropped_rows(points, states)
        if not points and not states:
            return
        with self._conn_lock:
            if self._conn is None:
                raise StoreError("Store is not set up")
            self._conn.executemany(
                _INSERT_POINT_SQL,
                [(tracker_id, *row) for tracker_id, row in points],
            )
            self._conn.executemany(
                _INSERT_ENTITY_STATE_SQL,
                [(tracker_id, entity_id, *row) for tracker_id, entity_id, row in states],
            )
            self._conn.commit()
            _LOGGER.debug("Flushed %s points and %s entity states", len(points), len(states))

    async def async_write_backfill(
        self, points: list[tuple[int, tuple]], states: list[tuple[int, tuple]]
    ) -> tuple[int, int]:
        """Write rows directly (bypassing the queue) and return the number of inserted rows."""
        if self._closed:
            raise StoreError("Store is closed")
        if not points and not states:
            return (0, 0)
        return await asyncio.to_thread(self._write_backfill, points, states)

    def _write_backfill(
        self, points: list[tuple[int, tuple]], states: list[tuple[int, tuple]]
    ) -> tuple[int, int]:
        points, states = self._filter_dropped_rows(points, states)
        if not points and not states:
            return (0, 0)
        with self._conn_lock:
            if self._conn is None:
                raise StoreError("Store is not set up")
            before = self._conn.total_changes
            self._conn.executemany(
                _INSERT_POINT_SQL,
                [(tracker_id, *row) for tracker_id, row in points],
            )
            self._conn.executemany(
                _INSERT_ENTITY_STATE_SQL,
                [(tracker_id, entity_id, *row) for tracker_id, entity_id, row in states],
            )
            self._conn.commit()
            inserted = self._conn.total_changes - before
        return (inserted, len(points) + len(states))

    async def async_purge_tracker(
        self,
        entry_id: str | None = None,
        *,
        tracker_id: int | None = None,
        live_entity_claims: dict[str, str] | None = None,
    ) -> dict[str, int]:
        """Delete a tracker row and all related rows in one transaction.

        Exactly one of ``entry_id`` / ``tracker_id`` must be provided. Both
        ``points`` and ``entity_states`` rows cascade-delete via foreign keys;
        ``live_entity_claims`` (entity id -> owning entry id) protects companion
        history still tracked by a live entry by re-stamping those rows to the
        surviving tracker instead of letting them cascade-delete.

        Returns affected row counts for logging.
        """
        if (entry_id is None) == (tracker_id is None):
            raise ValueError("Provide exactly one of entry_id or tracker_id")
        if tracker_id is not None:
            self._drop_pending_rows(tracker_id)
        try:
            target_id, counts = await asyncio.to_thread(
                self._purge_tracker, entry_id, tracker_id, live_entity_claims
            )
        except sqlite3.DatabaseError as err:
            target = entry_id or tracker_id
            raise StoreError(f"Could not purge tracker {target}: {err}") from err
        if entry_id is not None and target_id is not None:
            self._drop_pending_rows(target_id)
        return counts

    def _purge_tracker(
        self,
        entry_id: str | None,
        tracker_id: int | None,
        live_entity_claims: dict[str, str] | None,
    ) -> tuple[int | None, dict[str, int]]:
        with self._conn_lock:
            if self._conn is None:
                raise StoreError("Store is not set up")
            conn = self._conn
            if entry_id is not None:
                row = conn.execute(
                    "SELECT id FROM trackers WHERE entry_id = ?", (entry_id,)
                ).fetchone()
                bound_entry_id = entry_id
            else:
                assert tracker_id is not None
                row = conn.execute(
                    "SELECT entry_id FROM trackers WHERE id = ?", (tracker_id,)
                ).fetchone()
                bound_entry_id = row[0] if row else None
            if row is None:
                return (None, {"trackers": 0, "points": 0, "entity_states": 0})
            target_id = int(row[0] if entry_id is not None else tracker_id)
            if (
                entry_id is None
                and bound_entry_id is not None
                and (bound_entry_id in set((live_entity_claims or {}).values()))
            ):
                raise StoreError(
                    "Tracker data is claimed by a live GPS Timeline entry"
                    " and cannot be purged directly"
                )
            if live_entity_claims:
                for entity_id, owner_entry_id in live_entity_claims.items():
                    survivor = conn.execute(
                        "SELECT id FROM trackers WHERE entry_id = ?", (owner_entry_id,)
                    ).fetchone()
                    if survivor is None:
                        continue
                    conn.execute(
                        "UPDATE entity_states SET tracker_id = ?"
                        " WHERE tracker_id = ? AND entity_id = ?",
                        (int(survivor[0]), target_id, entity_id),
                    )
            points = conn.execute(
                "SELECT COUNT(*) FROM points WHERE tracker_id = ?", (target_id,)
            ).fetchone()[0]
            states = conn.execute(
                "SELECT COUNT(*) FROM entity_states WHERE tracker_id = ?", (target_id,)
            ).fetchone()[0]
            conn.execute("DELETE FROM trackers WHERE id = ?", (target_id,))
            conn.commit()
        _LOGGER.info(
            "Purged tracker %s (entry %s): %s points, %s entity states",
            target_id,
            bound_entry_id,
            points,
            states,
        )
        return (
            target_id,
            {
                "trackers": 1,
                "points": int(points),
                "entity_states": int(states),
            },
        )

    async def async_list_orphans(
        self, live_entry_ids: list[str], claimed_entity_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Return tracker rows not referenced by any live config entry."""
        return await asyncio.to_thread(self._list_orphans, live_entry_ids, claimed_entity_ids)

    def _list_orphans(
        self, live_entry_ids: list[str], claimed_entity_ids: list[str]
    ) -> list[dict[str, Any]]:
        with self._conn_lock:
            if self._conn is None:
                raise StoreError("Store is not set up")
            conditions: list[str] = []
            params: list[Any] = []
            known_entries = [entry_id for entry_id in live_entry_ids if entry_id]
            if known_entries:
                conditions.append(
                    "(entry_id IS NOT NULL AND entry_id NOT IN"
                    f" ({','.join('?' * len(known_entries))}))"
                )
                params.extend(known_entries)
            else:
                conditions.append("entry_id IS NOT NULL")
            known_entities = [
                entity_id.lower() for entity_id in claimed_entity_ids if entity_id
            ]
            if known_entities:
                conditions.append(
                    "(entry_id IS NULL AND entity_id NOT IN"
                    f" ({','.join('?' * len(known_entities))}))"
                )
                params.extend(known_entities)
            else:
                conditions.append("entry_id IS NULL")
            rows = self._conn.execute(
                "SELECT id, entity_id, subject_kind, subject_name, created_at, entry_id,"
                " (SELECT COUNT(*) FROM points p WHERE p.tracker_id = t.id),"
                " (SELECT MAX(ts) FROM points p WHERE p.tracker_id = t.id)"
                f" FROM trackers t WHERE {' OR '.join(conditions)}"
                " ORDER BY entity_id",
                params,
            ).fetchall()
        return [
            {
                "tracker_id": int(row[0]),
                "entity_id": row[1],
                "subject_kind": row[2],
                "subject_name": row[3],
                "created_at": row[4],
                "entry_id": row[5],
                "point_count": int(row[6]),
                "last_ts": float(row[7]) if row[7] is not None else None,
            }
            for row in rows
        ]

    async def async_get_tracker(self, tracker_id: int) -> dict[str, Any] | None:
        """Return id/entry_id/entity_id for a tracker row, or None."""
        return await asyncio.to_thread(self._get_tracker, int(tracker_id))

    def _get_tracker(self, tracker_id: int) -> dict[str, Any] | None:
        with self._conn_lock:
            if self._conn is None:
                raise StoreError("Store is not set up")
            row = self._conn.execute(
                "SELECT id, entry_id, entity_id FROM trackers WHERE id = ?", (tracker_id,)
            ).fetchone()
        if row is None:
            return None
        return {"tracker_id": int(row[0]), "entry_id": row[1], "entity_id": row[2]}

    async def async_get_tracker_id(self, entry_id: str) -> int | None:
        """Return the tracker row id bound to a config entry, or None."""
        return await asyncio.to_thread(self._get_tracker_id, entry_id)

    def _get_tracker_id(self, entry_id: str) -> int | None:
        with self._conn_lock:
            if self._conn is None:
                raise StoreError("Store is not set up")
            row = self._conn.execute(
                "SELECT id FROM trackers WHERE entry_id = ?", (entry_id,)
            ).fetchone()
        return int(row[0]) if row else None

    async def async_get_tracker_by_entity(self, entity_id: str) -> dict[str, Any] | None:
        """Return the tracker row owning an entity id, or None."""
        return await asyncio.to_thread(self._get_tracker_by_entity, entity_id.lower())

    def _get_tracker_by_entity(self, entity_id: str) -> dict[str, Any] | None:
        with self._conn_lock:
            if self._conn is None:
                raise StoreError("Store is not set up")
            row = self._conn.execute(
                "SELECT id, entry_id FROM trackers WHERE entity_id = ?", (entity_id,)
            ).fetchone()
        if row is None:
            return None
        return {"tracker_id": int(row[0]), "entry_id": row[1], "entity_id": entity_id}

    async def async_adopt_orphan(
        self,
        tracker_id: int,
        entry_id: str,
        entity_id: str,
        subject: tuple[str, str] | None = None,
        *,
        live_entry_ids: list[str] | None = None,
    ) -> int:
        """Attach an orphaned tracker row to a live entry.

        Updates ``entry_id``, ``entity_id`` and the subject columns in one
        transaction; afterwards the row is bound and stops being an orphan.
        The target must still be an orphan — adopting an already-bound row is
        rejected.
        """
        return await asyncio.to_thread(
            self._adopt_orphan,
            int(tracker_id),
            entry_id,
            entity_id.lower(),
            subject,
            live_entry_ids,
            True,
        )

    async def async_bind_tracker(
        self,
        entry_id: str | None,
        entity_id: str,
        *,
        adopt_tracker_id: int | None = None,
        live_entry_ids: list[str] | None = None,
    ) -> int:
        return await asyncio.to_thread(
            self._bind_tracker,
            entry_id,
            entity_id.lower(),
            adopt_tracker_id,
            live_entry_ids,
        )

    def _bind_tracker(
        self,
        entry_id: str | None,
        entity_id: str,
        adopt_tracker_id: int | None = None,
        live_entry_ids: list[str] | None = None,
    ) -> int:
        with self._conn_lock:
            if self._conn is None:
                raise StoreError("Store is not set up")
            conn = self._conn
            if adopt_tracker_id is not None:
                if entry_id is None:
                    raise StoreError("Adopting a tracker requires an entry id")
                self._adopt_orphan_locked(
                    conn,
                    adopt_tracker_id,
                    entry_id,
                    entity_id,
                    None,
                    live_entry_ids,
                    False,
                )
                return adopt_tracker_id
            if entry_id is not None:
                row = conn.execute(
                    "SELECT id, entity_id FROM trackers WHERE entry_id = ?", (entry_id,)
                ).fetchone()
                if row:
                    tracker_id, current_entity_id = int(row[0]), row[1]
                    if current_entity_id != entity_id:
                        owner = conn.execute(
                            "SELECT id FROM trackers WHERE entity_id = ?", (entity_id,)
                        ).fetchone()
                        if owner is not None:
                            _LOGGER.warning(
                                "Cannot move tracker %s to %s: entity id already"
                                " owned by tracker %s; new points stay invisible"
                                " until the conflict is resolved",
                                tracker_id,
                                entity_id,
                                int(owner[0]),
                            )
                        else:
                            conn.execute(
                                "UPDATE trackers SET entity_id = ? WHERE id = ?",
                                (entity_id, tracker_id),
                            )
                            conn.commit()
                    return tracker_id
                row = conn.execute(
                    "SELECT id FROM trackers WHERE entity_id = ? AND entry_id IS NULL",
                    (entity_id,),
                ).fetchone()
                if row:
                    tracker_id = int(row[0])
                    conn.execute(
                        "UPDATE trackers SET entry_id = ? WHERE id = ?",
                        (entry_id, tracker_id),
                    )
                    conn.commit()
                    return tracker_id
            else:
                row = conn.execute(
                    "SELECT id FROM trackers WHERE entity_id = ?", (entity_id,)
                ).fetchone()
                if row:
                    return int(row[0])
            try:
                cursor = conn.execute(
                    "INSERT INTO trackers (entity_id, created_at, entry_id) VALUES (?, ?, ?)",
                    (entity_id, time.time(), entry_id),
                )
                conn.commit()
            except sqlite3.IntegrityError as err:
                _LOGGER.warning(
                    "Could not create tracker for %s (entry %s): %s",
                    entity_id,
                    entry_id,
                    err,
                )
                raise StoreError(
                    f"Entity {entity_id} is already owned by another GPS Timeline tracker"
                ) from err
            self._dropped_trackers.discard(int(cursor.lastrowid))
            return int(cursor.lastrowid)

    def _adopt_orphan(
        self,
        tracker_id: int,
        entry_id: str,
        entity_id: str,
        subject: tuple[str, str] | None,
        live_entry_ids: list[str] | None,
        update_subject: bool,
    ) -> int:
        with self._conn_lock:
            if self._conn is None:
                raise StoreError("Store is not set up")
            self._adopt_orphan_locked(
                self._conn,
                tracker_id,
                entry_id,
                entity_id,
                subject,
                live_entry_ids,
                update_subject,
            )
        return tracker_id

    def _adopt_orphan_locked(
        self,
        conn: sqlite3.Connection,
        tracker_id: int,
        entry_id: str,
        entity_id: str,
        subject: tuple[str, str] | None,
        live_entry_ids: list[str] | None,
        update_subject: bool,
    ) -> None:
        row = conn.execute(
            "SELECT entity_id, entry_id, subject_kind, subject_name FROM trackers"
            " WHERE id = ?",
            (tracker_id,),
        ).fetchone()
        if row is None:
            raise StoreError(
                "Archived tracker no longer exists; the data may have been purged meanwhile"
            )
        current_entity_id, bound_entry_id, current_kind, current_name = row
        if (
            bound_entry_id is not None
            and bound_entry_id != entry_id
            and live_entry_ids is not None
            and bound_entry_id in live_entry_ids
        ):
            raise StoreError(
                f"Tracker {tracker_id} is already claimed by another GPS Timeline entry"
            )
        if (
            conn.execute(
                "SELECT id FROM trackers WHERE entity_id = ? AND id != ?",
                (entity_id, tracker_id),
            ).fetchone()
            is not None
        ):
            raise StoreError(
                f"Entity {entity_id} is already owned by another GPS Timeline tracker"
            )
        if update_subject:
            kind, name = subject if subject else (None, None)
            if not kind or not name:
                kind = None
                name = None
        else:
            kind, name = current_kind, current_name
        conn.execute(
            "UPDATE trackers SET entry_id = ?, entity_id = ?, subject_kind = ?,"
            " subject_name = ? WHERE id = ?",
            (entry_id, entity_id, kind, name, tracker_id),
        )
        conn.commit()
        _LOGGER.info(
            "Adopted archived tracker %s (%s) into entry %s as %s",
            tracker_id,
            current_entity_id,
            entry_id,
            entity_id,
        )

    async def async_set_subject(
        self, entry_id: str, kind: str | None, name: str | None
    ) -> None:
        await asyncio.to_thread(self._set_subject, entry_id, kind, name)

    def _set_subject(self, entry_id: str, kind: str | None, name: str | None) -> None:
        with self._conn_lock:
            if self._conn is None:
                raise StoreError("Store is not set up")
            if not kind or not name:
                kind = None
                name = None
            self._conn.execute(
                "UPDATE trackers SET subject_kind = ?, subject_name = ? WHERE entry_id = ?",
                (kind, name, entry_id),
            )
            self._conn.commit()

    async def async_rename_entity(self, old_entity_id: str, new_entity_id: str) -> None:
        await asyncio.to_thread(
            self._rename_entity, old_entity_id.lower(), new_entity_id.lower()
        )

    def _rename_entity(self, old_entity_id: str, new_entity_id: str) -> None:
        with self._conn_lock:
            if self._conn is None:
                raise StoreError("Store is not set up")
            conn = self._conn
            row = conn.execute(
                "SELECT id FROM trackers WHERE entity_id = ?", (old_entity_id,)
            ).fetchone()
            if row is not None:
                owner = conn.execute(
                    "SELECT id FROM trackers WHERE entity_id = ?", (new_entity_id,)
                ).fetchone()
                if owner is not None:
                    _LOGGER.warning(
                        "Cannot rename tracker entity %s to %s: new entity id is"
                        " already owned by tracker %s; leaving history under %s",
                        old_entity_id,
                        new_entity_id,
                        int(owner[0]),
                        old_entity_id,
                    )
                    return
                conn.execute(
                    "UPDATE trackers SET entity_id = ? WHERE id = ?",
                    (new_entity_id, int(row[0])),
                )
                conn.commit()
                return
            before = conn.total_changes
            conn.execute(
                "UPDATE OR IGNORE entity_states SET entity_id = ? WHERE entity_id = ?",
                (new_entity_id, old_entity_id),
            )
            conn.commit()
            moved = conn.total_changes - before
            if moved:
                _LOGGER.info(
                    "Moved %s archived states from %s to %s",
                    moved,
                    old_entity_id,
                    new_entity_id,
                )

    async def async_query_states(
        self,
        entity_ids: list[str],
        start_ts: float,
        end_ts: float,
        *,
        no_attributes: bool = False,
        minimal_response: bool = False,
        include_start_time_state: bool = True,
        significant_changes_only: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        return await asyncio.to_thread(
            self._query_states,
            entity_ids,
            start_ts,
            end_ts,
            no_attributes,
            minimal_response,
            include_start_time_state,
            significant_changes_only,
        )

    def _query_states(
        self,
        entity_ids: list[str],
        start_ts: float,
        end_ts: float,
        no_attributes: bool,
        minimal_response: bool,
        include_start_time_state: bool,
        significant_changes_only: bool,
    ) -> dict[str, list[dict[str, Any]]]:
        with self._conn_lock:
            if self._conn is None:
                raise StoreError("Store is not set up")
            tracker_map = {
                entity_id: tracker_id
                for tracker_id, entity_id in self._conn.execute(
                    "SELECT id, entity_id FROM trackers"
                )
            }
            result: dict[str, list[dict[str, Any]]] = {}
            seen: set[str] = set()
            for entity_id in entity_ids:
                entity_id = entity_id.lower()
                if entity_id in seen:
                    continue
                seen.add(entity_id)
                items = self._query_points(
                    tracker_map,
                    entity_id,
                    start_ts,
                    end_ts,
                    no_attributes,
                    include_start_time_state,
                )
                items.extend(
                    self._query_entity_states(
                        entity_id, start_ts, end_ts, no_attributes, include_start_time_state
                    )
                )
                items.sort(key=lambda item: item["lu"])
                if significant_changes_only:
                    items = _filter_significant_changes(items)
                if minimal_response:
                    for item in items[1:]:
                        item["a"] = {}
                if items:
                    result[entity_id] = items
            return result

    def _query_points(
        self,
        tracker_map: dict[str, int],
        entity_id: str,
        start_ts: float,
        end_ts: float,
        no_attributes: bool,
        include_start_time_state: bool,
    ) -> list[dict[str, Any]]:
        tracker_id = tracker_map.get(entity_id)
        if tracker_id is None:
            return []
        assert self._conn is not None
        items: list[dict[str, Any]] = []
        if include_start_time_state:
            cursor = self._conn.execute(_SELECT_POINT_BEFORE_SQL, (tracker_id, start_ts))
            row = cursor.fetchone()
            if row is not None:
                items.append(
                    {
                        "s": row[1],
                        "a": {} if no_attributes else json.loads(row[2]),
                        "lu": row[0],
                    }
                )
        cursor = self._conn.execute(_SELECT_POINT_SQL, (tracker_id, start_ts, end_ts))
        for row in cursor:
            items.append(
                {
                    "s": row[1],
                    "a": {} if no_attributes else json.loads(row[2]),
                    "lu": row[0],
                }
            )
        return items

    def _query_entity_states(
        self,
        entity_id: str,
        start_ts: float,
        end_ts: float,
        no_attributes: bool,
        include_start_time_state: bool,
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        items: list[dict[str, Any]] = []
        if include_start_time_state:
            cursor = self._conn.execute(_SELECT_ENTITY_STATE_BEFORE_SQL, (entity_id, start_ts))
            row = cursor.fetchone()
            if row is not None:
                items.append(
                    {
                        "s": row[1],
                        "a": {} if no_attributes or row[2] is None else json.loads(row[2]),
                        "lu": row[0],
                    }
                )
        cursor = self._conn.execute(_SELECT_ENTITY_STATE_SQL, (entity_id, start_ts, end_ts))
        for row in cursor:
            items.append(
                {
                    "s": row[1],
                    "a": {} if no_attributes or row[2] is None else json.loads(row[2]),
                    "lu": row[0],
                }
            )
        return items

    async def async_get_last_point(self, entity_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_last_point, entity_id.lower())

    def _get_last_point(self, entity_id: str) -> dict[str, Any] | None:
        with self._conn_lock:
            if self._conn is None:
                raise StoreError("Store is not set up")
            cursor = self._conn.execute(_SELECT_LAST_POINT_SQL, (entity_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "ts": row[0],
                "state": row[1],
                "lat": row[2],
                "lon": row[3],
                "accuracy": row[4],
                "battery": row[5],
                "speed": row[6],
                "altitude": row[7],
                "heading": row[8],
                "source_type": row[9],
                "attributes": json.loads(row[10]),
            }

    async def async_get_tracked_entity_ids(self) -> list[str]:
        return await asyncio.to_thread(self._get_tracked_entity_ids)

    def _get_tracked_entity_ids(self) -> list[str]:
        with self._conn_lock:
            if self._conn is None:
                raise StoreError("Store is not set up")
            return [
                row[0]
                for row in self._conn.execute(
                    "SELECT entity_id FROM trackers ORDER BY entity_id"
                )
            ]

    async def async_close(self) -> None:
        if self._closed:
            return
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
        if self._retry_tasks:
            for task in self._retry_tasks:
                task.cancel()
            await asyncio.gather(*self._retry_tasks, return_exceptions=True)
        self._closed = True
        await self.async_flush(final=True)
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        with self._conn_lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


async def _run_standalone(hass: HomeAssistant, db_path: str, action) -> Any:
    """Run an action against a short-lived store connection.

    For use when no live store exists (fresh start, first entry ever, or after
    the last entry was removed): opens the database, runs the action, and
    closes again, mirroring the normal setup/close path (pragmas, migrations,
    corrupt-DB recovery) without listeners or flush logic.
    """
    store = Store(hass, db_path)
    await store.async_setup()
    try:
        return await action(store)
    finally:
        await store.async_close()


async def async_purge_tracker_standalone(
    hass: HomeAssistant,
    db_path: str,
    entry_id: str | None = None,
    *,
    tracker_id: int | None = None,
    live_entity_claims: dict[str, str] | None = None,
) -> dict[str, int]:
    """Purge a tracker through a short-lived connection (store not running)."""
    return await _run_standalone(
        hass,
        db_path,
        lambda store: store.async_purge_tracker(
            entry_id, tracker_id=tracker_id, live_entity_claims=live_entity_claims
        ),
    )


async def async_list_orphans_standalone(
    hass: HomeAssistant,
    db_path: str,
    live_entry_ids: list[str],
    claimed_entity_ids: list[str],
) -> list[dict[str, Any]]:
    """List orphans through a short-lived connection (store not running)."""
    return await _run_standalone(
        hass,
        db_path,
        lambda store: store.async_list_orphans(live_entry_ids, claimed_entity_ids),
    )
