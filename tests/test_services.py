from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gps_timeline import services
from custom_components.gps_timeline.const import (
    CONF_ACCURACY_THRESHOLD,
    CONF_ENTITY_ID,
    CONF_PLACES_ENTITY,
    DOMAIN,
    SERVICE_BACKFILL,
)


def make_state(entity_id, state, attrs, ts):
    return State(entity_id, state, attrs, last_updated=dt_util.utc_from_timestamp(ts))


TRACKER_ATTRS = {
    "latitude": 50.1234567,
    "longitude": 8.9876543,
    "gps_accuracy": 10,
    "battery_level": 80,
    "source_type": "gps",
}


async def setup_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTITY_ID: "device_tracker.phone",
            CONF_PLACES_ENTITY: "sensor.places_phone",
            CONF_ACCURACY_THRESHOLD: 100,
        },
        title="GPS Timeline — Phone",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


class FakeRecorderInstance:
    async def async_add_executor_job(self, func, *args, **kwargs):
        return func(*args, **kwargs)


async def test_backfill_imports_recorder_states(hass, monkeypatch):
    await setup_entry(hass)
    monkeypatch.setattr(services, "_recorder_instance", lambda hass: FakeRecorderInstance())

    def fake_get_significant_states(
        hass, start_time=None, end_time=None, entity_ids=None, **kwargs
    ):
        assert "device_tracker.phone" in entity_ids
        assert "sensor.places_phone" in entity_ids
        return {
            "device_tracker.phone": [
                make_state(
                    "device_tracker.phone",
                    "home",
                    dict(TRACKER_ATTRS, latitude=50.0, longitude=9.0),
                    1000.0,
                ),
                make_state(
                    "device_tracker.phone",
                    "not_home",
                    dict(TRACKER_ATTRS, latitude=50.25, longitude=9.25),
                    2000.0,
                ),
            ],
            "sensor.places_phone": [
                make_state(
                    "sensor.places_phone", "Starbucks", {"place_name": "Starbucks"}, 1500.0
                ),
            ],
            "sensor.not_tracked": [
                make_state("sensor.not_tracked", "x", {}, 1000.0),
            ],
        }

    monkeypatch.setattr(services, "get_significant_states", fake_get_significant_states)

    await hass.services.async_call(
        DOMAIN, SERVICE_BACKFILL, {CONF_ENTITY_ID: "device_tracker.phone"}, blocking=True
    )

    store = hass.data[DOMAIN]["store"]
    end = dt_util.utcnow().timestamp() + 10000
    result = await store.async_query_states(
        ["device_tracker.phone", "sensor.places_phone"], 0, end
    )
    points = result["device_tracker.phone"]
    assert len(points) == 2
    assert points[0]["a"]["latitude"] == 50.0
    states = result["sensor.places_phone"]
    assert len(states) == 1
    assert states[0]["s"] == "Starbucks"


async def test_backfill_imports_place_name_child(hass, monkeypatch):
    await setup_entry(hass)
    hass.states.async_set("sensor.places_phone_place_name", "previous", {})
    monkeypatch.setattr(services, "_recorder_instance", lambda hass: FakeRecorderInstance())

    def fake_get_significant_states(
        hass, start_time=None, end_time=None, entity_ids=None, **kwargs
    ):
        assert "sensor.places_phone_place_name" in entity_ids
        return {
            "device_tracker.phone": [
                make_state(
                    "device_tracker.phone",
                    "home",
                    dict(TRACKER_ATTRS, latitude=50.0, longitude=9.0),
                    1000.0,
                ),
            ],
            "sensor.places_phone_place_name": [
                make_state(
                    "sensor.places_phone_place_name",
                    "Starbucks",
                    {"place_name": "Starbucks"},
                    1500.0,
                ),
            ],
        }

    monkeypatch.setattr(services, "get_significant_states", fake_get_significant_states)

    await hass.services.async_call(
        DOMAIN, SERVICE_BACKFILL, {CONF_ENTITY_ID: "device_tracker.phone"}, blocking=True
    )

    store = hass.data[DOMAIN]["store"]
    end = dt_util.utcnow().timestamp() + 10000
    result = await store.async_query_states(["sensor.places_phone_place_name"], 0, end)
    states = result["sensor.places_phone_place_name"]
    assert len(states) == 1
    assert states[0]["s"] == "Starbucks"


async def test_backfill_unknown_entity_errors(hass):
    await setup_entry(hass)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, SERVICE_BACKFILL, {CONF_ENTITY_ID: "device_tracker.other"}, blocking=True
        )


async def test_backfill_without_recorder_errors(hass, monkeypatch):
    await setup_entry(hass)

    def no_recorder(hass):
        raise KeyError("Expected recorder to be loaded")

    monkeypatch.setattr(services, "get_instance", no_recorder)
    with pytest.raises(HomeAssistantError, match="recorder"):
        await hass.services.async_call(
            DOMAIN, SERVICE_BACKFILL, {CONF_ENTITY_ID: "device_tracker.phone"}, blocking=True
        )


async def test_backfill_invalid_time_range_errors(hass, monkeypatch):
    await setup_entry(hass)
    monkeypatch.setattr(services, "_recorder_instance", lambda hass: FakeRecorderInstance())
    monkeypatch.setattr(
        services,
        "get_significant_states",
        lambda *args, **kwargs: {},
    )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BACKFILL,
            {
                CONF_ENTITY_ID: "device_tracker.phone",
                "start_time": "2026-01-02T00:00:00+00:00",
                "end_time": "2026-01-01T00:00:00+00:00",
            },
            blocking=True,
        )
