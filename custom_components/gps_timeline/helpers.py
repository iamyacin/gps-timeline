from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_ACCURACY_THRESHOLD,
    CONF_ACTIVITY_ENTITY,
    CONF_ENTITY_ID,
    CONF_PLACES_ENTITY,
    DEFAULT_ACCURACY_THRESHOLD,
)

_LOGGER = logging.getLogger(__name__)

PLACE_NAME_SUFFIX = "_place_name"


def entry_setting(entry: ConfigEntry, key: str, default: Any = None) -> Any:
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


def accuracy_threshold(entry: ConfigEntry) -> float:
    return entry_setting(entry, CONF_ACCURACY_THRESHOLD, DEFAULT_ACCURACY_THRESHOLD)


def resolve_place_name_entity(hass: HomeAssistant, place_entity_id: str | None) -> str | None:
    """Return the Places v3 `_place_name` child for a Places sensor, if any."""
    if not place_entity_id:
        return None
    place_entity_id = place_entity_id.lower()
    if place_entity_id.endswith(PLACE_NAME_SUFFIX):
        return None

    registry = er.async_get(hass)
    registry_entry = registry.async_get(place_entity_id)
    if registry_entry is not None and registry_entry.device_id is not None:
        for candidate in er.async_entries_for_device(
            registry, registry_entry.device_id, include_disabled_entities=True
        ):
            if candidate.entity_id.endswith(PLACE_NAME_SUFFIX):
                return candidate.entity_id

    fallback = f"{place_entity_id}{PLACE_NAME_SUFFIX}"
    if hass.states.get(fallback) is not None:
        return fallback

    _LOGGER.debug("No `_place_name` child sensor found for Places entity %s", place_entity_id)
    return None


def tracked_entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    entity_ids = [entry.data[CONF_ENTITY_ID]]
    for key in (CONF_PLACES_ENTITY, CONF_ACTIVITY_ENTITY):
        companion = entry_setting(entry, key)
        if companion:
            entity_ids.append(companion)

    place_name_entity = resolve_place_name_entity(
        hass, entry_setting(entry, CONF_PLACES_ENTITY)
    )
    if place_name_entity is not None:
        tracked = {entity_id.lower() for entity_id in entity_ids}
        if place_name_entity not in tracked:
            entity_ids.append(place_name_entity)
    return entity_ids
