"""Entity restoration, lifecycle, and real-HA entity tests."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.components import climate, select, switch
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

import custom_components.linking_the_world_temp_ha.hub as hub_module
from custom_components.linking_the_world_temp_ha.climate import RoomThermostat
from custom_components.linking_the_world_temp_ha.command_queue import QueuedCommand
from custom_components.linking_the_world_temp_ha.const import DOMAIN
from custom_components.linking_the_world_temp_ha.entity import LinkingThermostatEntity
from custom_components.linking_the_world_temp_ha.health import HealthTracker
from custom_components.linking_the_world_temp_ha.hub import LinkingTempHub
from custom_components.linking_the_world_temp_ha.panel_registry import PanelRegistry
from custom_components.linking_the_world_temp_ha.protocol import (
    ThermostatState,
    YasHcpFrame,
    parse_tlvs,
    tlv,
)
from custom_components.linking_the_world_temp_ha.runtime import ConnectionStage
from custom_components.linking_the_world_temp_ha.select import SystemModeSelect
from custom_components.linking_the_world_temp_ha.switch import (
    SystemPowerSwitch,
    WinterHumidifierSwitch,
)
from custom_components.linking_the_world_temp_ha.switch import (
    async_setup_entry as async_setup_switches,
)


async def _wait_for(predicate, *, timeout: float = 1) -> None:
    """Wait for a push-driven Home Assistant state without fixed sleeps."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


def _system_status(
    hub: LinkingTempHub,
    *,
    power: bool,
    mode: int = 1,
    scene: int = 1,
    humidifier: bool = False,
    energy_saving: bool = False,
    temperature: float = 32.9,
    humidity: int = 62,
    pm25: float = 5.0,
    co2: int = 496,
    system_fault_code: int = 0,
    filter_fault_code: int = 0,
) -> bytes:
    """Build one verified total-control report using the captured TLV shape."""
    packed = bytes((mode, scene, humidifier, energy_saving))
    packed += round(temperature * 10).to_bytes(2, "little")
    packed += humidity.to_bytes(2, "little")
    packed += round(pm25 * 10).to_bytes(2, "little")
    packed += co2.to_bytes(2, "little")
    packed += bytes((system_fault_code, filter_fault_code))
    return (
        tlv(0x0004, hub.tech_system_mac)
        + tlv(0x000B, bytes((power,)))
        + tlv(0x000A, packed)
    )


def _thermostat_status(
    hub: LinkingTempHub,
    mac_hex: str,
    *,
    room_id: str = "r0100",
    target: int = 22,
    current: float = 24.6,
    humidity: int = 58,
    power: bool = True,
) -> bytes:
    """Build one verified room-panel status report."""
    current_tenths = round(current * 10)
    return (
        tlv(0x0004, bytes.fromhex(mac_hex))
        + tlv(0x0075, hub.tech_system_mac)
        + tlv(0x0030, room_id.encode())
        + tlv(
            0x000A,
            bytes((target * 2, current_tenths & 0xFF, current_tenths >> 8, humidity, 0)),
        )
        + tlv(0x000B, bytes((power,)))
    )


def _entity_id(hass, entry_id: str, platform: str, unique_key: str) -> str:
    """Resolve an entity by the integration's stable public unique ID."""
    entity_id = er.async_get(hass).async_get_entity_id(
        platform,
        "linking_the_world_temp_ha",
        f"{entry_id}_{unique_key}",
    )
    assert entity_id is not None
    return entity_id


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_room_panel_device_info_uses_registered_parent_device_id(
    hass, setup_integration, fake_controller, monkeypatch
):
    """A room panel links to the exact controller device registered for its entry."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    monkeypatch.setattr(
        dr.async_get(hass),
        "async_get_device",
        lambda *_args, **_kwargs: pytest.fail(
            "room-name updates used the deprecated ambiguous device lookup"
        ),
    )
    await fake_controller.async_send_status(_system_status(hub, power=True))
    await fake_controller.async_send_status(_thermostat_status(hub, mac_hex))
    await _wait_for(lambda: mac_hex in hub.thermostats)

    device_info = LinkingThermostatEntity(hub, mac_hex, "relationship").device_info

    assert device_info["via_device_id"] == hub.controller_device_id
    assert "via_device" not in device_info

    hub.controller_device_id = None
    with pytest.raises(RuntimeError, match="Controller device was not registered"):
        LinkingThermostatEntity(hub, mac_hex, "missing_parent").device_info


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_system_switch_service_round_trips_through_controller_push_ack(
    hass, setup_integration, fake_controller
):
    """A switch service call stays pending only until a real push acknowledgement."""
    runtime = setup_integration
    hub = runtime.hub
    await fake_controller.async_send_status(_system_status(hub, power=False))
    await _wait_for(lambda: hub.available and hub.state.power == "OFF")

    async def acknowledge_command(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != hub.tech_system_mac:
            return
        if fields.get(0x0009) == b"\x02":
            await fake_controller.async_send_status(_system_status(hub, power=True))

    # The fake controller deliberately exposes a command hook so real HA service
    # tests can use deterministic controller-originated status acknowledgements.
    fake_controller.on_command = acknowledge_command
    entity_id = _entity_id(hass, hub.entry.entry_id, "switch", "system_power")
    await hass.services.async_call(
        switch.DOMAIN, switch.SERVICE_TURN_ON, {"entity_id": entity_id}, blocking=True
    )

    await _wait_for(lambda: hub.state.power == "ON")
    assert hub.last_command_status.startswith("confirmed:")
    assert not hub._pending


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_thermostat_power_service_skips_an_already_applied_state(
    hass, setup_integration, fake_controller
):
    """A repeated panel power request cannot wait for an acknowledgement that will not come."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, power=True)
    )
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)
    await hass.async_block_till_done()

    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )
    command_count = sum(
        frame.kind == 4 and frame.opcode == 9
        for frame in fake_controller.received_frames
    )

    await hass.services.async_call(
        climate.DOMAIN,
        climate.SERVICE_SET_HVAC_MODE,
        {"entity_id": climate_entity, "hvac_mode": "heat_cool"},
        blocking=True,
    )

    assert sum(
        frame.kind == 4 and frame.opcode == 9
        for frame in fake_controller.received_frames
    ) == command_count
    assert f"thermostat_{mac_hex}" not in hub._pending
    assert hub.last_command_status.startswith("confirmed:")


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_room_panel_can_turn_on_while_system_power_remains_off(
    hass, setup_integration, fake_controller
):
    """A room panel is independently switchable after the total system stops."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(hub, power=False, mode=1))
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, power=False)
    )
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)
    await hass.async_block_till_done()

    commands: list[tuple[bytes | None, int]] = []

    async def acknowledge_panel_power_on(frame) -> None:
        fields = parse_tlvs(frame.body)
        command = fields.get(0x0009, b"\x00")[0]
        commands.append((fields.get(0x0004), command))
        if fields.get(0x0004) == bytes.fromhex(mac_hex) and command == 2:
            await fake_controller.async_send_status(
                _thermostat_status(hub, mac_hex, power=True)
            )

    fake_controller.on_command = acknowledge_panel_power_on
    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )
    state = hass.states.get(climate_entity)
    assert state is not None
    assert state.attributes["hvac_modes"] == ["off", "heat_cool"]

    await hass.services.async_call(
        climate.DOMAIN,
        climate.SERVICE_SET_HVAC_MODE,
        {"entity_id": climate_entity, "hvac_mode": "heat_cool"},
        blocking=True,
    )

    await _wait_for(lambda: hub.thermostats[mac_hex].power == "ON")
    assert hub.state.power == "OFF"
    assert commands == [(bytes.fromhex(mac_hex), 2)]
    state = hass.states.get(climate_entity)
    assert state is not None
    assert state.state == "heat_cool"


@pytest.mark.parametrize(
    "unsupported_mode",
    [HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY, HVACMode.DRY],
)
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_room_climate_delegates_cooling_and_heating_to_system_mode(
    hass, setup_integration, fake_controller, unsupported_mode
):
    """Room Climate exposes power only and never changes the system mode."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, power=False)
    )
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)
    await hass.async_block_till_done()

    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )
    state = hass.states.get(climate_entity)
    assert state is not None
    assert state.attributes["hvac_modes"] == ["off", "heat_cool"]
    assert state.state == "off"

    room_entity = RoomThermostat(hub, mac_hex)
    received_before = len(fake_controller.received_frames)
    with pytest.raises(HomeAssistantError, match="由科技系统总控选择"):
        await room_entity.async_set_hvac_mode(unsupported_mode)
    assert len(fake_controller.received_frames) == received_before
    assert hub.state.mode == "cool"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_room_climate_stays_off_until_system_state_is_verified(
    hass, setup_integration, fake_controller
):
    """An unverified controller cannot enable a room panel."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, power=False)
    )
    await _wait_for(lambda: mac_hex in hub.thermostats)
    await hass.async_block_till_done()

    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )
    state = hass.states.get(climate_entity)
    assert state is not None
    assert state.state == "unavailable"
    assert state.attributes["hvac_modes"] == ["off", "heat_cool"]

    room_entity = RoomThermostat(hub, mac_hex)
    assert room_entity.hvac_mode == HVACMode.OFF
    received_before = len(fake_controller.received_frames)
    with pytest.raises(HomeAssistantError, match="只能跟随当前科技系统模式"):
        await room_entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    assert len(fake_controller.received_frames) == received_before


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_opposite_thermostat_power_request_is_applied_after_pending_ack(
    hass, setup_integration, fake_controller
):
    """The final panel power intent survives an opposite command awaiting confirmation."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, power=False)
    )
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)
    await hass.async_block_till_done()

    power_commands: list[int] = []

    async def acknowledge_only_power_off(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != bytes.fromhex(mac_hex):
            return
        command = fields.get(0x0009, b"\x00")[0]
        power_commands.append(command)
        if command == 1:
            await fake_controller.async_send_status(
                _thermostat_status(hub, mac_hex, power=False)
            )

    fake_controller.on_command = acknowledge_only_power_off
    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )
    await hass.services.async_call(
        climate.DOMAIN,
        climate.SERVICE_SET_HVAC_MODE,
        {"entity_id": climate_entity, "hvac_mode": "heat_cool"},
        blocking=True,
    )
    await _wait_for(lambda: power_commands == [2])

    await hass.services.async_call(
        climate.DOMAIN,
        climate.SERVICE_SET_HVAC_MODE,
        {"entity_id": climate_entity, "hvac_mode": "off"},
        blocking=True,
    )
    target = f"thermostat_{mac_hex}"
    assert hub._queued[target][0].expected == {"power": "OFF"}

    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, power=True)
    )
    await _wait_for(
        lambda: power_commands == [2, 1]
        and hub.thermostats[mac_hex].power == "OFF"
        and target not in hub._pending
        and target not in hub._queued,
        timeout=2,
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_queued_panel_enable_rechecks_the_operating_mode_before_send(
    hass, setup_integration, fake_controller
):
    """An enable queued in cooling cannot bypass a later dehumidify interlock."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, target=22, power=False)
    )
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)
    await hass.async_block_till_done()

    commands: list[int] = []

    async def record_commands(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) == bytes.fromhex(mac_hex):
            commands.append(fields.get(0x0009, b"\x00")[0])

    fake_controller.on_command = record_commands
    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )
    await hass.services.async_call(
        climate.DOMAIN,
        climate.SERVICE_SET_TEMPERATURE,
        {"entity_id": climate_entity, ATTR_TEMPERATURE: 23},
        blocking=True,
    )
    await _wait_for(lambda: commands == [3])
    await hass.services.async_call(
        climate.DOMAIN,
        climate.SERVICE_SET_HVAC_MODE,
        {"entity_id": climate_entity, "hvac_mode": "heat_cool"},
        blocking=True,
    )

    await fake_controller.async_send_status(_system_status(hub, power=True, mode=4))
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, target=23, power=False)
    )
    await _wait_for(lambda: not hub._pending and not hub._queued, timeout=2)

    assert commands == [3]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_homekit_half_degree_setpoint_is_normalized_to_integer_controller_value(
    hass, setup_integration, fake_controller
):
    """A HomeKit half-degree request is accepted without changing HA controls."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, target=26)
    )
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)
    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )
    sent_values: list[int] = []

    async def acknowledge_temperature(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != bytes.fromhex(mac_hex):
            return
        value = fields.get(0x000A)
        if fields.get(0x0009) == b"\x03" and value is not None:
            sent_values.append(value[0])
            await fake_controller.async_send_status(
                _thermostat_status(hub, mac_hex, target=value[0] // 2)
            )

    fake_controller.on_command = acknowledge_temperature
    await hass.services.async_call(
        climate.DOMAIN,
        climate.SERVICE_SET_TEMPERATURE,
        {"entity_id": climate_entity, ATTR_TEMPERATURE: 23.5},
        blocking=True,
    )
    await _wait_for(lambda: sent_values == [46])
    await _wait_for(lambda: hub.thermostats[mac_hex].target_temperature == 23)

    state = hass.states.get(climate_entity)
    assert state.attributes["min_temp"] == 16
    assert state.attributes["max_temp"] == 28
    assert state.attributes["target_temp_step"] == 1


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_matching_power_request_cancels_a_stale_opposite_queue(
    hass, setup_integration, fake_controller
):
    """A confirmed OFF request removes a not-yet-sent queued ON intent."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, power=False)
    )
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)
    target = f"thermostat_{mac_hex}"
    hub._queued[target] = [QueuedCommand(
        f"{hub.thermostat_name(hub.thermostats[mac_hex])} 开关",
        target,
        {"power": "ON"},
        bytes.fromhex(mac_hex),
        2,
        None,
    )]

    await hub.async_set_thermostat_power(mac_hex, False)
    await hub._async_dispatch_queued()

    assert target not in hub._queued
    assert target not in hub._pending


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_power_and_temperature_queue_intents_do_not_overwrite_each_other(
    hass, setup_integration, fake_controller
):
    """A queued OFF and the latest setpoint both survive one pending setpoint."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, target=22, power=True)
    )
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)
    await hass.async_block_till_done()

    commands: list[tuple[int, int | None]] = []

    async def acknowledge_queued_commands(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != bytes.fromhex(mac_hex):
            return
        command = fields.get(0x0009, b"\x00")[0]
        value = fields.get(0x000A)
        commands.append((command, value[0] if value else None))
        if command == 1:
            await fake_controller.async_send_status(
                _thermostat_status(hub, mac_hex, target=24, power=False)
            )
        elif command == 3 and value == b"\x32":
            await fake_controller.async_send_status(
                _thermostat_status(hub, mac_hex, target=25, power=False)
            )

    fake_controller.on_command = acknowledge_queued_commands
    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )
    await hass.services.async_call(
        climate.DOMAIN,
        climate.SERVICE_SET_TEMPERATURE,
        {"entity_id": climate_entity, ATTR_TEMPERATURE: 24},
        blocking=True,
    )
    await _wait_for(lambda: commands == [(3, 48)])
    await hass.services.async_call(
        climate.DOMAIN,
        climate.SERVICE_SET_HVAC_MODE,
        {"entity_id": climate_entity, "hvac_mode": "off"},
        blocking=True,
    )
    await hass.services.async_call(
        climate.DOMAIN,
        climate.SERVICE_SET_TEMPERATURE,
        {"entity_id": climate_entity, ATTR_TEMPERATURE: 25},
        blocking=True,
    )

    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, target=24, power=True)
    )
    await _wait_for(
        lambda: commands == [(3, 48), (1, None), (3, 50)]
        and hub.thermostats[mac_hex].power == "OFF"
        and hub.thermostats[mac_hex].target_temperature == 25
        and not hub._pending
        and not hub._queued,
        timeout=3,
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_unrelated_power_queue_does_not_suppress_temperature_retry(
    hass, setup_integration, fake_controller
):
    """A queued power change cannot make an unconfirmed setpoint skip its retry."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, target=22, power=True)
    )
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)
    await hass.async_block_till_done()

    commands: list[tuple[int, int | None]] = []

    async def record_commands(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != bytes.fromhex(mac_hex):
            return
        command = fields.get(0x0009, b"\x00")[0]
        value = fields.get(0x000A)
        commands.append((command, value[0] if value else None))

    fake_controller.on_command = record_commands
    await hub.async_set_thermostat_temperature(mac_hex, 24)
    await _wait_for(lambda: commands == [(3, 48)])
    await hub.async_set_thermostat_power(mac_hex, False)

    pending = hub._pending[f"thermostat_{mac_hex}"]
    pending.deadline = 0
    await hub._async_expire_pending(1)

    await _wait_for(lambda: commands == [(3, 48), (3, 48)])
    assert pending.attempts == 2
    assert hub._queued[f"thermostat_{mac_hex}"][0].expected == {"power": "OFF"}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_latest_temperature_replaces_a_queue_waiting_for_dispatch(
    hass, setup_integration, fake_controller
):
    """A new setpoint wins if an older queue value outlives its pending command."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(_thermostat_status(hub, mac_hex))
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)
    await hass.async_block_till_done()

    target = f"thermostat_{mac_hex}"
    hub._queued[target] = [QueuedCommand(
        f"{hub.thermostat_name(hub.thermostats[mac_hex])} 设定温度",
        target,
        {"target_temperature": "23"},
        bytes.fromhex(mac_hex),
        3,
        46,
    )]
    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )
    sent_targets: list[int] = []

    async def acknowledge_temperature(frame) -> None:
        fields = parse_tlvs(frame.body)
        if (
            fields.get(0x0004) != bytes.fromhex(mac_hex)
            or fields.get(0x0009) != b"\x03"
        ):
            return
        target_temperature = fields[0x000A][0] // 2
        sent_targets.append(target_temperature)
        await fake_controller.async_send_status(
            _thermostat_status(hub, mac_hex, target=target_temperature)
        )

    fake_controller.on_command = acknowledge_temperature

    await hass.services.async_call(
        climate.DOMAIN,
        climate.SERVICE_SET_TEMPERATURE,
        {"entity_id": climate_entity, ATTR_TEMPERATURE: 25},
        blocking=True,
    )

    await _wait_for(
        lambda: sent_targets == [25]
        and hub.thermostats[mac_hex].target_temperature == 25
        and target not in hub._pending
        and target not in hub._queued,
        timeout=2,
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_system_mode_scene_and_humidifier_follow_controller_push_state(
    hass, setup_integration, fake_controller
):
    """System controls use HA services and reflect controller-originated updates."""
    hub = setup_integration.hub
    controller = {"power": False, "mode": 1, "scene": 1, "humidifier": False}

    def status() -> bytes:
        return _system_status(
            hub,
            power=controller["power"],
            mode=controller["mode"],
            scene=controller["scene"],
            humidifier=controller["humidifier"],
        )

    async def acknowledge_command(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != hub.tech_system_mac:
            return
        command = fields.get(0x0009, b"\x00")[0]
        value = fields.get(0x000A, b"\x00")[0]
        if command == 2:
            controller["power"] = True
        elif command == 1:
            controller["power"] = False
        elif command == 3:
            controller["mode"] = value
        elif command == 4:
            controller["scene"] = value
        elif command == 5:
            controller["humidifier"] = bool(value)
        await fake_controller.async_send_status(status())

    fake_controller.on_command = acknowledge_command
    await fake_controller.async_send_status(status())
    await _wait_for(lambda: hub.available)

    mode_entity = _entity_id(hass, hub.entry.entry_id, "select", "system_mode")
    scene_entity = _entity_id(hass, hub.entry.entry_id, "select", "system_scene")
    humidifier_entity = _entity_id(
        hass, hub.entry.entry_id, "switch", "winter_humidifier"
    )
    assert hass.states.get(humidifier_entity).state == "unavailable"

    await hass.services.async_call(
        select.DOMAIN,
        select.SERVICE_SELECT_OPTION,
        {"entity_id": mode_entity, "option": "制热"},
        blocking=True,
    )
    await _wait_for(lambda: hub.state.mode == "heat")
    await hass.async_block_till_done()
    assert hass.states.get(mode_entity).state == "制热"
    assert hass.states.get(humidifier_entity).state == "off"

    await hass.services.async_call(
        select.DOMAIN,
        select.SERVICE_SELECT_OPTION,
        {"entity_id": scene_entity, "option": "离家"},
        blocking=True,
    )
    await _wait_for(lambda: hub.state.scene == "away")
    await hass.async_block_till_done()
    assert hass.states.get(scene_entity).state == "离家"

    await hass.services.async_call(
        switch.DOMAIN,
        switch.SERVICE_TURN_ON,
        {"entity_id": humidifier_entity},
        blocking=True,
    )
    await _wait_for(lambda: hub.state.winter_humidifier == "ON")
    await hass.async_block_till_done()
    assert hass.states.get(humidifier_entity).state == "on"

    await hass.services.async_call(
        switch.DOMAIN,
        switch.SERVICE_TURN_OFF,
        {"entity_id": humidifier_entity},
        blocking=True,
    )
    await _wait_for(lambda: hub.state.winter_humidifier == "OFF")
    system_entity = _entity_id(hass, hub.entry.entry_id, "switch", "system_power")
    await hass.services.async_call(
        switch.DOMAIN,
        switch.SERVICE_TURN_OFF,
        {"entity_id": system_entity},
        blocking=True,
    )
    await _wait_for(lambda: hub.state.power == "OFF")


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_full_system_status_exposes_environment_and_problem_entities(
    hass, setup_integration, fake_controller
) -> None:
    """One controller push updates every verified 14-byte read-only field."""
    hub = setup_integration.hub
    for key in ("system_fault", "filter_fault"):
        entity_id = _entity_id(hass, hub.entry.entry_id, "binary_sensor", key)
        assert hass.states.get(entity_id).state == "unavailable"
    for key in ("system_fault_code", "filter_fault_code"):
        entity_id = _entity_id(hass, hub.entry.entry_id, "sensor", key)
        assert hass.states.get(entity_id).state == "unavailable"

    await fake_controller.async_send_status(
        _system_status(
            hub,
            power=True,
            energy_saving=True,
            temperature=25.4,
            humidity=65,
            pm25=12.3,
            co2=700,
            system_fault_code=42,
            filter_fault_code=7,
        )
    )
    await _wait_for(lambda: hub.state.filter_fault_code == 7)
    await hass.async_block_till_done()

    expected_states = {
        ("switch", "energy_control"): "on",
        ("sensor", "system_temperature"): "25.4",
        ("sensor", "system_humidity"): "65",
        ("sensor", "system_pm25"): "12.3",
        ("sensor", "system_co2"): "700",
        ("sensor", "system_fault_code"): "42",
        ("sensor", "filter_fault_code"): "7",
        ("binary_sensor", "system_fault"): "on",
        ("binary_sensor", "filter_fault"): "on",
    }
    for (platform, key), expected in expected_states.items():
        entity_id = _entity_id(hass, hub.entry.entry_id, platform, key)
        state = hass.states.get(entity_id)
        assert state.state == expected
        if platform == "binary_sensor":
            assert state.attributes["device_class"] == "problem"
            assert "raw_code" not in state.attributes
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"system_fault_{hub.entry.entry_id}"
    ) is not None
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"filter_fault_{hub.entry.entry_id}"
    ) is not None

    await fake_controller.async_send_status(
        _system_status(
            hub,
            power=True,
            system_fault_code=0,
            filter_fault_code=0,
        )
    )
    await _wait_for(lambda: hub.state.filter_fault_code == 0)
    await hass.async_block_till_done()
    for key in ("system_fault", "filter_fault"):
        entity_id = _entity_id(hass, hub.entry.entry_id, "binary_sensor", key)
        state = hass.states.get(entity_id)
        assert state.state == "off"
        assert "raw_code" not in state.attributes
    for key in ("system_fault_code", "filter_fault_code"):
        entity_id = _entity_id(hass, hub.entry.entry_id, "sensor", key)
        assert hass.states.get(entity_id).state == "healthy"
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"system_fault_{hub.entry.entry_id}"
    ) is None
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"filter_fault_{hub.entry.entry_id}"
    ) is None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_energy_control_entity_is_always_registered(
    hass, setup_integration
) -> None:
    """The writable energy switch is a permanent total-controller entity."""
    hub = setup_integration.hub
    added = []

    await async_setup_switches(hass, hub.entry, added.extend)
    assert any(
        entity._attr_unique_id == f"{hub.entry.entry_id}_energy_control"
        for entity in added
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_energy_control_uses_command_06_and_controller_push_confirmation(
    setup_integration, fake_controller
) -> None:
    """Energy control is confirmed only by the matching full status report."""
    hub = setup_integration.hub
    await fake_controller.async_send_status(
        _system_status(hub, power=False, energy_saving=False)
    )
    await _wait_for(lambda: hub.available and hub.state.energy_saving == "OFF")
    commands: list[tuple[int, int]] = []

    async def acknowledge_command(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != hub.tech_system_mac:
            return
        command = fields.get(0x0009, b"\x00")[0]
        value = fields.get(0x000A, b"\x00")[0]
        commands.append((command, value))
        await fake_controller.async_send_status(
            _system_status(hub, power=False, energy_saving=bool(value))
        )

    fake_controller.on_command = acknowledge_command
    await hub.async_set_energy_saving(True)
    assert hub._pending["system"].expected == {"energy_saving": "ON"}
    await _wait_for(
        lambda: hub.state.energy_saving == "ON" and "system" not in hub._pending
    )
    assert commands == [(6, 1)]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_energy_control_confirmation_is_independent_of_mode_and_scene(
    setup_integration, fake_controller
) -> None:
    """All verified mode/scene combinations preserve command 0x06 semantics."""
    hub = setup_integration.hub
    hub.command_min_interval = 0
    controller = {"mode": 1, "scene": 0, "energy_saving": False}
    commands: list[tuple[int, int]] = []

    def status() -> bytes:
        return _system_status(
            hub,
            power=False,
            mode=controller["mode"],
            scene=controller["scene"],
            energy_saving=controller["energy_saving"],
        )

    async def acknowledge_command(frame) -> None:
        fields = parse_tlvs(frame.body)
        command = fields.get(0x0009, b"\x00")[0]
        value = fields.get(0x000A, b"\x00")[0]
        commands.append((command, value))
        controller["energy_saving"] = bool(value)
        await fake_controller.async_send_status(status())

    fake_controller.on_command = acknowledge_command
    await fake_controller.async_send_status(status())
    await _wait_for(lambda: hub.available)

    expected_commands = []
    for mode in range(1, 5):
        for scene in range(2):
            controller["mode"] = mode
            controller["scene"] = scene
            controller["energy_saving"] = False
            await fake_controller.async_send_status(status())
            await _wait_for(lambda: hub.state.energy_saving == "OFF")
            for enabled in (True, False):
                await hub.async_set_energy_saving(enabled)
                expected_state = "ON" if enabled else "OFF"
                await _wait_for(
                    lambda: hub.state.energy_saving == expected_state
                    and "system" not in hub._pending
                )
                expected_commands.append((6, int(enabled)))

    assert commands == expected_commands


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_system_commands_queue_independent_intents_and_keep_latest_mode(
    setup_integration, fake_controller
) -> None:
    """Rapid total-control actions are serialized without rejecting the user."""
    hub = setup_integration.hub
    hub.command_min_interval = 0
    await fake_controller.async_send_status(_system_status(hub, power=False, mode=1))
    await _wait_for(lambda: hub.available and hub.state.mode == "cool")

    await hub.async_set_mode("heat")
    await hub.async_set_scene("away")
    await hub.async_set_mode("ventilation")

    assert hub._pending["system"].expected == {"mode": "heat"}
    assert [command.expected for command in hub._queued["system"]] == [
        {"scene": "away"},
        {"mode": "ventilation"},
    ]

    await fake_controller.async_send_status(
        _system_status(hub, power=False, mode=2, scene=1)
    )
    await _wait_for(lambda: "system" not in hub._pending)
    await hub._async_dispatch_queued()
    assert hub._pending["system"].expected == {"scene": "away"}

    await fake_controller.async_send_status(
        _system_status(hub, power=False, mode=2, scene=0)
    )
    await _wait_for(lambda: "system" not in hub._pending)
    await hub._async_dispatch_queued()
    assert hub._pending["system"].expected == {"mode": "ventilation"}


async def test_mode_select_keeps_all_options_while_system_runs(
    hass, mock_config_entry
) -> None:
    """HomeKit always receives the complete, stable set of exclusive modes."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub.connected = True
    hub.protocol_verified = True
    hub.health.mark_stage(ConnectionStage.READY)
    hub.state.mode = "cool"
    entity = SystemModeSelect(hub)

    hub.state.power = "ON"
    assert entity.available
    assert entity.options == ["制冷", "制热", "通风", "除湿"]
    assert entity.extra_state_attributes["can_change_mode"] is True

    hub.state.power = "OFF"
    assert entity.options == ["制冷", "制热", "通风", "除湿"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_mode_select_rejects_a_new_transition_while_power_is_changing(
    hass, setup_integration, fake_controller
) -> None:
    """A separate in-flight power request cannot be folded into a mode transaction."""
    hub = setup_integration.hub
    hub.command_min_interval = 0
    await fake_controller.async_send_status(_system_status(hub, power=False, mode=1))
    await _wait_for(lambda: hub.available and hub.state.mode == "cool")
    entity = SystemModeSelect(hub)

    await hub.async_set_scene("away")
    await hub.async_set_system_power(True)

    assert entity.options == ["制冷", "制热", "通风", "除湿"]
    assert entity.extra_state_attributes["can_change_mode"] is False
    with pytest.raises(HomeAssistantError, match="总控开关状态正在变化"):
        await entity.async_select_option("制热")


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_mode_selection_power_cycles_an_enabled_system(
    hass, setup_integration, fake_controller
) -> None:
    """Selecting another mode performs one confirmed off-mode-on transaction."""
    hub = setup_integration.hub
    hub.command_min_interval = 0
    controller = {"power": True, "mode": 1}
    commands: list[tuple[int, int | None]] = []

    async def acknowledge_command(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != hub.tech_system_mac:
            return
        command = fields.get(0x0009, b"\x00")[0]
        value_bytes = fields.get(0x000A)
        value = value_bytes[0] if value_bytes else None
        commands.append((command, value))
        if command == 1:
            controller["power"] = False
        elif command == 2:
            controller["power"] = True
        elif command == 3 and value is not None:
            controller["mode"] = value
        await fake_controller.async_send_status(
            _system_status(
                hub,
                power=controller["power"],
                mode=controller["mode"],
            )
        )

    fake_controller.on_command = acknowledge_command
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await _wait_for(lambda: hub.available and hub.state.power == "ON")
    mode_entity = _entity_id(hass, hub.entry.entry_id, "select", "system_mode")

    await hass.services.async_call(
        select.DOMAIN,
        select.SERVICE_SELECT_OPTION,
        {"entity_id": mode_entity, "option": "制热"},
        blocking=True,
    )

    assert commands == [(1, None), (3, 2), (2, None)]
    assert hub.state.power == "ON"
    assert hub.state.mode == "heat"
    assert hass.states.get(mode_entity).state == "制热"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_mode_selection_keeps_a_disabled_system_off(
    setup_integration, fake_controller
) -> None:
    """Selecting a mode while disabled sends no unnecessary power commands."""
    hub = setup_integration.hub
    hub.command_min_interval = 0
    commands: list[tuple[int, int | None]] = []

    async def acknowledge_mode(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != hub.tech_system_mac:
            return
        command = fields.get(0x0009, b"\x00")[0]
        value_bytes = fields.get(0x000A)
        value = value_bytes[0] if value_bytes else None
        commands.append((command, value))
        if command == 3 and value is not None:
            await fake_controller.async_send_status(
                _system_status(hub, power=False, mode=value)
            )

    fake_controller.on_command = acknowledge_mode
    await fake_controller.async_send_status(_system_status(hub, power=False, mode=1))
    await _wait_for(lambda: hub.available and hub.state.power == "OFF")

    await hub.async_select_mode("heat")

    assert commands == [(3, 2)]
    assert hub.state.power == "OFF"
    assert hub.state.mode == "heat"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reselecting_the_active_mode_sends_no_command(
    setup_integration, fake_controller
) -> None:
    """HomeKit turning off its active option cannot power-cycle the system."""
    hub = setup_integration.hub
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await _wait_for(lambda: hub.available and hub.state.power == "ON")
    command_count = sum(
        frame.kind == 4 and frame.opcode == 9
        for frame in fake_controller.received_frames
    )

    await hub.async_select_mode("cool")

    assert sum(
        frame.kind == 4 and frame.opcode == 9
        for frame in fake_controller.received_frames
    ) == command_count
    assert hub.state.power == "ON"
    assert hub.state.mode == "cool"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_failed_mode_confirmation_leaves_the_system_safely_off(
    setup_integration, fake_controller
) -> None:
    """A failed middle step never blindly restores power after the wrong mode."""
    hub = setup_integration.hub
    hub.command_min_interval = 0
    hub.command_confirmation_timeout = 0.1
    commands: list[tuple[int, int | None]] = []

    async def acknowledge_only_power_off(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != hub.tech_system_mac:
            return
        command = fields.get(0x0009, b"\x00")[0]
        value_bytes = fields.get(0x000A)
        commands.append((command, value_bytes[0] if value_bytes else None))
        if command == 1:
            await fake_controller.async_send_status(
                _system_status(hub, power=False, mode=1)
            )

    fake_controller.on_command = acknowledge_only_power_off
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await _wait_for(lambda: hub.available and hub.state.power == "ON")

    with pytest.raises(HomeAssistantError, match="模式切换未得到主机确认"):
        await hub.async_select_mode("heat")

    assert commands == [(1, None), (3, 2)]
    assert hub.state.power == "OFF"
    assert hub.state.mode == "cool"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_unload_during_mode_transition_stops_without_later_commands(
    hass, setup_integration, mock_config_entry, fake_controller
) -> None:
    """An unload ends a waiting mode transaction before mode or restore writes."""
    hub = setup_integration.hub
    hub.command_min_interval = 0
    commands: list[tuple[int, int | None]] = []
    first_command = asyncio.Event()

    async def record_without_confirmation(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != hub.tech_system_mac:
            return
        value = fields.get(0x000A)
        commands.append((fields[0x0009][0], value[0] if value else None))
        first_command.set()

    fake_controller.on_command = record_without_confirmation
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await _wait_for(lambda: hub.available and hub.state.power == "ON")
    transition = asyncio.create_task(hub.async_select_mode("heat"))
    await asyncio.wait_for(first_command.wait(), timeout=1)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    with pytest.raises(HomeAssistantError, match="连接已经断开"):
        await asyncio.wait_for(transition, timeout=1)

    assert commands == [(1, None)]
    assert not hub._pending
    assert not hub._queued


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_climate_and_filtered_sensors_follow_app_pushes(
    hass, setup_integration, fake_controller
):
    """A controller-discovered panel adds native entities and filters valid samples."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, current=20.1, humidity=50)
    )
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, current=25.2, humidity=60)
    )
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, current=29.4, humidity=70)
    )
    await _wait_for(lambda: mac_hex in hub.thermostats and hub.available)
    await hass.async_block_till_done()

    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )
    temperature_entity = _entity_id(
        hass,
        hub.entry.entry_id,
        "sensor",
        f"thermostat_{mac_hex}_automation_temperature",
    )
    humidity_entity = _entity_id(
        hass,
        hub.entry.entry_id,
        "sensor",
        f"thermostat_{mac_hex}_automation_humidity",
    )
    state = hass.states.get(climate_entity)
    assert state is not None
    assert state.attributes["min_temp"] == 16
    assert state.attributes["max_temp"] == 28
    assert state.attributes["target_temp_step"] == 1
    assert state.state == "heat_cool"
    # Climate exposes the latest raw panel report. Automation sensors use the
    # three-sample median so transient changes do not trigger automations.
    assert float(state.attributes["current_temperature"]) == 29.4
    assert int(state.attributes["current_humidity"]) == 70
    assert float(hass.states.get(temperature_entity).state) == 25.2
    assert int(hass.states.get(humidity_entity).state) == 60

    # This is an App-originated push: no HA service call is involved.
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, target=23, current=26.4, humidity=62)
    )
    await _wait_for(lambda: hub.thermostats[mac_hex].target_temperature == 23)
    await hass.async_block_till_done()
    assert float(hass.states.get(climate_entity).attributes["temperature"]) == 23
    assert float(hass.states.get(climate_entity).attributes["current_temperature"]) == 26.4
    assert int(hass.states.get(climate_entity).attributes["current_humidity"]) == 62

    # The source system forces individual panels off in ventilation/dehumidify.
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=3))
    await _wait_for(lambda: hub.state.mode == "ventilation")
    await hass.async_block_till_done()
    state = hass.states.get(climate_entity)
    assert state.state == "off"
    assert state.attributes["hvac_modes"] == ["off", "heat_cool"]

    await fake_controller.async_send_status(_system_status(hub, power=True, mode=4))
    await _wait_for(lambda: hub.state.mode == "dehumidify")
    await hass.async_block_till_done()
    state = hass.states.get(climate_entity)
    assert state.state == "off"
    assert state.attributes["hvac_modes"] == ["off", "heat_cool"]

    received_before = len(fake_controller.received_frames)
    with pytest.raises(HomeAssistantError, match="只能跟随当前科技系统模式"):
        await hass.services.async_call(
            climate.DOMAIN,
            climate.SERVICE_SET_HVAC_MODE,
            {"entity_id": climate_entity, "hvac_mode": "heat_cool"},
            blocking=True,
        )
    await hass.async_block_till_done()
    assert len(fake_controller.received_frames) == received_before


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_fragmented_status_stress_and_rapid_setpoints_recover_to_latest_value(
    hass, setup_integration, fake_controller, monkeypatch
):
    """A long fragmented stream and 50 updates never desynchronize the session."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(_thermostat_status(hub, mac_hex))
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)
    await hass.async_block_till_done()
    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )

    counters_before = hub.health.snapshot()["counters"]
    decoded_before = counters_before["frames_decoded"]
    malformed_before = counters_before["frames_malformed"]
    resynchronized_before = counters_before["frames_resynchronized"]
    frames = [
        YasHcpFrame(
            5,
            0x0C,
            sequence,
            _thermostat_status(
                hub,
                mac_hex,
                target=20 + sequence % 8,
                current=20 + sequence / 10,
                humidity=40 + sequence % 40,
            ),
        )
        for sequence in range(100)
    ]
    await fake_controller.async_send_frames(*frames, fragment_size=7)
    await _wait_for(
        lambda: hub.health.snapshot()["counters"]["frames_decoded"]
        >= decoded_before + 100
    )
    await hass.async_block_till_done()
    thermostat = hub.thermostats[mac_hex]
    assert thermostat.current_temperature == 29.9
    assert thermostat.humidity == 59
    counters_after_frames = hub.health.snapshot()["counters"]
    assert counters_after_frames["frames_malformed"] == malformed_before
    assert counters_after_frames["frames_resynchronized"] == resynchronized_before

    invalid_before = counters_after_frames["invalid_measurements"]
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, current=100, humidity=100)
    )
    await _wait_for(
        lambda: hub.health.snapshot()["counters"]["invalid_measurements"]
        == invalid_before + 1
    )
    await hass.async_block_till_done()
    assert thermostat.current_temperature == 29.9
    assert thermostat.humidity == 59
    assert float(hass.states.get(climate_entity).attributes["current_temperature"]) != 100
    assert int(hass.states.get(climate_entity).attributes["current_humidity"]) != 100

    received_setpoints: list[int] = []

    async def acknowledge_latest_setpoint(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != bytes.fromhex(mac_hex):
            return
        if fields.get(0x0009) != b"\x03":
            return
        received_setpoints.append(fields[0x000A][0] // 2)
        # Let the first request time out. The hub must then release the pending
        # slot and dispatch the newest coalesced temperature without blocking.
        if len(received_setpoints) == 2:
            await fake_controller.async_send_status(
                _thermostat_status(hub, mac_hex, target=received_setpoints[-1])
            )

    fake_controller.on_command = acknowledge_latest_setpoint
    hub.command_confirmation_timeout = 0.02
    hub.command_min_interval = 0
    monkeypatch.setattr(hub_module, "SESSION_IDLE_INTERVAL", 0.01)
    monkeypatch.setattr(hub_module, "SESSION_ACTIVE_INTERVAL", 0.01)
    for temperature in range(16, 29):
        for _ in range(4):
            await hass.services.async_call(
                climate.DOMAIN,
                climate.SERVICE_SET_TEMPERATURE,
                {"entity_id": climate_entity, ATTR_TEMPERATURE: temperature},
                blocking=True,
            )
    target = f"thermostat_{mac_hex}"
    assert hub._pending[target].expected == {"target_temperature": "16"}
    assert hub._queued[target][0].expected == {"target_temperature": "28"}

    await _wait_for(
        lambda: not hub._pending
        and not hub._queued
        and hub.thermostats[mac_hex].target_temperature == 28,
        timeout=1,
    )
    assert received_setpoints == [16, 28]
    # Repeating the in-flight value cancels a stale replacement rather than
    # counting as a new coalesced value; all later rapid values are retained.
    assert hub.health.snapshot()["counters"]["commands_coalesced"] >= 48
    assert hub.health.snapshot()["counters"]["commands_timed_out"] >= 1
    assert not hub._pending
    assert not hub._queued


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_thermostat_timeout_retries_once_and_accepts_the_late_ack(
    hass, setup_integration, fake_controller, monkeypatch
):
    """A dropped first setpoint is retried once and cannot poison later commands."""
    hub = setup_integration.hub
    hub.command_min_interval = 0
    hub.command_confirmation_timeout = 0.02
    monkeypatch.setattr(hub_module, "SESSION_IDLE_INTERVAL", 0.01)
    monkeypatch.setattr(hub_module, "SESSION_ACTIVE_INTERVAL", 0.01)
    mac_hex = "ff00ffffffff02ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(_thermostat_status(hub, mac_hex))
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)
    await hass.async_block_till_done()
    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )
    attempts = 0

    async def acknowledge_retry(frame) -> None:
        nonlocal attempts
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != bytes.fromhex(mac_hex):
            return
        if fields.get(0x0009) != b"\x03":
            return
        attempts += 1
        if attempts == 2:
            await fake_controller.async_send_status(
                _thermostat_status(hub, mac_hex, target=fields[0x000A][0] // 2)
            )

    fake_controller.on_command = acknowledge_retry
    await hass.services.async_call(
        climate.DOMAIN,
        climate.SERVICE_SET_TEMPERATURE,
        {"entity_id": climate_entity, ATTR_TEMPERATURE: 22},
        blocking=True,
    )
    await _wait_for(
        lambda: not hub._pending
        and hub.thermostats[mac_hex].target_temperature == 22,
        timeout=1,
    )

    assert attempts == 2
    counters = hub.health.snapshot()["counters"]
    assert counters["commands_retried"] == 1
    assert counters["commands_confirmed"] >= 1


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_control_guards_reject_invalid_or_unsafe_operations(
    setup_integration, fake_controller
):
    """Native controls retain the controller's safety gates before writing TCP."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff03ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(_thermostat_status(hub, mac_hex))
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)

    with pytest.raises(HomeAssistantError, match="请先关闭"):
        await hub.async_set_mode("heat")
    with pytest.raises(HomeAssistantError, match="不支持的场景"):
        await hub.async_set_scene("party")
    with pytest.raises(HomeAssistantError, match="冬季加湿"):
        await hub.async_set_winter_humidifier(True)
    with pytest.raises(HomeAssistantError, match="整数或半度"):
        await hub.async_set_thermostat_temperature(mac_hex, 22.25)
    with pytest.raises(HomeAssistantError, match="整数或半度"):
        await hub.async_set_thermostat_temperature(mac_hex, 28.5)
    with pytest.raises(HomeAssistantError, match="整数"):
        await hub.async_set_thermostat_temperature(mac_hex, 29)
    with pytest.raises(HomeAssistantError, match="尚未被主机发现"):
        await hub.async_set_thermostat_temperature("ff00ffffffff09ff", 22)

    await fake_controller.async_send_status(_system_status(hub, power=True, mode=4))
    await _wait_for(lambda: hub.state.mode == "dehumidify")
    received_before = len(fake_controller.received_frames)
    blocked_before = hub.health.snapshot()["counters"]["commands_blocked"]
    with pytest.raises(HomeAssistantError, match="当前为除湿模式"):
        await hub.async_set_thermostat_power(mac_hex, True)
    assert hub.health.snapshot()["counters"]["commands_blocked"] == blocked_before + 1
    assert len(fake_controller.received_frames) == received_before

    hub.allow_control = False
    assert hub.control_permission == "read_only"
    with pytest.raises(HomeAssistantError, match="只读模式"):
        await hub.async_set_system_power(False)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_switches_report_unknown_before_the_first_controller_status(
    setup_integration,
) -> None:
    """Startup never invents an on/off value before MC7021 sends a state frame."""
    hub = setup_integration.hub
    assert SystemPowerSwitch(hub).is_on is None
    assert WinterHumidifierSwitch(hub).is_on is None


async def test_malformed_status_pauses_registry_monitoring_without_erasing_panel(
    hass, mock_config_entry
):
    """A malformed status body cannot turn wall-clock downtime into absence time."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    now = datetime(2026, 8, 24, tzinfo=UTC)
    await hub.panel_registry.async_load()
    await hub.panel_registry.async_note_panel_report(
        "ff00ffffffff01ff", "r0100", now
    )
    await hub.panel_registry.async_note_status_stream(now)
    hub.health.mark_stage(ConnectionStage.READY)
    hub.connected = True

    await hub._async_status_received(b"malformed-status")

    record = hub.panel_registry.records["ff00ffffffff01ff"]
    assert record.last_report_utc == now
    assert record.monitored_absence_seconds == 0
    assert record.checkpoint_utc is None
    assert not record.available


async def test_malformed_transport_frame_pauses_registry_monitoring(
    hass, mock_config_entry
):
    """Decoder recovery events also stop observed-absence accounting."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    now = datetime(2026, 8, 24, tzinfo=UTC)
    await hub.panel_registry.async_load()
    await hub.panel_registry.async_note_panel_report(
        "ff00ffffffff01ff", "r0100", now
    )
    await hub.panel_registry.async_note_status_stream(now)

    await hub._async_parser_event("frames_malformed", 1)

    record = hub.panel_registry.records["ff00ffffffff01ff"]
    assert record.checkpoint_utc is None
    assert not record.available


async def test_truncated_tlv_status_cannot_partially_update_controller_state(
    hass, mock_config_entry
):
    """A valid TLV prefix followed by a truncated tail is one invalid report."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    body = tlv(0x0004, hub.tech_system_mac) + tlv(0x000B, b"\x01")
    body += b"\x0a\x00\x05\x00\x01"

    await hub._async_status_received(body)

    assert hub.state.power is None
    assert not hub.protocol_verified
    assert hub.health.snapshot()["counters"]["ignored_statuses"] == 1


async def test_status_gap_pauses_absence_even_when_other_tcp_traffic_continues(
    hass, mock_config_entry, monkeypatch
):
    """Only valid status reports may keep observed-absence accounting alive."""
    monotonic = [0.0]
    now = [datetime(2026, 8, 24, tzinfo=UTC)]
    monkeypatch.setattr(hub_module.time, "monotonic", lambda: monotonic[0])
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub.controller_silence_timeout = 10
    hub.panel_registry = PanelRegistry(
        hass,
        mock_config_entry.entry_id,
        clock=lambda: now[0],
        monotonic_clock=lambda: monotonic[0],
        status_gap_timeout=10,
    )
    await hub.panel_registry.async_load()
    await hub.panel_registry.async_note_panel_report(
        "ff00ffffffff01ff", "r0100", now[0]
    )
    hub.health.mark_stage(ConnectionStage.READY)
    hub.connected = True
    body = tlv(0x0004, hub.tech_system_mac) + tlv(0x000B, b"\x01")

    await hub._async_status_received(body)
    now[0] += timedelta(days=31)
    monotonic[0] += 31 * 86400
    await hub._async_status_received(body)

    record = hub.panel_registry.records["ff00ffffffff01ff"]
    assert record.monitored_absence_seconds == 0
    assert not record.available


async def test_ready_stage_without_tcp_session_does_not_start_absence_monitoring(
    hass, mock_config_entry
):
    """Observed absence needs both READY and a real controller session."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    now = datetime(2026, 8, 24, tzinfo=UTC)
    await hub.panel_registry.async_load()
    await hub.panel_registry.async_note_panel_report(
        "ff00ffffffff01ff", "r0100", now
    )
    hub.health.mark_stage(ConnectionStage.READY)
    hub.connected = False
    body = tlv(0x0004, hub.tech_system_mac) + tlv(0x000B, b"\x01")

    await hub._async_status_received(body)

    assert hub.panel_registry.records["ff00ffffffff01ff"].checkpoint_utc is None


async def test_short_term_offline_timeout_is_independent_from_absence_counter(
    hass, mock_config_entry, hass_storage
):
    """Entity availability may expire without resetting persistent absence facts."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub.thermostat_offline_after = 1
    now = datetime(2026, 8, 24, tzinfo=UTC)
    mac_hex = "ff00ffffffff01ff"
    await hub.panel_registry.async_load()
    await hub.panel_registry.async_note_panel_report(mac_hex, "r0100", now)
    await hub.panel_registry.async_note_status_stream(now)
    await hub.panel_registry.async_note_status_stream(now.replace(hour=1))
    record = hub.panel_registry.records[mac_hex]
    absence_before = record.monitored_absence_seconds
    hub.thermostats[mac_hex] = ThermostatState(
        mac=bytes.fromhex(mac_hex),
        room_id="r0100",
        available=True,
        last_seen=time.monotonic() - 2,
    )

    await hub._async_refresh_thermostat_availability(time.monotonic())

    assert not hub.thermostats[mac_hex].available
    assert not record.available
    assert record.monitored_absence_seconds == absence_before
    assert record.last_report_utc == now
    await hub.panel_registry.async_flush()
    stored = hass_storage[
        f"linking_the_world_temp_ha.{mock_config_entry.entry_id}.panels"
    ]
    assert not stored["data"]["panels"][0]["available"]
