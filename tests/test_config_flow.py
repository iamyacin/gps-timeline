from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv

from custom_components.gps_timeline.const import (
    CONF_ACCURACY_THRESHOLD,
    CONF_ACTIVITY_ENTITY,
    CONF_ENTITY_ID,
    CONF_PLACES_ENTITY,
    DOMAIN,
)


def serialize_schema(schema):
    return cv.to_field_list(schema, custom_serializer=cv.custom_serializer)


async def _set_tracker_state(hass):
    hass.states.async_set(
        "device_tracker.phone",
        "home",
        {
            "latitude": 50.1234567,
            "longitude": 8.9876543,
            "gps_accuracy": 15,
            "friendly_name": "Phone",
        },
    )


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
    assert names == ["entity_id", "places_entity", "activity_entity", "accuracy_threshold"]


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
    assert names == ["places_entity", "activity_entity", "accuracy_threshold"]

    suggested = {
        marker.description["suggested_value"]
        for marker in result["data_schema"].schema
        if marker.description and "suggested_value" in marker.description
    }
    assert {"sensor.places_phone", 100} <= suggested
