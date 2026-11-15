"""Exercise the real HomeKit adapter without advertising or pairing a bridge."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest
from homeassistant.components.homekit.const import CHAR_TARGET_HEATING_COOLING
from homeassistant.components.homekit.iidmanager import AccessoryIIDStorage
from homeassistant.components.homekit.type_thermostats import Thermostat
from pyhap.loader import get_loader

from custom_components.linking_the_world_temp_ha import climate as climate_module
from tests.native.test_entities import (
    _entity_id,
    _system_status,
    _thermostat_status,
    _wait_for,
)

PANEL = "ff00ffffffff01ff"
pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
async def homekit_room(hass, setup_integration, monkeypatch):
    """Use HA's real thermostat and HAP characteristics; mock only transport."""
    monkeypatch.setattr(climate_module, "ROOM_CONTROLS_REANNOUNCE_DELAY", 0.05)
    hub = setup_integration.hub
    await hub._async_status_received(_thermostat_status(hub, PANEL, power=False))
    await hass.async_block_till_done()
    entity_id = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{PANEL}_climate"
    )
    storage = AccessoryIIDStorage(hass, "homekit-room-test")
    await storage.async_initialize()
    driver = Mock(loader=get_loader(), iid_storage=storage)
    accessory = Thermostat(hass, driver, "Room", entity_id, 2, {})
    accessory.run()
    yield hub, accessory, driver
    accessory.async_stop()


def assert_modes(accessory):
    assert accessory.hc_homekit_to_hass == {0: "off", 3: "auto"}
    assert set(accessory.char_target_heat_cool.properties["ValidValues"].values()) == {0, 3}


async def test_homekit_off_auto_mapping_survives_offline_and_mode_changes(
    hass, homekit_room, caplog
):
    hub, accessory, _ = homekit_room
    assert not accessory.available
    assert_modes(accessory)
    for mode, expected in [(1, 3), (2, 3), (3, 0), (4, 0), (1, 3)]:
        await hub._async_status_received(_system_status(hub, power=True, mode=mode))
        await hub._async_status_received(_thermostat_status(hub, PANEL, power=True))
        await hass.async_block_till_done()
        assert accessory.available
        assert_modes(accessory)
        assert accessory.char_target_heat_cool.value == expected

    hub.protocol_verified = False
    hub.thermostats[PANEL].available = False
    hub._notify()
    await hass.async_block_till_done()
    assert not accessory.available
    assert_modes(accessory)
    await hub._async_status_received(_system_status(hub, power=True))
    await hass.async_block_till_done()
    assert not accessory.available
    await hub._async_status_received(_thermostat_status(hub, PANEL, power=True))
    await hass.async_block_till_done()
    assert accessory.available
    assert accessory.char_target_heat_cool.value == 3
    assert_modes(accessory)
    assert "Cannot map hvac target mode" not in caplog.text


async def test_settled_event_corrects_stale_homekit_mode_and_availability(
    hass, homekit_room
):
    """Model an out-of-date bridge cache, not an unproven Apple client failure."""
    hub, accessory, driver = homekit_room
    await hub._async_status_received(_system_status(hub, power=True))
    await hass.async_block_till_done()
    await asyncio.sleep(0.1)
    await hub._async_status_received(_thermostat_status(hub, PANEL, power=True))
    await hass.async_block_till_done()

    # Deliberately stale adapter values stand in for a missed transition event.
    accessory._available = False
    accessory.char_target_heat_cool.set_value(0, should_notify=False)
    driver.publish.reset_mock()
    await asyncio.sleep(0.1)
    await hass.async_block_till_done()

    assert accessory.available
    assert accessory.char_target_heat_cool.value == 3
    assert_modes(accessory)
    assert any(call.args[0]["value"] == 3 for call in driver.publish.call_args_list)
    driver.publish.reset_mock()
    await asyncio.sleep(0.15)
    assert not driver.publish.called


async def test_homekit_auto_request_only_switches_the_room_on(
    hass, homekit_room, fake_controller
):
    hub, accessory, _ = homekit_room
    await hub._async_status_received(_system_status(hub, power=False))
    await hass.async_block_till_done()
    before = len(fake_controller.received_frames)
    accessory._set_chars({CHAR_TARGET_HEATING_COOLING: 3})
    await hass.async_block_till_done()
    await _wait_for(lambda: len(fake_controller.received_frames) > before)

    pending = hub._pending[f"thermostat_{PANEL}"]
    assert pending.command == 2
    assert pending.expected == {"power": "ON"}
    assert "system" not in hub._pending
    assert hub.state.power == "OFF"
    await hub._async_status_received(_thermostat_status(hub, PANEL, power=True))
    await hass.async_block_till_done()
    assert accessory.char_target_heat_cool.value == 3
    assert_modes(accessory)
