"""Native room thermostat climate entities."""

from __future__ import annotations

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import THERMOSTAT_MAX_TEMPERATURE, THERMOSTAT_MIN_TEMPERATURE
from .entity import LinkingThermostatEntity
from .hub import LinkingTempHub
from .thermostat_policy import can_operate_room_thermostat

MODE_TO_HVAC = {
    "cool": HVACMode.COOL,
    "heat": HVACMode.HEAT,
}


class RoomThermostat(LinkingThermostatEntity, ClimateEntity):
    """A room panel controlled by the system-wide operating mode."""

    _attr_translation_key = "room_thermostat"
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
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
    def current_temperature(self) -> float | None:
        return self.hub.thermostats[self.mac_hex].current_temperature

    @property
    def target_temperature(self) -> float | None:
        return self.hub.thermostats[self.mac_hex].target_temperature

    @property
    def current_humidity(self) -> int | None:
        return self.hub.thermostats[self.mac_hex].humidity

    @property
    def hvac_modes(self) -> list[HVACMode]:
        if not can_operate_room_thermostat(
            self.hub.state.power, self.hub.state.mode
        ):
            return [HVACMode.OFF]
        mode = self.hub.state.mode
        active_mode = MODE_TO_HVAC.get(mode) if mode is not None else None
        return [HVACMode.OFF] + ([active_mode] if active_mode is not None else [])

    @property
    def hvac_mode(self) -> HVACMode:
        thermostat = self.hub.thermostats[self.mac_hex]
        if thermostat.power != "ON" or not can_operate_room_thermostat(
            self.hub.state.power, self.hub.state.mode
        ):
            return HVACMode.OFF
        mode = self.hub.state.mode
        return MODE_TO_HVAC[mode] if mode is not None else HVACMode.OFF

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.hub.async_set_thermostat_power(
            self.mac_hex, hvac_mode != HVACMode.OFF
        )

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
