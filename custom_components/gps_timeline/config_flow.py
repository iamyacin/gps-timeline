from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.core import State, callback
from homeassistant.helpers.selector import (
    EntitySelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)
import voluptuous as vol

from .const import (
    CONF_ACCURACY_THRESHOLD,
    CONF_ACTIVITY_ENTITY,
    CONF_ENTITY_ID,
    CONF_PLACES_ENTITY,
    DEFAULT_ACCURACY_THRESHOLD,
    DOMAIN,
)

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


def optional_entity_selector(value):
    if value in (None, ""):
        return None
    return COMPANION_ENTITY_SELECTOR(value)


def build_user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_ENTITY_ID, default=defaults.get(CONF_ENTITY_ID)
            ): SOURCE_ENTITY_SELECTOR,
            vol.Optional(
                CONF_PLACES_ENTITY, default=defaults.get(CONF_PLACES_ENTITY)
            ): optional_entity_selector,
            vol.Optional(
                CONF_ACTIVITY_ENTITY, default=defaults.get(CONF_ACTIVITY_ENTITY)
            ): optional_entity_selector,
            vol.Required(
                CONF_ACCURACY_THRESHOLD,
                default=defaults.get(CONF_ACCURACY_THRESHOLD, DEFAULT_ACCURACY_THRESHOLD),
            ): ACCURACY_SELECTOR,
        }
    )


def build_options_schema(entry_data: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_PLACES_ENTITY, default=entry_data.get(CONF_PLACES_ENTITY)
            ): optional_entity_selector,
            vol.Optional(
                CONF_ACTIVITY_ENTITY, default=entry_data.get(CONF_ACTIVITY_ENTITY)
            ): optional_entity_selector,
            vol.Required(
                CONF_ACCURACY_THRESHOLD,
                default=entry_data.get(CONF_ACCURACY_THRESHOLD, DEFAULT_ACCURACY_THRESHOLD),
            ): ACCURACY_SELECTOR,
        }
    )


def has_coordinates(state: State | None) -> bool:
    if state is None:
        return False
    latitude = state.attributes.get("latitude")
    longitude = state.attributes.get("longitude")
    return isinstance(latitude, (int, float)) and isinstance(longitude, (int, float))


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
                return self.async_create_entry(
                    title=f"GPS Timeline — {friendly_name}",
                    data=user_input,
                )
        return self.async_show_form(
            step_id="user", data_schema=build_user_schema(), errors=errors
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
        entry_data = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=build_options_schema(entry_data)
        )
