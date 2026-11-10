"""Repository-level checks for the HACS-only distribution."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD_REPOSITORY_URL = "https://github.com/jieinfo/" + (
    "Linking-The-World-Temp-HA-" + "Bridge"
)
NEW_REPOSITORY_URL = (
    "https://github.com/jieinfo/Linking-The-World-Temp-HA-Integration"
)


class RepositorySurfaceTests(unittest.TestCase):
    """Ensure the repository contains only the native integration delivery path."""

    def test_legacy_addon_delivery_files_are_absent(self) -> None:
        """The removed MQTT add-on must not remain installable from main."""
        retired_paths = (
            "bridge.py",
            "test_protocol.py",
            "scripts/sync_addon_bridge.py",
            "linking_the_world_temp_ha_bridge_addon",
        )

        self.assertEqual(
            [path for path in retired_paths if (ROOT / path).exists()],
            [],
        )

    def test_documentation_no_longer_describes_legacy_bridge_installation(self) -> None:
        """Public documentation must advertise the sole supported delivery path."""
        for path in (ROOT / "README.md", ROOT / "docs/TROUBLESHOOTING.md"):
            self.assertNotIn("旧 Bridge", path.read_text(encoding="utf-8"))

    def test_ci_gates_tagged_releases_on_release_metadata(self) -> None:
        """Tag builds must compare the tag with the three release documents."""
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertIn("tags: ['v*']", workflow)
        self.assertIn("scripts/release_metadata.py --tag", workflow)
        self.assertIn("pytest-homeassistant-custom-component==0.13.363", workflow)
        self.assertNotIn("'0.13.354'", workflow)
        self.assertNotIn("docker build", workflow)

    def test_ci_checks_quality_and_minimum_plus_latest_home_assistant(self) -> None:
        """CI must catch static defects and future stable HA compatibility changes."""
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertIn("ruff check", workflow)
        self.assertIn("mypy custom_components/linking_the_world_temp_ha", workflow)
        self.assertIn("name: minimum", workflow)
        self.assertIn("name: latest", workflow)
        self.assertIn("pytest-homeassistant-custom-component==0.13.363", workflow)

    def test_ci_checks_real_native_climate_api_without_pytest_fixture_pin(self) -> None:
        """A lagging pytest helper must not skip the new native API regression."""
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertIn("homeassistant==2026.11.1", workflow)
        self.assertIn("python -m unittest -v tests.test_climate_native_api", workflow)

    def test_public_repository_urls_use_integration_name(self) -> None:
        """Every maintained public link must use the renamed repository."""
        text_suffixes = {".json", ".md", ".py", ".yaml", ".yml"}
        old_url_locations: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in text_suffixes:
                continue
            if any(part in {".git", ".venv", "__pycache__"} for part in path.parts):
                continue
            if OLD_REPOSITORY_URL in path.read_text(encoding="utf-8"):
                old_url_locations.append(str(path.relative_to(ROOT)))

        self.assertEqual(old_url_locations, [])

        manifest = json.loads(
            (
                ROOT
                / "custom_components/linking_the_world_temp_ha/manifest.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["documentation"], NEW_REPOSITORY_URL)
        self.assertEqual(
            manifest["issue_tracker"], f"{NEW_REPOSITORY_URL}/issues"
        )

    def test_readme_is_task_oriented_and_points_to_detailed_guides(self) -> None:
        """The README must lead a new household from install to support."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for heading in (
            "## 快速开始",
            "## 支持的设备与功能",
            "## 配置集成",
            "## 设备与实体",
            "## 控制规则",
            "## 故障提醒与 Repairs",
            "## HomeKit",
            "## 升级与卸载",
            "## 获取帮助",
        ):
            self.assertIn(heading, readme)
        self.assertIn(NEW_REPOSITORY_URL, readme)
        self.assertIn("docs/TROUBLESHOOTING.md", readme)
        self.assertIn("docs/PRIVACY.md", readme)

    def test_readme_introduction_and_environment_scope_are_user_facing(self) -> None:
        """The introduction must avoid release and implementation trivia."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        introduction = readme.split("## 快速开始", maxsplit=1)[0]
        manifest = json.loads(
            (
                ROOT
                / "custom_components/linking_the_world_temp_ha/manifest.json"
            ).read_text(encoding="utf-8")
        )

        self.assertIsNone(re.search(r"`\d+\.\d+\.\d+` 是", introduction))
        for implementation_term in ("MQTT", "Mosquitto", "MT8157"):
            self.assertNotIn(implementation_term, introduction)
        self.assertIn(f"当前稳定版本：`{manifest['version']}`", readme)
        self.assertIn("| 环境状态 | 温度、湿度、PM2.5、CO2 |", readme)
        self.assertIn(
            "温度、湿度、PM2.5 和 CO2 均来自安装在新风管道、回风口或同类位置的独立传感器",
            readme,
        )

    def test_readme_uses_only_the_official_hacs_repository(self) -> None:
        """The README must not advertise third-party download proxies."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        hacs_steps = readme.split("## 快速开始", maxsplit=1)[1].split(
            "## 支持的设备与功能", maxsplit=1
        )[0]

        self.assertIn(NEW_REPOSITORY_URL, hacs_steps)
        self.assertNotIn("中国大陆网络访问", readme)
        self.assertNotIn("gh-proxy", readme)

    def test_system_environment_names_do_not_claim_controller_measurements(self) -> None:
        """Temperature and humidity come from field sensors, not the MC7021 itself."""
        for relative_path in (
            "custom_components/linking_the_world_temp_ha/strings.json",
            "custom_components/linking_the_world_temp_ha/translations/zh-Hans.json",
        ):
            document = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
            sensors = document["entity"]["sensor"]
            self.assertEqual(sensors["system_temperature"]["name"], "温度")
            self.assertEqual(sensors["system_humidity"]["name"], "湿度")

    def test_fault_names_follow_official_terminology(self) -> None:
        """Fault sensors and Repairs must use the two official fault names."""
        for relative_path in (
            "custom_components/linking_the_world_temp_ha/strings.json",
            "custom_components/linking_the_world_temp_ha/translations/zh-Hans.json",
        ):
            document = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
            binary_sensors = document["entity"]["binary_sensor"]
            sensors = document["entity"]["sensor"]
            issues = document["issues"]
            self.assertEqual(
                binary_sensors["system_fault"]["name"], "主机故障状态"
            )
            self.assertEqual(
                binary_sensors["filter_fault"]["name"], "新风滤网故障状态"
            )
            self.assertEqual(
                sensors["system_fault_code"]["name"], "主机故障原始码"
            )
            self.assertEqual(
                sensors["filter_fault_code"]["name"], "新风滤网故障原始码"
            )
            self.assertEqual(
                sensors["system_fault_code"]["state"]["healthy"], "无故障"
            )
            self.assertEqual(
                sensors["filter_fault_code"]["state"]["healthy"], "无故障"
            )
            self.assertEqual(issues["system_fault"]["title"], "主机故障")
            self.assertEqual(issues["filter_fault"]["title"], "新风滤网故障")

        english = json.loads(
            (
                ROOT
                / "custom_components/linking_the_world_temp_ha/translations/en.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            english["entity"]["binary_sensor"]["system_fault"]["name"],
            "Controller fault status",
        )
        self.assertEqual(
            english["entity"]["binary_sensor"]["filter_fault"]["name"],
            "Fresh-air filter fault status",
        )
        self.assertEqual(
            english["entity"]["sensor"]["system_fault_code"]["name"],
            "Controller fault raw code",
        )
        self.assertEqual(
            english["entity"]["sensor"]["filter_fault_code"]["name"],
            "Fresh-air filter fault raw code",
        )
        self.assertEqual(
            english["entity"]["sensor"]["system_fault_code"]["state"]["healthy"],
            "No fault",
        )
        self.assertEqual(
            english["entity"]["sensor"]["filter_fault_code"]["state"]["healthy"],
            "No fault",
        )
        self.assertEqual(english["issues"]["system_fault"]["title"], "Controller fault")
        self.assertEqual(
            english["issues"]["filter_fault"]["title"],
            "Fresh-air filter fault",
        )

    def test_home_assistant_2026_9_is_the_only_supported_baseline(self) -> None:
        """Distribution metadata and CI must no longer advertise HA 2026.8."""
        hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertEqual(hacs["homeassistant"], "2026.9.0")
        self.assertNotIn("0.13.354", workflow)
        self.assertIn("0.13.363", workflow)
        self.assertNotIn("2026.8.0", readme)
        self.assertIn("2026.9.0", readme)

    def test_sensor_units_use_current_home_assistant_enums(self) -> None:
        """HA 2026.7+ concentration units must not emit deprecation warnings."""
        sensor_source = (
            ROOT / "custom_components/linking_the_world_temp_ha/sensor.py"
        ).read_text(encoding="utf-8")

        self.assertIn("UnitOfDensity.MICROGRAMS_PER_CUBIC_METER", sensor_source)
        self.assertIn("UnitOfRatio.PARTS_PER_MILLION", sensor_source)
        self.assertNotIn("CONCENTRATION_MICROGRAMS_PER_CUBIC_METER", sensor_source)
        self.assertNotIn("CONCENTRATION_PARTS_PER_MILLION", sensor_source)

    def test_energy_control_is_a_single_non_experimental_entity(self) -> None:
        """Energy saving must be one permanent switch without a duplicate sensor."""
        component = ROOT / "custom_components/linking_the_world_temp_ha"
        combined_source = "\n".join(
            path.read_text(encoding="utf-8") for path in component.glob("*.py")
        )
        strings = json.loads((component / "strings.json").read_text(encoding="utf-8"))

        self.assertNotIn("CONF_ENABLE_EXPERIMENTAL_ENERGY_CONTROL", combined_source)
        self.assertNotIn("EnergySavingSensor", combined_source)
        self.assertNotIn("energy_saving", strings["entity"]["binary_sensor"])
        self.assertEqual(
            strings["entity"]["switch"]["energy_control"]["name"],
            "节能控制",
        )
