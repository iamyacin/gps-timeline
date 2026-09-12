from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.issue_registry import IssueSeverity

from .const import (
    CONF_ENTITY_ID,
    DB_DIR_NAME,
    DB_FILE_NAME,
    DOMAIN,
)
from .helpers import accuracy_threshold, tracked_entity_ids
from .services import async_register_services
from .store import Store, normalize_entity_state, normalize_point
from .websocket import async_register_websocket

PLATFORMS = ["device_tracker"]

_CORRUPT_DB_ISSUE_ID = "corrupt_database"

_LOGGER = logging.getLogger(__name__)


@callback
def _update_corrupt_db_issue(hass: HomeAssistant, store: Store) -> None:
    if (backup_path := store.corrupt_backup_path) is not None:
        ir.async_create_issue(
            hass,
            DOMAIN,
            _CORRUPT_DB_ISSUE_ID,
            is_fixable=False,
            severity=IssueSeverity.ERROR,
            translation_key=_CORRUPT_DB_ISSUE_ID,
            data={"backup_path": str(backup_path)},
            translation_placeholders={"backup_path": str(backup_path)},
        )
        return
    ir.async_delete_issue(hass, DOMAIN, _CORRUPT_DB_ISSUE_ID)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.setdefault(DOMAIN, {})
    store: Store | None = data.get("store")
    if store is None:
        store = Store(hass, hass.config.path(DB_DIR_NAME, DB_FILE_NAME))
        await store.async_setup()
        data["store"] = store
        _update_corrupt_db_issue(hass, store)

        async def _handle_stop(_event: Event) -> None:
            await store.async_close()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _handle_stop)

    if not data.get("websocket_registered"):
        async_register_websocket(hass)
        data["websocket_registered"] = True

    if not data.get("services_registered"):
        async_register_services(hass)
        data["services_registered"] = True

    source_entity_id = entry.data[CONF_ENTITY_ID].lower()
    tracker_id = await store.async_ensure_tracker(source_entity_id)

    listeners = data.setdefault("listeners", {})
    entity_ids = tracked_entity_ids(entry)

    @callback
    def _handle_state_change(event: Event[EventStateChangedData]) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        entity_id = new_state.entity_id
        if entity_id == source_entity_id:
            if row := normalize_point(new_state, accuracy_threshold(entry)):
                store.async_add_point(tracker_id, row)
        elif row := normalize_entity_state(new_state):
            store.async_add_entity_state(tracker_id, entity_id, row)

    listeners[entry.entry_id] = async_track_state_change_event(
        hass, entity_ids, _handle_state_change
    )
    data.setdefault("entries", set()).add(entry.entry_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.debug("Set up GPS Timeline for %s", source_entity_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data[DOMAIN]
    unsubscribe = data.get("listeners", {}).pop(entry.entry_id, None)
    if unsubscribe is not None:
        unsubscribe()
    data.get("entries", set()).discard(entry.entry_id)

    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, PLATFORMS[0])

    if not data.get("entries"):
        store: Store | None = data.get("store")
        if store is not None:
            await store.async_close()
            data.pop("store", None)
        _LOGGER.debug("Last entry unloaded; store closed")

    return unload_ok
