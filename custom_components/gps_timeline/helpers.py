from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_ACCURACY_THRESHOLD,
    CONF_ACTIVITY_ENTITY,
    CONF_ENTITY_ID,
    CONF_PLACES_ENTITY,
    DEFAULT_ACCURACY_THRESHOLD,
)


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
