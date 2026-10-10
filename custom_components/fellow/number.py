"""Number entities for the Fellow Stagg EKG+."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.number import (
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import StaggEKGDataUpdateCoordinator
from .const import (
    DOMAIN,
    MAX_TEMP_C,
    MIN_TEMP_C,
    UNIT_FAHRENHEIT,
)
from .kettle import StaggEKGClient

_LOGGER = logging.getLogger(__name__)

MIN_TEMP_F = 104
MAX_TEMP_F = 212

# Seconds of quiet before a slider burst results in one command to the
# kettle. Short enough to feel instant, long enough to coalesce a drag.
DEBOUNCE_SECONDS = 0.25


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Stagg EKG target temperature number."""
    coordinator: StaggEKGDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([StaggEKGTargetTemperature(coordinator, entry)])


class StaggEKGTargetTemperature(NumberEntity):
    """Target temperature, as a plain slider on any dashboard card.

    The water_heater entity only shows its temperature control in the
    more-info dialog; this one-liner gives the kettle a real slider.

    Setting is debounced (one `setsetting settempr` command per slider
    burst, no inline read-back: the coordinator's poll confirms), so the
    kettle's HTTP server never sees a burst of writes.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:thermometer"
    _send_task: asyncio.Task | None = None
    _pending_target_c: float = 0.0

    def __init__(
        self, coordinator: StaggEKGDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the number entity."""
        super().__init__()
        self.coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_target_temp_number"
        self._attr_name = "Target Temperature"
        self._attr_mode = NumberMode.BOX  # slider set via the card

        configured_unit = entry.data.get("temperature_unit", "celsius")
        if configured_unit == UNIT_FAHRENHEIT:
            self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
            self._attr_native_min_value = float(MIN_TEMP_F)
            self._attr_native_max_value = float(MAX_TEMP_F)
            self._attr_native_step = 1.0
        else:
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_native_min_value = float(MIN_TEMP_C)
            self._attr_native_max_value = float(MAX_TEMP_C)
            self._attr_native_step = 0.5

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Stagg EKG+",
            "manufacturer": "Fellow",
            "model": "Stagg EKG+",
        }

    @property
    def native_value(self) -> float | None:
        """Return the target temperature in the entity's native unit."""
        if self.coordinator.data and "state" in self.coordinator.data:
            set_c = self.coordinator.data["state"].set_temp_c
            if not set_c:
                return None
            if (
                self._attr_native_unit_of_measurement
                == UnitOfTemperature.FAHRENHEIT
            ):
                return round(set_c * 9 / 5 + 32, 1)
            return set_c
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the target temperature, debounced.

        Each drag-drop fires a write; a burst would queue several blocking
        commands on the kettle. Instead, cancel any pending send and wait
        DEBOUNCE_SECONDS of quiet first: a burst results in exactly ONE
        command carrying the latest value. No read-back: the coordinator's
        5s poll confirms, so the entity tracks the kettle closely without
        verifying inline.
        """
        client: StaggEKGClient = self.coordinator.client
        # `value` arrives in the entity's native unit (the kettle's display
        # unit); send_set_temperature takes Celsius.
        if self._attr_native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT:
            value = (value - 32) * 5 / 9
        self._pending_target_c = float(value)
        if self._send_task:
            self._send_task.cancel()
        self._send_task = asyncio.create_task(self._debounced_send(client))

    async def _debounced_send(self, client: StaggEKGClient) -> None:
        """Send the latest pending target after DEBOUNCE_SECONDS of quiet."""
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        target_c = self._pending_target_c
        self._send_task = None
        await self.hass.async_add_executor_job(client.send_set_temperature, target_c)
        await self.coordinator.async_request_refresh()