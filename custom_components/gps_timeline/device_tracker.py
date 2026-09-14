from __future__ import annotations

import json
from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_ENTITY_ID, CONF_SUBJECT_KIND, CONF_SUBJECT_NAME, DOMAIN
from .helpers import accuracy_threshold, entry_setting
from .store import normalize_point

_SKIPPED_ATTRIBUTES = {
    "latitude",
    "longitude",
    "gps_accuracy",
    "accuracy",
    "battery_level",
    "battery",
    "source_type",
    "friendly_name",
    "icon",
    "entity_picture",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([GPSTimelineTrackerEntity(entry, entry.data[CONF_ENTITY_ID])])


class GPSTimelineTrackerEntity(TrackerEntity):
    """Device tracker mirroring the latest archived GPS point of the source entity."""

    _attr_entity_category = None
    _attr_has_entity_name = True
    _attr_icon = "mdi:map-marker-path"
    _attr_name = "Timeline"
    _attr_source_type = SourceType.GPS

    def __init__(self, entry: ConfigEntry, source_entity_id: str) -> None:
        self._entry = entry
        self._source = source_entity_id
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="gps_timeline",
            model="Location history archive",
        )
        self._point: dict[str, Any] | None = None
        self._attr_latitude = None
        self._attr_longitude = None
        self._attr_location_accuracy = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        store = self.hass.data[DOMAIN].get("store")
        if store is not None:
            self._apply_point(await store.async_get_last_point(self._source))
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._source], self._handle_source_state
            )
        )
        self.async_write_ha_state()

    def _apply_point(self, point: dict[str, Any] | None) -> None:
        self._point = point
        if point is None:
            return
        self._attr_latitude = point["lat"]
        self._attr_longitude = point["lon"]
        self._attr_location_accuracy = point["accuracy"] or 0
        if point["source_type"]:
            try:
                self._attr_source_type = SourceType(point["source_type"])
            except ValueError:
                self._attr_source_type = SourceType.GPS

    @callback
    def _handle_source_state(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        row = normalize_point(new_state, accuracy_threshold(self._entry))
        if row is None:
            return
        (
            ts,
            state,
            latitude,
            longitude,
            accuracy,
            _battery,
            speed,
            altitude,
            _heading,
            source_type,
            attributes_json,
        ) = row
        self._apply_point(
            {
                "ts": ts,
                "state": state,
                "lat": latitude,
                "lon": longitude,
                "accuracy": accuracy,
                "speed": speed,
                "altitude": altitude,
                "source_type": source_type,
                "attributes": json.loads(attributes_json),
            }
        )
        self.async_write_ha_state()

    @property
    def state(self) -> str | None:
        if self._point is not None and self._point["state"]:
            return self._point["state"]
        return super().state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {"source_entity": self._source}
        if (subject_kind := entry_setting(self._entry, CONF_SUBJECT_KIND)) and (
            subject_name := entry_setting(self._entry, CONF_SUBJECT_NAME)
        ):
            attributes["subject_kind"] = subject_kind
            attributes["subject_name"] = subject_name
        if self._point is None:
            return attributes
        if self._point.get("speed") is not None:
            attributes["speed"] = self._point["speed"]
        if self._point.get("altitude") is not None:
            attributes["altitude"] = self._point["altitude"]
        for key, value in (self._point.get("attributes") or {}).items():
            if key not in _SKIPPED_ATTRIBUTES and key not in attributes:
                attributes[key] = value
        return attributes
