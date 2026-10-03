"""Regression tests for the room thermostat operating policy."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = (
    ROOT
    / "custom_components"
    / "linking_the_world_temp_ha"
    / "thermostat_policy.py"
)
CLIMATE_SOURCE = (
    ROOT / "custom_components" / "linking_the_world_temp_ha" / "climate.py"
).read_text(encoding="utf-8")
HUB_SOURCE = (
    ROOT / "custom_components" / "linking_the_world_temp_ha" / "hub.py"
).read_text(encoding="utf-8")


def load_policy():
    """Load the policy without requiring a Home Assistant runtime."""
    if not POLICY_PATH.exists():
        raise AssertionError("thermostat_policy.py has not been implemented")
    spec = importlib.util.spec_from_file_location("thermostat_policy", POLICY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_hub_module():
    """Load the hub with the small HA surface needed by this unit test."""
    homeassistant = sys.modules.setdefault(
        "homeassistant", types.ModuleType("homeassistant")
    )
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = type("ConfigEntry", (), {})
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.callback = lambda function: function
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = type("Store", (), {})
    homeassistant.config_entries = config_entries
    homeassistant.core = core
    homeassistant.exceptions = exceptions
    homeassistant.helpers = helpers
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.exceptions"] = exceptions
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.storage"] = storage

    package_name = "custom_components.linking_the_world_temp_ha"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(POLICY_PATH.parent)]
        sys.modules[package_name] = package

    hub_path = POLICY_PATH.parent / "hub.py"
    spec = importlib.util.spec_from_file_location(f"{package_name}.hub", hub_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ThermostatPolicyTests(unittest.TestCase):
    def test_only_cooling_and_heating_allow_room_panels_to_run(self) -> None:
        policy = load_policy()

        self.assertTrue(policy.can_operate_room_thermostat("ON", "cool"))
        self.assertTrue(policy.can_operate_room_thermostat("ON", "heat"))
        self.assertFalse(policy.can_operate_room_thermostat("ON", "ventilation"))
        self.assertFalse(policy.can_operate_room_thermostat("ON", "dehumidify"))

    def test_total_power_off_keeps_room_panels_operable_in_temperature_modes(
        self,
    ) -> None:
        policy = load_policy()

        self.assertTrue(policy.can_operate_room_thermostat("OFF", "cool"))
        self.assertTrue(policy.can_operate_room_thermostat("OFF", "heat"))
        self.assertFalse(policy.can_operate_room_thermostat(None, "heat"))

    def test_block_reason_is_clear_for_home_assistant_users(self) -> None:
        policy = load_policy()

        self.assertIsNone(policy.room_thermostat_block_reason("OFF", "cool"))
        self.assertEqual(
            policy.room_thermostat_block_reason("ON", "ventilation"),
            "当前为通风模式，房间温控面板由科技系统总控强制关闭",
        )
        self.assertEqual(
            policy.room_thermostat_block_reason("ON", "dehumidify"),
            "当前为除湿模式，房间温控面板由科技系统总控强制关闭",
        )
        self.assertEqual(
            policy.room_thermostat_block_reason("ON", None),
            "当前运行模式不支持开启房间温控面板",
        )
        self.assertIsNone(policy.room_thermostat_block_reason("ON", "cool"))

    def test_climate_and_command_layers_share_the_policy(self) -> None:
        self.assertGreaterEqual(
            CLIMATE_SOURCE.count("can_operate_room_thermostat("), 2
        )
        self.assertIn("room_thermostat_block_reason(", HUB_SOURCE)

    def test_mode_change_while_waiting_prevents_the_command_from_being_sent(
        self,
    ) -> None:
        hub_module = load_hub_module()
        method = hub_module.LinkingTempHub._async_send_tracked
        if "send_guard" not in inspect.signature(method).parameters:
            self.fail("tracked commands do not support a final pre-send guard")

        if "before_write" not in inspect.signature(
            hub_module.AsyncMoorgenClient.send_command
        ).parameters:
            self.fail("protocol writes do not support a final guard")

        class FakeClient:
            def __init__(self, block) -> None:
                self.commands: list[tuple[bytes, int, int | None]] = []
                self.block = block

            async def send_command(
                self,
                mac: bytes,
                command: int,
                value: int | None,
                *,
                before_write=None,
            ) -> None:
                await asyncio.sleep(0)
                self.block()
                if before_write is not None:
                    before_write()
                self.commands.append((mac, command, value))

            async def request_status(self) -> None:
                return None

        async def exercise() -> None:
            instance = object.__new__(hub_module.LinkingTempHub)
            instance.health = hub_module.HealthTracker()
            instance.health.mark_stage(hub_module.ConnectionStage.READY)
            instance.allow_control = True
            instance.connected = True
            instance.protocol_verified = True
            instance._command_lock = asyncio.Lock()
            instance._pending = {}
            instance._queued = {}
            instance._listeners = set()
            instance.last_command_status = "idle"
            instance.command_min_interval = 0.02
            instance.command_confirmation_timeout = 8
            instance._last_command_at = None
            state = {"blocked": False}
            instance._client = FakeClient(
                lambda: state.__setitem__("blocked", True)
            )
            with self.assertRaisesRegex(
                hub_module.HomeAssistantError, "总控强制关闭"
            ):
                await instance._async_send_tracked(
                    "thermostat_panel",
                    "客餐厅 温控面板 开关",
                    {"power": "ON"},
                    b"panelmac",
                    2,
                    send_guard=lambda: (
                        "当前为通风模式，房间温控面板由科技系统总控强制关闭"
                        if state["blocked"]
                        else None
                    ),
                )
            self.assertEqual(instance._client.commands, [])
            self.assertEqual(instance._pending, {})

        asyncio.run(exercise())

    def test_pending_system_shutdown_blocks_panel_power_on(self) -> None:
        hub_module = load_hub_module()
        if not hasattr(hub_module.LinkingTempHub, "_room_thermostat_block_reason"):
            self.fail("pending system power-off is not part of the thermostat policy")

        instance = object.__new__(hub_module.LinkingTempHub)
        instance.state = hub_module.TechSystemState(power="ON", mode="cool")
        instance._pending = {
            "system": hub_module.PendingCommand(
                "总控开关",
                "system",
                {"power": "OFF"},
                1,
                9,
                3,
                b"totalmac",
                1,
                None,
            )
        }

        self.assertEqual(
            instance._room_thermostat_block_reason(),
            "科技系统总开关正在关闭，房间温控面板不能开启",
        )


if __name__ == "__main__":
    unittest.main()
