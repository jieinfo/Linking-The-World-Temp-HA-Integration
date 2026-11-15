"""Native room thermostat climate entities."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import THERMOSTAT_MAX_TEMPERATURE, THERMOSTAT_MIN_TEMPERATURE
from .entity import LinkingThermostatEntity
from .hub import LinkingTempHub
from .thermostat_policy import can_operate_room_thermostat

ROOM_CONTROLS_REANNOUNCE_DELAY = 1.0


class RoomThermostat(LinkingThermostatEntity, ClimateEntity):
    """A room panel controlled by the system-wide operating mode."""

    _attr_translation_key = "room_thermostat"
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_force_update = False
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = THERMOSTAT_MIN_TEMPERATURE
    _attr_max_temp = THERMOSTAT_MAX_TEMPERATURE
    _attr_target_temperature_step = 1
    _attr_precision = 0.1

    def __init__(self, hub: LinkingTempHub, mac_hex: str) -> None:
        super().__init__(hub, mac_hex, "climate")
        self._cancel_reannounce: CALLBACK_TYPE | None = None
        self._last_controls: tuple[
            bool, HVACMode, str | None, str | None, str | None
        ] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hub.async_add_listener(
                self._async_schedule_reannounce, scopes=self.update_scopes
            )
        )
        self.async_on_remove(self._async_cancel_reannounce)
        self._async_schedule_reannounce()

    @callback
    def _async_schedule_reannounce(self) -> None:
        """Coalesce control/availability transitions, not routine telemetry."""
        available = self.available
        controls = (
            available,
            self.hvac_mode if available else HVACMode.OFF,
            self.hub.thermostats[self.mac_hex].power if available else None,
            self.hub.state.power,
            self.hub.state.mode,
        )
        if controls == self._last_controls:
            return
        self._last_controls = controls
        self._async_cancel_reannounce()
        if available:
            self._cancel_reannounce = async_call_later(
                self.hass, ROOM_CONTROLS_REANNOUNCE_DELAY, self._async_reannounce
            )

    @callback
    def _async_cancel_reannounce(self) -> None:
        if self._cancel_reannounce is not None:
            self._cancel_reannounce()
            self._cancel_reannounce = None

    @callback
    def _async_reannounce(self, _now: datetime) -> None:
        """Let state-change subscribers re-read one confirmed room state."""
        self._cancel_reannounce = None
        if not self.available:
            return
        # Normal repeated writes only emit state_reported, which HomeKit ignores.
        self._attr_force_update = True
        try:
            self.async_write_ha_state()
        finally:
            self._attr_force_update = False

    @property
    def name(self) -> str:
        return "温控"

    @property
    def update_scopes(self) -> frozenset[str]:
        return super().update_scopes | {"room_controls"}

    @property
    def native_current_temperature(self) -> float | None:
        return self.hub.thermostats[self.mac_hex].current_temperature

    @property
    def native_target_temperature(self) -> float | None:
        return self.hub.thermostats[self.mac_hex].target_temperature

    # HA 2026.9/2026.10 still read the old API. Do not define these aliases on
    # newer HA, whose class-registration hook flags them as deprecated.
    if not hasattr(ClimateEntity, "native_temperature_unit"):

        @property
        def native_temperature_unit(self) -> str:
            return self._attr_native_temperature_unit

        @property
        def temperature_unit(self) -> str:
            return self.native_temperature_unit

        @property
        def current_temperature(self) -> float | None:
            return self.native_current_temperature

        @property
        def target_temperature(self) -> float | None:
            return self.native_target_temperature

    @property
    def current_humidity(self) -> int | None:
        return self.hub.thermostats[self.mac_hex].humidity

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Expose stable power-only modes; the system select owns operation mode."""
        return [HVACMode.OFF, HVACMode.AUTO]

    @property
    def hvac_mode(self) -> HVACMode:
        thermostat = self.hub.thermostats[self.mac_hex]
        if thermostat.power != "ON" or not can_operate_room_thermostat(
            self.hub.state.power, self.hub.state.mode
        ):
            return HVACMode.OFF
        return HVACMode.AUTO

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.hub.async_set_thermostat_power(self.mac_hex, False)
            return
        if hvac_mode != HVACMode.AUTO:
            raise HomeAssistantError("房间温控面板的运行模式由科技系统总控选择")
        if not can_operate_room_thermostat(
            self.hub.state.power, self.hub.state.mode
        ):
            raise HomeAssistantError("房间温控面板只能跟随当前科技系统模式")
        await self.hub.async_set_thermostat_power(self.mac_hex, True)

    async def async_set_temperature(self, **kwargs) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        await self.hub.async_set_thermostat_temperature(
            self.mac_hex, float(temperature)
        )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: LinkingTempHub = entry.runtime_data.hub
    added: set[str] = set()

    @callback
    def add_new_entities() -> None:
        new_macs = [mac for mac in hub.thermostats if mac not in added]
        if not new_macs:
            return
        added.update(new_macs)
        async_add_entities([RoomThermostat(hub, mac) for mac in new_macs])

    add_new_entities()
    entry.async_on_unload(
        hub.async_add_listener(add_new_entities, scopes={"discovery"})
    )
