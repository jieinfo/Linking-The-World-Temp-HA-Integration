"""Bounded command timelines retain outcomes without household identifiers."""

import json
import time
from types import SimpleNamespace

import pytest

from custom_components.linking_the_world_temp_ha.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.linking_the_world_temp_ha.health import HealthTracker
from tests.native.test_diagnostics import _CommandClient, _ready_command_hub


def test_history_is_bounded_anonymous_and_copied():
    health = HealthTracker()
    for index in range(35):
        trace = health.start_command("thermostat_MAC-CANARY", "target_temperature")
        health.command_sent(trace)
        health.command_result(trace, "confirmed")
    history = health.command_history({"MAC-CANARY": "panel_01"})
    assert len(history) == 30
    assert history[0]["id"] == 6
    assert history[-1]["target"] == "panel_01"
    assert history[-1]["result"] == "confirmed"
    assert "MAC-CANARY" not in json.dumps(history)
    assert "MAC-CANARY" not in json.dumps(health.command_history({}))
    history[-1]["result"] = "tampered"
    assert health.command_history({})[-1]["result"] == "confirmed"
    health.command_result(1, "failed")  # An evicted trace is harmless.


async def test_coalesced_commands_keep_queue_timing_and_terminal_outcomes(
    hass, mock_config_entry
):
    hub = _ready_command_hub(hass, mock_config_entry)
    hub._client = _CommandClient()
    mac = "aabbccddeeff0011"
    await hub.async_set_thermostat_temperature(mac, 22)
    await hub.async_set_thermostat_temperature(mac, 23)
    await hub.async_set_thermostat_temperature(mac, 24)
    hub._confirm_pending(f"thermostat_{mac}", {"target_temperature": "22"})
    await hub._async_dispatch_queued()
    await hub._async_poll_pending_status(time.monotonic() + 0.6)
    hub._confirm_pending(f"thermostat_{mac}", {"target_temperature": "24"})
    history = hub.health.command_history({mac: "panel_01"})
    assert [item["result"] for item in history] == [
        "confirmed",
        "superseded",
        "confirmed",
    ]
    assert history[-1]["queue_delay_seconds"] is not None
    assert history[-1]["status_queries"] == 1
    assert history[-1]["attempts"] == 1


async def test_retry_and_final_timeout_are_one_history_item(hass, mock_config_entry):
    hub = _ready_command_hub(hass, mock_config_entry)
    hub._client = _CommandClient()
    target = "thermostat_aabbccddeeff0011"
    await hub.async_set_thermostat_temperature("aabbccddeeff0011", 22)
    await hub._async_expire_pending(hub._pending[target].deadline)
    await hub._async_expire_pending(hub._pending[target].deadline)
    history = hub.health.command_history({})
    assert len(history) == 1
    assert history[0]["attempts"] == 2
    assert history[0]["result"] == "timeout"


async def test_failed_send_and_disconnect_have_terminal_history(
    hass, mock_config_entry
):
    hub = _ready_command_hub(hass, mock_config_entry)
    hub._client = _CommandClient(fail_send=True)
    with pytest.raises(ConnectionError):
        await hub.async_set_thermostat_temperature("aabbccddeeff0011", 22)
    hub._client = _CommandClient()
    await hub.async_set_thermostat_temperature("aabbccddeeff0011", 23)
    await hub.async_set_thermostat_temperature("aabbccddeeff0011", 24)
    hub._client = None
    await hub._async_disconnect()
    assert [item["result"] for item in hub.health.command_history({})] == [
        "failed",
        "disconnected",
        "disconnected",
    ]


async def test_diagnostics_include_only_anonymous_history(hass, mock_config_entry):
    hub = _ready_command_hub(hass, mock_config_entry)
    hub._client = _CommandClient()
    await hub.async_set_thermostat_temperature("aabbccddeeff0011", 22)
    mock_config_entry.runtime_data = SimpleNamespace(hub=hub, health=hub.health)
    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    history = diagnostics["runtime"]["command_history"]
    assert history[0]["target"] == "panel_unknown"
    assert "aabbccddeeff0011" not in json.dumps(diagnostics)
    assert "ROOM-NAME-CANARY" not in json.dumps(diagnostics)
