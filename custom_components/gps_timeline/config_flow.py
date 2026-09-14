from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.core import State, callback
from homeassistant.helpers.selector import (
    EntitySelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
)
import voluptuous as vol

from .const import (
    CONF_ACCURACY_THRESHOLD,
    CONF_ACTIVITY_ENTITY,
    CONF_ENTITY_ID,
    CONF_PLACES_ENTITY,
    CONF_SUBJECT_KIND,
    CONF_SUBJECT_NAME,
    DEFAULT_ACCURACY_THRESHOLD,
    DOMAIN,
)
from .helpers import PLACE_NAME_SUFFIX

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
) -> list[tuple[str, str, str | None]]:
    """Diff old vs new companion entity ids into (old, new, child) store moves."""
    moves: list[tuple[str, str, str | None]] = []
    for key in (CONF_PLACES_ENTITY, CONF_ACTIVITY_ENTITY):
        old_value = (old_data.get(key) or "").lower()
        new_value = (new_settings.get(key) or "").lower()
        if not old_value or not new_value or old_value == new_value:
            continue
        child_old = None
        if key == CONF_PLACES_ENTITY:
            child_old = f"{old_value}{PLACE_NAME_SUFFIX}"
        moves.append((old_value, new_value, child_old))
    return moves


class GPSTimelineConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the GPS Timeline config flow."""

    VERSION = 1

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
                return self.async_create_entry(
                    title=f"GPS Timeline — {friendly_name}",
                    data=data,
                )
        return self.async_show_form(step_id="user", data_schema=TRACKER_SCHEMA, errors=errors)

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
                    for old_id, new_id, child_old in companion_moves(entry.data, data):
                        await store.async_rename_entity(old_id, new_id)
                        if child_old is not None:
                            await store.async_rename_entity(
                                child_old, f"{new_id}{PLACE_NAME_SUFFIX}"
                            )
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


class OptionsFlowHandler(config_entries.OptionsFlowWithReload):
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
