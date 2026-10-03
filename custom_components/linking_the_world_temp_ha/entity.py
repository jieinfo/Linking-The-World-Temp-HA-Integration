"""Shared entity models for Linking The World Temp HA."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .hub import LinkingTempHub


class LinkingTempEntity(Entity):
    """Base push entity attached to the MC7021 controller."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: LinkingTempHub, key: str) -> None:
        self.hub = hub
        self._attr_unique_id = f"{hub.entry.entry_id}_{key}"

    @property
    def available(self) -> bool:
        return self.hub.available

    @property
    def update_scopes(self) -> frozenset[str]:
        return frozenset({"system"})

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.hub.entry.entry_id)},
            manufacturer="Moorgen",
            model="MC7021",
            name="科技系统总控",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.hub.async_add_listener(
                self.async_write_ha_state, scopes=self.update_scopes
            )
        )


class LinkingThermostatEntity(LinkingTempEntity):
    """Base push entity attached to one room panel."""

    def __init__(self, hub: LinkingTempHub, mac_hex: str, key: str) -> None:
        super().__init__(hub, f"thermostat_{mac_hex}_{key}")
        self.mac_hex = mac_hex

    @property
    def available(self) -> bool:
        thermostat = self.hub.thermostats.get(self.mac_hex)
        return bool(self.hub.available and thermostat and thermostat.available)

    @property
    def update_scopes(self) -> frozenset[str]:
        return frozenset({f"thermostat_{self.mac_hex}"})

    @property
    def device_info(self) -> DeviceInfo:
        thermostat = self.hub.thermostats[self.mac_hex]
        if self.hub.controller_device_id is None:
            raise RuntimeError("Controller device was not registered before its panels")
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.hub.entry.entry_id}_{self.mac_hex}")},
            manufacturer="Moorgen",
            model="六恒房间温控面板",
            name=self.hub.thermostat_name(thermostat),
            via_device_id=self.hub.controller_device_id,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hub.async_add_listener(
                self._update_device_name, scopes={f"thermostat_{self.mac_hex}"}
            )
        )
        self._update_device_name()

    @callback
    def _update_device_name(self) -> None:
        """Apply a room name that may arrive after the entity was registered."""
        registry = dr.async_get(self.hass)
        try:
            device_id = dr.async_get_device_id_by_identifier(
                self.hass,
                (DOMAIN, f"{self.hub.entry.entry_id}_{self.mac_hex}"),
                config_entry_id=self.hub.entry.entry_id,
            )
        except ValueError:
            return
        device = registry.async_get(device_id)
        if device is None:
            return
        name = self.hub.thermostat_name(self.hub.thermostats[self.mac_hex])
        if device.name != name:
            registry.async_update_device(device.id, name=name)
