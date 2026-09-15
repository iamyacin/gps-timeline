from pathlib import Path
import sqlite3

from homeassistant.components import repairs
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gps_timeline.const import (
    CONF_ACCURACY_THRESHOLD,
    CONF_ACTIVITY_ENTITY,
    CONF_ENTITY_ID,
    CONF_PLACES_ENTITY,
    DB_DIR_NAME,
    DB_FILE_NAME,
    DOMAIN,
)
from custom_components.gps_timeline.store import StoreError

TRACKER_ATTRS = {
    "latitude": 50.1234567,
    "longitude": 8.9876543,
    "gps_accuracy": 10,
    "battery_level": 80,
    "source_type": "gps",
}


async def setup_entry(hass, **overrides):
    data = {
        CONF_ENTITY_ID: "device_tracker.phone",
        CONF_PLACES_ENTITY: None,
        CONF_ACTIVITY_ENTITY: None,
        CONF_ACCURACY_THRESHOLD: 100,
        **overrides,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data, title="GPS Timeline — Phone")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def flush_store(hass):
    await hass.data[DOMAIN]["store"].async_flush()


def db_counts(hass):
    conn = sqlite3.connect(str(Path(hass.config.path(DB_DIR_NAME, DB_FILE_NAME))))
    counts = {
        "trackers": conn.execute("SELECT COUNT(*) FROM trackers").fetchone()[0],
        "points": conn.execute("SELECT COUNT(*) FROM points").fetchone()[0],
        "entity_states": conn.execute("SELECT COUNT(*) FROM entity_states").fetchone()[0],
    }
    conn.close()
    return counts


def stale_issue_id(entry_id):
    return f"stale_timeline_data_{entry_id}"


async def open_fix_flow(hass, entry):
    assert await async_setup_component(hass, repairs.DOMAIN, {})
    flow_manager = repairs.repairs_flow_manager(hass)
    result = await flow_manager.async_init(
        DOMAIN, data={"issue_id": stale_issue_id(entry.entry_id)}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "confirm"
    return flow_manager, result


async def test_fix_flow_delete_purges_data_and_resolves_issue(hass):
    entry = await setup_entry(hass)
    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    await flush_store(hass)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert db_counts(hass) == {"trackers": 1, "points": 1, "entity_states": 0}

    flow_manager, result = await open_fix_flow(hass, entry)
    result = await flow_manager.async_configure(result["flow_id"], {"delete_data": True})
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert db_counts(hass) == {"trackers": 0, "points": 0, "entity_states": 0}
    assert (DOMAIN, stale_issue_id(entry.entry_id)) not in ir.async_get(hass).issues


async def test_fix_flow_keep_leaves_data_intact(hass):
    entry = await setup_entry(hass)
    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    await flush_store(hass)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    flow_manager, result = await open_fix_flow(hass, entry)
    result = await flow_manager.async_configure(result["flow_id"], {"delete_data": False})
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert db_counts(hass) == {"trackers": 1, "points": 1, "entity_states": 0}
    assert (DOMAIN, stale_issue_id(entry.entry_id)) not in ir.async_get(hass).issues


async def test_fix_flow_delete_with_live_store_keeps_survivor_working(hass):
    entry1 = await setup_entry(hass)
    hass.states.async_set(
        "device_tracker.tablet",
        "not_home",
        {**TRACKER_ATTRS, "latitude": 51.0, "longitude": 9.0},
    )
    await setup_entry(hass, entity_id="device_tracker.tablet")
    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    hass.states.async_set(
        "device_tracker.tablet",
        "home",
        {**TRACKER_ATTRS, "latitude": 52.0, "longitude": 10.0},
    )
    await flush_store(hass)

    assert await hass.config_entries.async_remove(entry1.entry_id)
    await hass.async_block_till_done()

    flow_manager, result = await open_fix_flow(hass, entry1)
    result = await flow_manager.async_configure(result["flow_id"], {"delete_data": True})
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert db_counts(hass) == {"trackers": 1, "points": 1, "entity_states": 0}

    # The surviving entry keeps archiving after the purge.
    hass.states.async_set(
        "device_tracker.tablet",
        "not_home",
        {**TRACKER_ATTRS, "latitude": 53.0, "longitude": 11.0},
    )
    await flush_store(hass)
    store = hass.data[DOMAIN]["store"]
    now = dt_util.utcnow().timestamp()
    result = await store.async_query_states(["device_tracker.tablet"], now - 3600, now + 3600)
    assert len(result["device_tracker.tablet"]) == 2


async def test_fix_flow_delete_after_last_entry_removal_standalone(hass):
    entry = await setup_entry(hass)
    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    await flush_store(hass)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN not in hass.data or "store" not in hass.data.get(DOMAIN, {})

    flow_manager, result = await open_fix_flow(hass, entry)
    result = await flow_manager.async_configure(result["flow_id"], {"delete_data": True})
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert db_counts(hass) == {"trackers": 0, "points": 0, "entity_states": 0}


async def test_fix_flow_delete_failure_shows_error(hass, monkeypatch):
    entry1 = await setup_entry(hass)
    hass.states.async_set(
        "device_tracker.tablet",
        "not_home",
        {**TRACKER_ATTRS, "latitude": 51.0, "longitude": 9.0},
    )
    await setup_entry(hass, entity_id="device_tracker.tablet")
    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    hass.states.async_set(
        "device_tracker.tablet",
        "home",
        {**TRACKER_ATTRS, "latitude": 52.0, "longitude": 10.0},
    )
    await flush_store(hass)
    assert await hass.config_entries.async_remove(entry1.entry_id)
    await hass.async_block_till_done()

    store = hass.data[DOMAIN]["store"]
    original_purge = store.async_purge_tracker
    calls = {"count": 0}

    async def failing_purge(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise StoreError("database is locked")
        return await original_purge(*args, **kwargs)

    monkeypatch.setattr(store, "async_purge_tracker", failing_purge)

    flow_manager, result = await open_fix_flow(hass, entry1)
    result = await flow_manager.async_configure(result["flow_id"], {"delete_data": True})

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "purge_failed"}
    assert db_counts(hass) == {"trackers": 2, "points": 2, "entity_states": 0}

    # Retry succeeds once the failure is gone.
    result = await flow_manager.async_configure(result["flow_id"], {"delete_data": True})
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert db_counts(hass) == {"trackers": 1, "points": 1, "entity_states": 0}
    assert (DOMAIN, stale_issue_id(entry1.entry_id)) not in ir.async_get(hass).issues


async def test_fix_flow_delete_shared_companion_restamps_survivor(hass):
    entry1 = await setup_entry(
        hass, places_entity="sensor.places_phone", subject_kind="person", subject_name="Yacin"
    )
    hass.states.async_set("sensor.places_phone", "Starbucks", {"place_name": "Starbucks"})
    await flush_store(hass)

    hass.states.async_set(
        "device_tracker.tablet",
        "not_home",
        {**TRACKER_ATTRS, "latitude": 51.0, "longitude": 9.0},
    )
    await setup_entry(
        hass, entity_id="device_tracker.tablet", places_entity="sensor.places_phone"
    )
    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    await flush_store(hass)
    assert await hass.config_entries.async_remove(entry1.entry_id)
    await hass.async_block_till_done()

    flow_manager, result = await open_fix_flow(hass, entry1)
    result = await flow_manager.async_configure(result["flow_id"], {"delete_data": True})
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    store = hass.data[DOMAIN]["store"]
    now = dt_util.utcnow().timestamp()
    result = await store.async_query_states(["sensor.places_phone"], 0, now + 3600)
    items = result["sensor.places_phone"]
    assert len(items) == 1
    assert items[0]["s"] == "Starbucks"
