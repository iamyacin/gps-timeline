from __future__ import annotations

from datetime import timedelta
import functools
import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import (
    CONF_ACCURACY_THRESHOLD,
    CONF_ENTITY_ID,
    DEFAULT_ACCURACY_THRESHOLD,
    DOMAIN,
    SERVICE_BACKFILL,
)
from .helpers import entry_setting, tracked_entity_ids
from .store import Store, normalize_entity_state, normalize_point

BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENTITY_ID): cv.entity_id,
        vol.Optional("start_time"): cv.datetime,
        vol.Optional("end_time"): cv.datetime,
    }
)

DEFAULT_BACKFILL_DAYS = 10

_LOGGER = logging.getLogger(__name__)


def _find_entry(hass: HomeAssistant, entity_id: str):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_ENTITY_ID, "").lower() == entity_id:
            return entry
    return None


def _recorder_instance(hass: HomeAssistant):
    try:
        return get_instance(hass)
    except KeyError as err:
        raise HomeAssistantError(
            "The recorder integration is not available; backfill requires it"
        ) from err


@callback
def async_register_services(hass: HomeAssistant) -> None:
    hass.services.async_register(
        DOMAIN, SERVICE_BACKFILL, _async_handle_backfill, schema=BACKFILL_SCHEMA
    )


async def _async_handle_backfill(call: ServiceCall) -> None:
    hass = call.hass
    entity_id = call.data[CONF_ENTITY_ID].lower()
    entry = _find_entry(hass, entity_id)
    if entry is None:
        raise HomeAssistantError(f"{entity_id} is not tracked by GPS Timeline")

    store: Store | None = hass.data[DOMAIN].get("store")
    if store is None:
        raise HomeAssistantError("GPS Timeline store is not available")

    end_time = dt_util.as_utc(call.data.get("end_time") or dt_util.utcnow())
    start_time = dt_util.as_utc(
        call.data.get("start_time") or (end_time - timedelta(days=DEFAULT_BACKFILL_DAYS))
    )
    if start_time > end_time:
        raise HomeAssistantError("start_time must be before end_time")

    tracker_id = await store.async_bind_tracker(entry.entry_id, entity_id)
    query_entity_ids = tracked_entity_ids(entry)
    threshold = entry_setting(entry, CONF_ACCURACY_THRESHOLD, DEFAULT_ACCURACY_THRESHOLD)

    result = await _recorder_instance(hass).async_add_executor_job(
        functools.partial(
            get_significant_states,
            hass,
            start_time=start_time,
            end_time=end_time,
            entity_ids=query_entity_ids,
            include_start_time_state=False,
            significant_changes_only=False,
            no_attributes=False,
        )
    )

    points: list[tuple[int, tuple]] = []
    states: list[tuple[int, str, tuple]] = []
    for state_entity_id, history_states in result.items():
        if state_entity_id == entity_id:
            for state in history_states:
                if row := normalize_point(state, threshold):
                    points.append((tracker_id, row))
        else:
            for state in history_states:
                if row := normalize_entity_state(state):
                    states.append((tracker_id, state_entity_id, row))

    inserted, total = await store.async_write_backfill(points, states)
    _LOGGER.info(
        "Backfill for %s: %s of %s rows imported (%s points, %s companion states)",
        entity_id,
        inserted,
        total,
        len(points),
        len(states),
    )
