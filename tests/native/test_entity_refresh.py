"""Verify push updates only write entities affected by the report."""

from __future__ import annotations

import asyncio
import time

import pytest
from homeassistant.helpers.entity import Entity

from custom_components.linking_the_world_temp_ha.protocol import (
    YasHcpFrame,
    parse_tlvs,
    tlv,
)
from tests.native.test_entities import (
    _entity_id,
    _system_status,
    _thermostat_status,
    _wait_for,
)

PANEL_ONE = "ff00ffffffff01ff"
PANEL_TWO = "ff00ffffffff02ff"
pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
async def refresh_capture(hass, setup_integration, monkeypatch):
    """Observe real entity writes after two panels have been discovered."""
    hub = setup_integration.hub
    await hub._async_status_received(_system_status(hub, power=True))
    await hub._async_status_received(_thermostat_status(hub, PANEL_ONE, room_id="r0100"))
    await hub._async_status_received(_thermostat_status(hub, PANEL_TWO, room_id="r0200"))
    await hass.async_block_till_done()
    writes: list[str] = []
    original = Entity._async_write_ha_state

    def record_write(entity):
        if entity.unique_id and entity.unique_id.startswith(hub.entry.entry_id):
            writes.append(entity.unique_id.removeprefix(f"{hub.entry.entry_id}_"))
        original(entity)

    monkeypatch.setattr(Entity, "_async_write_ha_state", record_write)
    return hub, writes


async def test_panel_report_does_not_refresh_other_devices(hass, refresh_capture):
    hub, writes = refresh_capture
    await hub._async_status_received(_thermostat_status(hub, PANEL_ONE, current=26.1))
    await hass.async_block_till_done()

    assert set(writes) == {
        f"thermostat_{PANEL_ONE}_climate",
        f"thermostat_{PANEL_ONE}_automation_temperature",
        f"thermostat_{PANEL_ONE}_automation_humidity",
    }


async def test_environment_report_does_not_refresh_room_entities(hass, refresh_capture):
    hub, writes = refresh_capture
    await hub._async_status_received(_system_status(hub, power=True, temperature=30.1))
    await hass.async_block_till_done()

    assert "system_temperature" in writes
    assert not any(key.startswith("thermostat_") for key in writes)
    assert "last_command" not in writes


@pytest.mark.parametrize("power,mode", [(False, 1), (True, 2), (True, 3), (True, 4)])
async def test_system_control_change_refreshes_climates_not_room_sensors(
    hass, refresh_capture, power, mode
):
    hub, writes = refresh_capture
    await hub._async_status_received(_system_status(hub, power=power, mode=mode))
    await hass.async_block_till_done()

    assert f"thermostat_{PANEL_ONE}_climate" in writes
    assert f"thermostat_{PANEL_TWO}_climate" in writes
    assert not any("automation_" in key for key in writes)


async def test_repeated_system_report_keeps_freshness_without_entity_writes(
    hass, refresh_capture
):
    hub, writes = refresh_capture
    previous = hub._last_system_status_at
    previous_any = hub._last_valid_status_at
    hub._system_status_query_at = time.monotonic()
    await hub._async_status_received(_system_status(hub, power=True))
    await hass.async_block_till_done()

    assert hub._last_system_status_at > previous
    assert hub._last_valid_status_at > previous_any
    assert hub._system_status_query_at is None
    assert writes == []


async def test_room_name_report_refreshes_only_that_room(hass, refresh_capture):
    hub, writes = refresh_capture
    await hub._async_frame_received(
        YasHcpFrame(3, 8, 1, tlv(0x0030, b"r0100") + tlv(0x0036, b"Living room"))
    )
    await hass.async_block_till_done()

    assert writes
    assert all(key.startswith(f"thermostat_{PANEL_ONE}_") for key in writes)


async def test_pending_command_refreshes_diagnostics_not_unconfirmed_room_state(
    hass, refresh_capture
):
    hub, writes = refresh_capture
    await hub.async_set_thermostat_temperature(PANEL_ONE, 23)
    await hass.async_block_till_done()

    assert "last_command" in writes
    assert not any(key.startswith("thermostat_") for key in writes)
    writes.clear()
    await hub._async_status_received(_thermostat_status(hub, PANEL_ONE, target=23))
    await hass.async_block_till_done()
    assert hub.last_command_status.startswith("confirmed:")
    assert "last_command" in writes
    assert f"thermostat_{PANEL_ONE}_climate" in writes
    assert not any(PANEL_TWO in key for key in writes)


async def test_pending_system_power_updates_mode_hint_before_confirmation(
    hass, refresh_capture
):
    hub, writes = refresh_capture
    entity_id = _entity_id(hass, hub.entry.entry_id, "select", "system_mode")
    assert hass.states.get(entity_id).attributes["can_change_mode"]
    await hub.async_set_system_power(False)
    await hass.async_block_till_done()

    assert not hass.states.get(entity_id).attributes["can_change_mode"]
    assert "system_mode" in writes
    assert not any(key.startswith("thermostat_") for key in writes)
    await hub._async_status_received(_system_status(hub, power=False))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["can_change_mode"]


async def test_completed_mode_transition_restores_hint_without_another_report(
    hass, refresh_capture, fake_controller
):
    hub, _ = refresh_capture
    mode = 1
    power = True

    async def acknowledge(frame):
        nonlocal mode, power
        fields = parse_tlvs(frame.body)
        command = fields.get(0x0009)
        if command == b"\x01":
            power = False
        elif command == b"\x02":
            power = True
        elif command == b"\x03":
            mode = 2
        await fake_controller.async_send_status(_system_status(hub, power=power, mode=mode))

    fake_controller.on_command = acknowledge
    await hub.async_select_mode("heat")
    await hass.async_block_till_done()
    entity_id = _entity_id(hass, hub.entry.entry_id, "select", "system_mode")
    assert hub.can_change_system_mode
    assert hass.states.get(entity_id).attributes["can_change_mode"]


async def test_single_panel_timeout_does_not_refresh_other_devices(hass, refresh_capture):
    hub, writes = refresh_capture
    now = time.monotonic()
    hub.thermostats[PANEL_ONE].last_seen = now - hub.thermostat_offline_after - 1
    await hub._async_refresh_thermostat_availability(now)
    await hass.async_block_till_done()

    assert not hub.thermostats[PANEL_ONE].available
    assert hub.thermostats[PANEL_TWO].available
    assert writes
    assert all(key.startswith(f"thermostat_{PANEL_ONE}_") for key in writes)


async def test_disconnect_and_first_verified_report_refresh_all_entities(
    hass, refresh_capture, fake_controller
):
    hub, writes = refresh_capture
    previous_client = hub._client
    connection_id = _entity_id(hass, hub.entry.entry_id, "binary_sensor", "controller_connection")
    climate_id = _entity_id(hass, hub.entry.entry_id, "climate", f"thermostat_{PANEL_ONE}_climate")
    other_id = _entity_id(hass, hub.entry.entry_id, "climate", f"thermostat_{PANEL_TWO}_climate")
    await fake_controller.async_close_client()
    await _wait_for(lambda: not hub.connected, timeout=2)
    await _wait_for(lambda: "system_power" in writes, timeout=2)
    await hass.async_block_till_done()
    assert "system_power" in writes
    assert "controller_connection" in writes
    assert f"thermostat_{PANEL_TWO}_automation_temperature" in writes
    assert hass.states.get(connection_id).state == "off"
    assert hass.states.get(climate_id).state == "unavailable"
    await _wait_for(lambda: hub.connected and hub._client is not previous_client, timeout=8)
    assert not hub.available
    writes.clear()
    await fake_controller.async_send_status(_system_status(hub, power=True))
    await _wait_for(lambda: hub.available)
    await hass.async_block_till_done()
    assert "controller_connection" in writes
    assert "protocol_verified" in writes
    assert f"thermostat_{PANEL_TWO}_climate" in writes
    assert hass.states.get(connection_id).state == "on"
    assert hass.states.get(climate_id).state == "unavailable"
    await fake_controller.async_send_status(_thermostat_status(hub, PANEL_ONE))
    await _wait_for(lambda: hub.thermostats[PANEL_ONE].available)
    await hass.async_block_till_done()
    assert hass.states.get(climate_id).state == "cool"
    assert hass.states.get(other_id).state == "unavailable"


async def test_reload_removes_old_listeners_and_restores_scoped_updates(
    hass, refresh_capture
):
    hub, writes = refresh_capture
    assert await hass.config_entries.async_reload(hub.entry.entry_id)
    await hass.async_block_till_done()
    assert hub._listeners == {}
    writes.clear()
    hub._notify()
    assert writes == []
    reloaded = hass.config_entries.async_get_entry(hub.entry.entry_id).runtime_data.hub
    assert reloaded is not hub
    await _wait_for(lambda: reloaded.connected, timeout=3)
    await reloaded._async_status_received(_system_status(reloaded, power=True))
    await reloaded._async_status_received(_thermostat_status(reloaded, PANEL_ONE))
    await hass.async_block_till_done()
    writes.clear()
    await reloaded._async_status_received(_thermostat_status(reloaded, PANEL_ONE, target=23))
    await hass.async_block_till_done()
    assert writes
    assert all(key.startswith(f"thermostat_{PANEL_ONE}_") for key in writes)


async def test_cancelled_transition_removes_waiter_and_updates_hint_on_confirmation(
    hass, refresh_capture
):
    hub, _ = refresh_capture
    listeners = set(hub._listeners)
    transition = asyncio.create_task(hub.async_select_mode("heat"))
    await _wait_for(lambda: len(hub._listeners) > len(listeners))
    transition.cancel()
    with pytest.raises(asyncio.CancelledError):
        await transition
    assert set(hub._listeners) == listeners
    assert not hub._mode_transition_lock.locked()
    await hub._async_status_received(_system_status(hub, power=False))
    await hass.async_block_till_done()
    entity_id = _entity_id(hass, hub.entry.entry_id, "select", "system_mode")
    assert hass.states.get(entity_id).attributes["can_change_mode"]


async def test_new_panel_discovery_does_not_refresh_existing_panels(hass, refresh_capture):
    hub, writes = refresh_capture
    new_mac = "ff00ffffffff03ff"
    await hub._async_status_received(_thermostat_status(hub, new_mac))
    await hass.async_block_till_done()

    assert "panel_count" in writes
    assert f"thermostat_{new_mac}_climate" in writes
    assert not any(PANEL_ONE in key or PANEL_TWO in key for key in writes)


async def test_scoped_listener_removal_preserves_global_transaction_listeners(
    refresh_capture
):
    hub, _ = refresh_capture
    calls: list[str] = []
    remove = hub.async_add_listener(lambda: calls.append("panel"), scopes={"panel"})
    remove_global = hub.async_add_listener(lambda: calls.append("transaction"))
    hub._notify("other")
    assert calls == ["transaction"]
    calls.clear()
    hub._notify("panel", "other")
    assert set(calls) == {"panel", "transaction"}
    assert len(calls) == 2
    remove()
    remove_global()
    calls.clear()
    hub._notify()
    assert calls == []
