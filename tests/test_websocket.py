from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gps_timeline.const import (
    CONF_ACCURACY_THRESHOLD,
    CONF_ENTITY_ID,
    DOMAIN,
    WS_HISTORY_DURING_PERIOD,
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

HISTORY_MESSAGE_BASE = {
    "id": 1,
    "type": WS_HISTORY_DURING_PERIOD,
    "entity_ids": ["device_tracker.phone"],
}


async def setup_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTITY_ID: "device_tracker.phone",
            CONF_ACCURACY_THRESHOLD: 100,
        },
        title="GPS Timeline — Phone",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


_next_msg_id = 0


async def query(client, **overrides):
    global _next_msg_id
    _next_msg_id += 1
    message = {
        "id": _next_msg_id,
        "type": WS_HISTORY_DURING_PERIOD,
        "start_time": "2020-01-01T00:00:00+00:00",
        "end_time": "2100-01-01T00:00:00+00:00",
        "entity_ids": ["device_tracker.phone"],
        "minimal_response": False,
        "no_attributes": False,
        **overrides,
    }
    await client.send_json(message)
    return await client.receive_json()


async def test_history_during_period_serves_archived_points(hass, hass_ws_client):
    await setup_entry(hass)
    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    await hass.data[DOMAIN]["store"].async_flush()

    client = await hass_ws_client(hass)
    msg = await query(client)
    assert msg["success"]
    result = msg["result"]
    assert "device_tracker.phone" in result
    item = result["device_tracker.phone"][0]
    assert {"s", "a", "lu"} <= set(item)
    assert item["s"] == "not_home"
    assert item["a"]["latitude"] == 50.123457
    assert isinstance(item["lu"], float)


async def test_history_serves_exposed_entity_id(hass, hass_ws_client):
    await setup_entry(hass)
    hass.states.async_set("device_tracker.phone", "home", TRACKER_ATTRS)
    await hass.data[DOMAIN]["store"].async_flush()

    client = await hass_ws_client(hass)
    msg = await query(client, entity_ids=["device_tracker.gps_timeline_phone_timeline"])
    assert msg["success"]
    assert "device_tracker.gps_timeline_phone_timeline" in msg["result"]


async def test_history_unknown_entity_returns_empty(hass, hass_ws_client):
    await setup_entry(hass)
    client = await hass_ws_client(hass)
    msg = await query(client, entity_ids=["sensor.unknown_entity"])
    assert msg["success"]
    assert msg["result"] == {}


async def test_history_invalid_entity_id_errors(hass, hass_ws_client):
    await setup_entry(hass)
    client = await hass_ws_client(hass)
    msg = await query(client, entity_ids=["not an entity"])
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_entity_ids"


async def test_history_invalid_start_time_errors(hass, hass_ws_client):
    await setup_entry(hass)
    client = await hass_ws_client(hass)
    msg = await query(client, start_time="not-a-date")
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_start_time"


async def test_history_future_start_time_returns_empty(hass, hass_ws_client):
    await setup_entry(hass)
    client = await hass_ws_client(hass)
    msg = await query(client, start_time="2100-06-01T00:00:00+00:00")
    assert msg["success"]
    assert msg["result"] == {}


async def test_history_significant_changes_only(hass, hass_ws_client):
    await setup_entry(hass)
    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS)
    await hass.data[DOMAIN]["store"].async_flush()
    hass.states.async_set("device_tracker.phone", "not_home", TRACKER_ATTRS, force_update=True)
    await hass.async_block_till_done()
    await hass.data[DOMAIN]["store"].async_flush()

    client = await hass_ws_client(hass)
    msg = await query(client, significant_changes_only=True)
    assert msg["success"]
    assert len(msg["result"]["device_tracker.phone"]) == 1

    msg = await query(client, significant_changes_only=False)
    assert msg["success"]
    assert len(msg["result"]["device_tracker.phone"]) == 2
