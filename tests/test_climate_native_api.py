"""Real HA 2026.11 API smoke tests, independent of the pytest HA fixture pin."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
