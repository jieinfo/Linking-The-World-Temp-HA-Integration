"""Climate temperature API migration across supported Home Assistant versions."""

from __future__ import annotations

import importlib.util
import logging

import pytest
from homeassistant.components import climate
from homeassistant.components.climate import ClimateEntity
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM

from custom_components.linking_the_world_temp_ha.climate import RoomThermostat
from tests.native.test_entities import (
    _entity_id,
    _system_status,
    _thermostat_status,
    _wait_for,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
PANEL_MAC = "ff00ffffffff01ff"


async def test_native_temperature_properties_preserve_controller_celsius_values(
    setup_integration
):
    hub = setup_integration.hub
    await hub._async_status_received(_system_status(hub, power=True))
    await hub._async_status_received(
        _thermostat_status(hub, PANEL_MAC, target=24, current=26.4)
    )
    entity = RoomThermostat(hub, PANEL_MAC)

    assert entity.native_current_temperature == 26.4
    assert entity.native_target_temperature == 24
    assert entity.native_temperature_unit == UnitOfTemperature.CELSIUS
    assert entity.min_temp == 16
    assert entity.max_temp == 28
    assert entity.target_temperature_step == 1


@pytest.mark.parametrize(
    "units,requested,controller_target,display_current,display_target,minimum,maximum",
    [
        (METRIC_SYSTEM, 23.5, 23, 26.4, 23, 16, 28),
        (US_CUSTOMARY_SYSTEM, 77, 25, 79.5, 77, 60.8, 82.4),
    ],
)
async def test_temperature_state_and_service_use_one_native_unit_conversion(
    hass, setup_integration, fake_controller,
    units, requested, controller_target, display_current, display_target, minimum, maximum,
):
    """HA converts its UI unit once; MC7021 still receives integer Celsius."""
    hass.config.units = units
    hub = setup_integration.hub
    await hub._async_status_received(_system_status(hub, power=True))
    await hub._async_status_received(
        _thermostat_status(hub, PANEL_MAC, target=26, current=26.4)
    )
    await hass.async_block_till_done()
    entity_id = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{PANEL_MAC}_climate"
    )
    state = hass.states.get(entity_id)
    assert state.attributes["current_temperature"] == pytest.approx(display_current)
    assert state.attributes["min_temp"] == pytest.approx(minimum)
    assert state.attributes["max_temp"] == pytest.approx(maximum)
    if hasattr(ClimateEntity, "native_temperature_unit"):
        assert state.attributes["temperature_unit"] == units.temperature_unit

    await hass.services.async_call(
        climate.DOMAIN,
        climate.SERVICE_SET_TEMPERATURE,
        {"entity_id": entity_id, ATTR_TEMPERATURE: requested},
        blocking=True,
    )
    pending = hub._pending[f"thermostat_{PANEL_MAC}"]
    assert pending.value == controller_target * 2
    assert pending.expected == {"target_temperature": str(controller_target)}
    await fake_controller.async_send_status(
        _thermostat_status(hub, PANEL_MAC, target=controller_target, current=26.4)
    )
    await _wait_for(lambda: not hub._pending)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["temperature"] == pytest.approx(display_target)


@pytest.mark.skipif(
    not hasattr(ClimateEntity, "native_temperature_unit"),
    reason="Climate native temperature deprecations start in HA 2026.11",
)
def test_modern_climate_class_registration_has_no_temperature_deprecation(caplog):
    """Load a fresh class so the HA deprecation detector actually runs."""
    module_spec = importlib.util.find_spec(
        "custom_components.linking_the_world_temp_ha.climate"
    )
    spec = importlib.util.spec_from_file_location(
        "custom_components.linking_the_world_temp_ha._temperature_api_test",
        module_spec.origin,
    )
    module = importlib.util.module_from_spec(spec)
    with caplog.at_level(logging.WARNING):
        spec.loader.exec_module(module)
    assert not [
        record.getMessage()
        for record in caplog.records
        if "deprecated" in record.getMessage()
        and "temperature" in record.getMessage()
    ]
    assert "current_temperature" not in module.RoomThermostat.__dict__
    assert "target_temperature" not in module.RoomThermostat.__dict__
    assert "_attr_temperature_unit" not in module.RoomThermostat.__dict__
