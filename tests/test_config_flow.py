from pathlib import Path
import sqlite3

from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gps_timeline.const import (
    ATTACH_START_FRESH,
    CONF_ACCURACY_THRESHOLD,
    CONF_ACTIVITY_ENTITY,
    CONF_ATTACH_TRACKER_ID,
    CONF_ENTITY_ID,
    CONF_PLACES_ENTITY,
    DB_DIR_NAME,
    DB_FILE_NAME,
    DOMAIN,
)


def serialize_schema(schema):
    return cv.to_field_list(schema, custom_serializer=cv.custom_serializer)


TRACKER_ATTRS = {
    "latitude": 50.1234567,
    "longitude": 8.9876543,
    "gps_accuracy": 15,
    "friendly_name": "Phone",
}

TRACKER_ATTRS_TABLET = {
    "latitude": 51.0,
    "longitude": 9.0,
    "gps_accuracy": 15,
    "friendly_name": "Tablet",
}


async def _set_tracker_state(hass):
    hass.states.async_set("device_tracker.phone", "home", TRACKER_ATTRS)


def _marker_for(schema, key):
    for marker in schema.schema:
        if marker == key:
            return marker
    return None


async def _make_orphan(hass, entity_id):
    """Archive data for an entity, then remove the entry to orphan the row."""
    hass.states.async_set(entity_id, "not_home", TRACKER_ATTRS)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTITY_ID: entity_id,
            CONF_PLACES_ENTITY: None,
            CONF_ACTIVITY_ENTITY: None,
            CONF_ACCURACY_THRESHOLD: 100,
        },
        title="GPS Timeline",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(entity_id, "home", TRACKER_ATTRS)
    await hass.data[DOMAIN]["store"].async_flush()
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()


async def test_user_flow_creates_entry(hass):
    await _set_tracker_state(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"entity_id": "device_tracker.phone", CONF_ACCURACY_THRESHOLD: 100},
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "GPS Timeline — Phone"

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].data[CONF_ENTITY_ID] == "device_tracker.phone"
    assert entries[0].data[CONF_PLACES_ENTITY] is None
    assert entries[0].data[CONF_ACTIVITY_ENTITY] is None
    assert entries[0].data[CONF_ACCURACY_THRESHOLD] == 100


async def test_user_flow_rejects_entity_without_coordinates(hass):
    hass.states.async_set("binary_sensor.door", "on", {"friendly_name": "Door"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"entity_id": "binary_sensor.door", CONF_ACCURACY_THRESHOLD: 100},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "missing_coordinates"}


async def test_duplicate_entity_aborts(hass):
    await _set_tracker_state(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"entity_id": "device_tracker.phone", CONF_ACCURACY_THRESHOLD: 100},
    )
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"entity_id": "device_tracker.phone", CONF_ACCURACY_THRESHOLD: 100},
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_updates_settings(hass):
    await _set_tracker_state(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"entity_id": "device_tracker.phone", CONF_ACCURACY_THRESHOLD: 100},
    )
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    hass.states.async_set("sensor.places_phone", "Starbucks", {})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_PLACES_ENTITY: "sensor.places_phone", CONF_ACCURACY_THRESHOLD: 50},
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PLACES_ENTITY] == "sensor.places_phone"
    assert entry.options[CONF_ACCURACY_THRESHOLD] == 50


async def test_user_flow_form_serializes_for_frontend(hass):
    await _set_tracker_state(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    fields = serialize_schema(result["data_schema"])
    names = [field["name"] for field in fields]
    assert names == [
        "entity_id",
        "places_entity",
        "activity_entity",
        "subject_kind",
        "subject_name",
        "accuracy_threshold",
    ]


async def test_options_flow_form_serializes_for_frontend(hass):
    await _set_tracker_state(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "entity_id": "device_tracker.phone",
            CONF_PLACES_ENTITY: "sensor.places_phone",
            CONF_ACCURACY_THRESHOLD: 100,
        },
    )
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    fields = serialize_schema(result["data_schema"])
    names = [field["name"] for field in fields]
    assert names == [
        "places_entity",
        "activity_entity",
        "subject_kind",
        "subject_name",
        "accuracy_threshold",
    ]

    suggested = {
        marker.description["suggested_value"]
        for marker in result["data_schema"].schema
        if marker.description and "suggested_value" in marker.description
    }
    assert {"sensor.places_phone", 100} <= suggested


async def test_user_flow_persists_subject(hass):
    await _set_tracker_state(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "entity_id": "device_tracker.phone",
            CONF_ACCURACY_THRESHOLD: 100,
            "subject_kind": "person",
            "subject_name": "Yacin",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data["subject_kind"] == "person"
    assert entry.data["subject_name"] == "Yacin"


async def test_options_flow_persists_subject(hass):
    await _set_tracker_state(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"entity_id": "device_tracker.phone", CONF_ACCURACY_THRESHOLD: 100},
    )
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_ACCURACY_THRESHOLD: 100,
            "subject_kind": "object",
            "subject_name": "Car",
        },
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["subject_kind"] == "object"
    assert entry.options["subject_name"] == "Car"


async def test_reconfigure_flow_changes_entity(hass):
    await _set_tracker_state(hass)
    hass.states.async_set("device_tracker.tablet", "home", TRACKER_ATTRS_TABLET)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"entity_id": "device_tracker.phone", CONF_ACCURACY_THRESHOLD: 100},
    )
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity_id": "device_tracker.tablet", CONF_ACCURACY_THRESHOLD: 100}
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_ENTITY_ID] == "device_tracker.tablet"


async def test_reconfigure_flow_duplicate_entity_aborts(hass):
    await _set_tracker_state(hass)
    hass.states.async_set("device_tracker.tablet", "home", TRACKER_ATTRS_TABLET)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"entity_id": "device_tracker.phone", CONF_ACCURACY_THRESHOLD: 100},
    )
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert result["step_id"] == "reconfigure"

    second = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    await hass.config_entries.flow.async_configure(
        second["flow_id"],
        {"entity_id": "device_tracker.tablet", CONF_ACCURACY_THRESHOLD: 100},
    )
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity_id": "device_tracker.tablet", CONF_ACCURACY_THRESHOLD: 100}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_flow_moves_companion_rows(hass):
    await _set_tracker_state(hass)
    hass.states.async_set("sensor.places_old", "Starbucks", {})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "entity_id": "device_tracker.phone",
            CONF_PLACES_ENTITY: "sensor.places_old",
            CONF_ACCURACY_THRESHOLD: 100,
        },
    )
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    store = hass.data[DOMAIN]["store"]
    tracker_id = await store.async_bind_tracker(entry.entry_id, "device_tracker.phone")
    store.async_add_entity_state(
        tracker_id,
        "sensor.places_old",
        (100.0, "Starbucks", "{}"),
    )
    await store.async_flush()

    hass.states.async_set("sensor.places_new", "Home", {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "entity_id": "device_tracker.phone",
            CONF_PLACES_ENTITY: "sensor.places_new",
            CONF_ACCURACY_THRESHOLD: 100,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PLACES_ENTITY] == "sensor.places_new"

    store = hass.data[DOMAIN]["store"]
    result = await store.async_query_states(["sensor.places_new"], 0, 1000)
    assert len(result["sensor.places_new"]) == 1
    assert await store.async_query_states(["sensor.places_old"], 0, 1000) == {}


async def test_reconfigure_flow_cleared_companion_keeps_old_rows(hass):
    await _set_tracker_state(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "entity_id": "device_tracker.phone",
            CONF_PLACES_ENTITY: "sensor.places_phone",
            CONF_ACCURACY_THRESHOLD: 100,
        },
    )
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    store = hass.data[DOMAIN]["store"]
    tracker_id = await store.async_bind_tracker(entry.entry_id, "device_tracker.phone")
    store.async_add_entity_state(
        tracker_id,
        "sensor.places_phone",
        (100.0, "Home", "{}"),
    )
    await store.async_flush()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity_id": "device_tracker.phone", CONF_ACCURACY_THRESHOLD: 100}
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert entry.data[CONF_PLACES_ENTITY] is None

    store = hass.data[DOMAIN]["store"]
    result = await store.async_query_states(["sensor.places_phone"], 0, 1000)
    assert len(result["sensor.places_phone"]) == 1


async def _start_user_flow(hass, entity_id="device_tracker.phone"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity_id": entity_id, CONF_ACCURACY_THRESHOLD: 100}
    )


def _chooser_options(result):
    marker = _marker_for(result["data_schema"], CONF_ATTACH_TRACKER_ID)
    assert marker is not None
    selector = result["data_schema"].schema[marker]
    options = selector.config["options"]
    values = [option if isinstance(option, str) else option["value"] for option in options]
    default = marker.default() if callable(marker.default) else marker.default
    return values, default


async def test_orphans_show_attach_step_with_preselected_match(hass):
    await _make_orphan(hass, "device_tracker.phone")
    result = await _start_user_flow(hass)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "attach"
    values, default = _chooser_options(result)
    assert ATTACH_START_FRESH in values
    assert default != ATTACH_START_FRESH
    orphan_choice = str(default)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ATTACH_TRACKER_ID: orphan_choice}
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].data[CONF_ATTACH_TRACKER_ID] == int(orphan_choice)
    assert entries[0].data[CONF_ENTITY_ID] == "device_tracker.phone"


async def test_attach_start_fresh_keeps_orphan(hass):
    await _make_orphan(hass, "device_tracker.phone")
    result = await _start_user_flow(hass)
    assert result["step_id"] == "attach"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ATTACH_TRACKER_ID: ATTACH_START_FRESH}
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY

    entries = hass.config_entries.async_entries(DOMAIN)
    assert entries[0].data[CONF_ATTACH_TRACKER_ID] is None

    conn = sqlite3.connect(str(Path(hass.config.path(DB_DIR_NAME, DB_FILE_NAME))))
    assert conn.execute("SELECT COUNT(*) FROM points").fetchone()[0] == 1
    conn.close()


async def test_attach_adopt_continues_history(hass):
    await _make_orphan(hass, "device_tracker.phone")
    result = await _start_user_flow(hass)
    _values, default = _chooser_options(result)
    orphan_choice = str(default)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ATTACH_TRACKER_ID: orphan_choice}
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY

    store = hass.data[DOMAIN]["store"]
    end = dt_util.utcnow().timestamp() + 3600
    history = await store.async_query_states(["device_tracker.phone"], 0, end)
    assert len(history["device_tracker.phone"]) == 1


async def test_attach_stale_orphan_shows_error(hass):
    await _make_orphan(hass, "device_tracker.phone")
    result = await _start_user_flow(hass)
    assert result["step_id"] == "attach"
    values, default = _chooser_options(result)
    orphan_choice = str(default)
    assert orphan_choice in values

    db_path = str(Path(hass.config.path(DB_DIR_NAME, DB_FILE_NAME)))
    from custom_components.gps_timeline.store import async_purge_tracker_standalone

    counts = await async_purge_tracker_standalone(hass, db_path, tracker_id=int(orphan_choice))
    assert counts["trackers"] == 1

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ATTACH_TRACKER_ID: orphan_choice}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "orphan_unavailable"}


async def test_attach_conflict_rejected(hass):
    await _make_orphan(hass, "device_tracker.phone")
    await _make_orphan(hass, "device_tracker.old")

    result = await _start_user_flow(hass)
    assert result["step_id"] == "attach"
    values, default = _chooser_options(result)
    assert len(values) == 3

    other = next(value for value in values if value not in (str(default), ATTACH_START_FRESH))

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ATTACH_TRACKER_ID: other}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "adopt_conflict"}


async def test_no_orphans_skips_attach_step(hass):
    await _set_tracker_state(hass)
    result = await _start_user_flow(hass)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert CONF_ATTACH_TRACKER_ID not in result.get("data", {})
