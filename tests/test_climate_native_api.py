"""Real HA 2026.11 API smoke tests, independent of the pytest HA fixture pin."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

try:
    from homeassistant.components.climate import ClimateEntity
except ImportError:
    ClimateEntity = None

ROOT = Path(__file__).resolve().parents[1]
NATIVE_API_AVAILABLE = ClimateEntity is not None and hasattr(
    ClimateEntity, "native_temperature_unit"
)


@unittest.skipUnless(NATIVE_API_AVAILABLE, "Requires the HA 2026.11 native Climate API")
class ClimateNativeApiTests(unittest.TestCase):
    """Exercise actual HA class registration and native-unit state rendering."""

    def test_registration_does_not_report_deprecated_temperature_members(self):
        spec = importlib.util.spec_from_file_location(
            "custom_components.linking_the_world_temp_ha._native_temperature_smoke",
            ROOT / "custom_components/linking_the_world_temp_ha/climate.py",
        )
        module = importlib.util.module_from_spec(spec)
        with self.assertNoLogs(
            "custom_components.linking_the_world_temp_ha", level="WARNING"
        ):
            spec.loader.exec_module(module)
        for member in (
            "_attr_temperature_unit", "temperature_unit",
            "current_temperature", "target_temperature",
        ):
            self.assertNotIn(member, module.RoomThermostat.__dict__)
        self.assertEqual(
            module.RoomThermostat.native_current_temperature.fget.__name__,
            "native_current_temperature",
        )

    def test_native_celsius_values_are_rendered_in_the_ha_unit_once(self):
        from homeassistant.const import UnitOfTemperature
        from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM

        from custom_components.linking_the_world_temp_ha.climate import RoomThermostat

        mac_hex = "ff00ffffffff01ff"
        hub = SimpleNamespace(
            entry=SimpleNamespace(entry_id="native-temperature-smoke"),
            thermostats={
                mac_hex: SimpleNamespace(
                    current_temperature=26.4, target_temperature=24, humidity=58,
                    power="ON", available=True,
                )
            },
            state=SimpleNamespace(power="ON", mode="cool"),
            available=True,
        )
        for units, current, target, minimum, maximum in (
            (METRIC_SYSTEM, 26.4, 24, 16, 28),
            (US_CUSTOMARY_SYSTEM, 79.5, 75.2, 60.8, 82.4),
        ):
            with self.subTest(unit=units.temperature_unit):
                entity = RoomThermostat(hub, mac_hex)
                entity.hass = SimpleNamespace(config=SimpleNamespace(units=units))
                self.assertEqual(entity.native_temperature_unit, UnitOfTemperature.CELSIUS)
                self.assertEqual(entity.native_current_temperature, 26.4)
                self.assertEqual(entity.native_target_temperature, 24)
                attributes = entity.state_attributes
                self.assertAlmostEqual(attributes["current_temperature"], current)
                self.assertAlmostEqual(attributes["temperature"], target)
                self.assertEqual(attributes["temperature_unit"], units.temperature_unit)
                capabilities = entity.capability_attributes
                self.assertAlmostEqual(capabilities["min_temp"], minimum)
                self.assertAlmostEqual(capabilities["max_temp"], maximum)
                self.assertEqual(entity.target_temperature_step, 1)


@unittest.skipUnless(NATIVE_API_AVAILABLE, "Requires the HA 2026.11 native Climate API")
class HomeKitNativeApiTests(unittest.IsolatedAsyncioTestCase):
    """Check real HAP capabilities with room state rendered by HA 2026.11."""

    async def test_off_auto_and_availability_mapping(self):
        from homeassistant.components.homekit.iidmanager import AccessoryIIDStorage
        from homeassistant.components.homekit.type_thermostats import Thermostat
        from homeassistant.core import HomeAssistant
        from pyhap.loader import get_loader

        from custom_components.linking_the_world_temp_ha.climate import RoomThermostat

        config_dir = self.enterContext(tempfile.TemporaryDirectory())
        hass = HomeAssistant(config_dir)
        self.addAsyncCleanup(hass.async_stop)
        hub = SimpleNamespace(
            entry=SimpleNamespace(entry_id="homekit-contract"),
            available=True,
            state=SimpleNamespace(power="ON", mode="cool"),
            thermostats={
                "test": SimpleNamespace(
                    power="ON", available=True, current_temperature=26.4,
                    target_temperature=24, humidity=58,
                )
            },
        )
        room = RoomThermostat(hub, "test")
        room.hass = hass
        room.entity_id = "climate.test_room"

        def publish():
            hass.states.async_set(
                room.entity_id, room.state if room.available else "unavailable",
                {
                    **room.capability_attributes, **room.state_attributes,
                    "supported_features": room.supported_features,
                },
            )

        publish()
        storage = AccessoryIIDStorage(hass, "homekit-contract")
        await storage.async_initialize()
        driver = Mock(loader=get_loader(), iid_storage=storage)
        accessory = Thermostat(hass, driver, "Room", room.entity_id, 2, {})
        accessory.run()
        self.addCleanup(accessory.async_stop)
        await hass.async_block_till_done()
        for mode, expected in (("cool", 3), ("heat", 3), ("fan_only", 0), ("dry", 0)):
            with self.subTest(mode=mode):
                hub.state.mode = mode
                publish()
                await hass.async_block_till_done()
                self.assertTrue(accessory.available)
                self.assertEqual(accessory.hc_homekit_to_hass, {0: "off", 3: "auto"})
                self.assertEqual(accessory.char_target_heat_cool.value, expected)

        hub.available = False
        publish()
        await hass.async_block_till_done()
        self.assertFalse(accessory.available)
        hub.available = True
        hub.state.mode = "heat"
        publish()
        await hass.async_block_till_done()
        self.assertTrue(accessory.available)
        self.assertEqual(accessory.char_target_heat_cool.value, 3)


if __name__ == "__main__":
    unittest.main()
