from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCURACY_THRESHOLD,
    CONF_ACTIVITY_ENTITY,
    CONF_ENTITY_ID,
    CONF_PLACES_ENTITY,
    DB_DIR_NAME,
    DB_FILE_NAME,
    DEFAULT_ACCURACY_THRESHOLD,
    DOMAIN,
)
from .store import async_list_orphans_standalone, async_purge_tracker_standalone


def entry_setting(entry: ConfigEntry, key: str, default: Any = None) -> Any:
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


def accuracy_threshold(entry: ConfigEntry) -> float:
    return entry_setting(entry, CONF_ACCURACY_THRESHOLD, DEFAULT_ACCURACY_THRESHOLD)


def tracked_entity_ids(entry: ConfigEntry) -> list[str]:
    entity_ids = [entry.data[CONF_ENTITY_ID]]
    for key in (CONF_PLACES_ENTITY, CONF_ACTIVITY_ENTITY):
        companion = entry_setting(entry, key)
        if companion:
            entity_ids.append(companion)
    return entity_ids


def live_entry_ids(hass: HomeAssistant) -> list[str]:
    """Entry ids of every gps_timeline config entry in the registry."""
    return [entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)]


def live_entity_claims(
    hass: HomeAssistant, exclude_entry_id: str | None = None
) -> dict[str, str]:
    """Map the entity ids tracked by existing entries to their owning entry id."""
    claims: dict[str, str] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if exclude_entry_id and entry.entry_id == exclude_entry_id:
            continue
        for entity_id in tracked_entity_ids(entry):
            claims[entity_id.lower()] = entry.entry_id
    return claims


async def async_get_orphans(hass: HomeAssistant) -> list[dict[str, Any]]:
    """List orphaned archived trackers, through the live store when possible."""
    data = hass.data.get(DOMAIN)
    store = data.get("store") if isinstance(data, dict) else None
    entry_ids = live_entry_ids(hass)
    claims = live_entity_claims(hass)
    if store is not None:
        return await store.async_list_orphans(entry_ids, list(claims))
    return await async_list_orphans_standalone(
        hass,
        hass.config.path(DB_DIR_NAME, DB_FILE_NAME),
        entry_ids,
        list(claims),
    )


async def async_purge_entry_data(
    hass: HomeAssistant,
    entry_id: str | None = None,
    *,
    tracker_id: int | None = None,
    exclude_entry_id: str | None = None,
) -> dict[str, int]:
    """Purge archived tracker data, routing through the live store when it exists."""
    data = hass.data.get(DOMAIN)
    store = data.get("store") if isinstance(data, dict) else None
    claims = live_entity_claims(hass, exclude_entry_id)
    if store is not None:
        return await store.async_purge_tracker(
            entry_id, tracker_id=tracker_id, live_entity_claims=claims
        )
    return await async_purge_tracker_standalone(
        hass,
        hass.config.path(DB_DIR_NAME, DB_FILE_NAME),
        entry_id,
        tracker_id=tracker_id,
        live_entity_claims=claims,
    )
