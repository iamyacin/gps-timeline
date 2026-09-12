from pathlib import Path

from homeassistant.helpers import issue_registry as ir
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

TRACKER_ATTRS = {
    "latitude": 50.1234567,
    "longitude": 8.9876543,
    "gps_accuracy": 10,
    "battery_level": 80,
    "speed": 3.5,
    "source_type": "gps",
    "friendly_name": "Phone",
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
