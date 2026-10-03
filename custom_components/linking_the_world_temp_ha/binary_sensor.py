"""Binary sensors for Linking The World Temp HA."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import LinkingTempEntity
from .hub import LinkingTempHub


class ControllerConnectionSensor(LinkingTempEntity, BinarySensorEntity):
    _attr_translation_key = "controller_connection"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: LinkingTempHub) -> None:
        super().__init__(hub, "controller_connection")

    @property
    def is_on(self) -> bool:
        return self.hub.available

    @property
    def update_scopes(self) -> frozenset[str]:
        return frozenset({"diagnostics"})

    @property
    def available(self) -> bool:
        return True


class ProtocolVerifiedSensor(LinkingTempEntity, BinarySensorEntity):
    _attr_translation_key = "protocol_verified"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: LinkingTempHub) -> None:
        super().__init__(hub, "protocol_verified")

    @property
    def is_on(self) -> bool:
        return self.hub.protocol_verified

    @property
    def update_scopes(self) -> frozenset[str]:
        return frozenset({"diagnostics"})

    @property
    def available(self) -> bool:
        return True


class ControllerFaultSensor(LinkingTempEntity, BinarySensorEntity):
    """Expose one controller fault with its raw code as diagnostic context."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, hub: LinkingTempHub, fault_type: str) -> None:
        super().__init__(hub, f"{fault_type}_fault")
        self.fault_type = fault_type
        self._attr_translation_key = f"{fault_type}_fault"

    @property
    def raw_code(self) -> int | None:
        if self.fault_type == "system":
            return self.hub.state.system_fault_code
        return self.hub.state.filter_fault_code

    @property
    def is_on(self) -> bool | None:
        code = self.raw_code
        return None if code is None else code != 0

    @property
    def available(self) -> bool:
        return super().available and self.raw_code is not None

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: LinkingTempHub = entry.runtime_data.hub
    async_add_entities(
        [
            ControllerConnectionSensor(hub),
            ProtocolVerifiedSensor(hub),
            ControllerFaultSensor(hub, "system"),
            ControllerFaultSensor(hub, "filter"),
        ]
    )
