"""Mode and scene selects for Linking The World Temp HA."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import MODE_BY_LABEL, MODE_LABELS, SCENE_BY_LABEL, SCENE_LABELS
from .entity import LinkingTempEntity
from .hub import LinkingTempHub


class SystemModeSelect(LinkingTempEntity, SelectEntity):
    _attr_translation_key = "system_mode"

    def __init__(self, hub: LinkingTempHub) -> None:
        super().__init__(hub, "system_mode")

    @property
    def options(self) -> list[str]:
        """Expose a stable option set for HA and HomeKit accessories."""
        return list(MODE_BY_LABEL)

    @property
    def update_scopes(self) -> frozenset[str]:
        # The operation hint also depends on pending commands and the mode lock.
        return super().update_scopes | {"diagnostics"}

    @property
    def current_option(self) -> str | None:
        mode = self.hub.state.mode
        return MODE_LABELS.get(mode) if mode is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, str | bool]:
        if self.hub.state.power not in ("ON", "OFF"):
            hint = "科技系统总开关状态尚未确认"
        elif not self.hub.can_change_system_mode:
            hint = "总控开关状态正在变化，请稍后再切换模式"
        elif self.hub.state.power == "ON":
            hint = "切换模式时将自动关闭并恢复科技系统"
        else:
            hint = "可以切换模式"
        return {
            "can_change_mode": self.hub.can_change_system_mode,
            "提示": hint,
        }

    async def async_select_option(self, option: str) -> None:
        await self.hub.async_select_mode(MODE_BY_LABEL[option])


class SystemSceneSelect(LinkingTempEntity, SelectEntity):
    _attr_translation_key = "system_scene"

    def __init__(self, hub: LinkingTempHub) -> None:
        super().__init__(hub, "system_scene")
        self._attr_options = list(SCENE_BY_LABEL)

    @property
    def current_option(self) -> str | None:
        scene = self.hub.state.scene
        return SCENE_LABELS.get(scene) if scene is not None else None

    async def async_select_option(self, option: str) -> None:
        await self.hub.async_set_scene(SCENE_BY_LABEL[option])


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: LinkingTempHub = entry.runtime_data.hub
    async_add_entities([SystemModeSelect(hub), SystemSceneSelect(hub)])
