from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.core import State, callback
from homeassistant.helpers.selector import (
    EntitySelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import (
    ATTACH_START_FRESH,
    CONF_ACCURACY_THRESHOLD,
    CONF_ACTIVITY_ENTITY,
    CONF_ATTACH_TRACKER_ID,
    CONF_ENTITY_ID,
    CONF_PLACES_ENTITY,
    CONF_SUBJECT_KIND,
    CONF_SUBJECT_NAME,
    DEFAULT_ACCURACY_THRESHOLD,
    DOMAIN,
)
from .helpers import async_get_orphans

SOURCE_ENTITY_SELECTOR = EntitySelector()
COMPANION_ENTITY_SELECTOR = EntitySelector()
ACCURACY_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=0,
        max=10000,
        step=1,
        unit_of_measurement="m",
        mode=NumberSelectorMode.BOX,
    )
)
SUBJECT_KIND_SELECTOR = SelectSelector(
    SelectSelectorConfig(options=["person", "topic", "object"])
)

TRACKER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENTITY_ID): SOURCE_ENTITY_SELECTOR,
        vol.Optional(CONF_PLACES_ENTITY): COMPANION_ENTITY_SELECTOR,
        vol.Optional(CONF_ACTIVITY_ENTITY): COMPANION_ENTITY_SELECTOR,
        vol.Optional(CONF_SUBJECT_KIND): SUBJECT_KIND_SELECTOR,
        vol.Optional(CONF_SUBJECT_NAME): str,
        vol.Required(
            CONF_ACCURACY_THRESHOLD, default=DEFAULT_ACCURACY_THRESHOLD
        ): ACCURACY_SELECTOR,
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PLACES_ENTITY): COMPANION_ENTITY_SELECTOR,
        vol.Optional(CONF_ACTIVITY_ENTITY): COMPANION_ENTITY_SELECTOR,
        vol.Optional(CONF_SUBJECT_KIND): SUBJECT_KIND_SELECTOR,
        vol.Optional(CONF_SUBJECT_NAME): str,
        vol.Required(
            CONF_ACCURACY_THRESHOLD, default=DEFAULT_ACCURACY_THRESHOLD
        ): ACCURACY_SELECTOR,
    }
)

RECONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENTITY_ID): SOURCE_ENTITY_SELECTOR,
        vol.Optional(CONF_PLACES_ENTITY): COMPANION_ENTITY_SELECTOR,
        vol.Optional(CONF_ACTIVITY_ENTITY): COMPANION_ENTITY_SELECTOR,
        vol.Optional(CONF_SUBJECT_KIND): SUBJECT_KIND_SELECTOR,
        vol.Optional(CONF_SUBJECT_NAME): str,
        vol.Required(
            CONF_ACCURACY_THRESHOLD, default=DEFAULT_ACCURACY_THRESHOLD
        ): ACCURACY_SELECTOR,
    }
)


def has_coordinates(state: State | None) -> bool:
    if state is None:
        return False
    latitude = state.attributes.get("latitude")
    longitude = state.attributes.get("longitude")
    return isinstance(latitude, (int, float)) and isinstance(longitude, (int, float))


def format_orphan_label(orphan: dict[str, Any]) -> str:
    """Build the rich chooser label, e.g.

    ``Yacin (person) — device_tracker.phone — 4,213 points — last: 2026-06-01``.
    """
    parts: list[str] = []
    if orphan.get("subject_name"):
        kind = orphan.get("subject_kind") or "subject"
        parts.append(f"{orphan['subject_name']} ({kind})")
    parts.append(orphan["entity_id"])
    if orphan.get("point_count"):
        parts.append(f"{orphan['point_count']:,} points")
        if orphan.get("last_ts"):
            last = dt_util.as_local(dt_util.utc_from_timestamp(orphan["last_ts"])).strftime(
                "%Y-%m-%d"
            )
            parts.append(f"last: {last}")
    else:
        parts.append("no archived points")
    return " — ".join(parts)


def merged_entry_data(entry: config_entries.ConfigEntry) -> dict[str, Any]:
    merged = {**entry.data, **entry.options}
    return {key: value for key, value in merged.items() if value is not None}


def normalize_subject(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalize subject fields; empty values mean 'no subject'."""
    values: dict[str, Any] = {}
    kind = user_input.get(CONF_SUBJECT_KIND)
    name = user_input.get(CONF_SUBJECT_NAME)
    if kind and name:
        values[CONF_SUBJECT_KIND] = kind
        values[CONF_SUBJECT_NAME] = name.strip()
    return values


def companion_moves(
    old_data: dict[str, Any], new_settings: dict[str, Any]
) -> list[tuple[str, str]]:
    """Diff old vs new companion entity ids into (old, new) store moves."""
    moves: list[tuple[str, str]] = []
    for key in (CONF_PLACES_ENTITY, CONF_ACTIVITY_ENTITY):
        old_value = (old_data.get(key) or "").lower()
        new_value = (new_settings.get(key) or "").lower()
        if not old_value or not new_value or old_value == new_value:
            continue
        moves.append((old_value, new_value))
    return moves


class GPSTimelineConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the GPS Timeline config flow."""

    VERSION = 1

    _pending_data: dict[str, Any] | None = None
    _attach_friendly_name: str = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_id = user_input[CONF_ENTITY_ID]
            state = self.hass.states.get(entity_id)
            if not has_coordinates(state):
                errors["base"] = "missing_coordinates"
            else:
                self._async_abort_entries_match({CONF_ENTITY_ID: entity_id})
                friendly_name = state.attributes.get("friendly_name", entity_id)
                data = {
                    **user_input,
                    CONF_PLACES_ENTITY: user_input.get(CONF_PLACES_ENTITY),
                    CONF_ACTIVITY_ENTITY: user_input.get(CONF_ACTIVITY_ENTITY),
                    **normalize_subject(user_input),
                }
                if await async_get_orphans(self.hass):
                    self._pending_data = data
                    self._attach_friendly_name = str(friendly_name)
                    return await self.async_step_attach()
                return self.async_create_entry(
                    title=f"GPS Timeline — {friendly_name}",
                    data=data,
                )
        return self.async_show_form(step_id="user", data_schema=TRACKER_SCHEMA, errors=errors)

    async def async_step_attach(self, user_input: dict[str, Any] | None = None):
        """Offer to attach the new entry to archived data left by a removed one."""
        assert self._pending_data is not None
        entity_id = str(self._pending_data[CONF_ENTITY_ID]).lower()
        errors: dict[str, str] = {}
        if user_input is not None:
            choice = user_input.get(CONF_ATTACH_TRACKER_ID)
            if choice in (None, "", ATTACH_START_FRESH):
                data = {**self._pending_data, CONF_ATTACH_TRACKER_ID: None}
                return self.async_create_entry(
                    title=f"GPS Timeline — {self._attach_friendly_name}",
                    data=data,
                )
            orphans = await async_get_orphans(self.hass)
            chosen = next(
                (orphan for orphan in orphans if str(orphan["tracker_id"]) == str(choice)),
                None,
            )
            if chosen is None:
                # The picked orphan vanished (purged meanwhile); re-show the
                # chooser so the list is current.
                errors["base"] = "orphan_unavailable"
            elif any(
                orphan["entity_id"] == entity_id
                and int(orphan["tracker_id"]) != int(chosen["tracker_id"])
                for orphan in orphans
            ):
                errors["base"] = "adopt_conflict"
            else:
                data = {
                    **self._pending_data,
                    CONF_ATTACH_TRACKER_ID: int(chosen["tracker_id"]),
                }
                return self.async_create_entry(
                    title=f"GPS Timeline — {self._attach_friendly_name}",
                    data=data,
                )
        orphans = await async_get_orphans(self.hass)
        options = [
            SelectOptionDict(
                value=str(orphan["tracker_id"]), label=format_orphan_label(orphan)
            )
            for orphan in orphans
        ]
        options.append(
            SelectOptionDict(
                value=ATTACH_START_FRESH, label="Start fresh (ignore archived data)"
            )
        )
        matched = next(
            (orphan for orphan in orphans if orphan["entity_id"] == entity_id), None
        )
        default = str(matched["tracker_id"]) if matched else ATTACH_START_FRESH
        return self.async_show_form(
            step_id="attach",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ATTACH_TRACKER_ID, default=default): SelectSelector(
                        SelectSelectorConfig(options=options)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_id = user_input[CONF_ENTITY_ID]
            state = self.hass.states.get(entity_id)
            if not has_coordinates(state):
                errors["base"] = "missing_coordinates"
            else:
                self._async_abort_entries_match({CONF_ENTITY_ID: entity_id})
                subject = normalize_subject(user_input)
                data = {
                    **entry.data,
                    CONF_ENTITY_ID: entity_id,
                    CONF_PLACES_ENTITY: user_input.get(CONF_PLACES_ENTITY),
                    CONF_ACTIVITY_ENTITY: user_input.get(CONF_ACTIVITY_ENTITY),
                    CONF_SUBJECT_KIND: subject.get(CONF_SUBJECT_KIND),
                    CONF_SUBJECT_NAME: subject.get(CONF_SUBJECT_NAME),
                    CONF_ACCURACY_THRESHOLD: user_input[CONF_ACCURACY_THRESHOLD],
                }
                store = self.hass.data.get(DOMAIN, {}).get("store")
                if store is not None:
                    for old_id, new_id in companion_moves(entry.data, data):
                        await store.async_rename_entity(old_id, new_id)
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                RECONFIGURE_SCHEMA, merged_entry_data(entry)
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> OptionsFlowHandler:
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle GPS Timeline options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, merged_entry_data(self.config_entry)
            ),
        )
