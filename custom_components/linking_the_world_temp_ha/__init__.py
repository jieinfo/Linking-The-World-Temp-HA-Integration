"""Linking The World Temp HA native integration."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, PLATFORMS
from .health import HealthTracker
from .hub import LinkingTempHub
from .panel_registry import async_remove_panel_storage
from .repairs import async_delete_entry_issues
from .runtime import LinkingTempConfigEntry, LinkingTempRuntime

_LOGGER = logging.getLogger(__name__)
_LEGACY_ENERGY_OPTION = "enable_experimental_energy_control"


def _register_controller_device(
    hass: HomeAssistant, entry: LinkingTempConfigEntry
) -> str:
    """Create the controller before child entities need its device id."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="Moorgen",
        model="MC7021",
        name="科技系统总控",
    )
    return device.id


def _remove_legacy_energy_artifacts(
    hass: HomeAssistant, entry: LinkingTempConfigEntry
) -> None:
    """Remove the retired read-only entity and experimental option."""
    if _LEGACY_ENERGY_OPTION in entry.options:
        options = dict(entry.options)
        options.pop(_LEGACY_ENERGY_OPTION)
        hass.config_entries.async_update_entry(entry, options=options)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_energy_saving"
    )
    if entity_id is not None:
        registry.async_remove(entity_id)


async def async_setup_entry(
    hass: HomeAssistant, entry: LinkingTempConfigEntry
) -> bool:
    """Set up one MC7021 controller."""
    _remove_legacy_energy_artifacts(hass, entry)
    health = HealthTracker()
    hub = LinkingTempHub(hass, entry, health)
    runtime = LinkingTempRuntime(
        hub=hub, health=health, panel_registry=hub.panel_registry
    )
    await hub.async_start()
    runtime_assigned = False
    try:
        entry.runtime_data = runtime
        runtime_assigned = True
        hub.controller_device_id = _register_controller_device(hass, entry)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
        return True
    except BaseException:
        try:
            await hub.async_stop()
        except BaseException:  # pragma: no cover - preserve the setup failure.
            _LOGGER.exception("Could not clean up controller runtime after setup failure")
        if runtime_assigned:
            try:
                entry.runtime_data = None
            except BaseException:  # pragma: no cover - preserve the setup failure.
                _LOGGER.exception("Could not clear failed controller runtime data")
        raise


async def async_unload_entry(
    hass: HomeAssistant, entry: LinkingTempConfigEntry
) -> bool:
    """Unload the controller and all native entities."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.hub.async_stop()
    return True


async def async_remove_entry(
    hass: HomeAssistant, entry: LinkingTempConfigEntry
) -> None:
    """Clean up private state that otherwise outlives a removed config entry."""
    async_delete_entry_issues(hass, entry.entry_id)
    await async_remove_panel_storage(hass, entry.entry_id)


async def _async_reload_entry(
    hass: HomeAssistant, entry: LinkingTempConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
