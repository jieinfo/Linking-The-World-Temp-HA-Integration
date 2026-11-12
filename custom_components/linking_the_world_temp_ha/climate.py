"""Native room thermostat climate entities."""

from __future__ import annotations

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import THERMOSTAT_MAX_TEMPERATURE, THERMOSTAT_MIN_TEMPERATURE
from .entity import LinkingTempEntity, LinkingThermostatEntity
from .hub import LinkingTempHub
from .thermostat_policy import can_operate_room_thermostat

SYSTEM_MODE_TO_HVAC = {
    "cool": HVACMode.COOL,
    "heat": HVACMode.HEAT,
    "ventilation": HVACMode.FAN_ONLY,
    "dehumidify": HVACMode.DRY,
}
HVAC_TO_SYSTEM_MODE = {value: key for key, value in SYSTEM_MODE_TO_HVAC.items()}
SYSTEM_MODE_TO_ACTION = {
    "cool": HVACAction.COOLING,
    "heat": HVACAction.HEATING,
    "ventilation": HVACAction.FAN,
    "dehumidify": HVACAction.DRYING,
}


class SystemClimate(LinkingTempEntity, ClimateEntity):
    """System-wide operating mode control with the original power switch separate."""

    _attr_translation_key = "system_climate"
    _attr_supported_features = ClimateEntityFeature(0)
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, hub: LinkingTempHub) -> None:
        super().__init__(hub, "system_climate")

    @property
    def native_current_temperature(self) -> float | None:
        return self.hub.state.temperature

    @property
    def current_humidity(self) -> int | None:
        return self.hub.state.humidity

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
    def hvac_modes(self) -> list[HVACMode]:
        """Expose all stable system modes; power remains a separate switch."""
        return list(HVAC_TO_SYSTEM_MODE)

    @property
    def hvac_mode(self) -> HVACMode:
        mode = self.hub.state.mode
        if mode is None:
            return HVACMode.COOL
        return SYSTEM_MODE_TO_HVAC.get(mode, HVACMode.COOL)

    @property
    def hvac_action(self) -> HVACAction:
        mode = self.hub.state.mode
        if self.hub.state.power != "ON" or mode is None:
            return HVACAction.OFF
        return SYSTEM_MODE_TO_ACTION.get(mode, HVACAction.IDLE)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if (mode := HVAC_TO_SYSTEM_MODE.get(hvac_mode)) is None:
            raise HomeAssistantError("科技系统总控 Climate 不负责控制总开关")
        await self.hub.async_select_mode(mode)


class RoomThermostat(LinkingThermostatEntity, ClimateEntity):
    """A room panel controlled by the system-wide operating mode."""

    _attr_translation_key = "room_thermostat"
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = THERMOSTAT_MIN_TEMPERATURE
    _attr_max_temp = THERMOSTAT_MAX_TEMPERATURE
    _attr_target_temperature_step = 1
    _attr_precision = 0.1

    def __init__(self, hub: LinkingTempHub, mac_hex: str) -> None:
        super().__init__(hub, mac_hex, "climate")

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
    async_add_entities([SystemClimate(hub)])
    entry.async_on_unload(
        hub.async_add_listener(add_new_entities, scopes={"discovery"})
    )
