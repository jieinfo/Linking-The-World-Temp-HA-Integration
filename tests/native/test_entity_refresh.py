"""Verify push updates only write entities affected by the report."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.linking_the_world_temp_ha import climate as climate_module
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


async def test_room_control_reannounce_refreshes_only_climates(
    hass, refresh_capture
):
    """A post-setup reannounce gives HomeKit fresh climate capabilities only."""
    hub, writes = refresh_capture
    writes.clear()
    hub.async_refresh_room_controls()
    await hass.async_block_till_done()

    assert f"thermostat_{PANEL_ONE}_climate" in writes
    assert f"thermostat_{PANEL_TWO}_climate" in writes
    assert not any("automation_" in key for key in writes)
    assert "system_temperature" not in writes


async def test_system_control_change_reannounces_climates_after_stable_state(
    hass, refresh_capture, monkeypatch
):
    """A settled total-control update gives HomeKit a second room state."""
    monkeypatch.setattr(climate_module, "ROOM_CONTROLS_REANNOUNCE_DELAY", 0.05)
    hub, writes = refresh_capture
    writes.clear()

    events = []
    climate_ids = [
        _entity_id(hass, hub.entry.entry_id, "climate", f"thermostat_{mac}_climate")
        for mac in (PANEL_ONE, PANEL_TWO)
    ]
    remove = async_track_state_change_event(hass, climate_ids, events.append)

    await hub._async_status_received(_system_status(hub, power=False))
    await hass.async_block_till_done()
    writes.clear()
    events.clear()

    await asyncio.sleep(0.1)
    await hass.async_block_till_done()
    remove()

    assert {event.data["entity_id"] for event in events} == set(climate_ids)
    assert [key for key in writes if key.endswith("_climate")] == [
        f"thermostat_{PANEL_ONE}_climate",
        f"thermostat_{PANEL_TWO}_climate",
    ]
    assert not any("automation_" in key for key in writes)


async def test_reconnect_reannounces_climates_after_verified_system_state(
    hass, refresh_capture, monkeypatch
):
    """A first verified state after reconnect refreshes room climates once."""
    monkeypatch.setattr(climate_module, "ROOM_CONTROLS_REANNOUNCE_DELAY", 0.05)
    hub, writes = refresh_capture
    writes.clear()

    hub.protocol_verified = False
    hub._notify()
    await hub._async_status_received(_system_status(hub, power=True))
    await hass.async_block_till_done()
    writes.clear()

    await asyncio.sleep(0.1)
    await hass.async_block_till_done()

    assert [key for key in writes if key.endswith("_climate")] == [
        f"thermostat_{PANEL_ONE}_climate",
        f"thermostat_{PANEL_TWO}_climate",
    ]
    assert not any("automation_" in key for key in writes)


async def test_repeated_panel_measurements_do_not_force_state_events(
    hass, refresh_capture
):
    """Steady telemetry must not create repeated automation/history events."""
    hub, _ = refresh_capture
    await asyncio.sleep(1.1)
    await hass.async_block_till_done()
    entity_id = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{PANEL_ONE}_climate"
    )
    events = []
    remove = async_track_state_change_event(hass, [entity_id], events.append)
    for _ in range(3):
        await hub._async_status_received(_thermostat_status(hub, PANEL_ONE))
    await hass.async_block_till_done()
    remove()
    assert events == []


async def test_panel_power_change_reannounces_only_that_climate(
    hass, refresh_capture
):
    """A room switch gets one settled event even if its state is unchanged."""
    hub, writes = refresh_capture
    await asyncio.sleep(1.1)
    await hass.async_block_till_done()
    await hub._async_status_received(_thermostat_status(hub, PANEL_ONE, power=False))
    await hass.async_block_till_done()
    writes.clear()
    entity_id = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{PANEL_ONE}_climate"
    )
    events = []
    remove = async_track_state_change_event(hass, [entity_id], events.append)
    await asyncio.sleep(1.1)
    await hass.async_block_till_done()
    remove()
    assert writes == [f"thermostat_{PANEL_ONE}_climate"]
    assert len(events) == 1
    assert events[0].data["new_state"].state == "off"
    assert events[0].data["new_state"].attributes["hvac_modes"] == ["off", "auto"]


@pytest.fixture
async def controlled_reannounces(hass, refresh_capture, monkeypatch):
    """Control only the Climate timer; keep real HA entity writes and events."""
    await asyncio.sleep(1.1)
    await hass.async_block_till_done()
    timers = []

    def schedule(_hass, delay, action):
        timer = Mock(action=action, delay=delay)
        timers.append(timer)
        return timer.cancel

    monkeypatch.setattr(climate_module, "async_call_later", schedule)
    return *refresh_capture, timers


async def test_control_reannounce_coalesces_to_latest_room_state(
    hass, controlled_reannounces
):
    """Rapid control changes leave one delayed notification for the final state."""
    hub, writes, timers = controlled_reannounces
    await hub._async_status_received(_system_status(hub, power=False))
    await hass.async_block_till_done()
    await hub._async_status_received(_system_status(hub, power=True, mode=2))
    await hass.async_block_till_done()
    writes.clear()
    assert len(timers) == 4
    for timer in timers[:2]:
        timer.cancel.assert_called_once_with()
    for timer in timers[2:]:
        timer.cancel.assert_not_called()
        timer.action(datetime.now(UTC))
    await hass.async_block_till_done()

    assert [key for key in writes if key.endswith("_climate")] == [
        f"thermostat_{PANEL_ONE}_climate",
        f"thermostat_{PANEL_TWO}_climate",
    ]
    assert hass.states.get(
        _entity_id(hass, hub.entry.entry_id, "climate", f"thermostat_{PANEL_ONE}_climate")
    ).state == "auto"


async def test_telemetry_does_not_postpone_pending_control_reannounce(
    hass, controlled_reannounces
):
    """Routine panel reports do not move a pending control reannounce deadline."""
    hub, writes, timers = controlled_reannounces
    await hub._async_status_received(_system_status(hub, power=False))
    await hass.async_block_till_done()
    for _ in range(3):
        await hub._async_status_received(_thermostat_status(hub, PANEL_ONE))
        await hass.async_block_till_done()
    writes.clear()
    assert len(timers) == 2
    for timer in timers:
        timer.cancel.assert_not_called()
        timer.action(datetime.now(UTC))
    await hass.async_block_till_done()
    assert [key for key in writes if key.endswith("_climate")] == [
        f"thermostat_{PANEL_ONE}_climate",
        f"thermostat_{PANEL_TWO}_climate",
    ]


@pytest.mark.parametrize("mode", [3, 4])
async def test_panel_power_change_in_forced_off_mode_still_reannounces_off(
    hass, controlled_reannounces, mode
):
    hub, writes, timers = controlled_reannounces
    await hub._async_status_received(_system_status(hub, power=True, mode=mode))
    await hass.async_block_till_done()
    for timer in timers:
        timer.action(datetime.now(UTC))
    timers.clear()
    await hub._async_status_received(_thermostat_status(hub, PANEL_ONE, power=False))
    await hass.async_block_till_done()
    writes.clear()
    assert len(timers) == 1
    timers[0].action(datetime.now(UTC))
    await hass.async_block_till_done()
    assert writes == [f"thermostat_{PANEL_ONE}_climate"]
    state = hass.states.get(
        _entity_id(hass, hub.entry.entry_id, "climate", f"thermostat_{PANEL_ONE}_climate")
    )
    assert state.state == "off"
    assert state.attributes["hvac_modes"] == ["off", "auto"]


async def test_control_reannounce_is_cancelled_when_room_goes_unavailable(
    hass, controlled_reannounces
):
    """A disconnect before the delay must not emit a fake recovered state."""
    hub, writes, timers = controlled_reannounces
    await hub._async_status_received(_system_status(hub, power=False))
    await hass.async_block_till_done()
    hub.protocol_verified = False
    hub._notify()
    await hass.async_block_till_done()
    writes.clear()

    assert len(timers) == 2
    for timer in timers:
        timer.cancel.assert_called_once_with()
        # Even an already queued callback must not republish an offline room.
        timer.action(datetime.now(UTC))
    await hass.async_block_till_done()
    assert writes == []
    assert hass.states.get(
        _entity_id(hass, hub.entry.entry_id, "climate", f"thermostat_{PANEL_ONE}_climate")
    ).state == "unavailable"


async def test_unload_cancels_pending_room_reannounces(hass, controlled_reannounces):
    hub, writes, timers = controlled_reannounces
    await hub._async_status_received(_system_status(hub, power=False))
    await hass.async_block_till_done()
    assert len(timers) == 2
    assert await hass.config_entries.async_unload(hub.entry.entry_id)
    await hass.async_block_till_done()
    for timer in timers:
        timer.cancel.assert_called_once_with()
    assert not hub._listeners
    writes.clear()
    hub._notify()
    assert writes == []


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
    assert hass.states.get(climate_id).state == "auto"
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
