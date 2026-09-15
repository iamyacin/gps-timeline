from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
import voluptuous as vol

from .helpers import async_purge_entry_data
from .store import StoreError

DELETE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("delete_data", default=False): bool,
    }
)


class StaleTimelineDataFixFlow(RepairsFlow):
    """Confirmation flow for purging the data archived by a removed tracker.

    This is the "popup": delete the archived points and companion states, or
    keep them (the safe default — they stay in the database as orphans and
    become re-claimable when the subject is added again).
    """

    def __init__(self, issue_id: str, issue_data: dict[str, Any] | None) -> None:
        super().__init__()
        self._issue_id = issue_id
        self._issue_data = issue_data or {}

    def _placeholders(self) -> dict[str, str]:
        return {"entity_id": str(self._issue_data.get("entity_id") or "")}

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None):
        placeholders = self._placeholders()
        if user_input is not None:
            if user_input["delete_data"]:
                try:
                    await async_purge_entry_data(
                        self.hass, entry_id=str(self._issue_data.get("entry_id") or "")
                    )
                except StoreError:
                    return self.async_show_form(
                        step_id="confirm",
                        data_schema=DELETE_DATA_SCHEMA,
                        errors={"base": "purge_failed"},
                        description_placeholders=placeholders,
                    )
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=DELETE_DATA_SCHEMA,
            description_placeholders=placeholders,
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create the confirmation flow for a stale archived-data issue."""
    return StaleTimelineDataFixFlow(issue_id, data or {})
