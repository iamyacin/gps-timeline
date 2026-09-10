from __future__ import annotations

import logging
from typing import Any

from homeassistant.auth.permissions import filter_entity_ids_by_permission
from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components import websocket_api
from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.core import HomeAssistant, callback, valid_entity_id
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import CONF_ENTITY_ID, DOMAIN, WS_HISTORY_DURING_PERIOD

_LOGGER = logging.getLogger(__name__)


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, _handle_history_during_period)


@callback
def _build_exposed_map(hass: HomeAssistant) -> dict[str, str]:
    registry = er.async_get(hass)
    mapping: dict[str, str] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.source == SOURCE_IGNORE:
            continue
        exposed_entity_id = registry.async_get_entity_id(
            "device_tracker", DOMAIN, entry.entry_id
        )
        if exposed_entity_id and CONF_ENTITY_ID in entry.data:
            mapping[exposed_entity_id] = entry.data[CONF_ENTITY_ID].lower()
    return mapping


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_HISTORY_DURING_PERIOD,
        vol.Required("start_time"): str,
        vol.Optional("end_time"): str,
        vol.Required("entity_ids"): [str],
        vol.Optional("include_start_time_state", default=True): bool,
        vol.Optional("significant_changes_only", default=True): bool,
        vol.Optional("minimal_response", default=False): bool,
        vol.Optional("no_attributes", default=False): bool,
    }
)
@websocket_api.async_response
async def _handle_history_during_period(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    store = hass.data.get(DOMAIN, {}).get("store")
    if store is None:
        connection.send_result(msg["id"], {})
        return

    if not (start_time := dt_util.parse_datetime(msg["start_time"])):
        connection.send_error(msg["id"], "invalid_start_time", "Invalid start_time")
        return
    start_time = dt_util.as_utc(start_time)

    end_time_str = msg.get("end_time")
    if end_time_str is None:
        end_time = dt_util.utcnow()
    elif not (end_time := dt_util.parse_datetime(end_time_str)):
        connection.send_error(msg["id"], "invalid_end_time", "Invalid end_time")
        return
    else:
        end_time = dt_util.as_utc(end_time)

    if start_time > dt_util.utcnow():
        connection.send_result(msg["id"], {})
        return

    entity_ids: list[str] = msg["entity_ids"]
    for entity_id in entity_ids:
        if not hass.states.get(entity_id) and not valid_entity_id(entity_id):
            connection.send_error(msg["id"], "invalid_entity_ids", "Invalid entity_ids")
            return

    entity_ids = filter_entity_ids_by_permission(connection.user, entity_ids, POLICY_READ)
    if not entity_ids:
        connection.send_result(msg["id"], {})
        return

    exposed_map = _build_exposed_map(hass)
    result = await store.async_query_states(
        [exposed_map.get(entity_id, entity_id) for entity_id in entity_ids],
        start_time.timestamp(),
        end_time.timestamp(),
        no_attributes=msg["no_attributes"],
        minimal_response=msg["minimal_response"],
        include_start_time_state=msg["include_start_time_state"],
    )

    response: dict[str, list[dict[str, Any]]] = {}
    for entity_id in entity_ids:
        query_id = exposed_map.get(entity_id, entity_id).lower()
        if states := result.get(query_id):
            response[entity_id] = states
    connection.send_result(msg["id"], response)
