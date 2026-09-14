from pathlib import Path
import sqlite3

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
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
from custom_components.gps_timeline.helpers import tracked_entity_ids

TRACKER_ATTRS = {
    "latitude": 50.1234567,
    "longitude": 8.9876543,
    "gps_accuracy": 10,
    "battery_level": 80,
    "speed": 3.5,
    "source_type": "gps",
    "friendly_name": "Phone",
}


def register_places_pair(hass, entry, base_entity_id):
    """Register a Places v3 main sensor and its `_place_name` child on one device."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("places", base_entity_id)},
    )
    registry = er.async_get(hass)
    object_id = base_entity_id.split(".", 1)[1]
    for entity_object_id in (object_id, f"{object_id}_place_name"):
        registry.async_get_or_create(
            "sensor",
            "places",
            entity_object_id,
            suggested_object_id=entity_object_id,
            device_id=device.id,
        )


async def setup_entry(hass, prepare=None, **overrides):
    data = {
        CONF_ENTITY_ID: "device_tracker.phone",
        CONF_PLACES_ENTITY: None,
        CONF_ACTIVITY_ENTITY: None,
        CONF_ACCURACY_THRESHOLD: 100,
        **overrides,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data, title="GPS Timeline — Phone")
    entry.add_to_hass(hass)
    if prepare is not None:
        prepare(entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def flush_store(hass):
    await hass.data[DOMAIN]["store"].async_flush()


async def test_point_recorded_rounded_and_exposed(hass):
    await setup_entry(hass)
    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    await flush_store(hass)

    store = hass.data[DOMAIN]["store"]
    now = dt_util.utcnow().timestamp()
    result = await store.async_query_states(["device_tracker.phone"], now - 3600, now + 3600)
    items = result["device_tracker.phone"]
    assert len(items) == 1
    assert items[0]["a"]["latitude"] == 50.123457
    assert items[0]["a"]["longitude"] == 8.987654

    exposed = hass.states.get("device_tracker.gps_timeline_phone_timeline")
    assert exposed is not None
    assert exposed.state == "not_home"
    assert exposed.attributes["latitude"] == 50.123457
    assert exposed.attributes["longitude"] == 8.987654
    assert exposed.attributes["gps_accuracy"] == 10
    assert exposed.attributes["source_entity"] == "device_tracker.phone"


async def test_exposed_entity_follows_source(hass):
    await setup_entry(hass)
    hass.states.async_set("device_tracker.phone", "home", TRACKER_ATTRS)
    await flush_store(hass)
    exposed = hass.states.get("device_tracker.gps_timeline_phone_timeline")
    assert exposed.state == "home"

    hass.states.async_set(
        "device_tracker.phone",
        "not_home",
        {**TRACKER_ATTRS, "latitude": 49.5, "longitude": 9.5},
    )
    await flush_store(hass)
    exposed = hass.states.get("device_tracker.gps_timeline_phone_timeline")
    assert exposed.state == "not_home"
    assert exposed.attributes["latitude"] == 49.5


async def test_accuracy_filter(hass):
    await setup_entry(hass)
    hass.states.async_set(
        "device_tracker.phone",
        "not_home",
        {**TRACKER_ATTRS, "gps_accuracy": 250},
    )
    await flush_store(hass)
    store = hass.data[DOMAIN]["store"]
    now = dt_util.utcnow().timestamp()
    result = await store.async_query_states(["device_tracker.phone"], now - 3600, now + 3600)
    assert "device_tracker.phone" not in result


async def test_accuracy_threshold_defaults_when_missing(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: "device_tracker.phone"},
        title="GPS Timeline — Phone",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    await flush_store(hass)

    exposed = hass.states.get("device_tracker.gps_timeline_phone_timeline")
    assert exposed is not None
    assert exposed.attributes["latitude"] == 50.123457
    assert exposed.attributes["longitude"] == 8.987654
    assert exposed.attributes["gps_accuracy"] == 10

    hass.states.async_set(
        "device_tracker.phone",
        "not_home",
        {**TRACKER_ATTRS, "gps_accuracy": 250},
    )
    await flush_store(hass)

    store = hass.data[DOMAIN]["store"]
    now = dt_util.utcnow().timestamp()
    result = await store.async_query_states(["device_tracker.phone"], now - 3600, now + 3600)
    items = result["device_tracker.phone"]
    assert len(items) == 1
    assert items[0]["a"]["gps_accuracy"] == 10

    exposed = hass.states.get("device_tracker.gps_timeline_phone_timeline")
    assert exposed.attributes["latitude"] == 50.123457
    assert exposed.attributes["longitude"] == 8.987654


async def test_companion_entities_archived(hass):
    entry = await setup_entry(
        hass, places_entity="sensor.places_phone", activity_entity="sensor.activity_phone"
    )
    hass.states.async_set(
        "device_tracker.phone",
        "not_home",
        TRACKER_ATTRS,
    )
    hass.states.async_set("sensor.places_phone", "Starbucks", {"place_name": "Starbucks"})
    hass.states.async_set("sensor.activity_phone", "walking", {})
    await flush_store(hass)

    store = hass.data[DOMAIN]["store"]
    now = dt_util.utcnow().timestamp()
    result = await store.async_query_states(
        ["sensor.places_phone", "sensor.activity_phone"], now - 3600, now + 3600
    )
    assert result["sensor.places_phone"][0]["s"] == "Starbucks"
    assert result["sensor.activity_phone"][0]["s"] == "walking"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get("device_tracker.gps_timeline_phone_timeline")
    assert state is None or state.state == "unavailable"
    assert DOMAIN not in hass.data or "store" not in hass.data[DOMAIN]


async def test_corrupt_db_creates_repair_issue(hass):
    db_path = Path(hass.config.path(DB_DIR_NAME, DB_FILE_NAME))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"this is not a sqlite database")

    await setup_entry(hass)

    registry = ir.async_get(hass)
    issue = registry.issues.get((DOMAIN, "corrupt_database"))
    assert issue is not None
    assert issue.translation_key == "corrupt_database"
    backup_path = issue.data["backup_path"]
    assert Path(backup_path).exists()

    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    await flush_store(hass)
    store = hass.data[DOMAIN]["store"]
    now = dt_util.utcnow().timestamp()
    result = await store.async_query_states(["device_tracker.phone"], now - 3600, now + 3600)
    assert len(result["device_tracker.phone"]) == 1


async def test_healthy_db_does_not_create_repair_issue(hass):
    await setup_entry(hass)
    registry = ir.async_get(hass)
    assert (DOMAIN, "corrupt_database") not in registry.issues


async def test_places_v3_place_name_child_archived(hass):
    await setup_entry(
        hass,
        places_entity="sensor.places_phone",
        prepare=lambda entry: register_places_pair(hass, entry, "sensor.places_phone"),
    )
    hass.states.async_set("sensor.places_phone", "Starbucks", {"place_name": "Starbucks"})
    hass.states.async_set(
        "sensor.places_phone_place_name", "Starbucks", {"place_name": "Starbucks"}
    )
    await flush_store(hass)

    store = hass.data[DOMAIN]["store"]
    now = dt_util.utcnow().timestamp()
    result = await store.async_query_states(
        ["sensor.places_phone", "sensor.places_phone_place_name"], now - 3600, now + 3600
    )
    assert result["sensor.places_phone"][0]["s"] == "Starbucks"
    assert result["sensor.places_phone_place_name"][0]["s"] == "Starbucks"


async def test_places_v2_sensor_without_child_archives_only_main(hass):
    await setup_entry(hass, places_entity="sensor.places_phone")
    hass.states.async_set("sensor.places_phone", "Starbucks", {"place_name": "Starbucks"})
    await flush_store(hass)

    store = hass.data[DOMAIN]["store"]
    now = dt_util.utcnow().timestamp()
    result = await store.async_query_states(
        ["sensor.places_phone", "sensor.places_phone_place_name"], now - 3600, now + 3600
    )
    assert result["sensor.places_phone"][0]["s"] == "Starbucks"
    assert "sensor.places_phone_place_name" not in result


async def test_place_name_child_resolved_from_state_fallback(hass):
    await setup_entry(
        hass,
        places_entity="sensor.places_phone",
        prepare=lambda entry: hass.states.async_set(
            "sensor.places_phone_place_name", "previous", {}
        ),
    )
    hass.states.async_set("sensor.places_phone", "Starbucks", {})
    hass.states.async_set("sensor.places_phone_place_name", "Starbucks", {})
    await flush_store(hass)

    store = hass.data[DOMAIN]["store"]
    now = dt_util.utcnow().timestamp()
    result = await store.async_query_states(
        ["sensor.places_phone_place_name"], now - 3600, now + 3600
    )
    assert result["sensor.places_phone_place_name"][0]["s"] == "Starbucks"


async def test_place_name_child_selected_directly_is_not_duplicated(hass):
    entry = await setup_entry(
        hass,
        places_entity="sensor.places_phone_place_name",
        prepare=lambda entry: register_places_pair(hass, entry, "sensor.places_phone"),
    )
    assert tracked_entity_ids(hass, entry) == [
        "device_tracker.phone",
        "sensor.places_phone_place_name",
    ]


async def test_options_reload_swaps_place_name_listener(hass):
    entry = await setup_entry(hass, places_entity="sensor.places_home")

    hass.states.async_set("sensor.places_work_place_name", "previous", {})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_PLACES_ENTITY: "sensor.places_work", CONF_ACCURACY_THRESHOLD: 100},
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PLACES_ENTITY] == "sensor.places_work"

    hass.states.async_set("sensor.places_home_place_name", "Home", {})
    hass.states.async_set("sensor.places_work_place_name", "Work", {})
    await flush_store(hass)

    store = hass.data[DOMAIN]["store"]
    now = dt_util.utcnow().timestamp()
    result = await store.async_query_states(
        ["sensor.places_home_place_name", "sensor.places_work_place_name"],
        now - 3600,
        now + 3600,
    )
    assert result["sensor.places_work_place_name"][0]["s"] == "Work"
    assert "sensor.places_home_place_name" not in result


def register_entity(hass, entity_id, platform="device_tracker", unique_id="phone"):
    domain, object_id = entity_id.split(".", 1)
    return er.async_get(hass).async_get_or_create(
        domain, platform, unique_id, suggested_object_id=object_id
    )


def tracker_id_for(hass, entity_id):
    db_path = Path(hass.config.path(DB_DIR_NAME, DB_FILE_NAME))
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT id FROM trackers WHERE entity_id = ?", (entity_id,)).fetchone()
    conn.close()
    return row[0] if row else None


async def test_registry_rename_updates_store_and_entry_data(hass):
    entry = await setup_entry(
        hass, prepare=lambda entry: register_entity(hass, "device_tracker.phone")
    )
    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    await flush_store(hass)
    old_tracker_id = tracker_id_for(hass, "device_tracker.phone")
    assert old_tracker_id is not None

    er.async_get(hass).async_update_entity(
        "device_tracker.phone", new_entity_id="device_tracker.phone_new"
    )
    await hass.async_block_till_done()

    assert entry.data[CONF_ENTITY_ID] == "device_tracker.phone_new"
    assert tracker_id_for(hass, "device_tracker.phone_new") == old_tracker_id
    assert tracker_id_for(hass, "device_tracker.phone") is None


async def test_live_archiving_continues_after_registry_rename(hass):
    await setup_entry(
        hass, prepare=lambda entry: register_entity(hass, "device_tracker.phone")
    )
    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    await flush_store(hass)

    er.async_get(hass).async_update_entity(
        "device_tracker.phone", new_entity_id="device_tracker.phone_new"
    )
    await hass.async_block_till_done()

    hass.states.async_set(
        "device_tracker.phone_new",
        "home",
        {**TRACKER_ATTRS, "latitude": 52.0, "longitude": 10.0},
    )
    await flush_store(hass)

    store = hass.data[DOMAIN]["store"]
    now = dt_util.utcnow().timestamp()
    result = await store.async_query_states(
        ["device_tracker.phone_new"], now - 3600, now + 3600
    )
    assert len(result["device_tracker.phone_new"]) == 2
    assert result["device_tracker.phone_new"][-1]["a"]["latitude"] == 52.0


async def test_companion_rename_moves_rows_and_updates_entry(hass):
    def prepare(entry):
        register_entity(hass, "device_tracker.phone")
        register_places_pair(hass, entry, "sensor.places_phone")

    entry = await setup_entry(
        hass,
        places_entity="sensor.places_phone",
        prepare=prepare,
    )
    hass.states.async_set("sensor.places_phone", "Starbucks", {"place_name": "Starbucks"})
    hass.states.async_set(
        "sensor.places_phone_place_name", "Starbucks", {"place_name": "Starbucks"}
    )
    await flush_store(hass)

    er.async_get(hass).async_update_entity(
        "sensor.places_phone", new_entity_id="sensor.places_phone_renamed"
    )
    await hass.async_block_till_done()

    assert entry.data[CONF_PLACES_ENTITY] == "sensor.places_phone_renamed"
    store = hass.data[DOMAIN]["store"]
    now = dt_util.utcnow().timestamp()
    result = await store.async_query_states(
        ["sensor.places_phone_renamed"], now - 3600, now + 3600
    )
    assert result["sensor.places_phone_renamed"][0]["s"] == "Starbucks"
    assert "sensor.places_phone" not in await store.async_query_states(
        ["sensor.places_phone"], now - 3600, now + 3600
    )

    hass.states.async_set("sensor.places_phone_renamed", "Home", {})
    await flush_store(hass)
    result = await store.async_query_states(
        ["sensor.places_phone_renamed"], now - 3600, now + 3600
    )
    assert result["sensor.places_phone_renamed"][-1]["s"] == "Home"


async def test_subject_configured_and_exposed(hass):
    await setup_entry(hass, subject_kind="person", subject_name="Yacin")
    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    await flush_store(hass)

    exposed = hass.states.get("device_tracker.gps_timeline_phone_timeline")
    assert exposed is not None
    assert exposed.attributes["subject_kind"] == "person"
    assert exposed.attributes["subject_name"] == "Yacin"

    conn = sqlite3.connect(str(Path(hass.config.path(DB_DIR_NAME, DB_FILE_NAME))))
    row = conn.execute(
        "SELECT subject_kind, subject_name FROM trackers WHERE entity_id = ?",
        ("device_tracker.phone",),
    ).fetchone()
    conn.close()
    assert row == ("person", "Yacin")


async def test_unload_reload_keeps_history_under_same_tracker_id(hass):
    entry = await setup_entry(hass)
    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    await flush_store(hass)
    old_tracker_id = tracker_id_for(hass, "device_tracker.phone")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert tracker_id_for(hass, "device_tracker.phone") == old_tracker_id
    store = hass.data[DOMAIN]["store"]
    now = dt_util.utcnow().timestamp()
    result = await store.async_query_states(["device_tracker.phone"], now - 3600, now + 3600)
    assert len(result["device_tracker.phone"]) == 1
