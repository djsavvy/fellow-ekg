"""Number entities for the Fellow Stagg EKG+."""

from __future__ import annotations

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

    Setting is a single `setsetting settempr` command with a patient
    /api read-back (kettle applies settings from its main loop) — no
    dial rotation and no button-2 wake (see kettle.py).
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:thermometer"

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
        """Set the target temperature."""
        client: StaggEKGClient = self.coordinator.client
        # `value` arrives in the entity's native unit (the kettle's display
        # unit); set_temperature takes Celsius.
        if self._attr_native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT:
            value = (value - 32) * 5 / 9
        await self.hass.async_add_executor_job(client.set_temperature, float(value))
        await self.coordinator.async_request_refresh()