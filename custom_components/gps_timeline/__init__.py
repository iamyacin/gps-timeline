from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.issue_registry import IssueSeverity

from .const import (
    CONF_ACTIVITY_ENTITY,
    CONF_ENTITY_ID,
    CONF_PLACES_ENTITY,
    CONF_SUBJECT_KIND,
    CONF_SUBJECT_NAME,
    DB_DIR_NAME,
    DB_FILE_NAME,
    DOMAIN,
)
from .helpers import (
    PLACE_NAME_SUFFIX,
    accuracy_threshold,
    entry_setting,
    tracked_entity_ids,
)
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
    tracker_id = await store.async_bind_tracker(entry.entry_id, source_entity_id)
    await store.async_set_subject(
        entry.entry_id,
        entry_setting(entry, CONF_SUBJECT_KIND),
        entry_setting(entry, CONF_SUBJECT_NAME),
    )

    listeners = data.setdefault("listeners", {})
    entity_ids = tracked_entity_ids(hass, entry)

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

    if not data.get("registry_listener_registered"):

        @callback
        def _handle_registry_update(event: Event) -> None:
            _handle_entity_registry_update(hass, event)

        data["registry_unsubscribe"] = hass.bus.async_listen(
            EVENT_ENTITY_REGISTRY_UPDATED, _handle_registry_update
        )
        data["registry_listener_registered"] = True

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.debug("Set up GPS Timeline for %s", source_entity_id)
    return True


def _handle_entity_registry_update(hass: HomeAssistant, event: Event) -> None:
    data = event.data
    if data.get("action") != "update":
        return
    if data.get("changes", {}).get("entity_id") is None:
        return
    old_entity_id = data["changes"]["entity_id"]
    new_entity_id = data["entity_id"]
    entry = _find_entry_for_entity(hass, old_entity_id)
    if entry is None:
        return
    hass.async_create_task(
        _async_chase_entity_rename(hass, entry, old_entity_id, new_entity_id)
    )


def _find_entry_for_entity(hass: HomeAssistant, entity_id: str) -> ConfigEntry | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_ENTITY_ID, "").lower() == entity_id:
            return entry
        for key in (CONF_PLACES_ENTITY, CONF_ACTIVITY_ENTITY):
            if entry.data.get(key, "").lower() == entity_id:
                return entry
    return None


async def _async_chase_entity_rename(
    hass: HomeAssistant, entry: ConfigEntry, old_entity_id: str, new_entity_id: str
) -> None:
    store: Store | None = hass.data[DOMAIN].get("store")
    if store is None:
        return
    old_entity_id = old_entity_id.lower()
    new_entity_id = new_entity_id.lower()
    await store.async_rename_entity(old_entity_id, new_entity_id)

    data_updates: dict[str, str] = {}
    if entry.data.get(CONF_ENTITY_ID, "").lower() == old_entity_id:
        data_updates[CONF_ENTITY_ID] = new_entity_id
    for key in (CONF_PLACES_ENTITY, CONF_ACTIVITY_ENTITY):
        if (entry.data.get(key) or "").lower() == old_entity_id:
            data_updates[key] = new_entity_id
            if key == CONF_PLACES_ENTITY:
                await store.async_rename_entity(
                    f"{old_entity_id}{PLACE_NAME_SUFFIX}",
                    f"{new_entity_id}{PLACE_NAME_SUFFIX}",
                )
    if data_updates:
        hass.config_entries.async_update_entry(entry, data={**entry.data, **data_updates})
        hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data[DOMAIN]
    unsubscribe = data.get("listeners", {}).pop(entry.entry_id, None)
    if unsubscribe is not None:
        unsubscribe()
    data.get("entries", set()).discard(entry.entry_id)

    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, PLATFORMS[0])

    if not data.get("entries"):
        registry_unsubscribe = data.pop("registry_unsubscribe", None)
        if registry_unsubscribe is not None:
            registry_unsubscribe()
            data.pop("registry_listener_registered", None)
        store: Store | None = data.get("store")
        if store is not None:
            await store.async_close()
            data.pop("store", None)
        _LOGGER.debug("Last entry unloaded; store closed")

    return unload_ok
