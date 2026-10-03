"""Diagnostic and automation sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfDensity,
    UnitOfRatio,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .diagnostics import _command_state, _protocol_status
from .entity import LinkingTempEntity, LinkingThermostatEntity
from .hub import LinkingTempHub


def _fault_code_state(code: int | None) -> int | str | None:
    """Present zero as healthy while preserving every unknown nonzero code."""
    if code is None:
        return None
    return "healthy" if code == 0 else code


@dataclass(frozen=True)
class DiagnosticDescription:
    key: str
    translation_key: str
    value_fn: Callable[[LinkingTempHub], Any]


@dataclass(frozen=True)
class SystemStatusDescription:
    """Metadata for one read-only value from the 14-byte controller status."""

    key: str
    translation_key: str
    value_fn: Callable[[LinkingTempHub], Any]
    device_class: SensorDeviceClass | None = None
    native_unit: str | None = None
    state_class: SensorStateClass | None = None
    entity_category: EntityCategory | None = None
    icon: str | None = None


DIAGNOSTICS = (
    DiagnosticDescription(
        "connection_stage", "connection_stage", lambda hub: hub.health.stage.value
    ),
    DiagnosticDescription(
        "connection_error",
        "connection_error",
        lambda hub: hub.health.failure_kind.value,
    ),
    DiagnosticDescription(
        "protocol_status", "protocol_status", lambda hub: _protocol_status(hub.protocol_status)
    ),
    DiagnosticDescription(
        "control_permission", "control_permission", lambda hub: hub.control_permission
    ),
    DiagnosticDescription(
        "last_command", "last_command", lambda hub: _command_state(hub.last_command_status)
    ),
    DiagnosticDescription(
        "panel_count", "panel_count", lambda hub: len(hub.thermostats)
    ),
)

SYSTEM_STATUS_SENSORS = (
    SystemStatusDescription(
        "system_temperature",
        "system_temperature",
        lambda hub: hub.state.temperature,
        SensorDeviceClass.TEMPERATURE,
        UnitOfTemperature.CELSIUS,
        SensorStateClass.MEASUREMENT,
    ),
    SystemStatusDescription(
        "system_humidity",
        "system_humidity",
        lambda hub: hub.state.humidity,
        SensorDeviceClass.HUMIDITY,
        PERCENTAGE,
        SensorStateClass.MEASUREMENT,
    ),
    SystemStatusDescription(
        "system_pm25",
        "system_pm25",
        lambda hub: hub.state.pm25,
        SensorDeviceClass.PM25,
        UnitOfDensity.MICROGRAMS_PER_CUBIC_METER,
        SensorStateClass.MEASUREMENT,
    ),
    SystemStatusDescription(
        "system_co2",
        "system_co2",
        lambda hub: hub.state.co2,
        SensorDeviceClass.CO2,
        UnitOfRatio.PARTS_PER_MILLION,
        SensorStateClass.MEASUREMENT,
    ),
    SystemStatusDescription(
        "system_fault_code",
        "system_fault_code",
        lambda hub: _fault_code_state(hub.state.system_fault_code),
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:alert-circle-outline",
    ),
    SystemStatusDescription(
        "filter_fault_code",
        "filter_fault_code",
        lambda hub: _fault_code_state(hub.state.filter_fault_code),
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:air-filter",
    ),
)


class DiagnosticSensor(LinkingTempEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: LinkingTempHub, description: DiagnosticDescription) -> None:
        super().__init__(hub, description.key)
        self.description = description
        self._attr_translation_key = description.translation_key

    @property
    def native_value(self) -> Any:
        return self.description.value_fn(self.hub)

    @property
    def update_scopes(self) -> frozenset[str]:
        return frozenset({"diagnostics", "discovery"})

    @property
    def available(self) -> bool:
        return True


class SystemStatusSensor(LinkingTempEntity, SensorEntity):
    """Expose one controller-reported environment or raw fault value."""

    def __init__(
        self, hub: LinkingTempHub, description: SystemStatusDescription
    ) -> None:
        super().__init__(hub, description.key)
        self.description = description
        self._attr_translation_key = description.translation_key
        self._attr_device_class = description.device_class
        self._attr_native_unit_of_measurement = description.native_unit
        self._attr_state_class = description.state_class
        self._attr_entity_category = description.entity_category
        self._attr_icon = description.icon

    @property
    def native_value(self) -> Any:
        return self.description.value_fn(self.hub)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class AutomationTemperatureSensor(LinkingThermostatEntity, SensorEntity):
    _attr_translation_key = "automation_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub: LinkingTempHub, mac_hex: str) -> None:
        super().__init__(hub, mac_hex, "automation_temperature")

    @property
    def native_value(self) -> float | None:
        values = self.hub.filtered.get(self.mac_hex)
        return values.temperature if values else None


class AutomationHumiditySensor(LinkingThermostatEntity, SensorEntity):
    _attr_translation_key = "automation_humidity"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub: LinkingTempHub, mac_hex: str) -> None:
        super().__init__(hub, mac_hex, "automation_humidity")

    @property
    def native_value(self) -> int | None:
        values = self.hub.filtered.get(self.mac_hex)
        return values.humidity if values else None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: LinkingTempHub = entry.runtime_data.hub
    async_add_entities(
        [DiagnosticSensor(hub, description) for description in DIAGNOSTICS]
        + [SystemStatusSensor(hub, description) for description in SYSTEM_STATUS_SENSORS]
    )
    added: set[str] = set()

    @callback
    def add_new_entities() -> None:
        new_macs = [mac for mac in hub.thermostats if mac not in added]
        if not new_macs:
            return
        added.update(new_macs)
        entities: list[SensorEntity] = []
        for mac in new_macs:
            entities.extend(
                [
                    AutomationTemperatureSensor(hub, mac),
                    AutomationHumiditySensor(hub, mac),
                ]
            )
        async_add_entities(entities)

    add_new_entities()
    entry.async_on_unload(
        hub.async_add_listener(add_new_entities, scopes={"discovery"})
    )
