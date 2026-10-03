"""Privacy-preserving diagnostics support."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .hub import LinkingTempHub
from .panel_registry import PanelRecord

_COMMAND_STATES = frozenset(
    {
        "idle",
        "waiting",
        "queued",
        "confirmed",
        "retrying",
        "timeout",
        "timeout_continuing",
        "failed",
    }
)
_PROTOCOL_STATUSES = frozenset({"waiting", "verified"})


def build_anonymous_panel_map(records: Mapping[str, PanelRecord]) -> dict[str, str]:
    """Assign deterministic labels used only in this one diagnostics export."""
    return {
        mac_hex: f"panel_{index:02d}"
        for index, mac_hex in enumerate(sorted(records), start=1)
    }


def _anonymous_room_map(records: Mapping[str, PanelRecord]) -> dict[str, str]:
    """Assign deterministic room labels without exporting IDs or room names."""
    return {
        room_id: f"room_{index:02d}"
        for index, room_id in enumerate(
            sorted({record.room_id for record in records.values()}), start=1
        )
    }


def _anonymous_thermostats(hub: LinkingTempHub) -> dict[str, dict[str, Any]]:
    """Export useful panel facts while excluding every household identifier."""
    records = hub.panel_registry.records
    panel_labels = build_anonymous_panel_map(records)
    room_labels = _anonymous_room_map(records)
    now_utc = datetime.now(UTC)
    panels: dict[str, dict[str, Any]] = {}
    for mac_hex, label in panel_labels.items():
        record = records[mac_hex]
        state = hub.thermostats.get(mac_hex)
        age = (
            max(0.0, (now_utc - record.last_report_utc).total_seconds())
            if record.last_report_utc is not None
            else None
        )
        panels[label] = {
            "room": room_labels.get(record.room_id, "room_unknown"),
            "available": record.available,
            "last_report_age_seconds": round(age, 3) if age is not None else None,
            "observed_absence_seconds": round(
                record.monitored_absence_seconds, 3
            ),
            "power": state.power if state is not None else None,
            "target_temperature": (
                state.target_temperature if state is not None else None
            ),
            "current_temperature": (
                state.current_temperature if state is not None else None
            ),
            "humidity": state.humidity if state is not None else None,
        }
    return panels


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    hub: LinkingTempHub = runtime.hub
    return {
        # Do not export entry data/options wholesale.  Future configuration
        # fields may contain credentials, keys, or vendor-specific payloads.
        "configuration": {
            "port": hub.port,
            "allow_control": hub.allow_control,
            "command_min_interval": hub.command_min_interval,
            "command_confirmation_timeout": hub.command_confirmation_timeout,
            "controller_silence_timeout": hub.controller_silence_timeout,
            "thermostat_offline_after": hub.thermostat_offline_after,
        },
        "runtime": {
            "connected": hub.connected,
            "protocol_verified": hub.protocol_verified,
            "connection_stage": runtime.health.stage.value,
            "connection_failure_kind": runtime.health.failure_kind.value,
            "protocol_status": _protocol_status(hub.protocol_status),
            "last_command_state": _command_state(hub.last_command_status),
            "control_permission": hub.control_permission,
            "status_freshness": hub.status_freshness(),
            "system_state": {
                "power": hub.state.power,
                "mode": hub.state.mode,
                "scene": hub.state.scene,
                "winter_humidifier": hub.state.winter_humidifier,
                "energy_saving": hub.state.energy_saving,
                "temperature": hub.state.temperature,
                "humidity": hub.state.humidity,
                "pm25": hub.state.pm25,
                "co2": hub.state.co2,
                "system_fault_code": hub.state.system_fault_code,
                "filter_fault_code": hub.state.filter_fault_code,
            },
            "command_queue": {
                "pending": len(hub._pending),
                "queued": sum(len(commands) for commands in hub._queued.values()),
            },
            "command_history": runtime.health.command_history(
                build_anonymous_panel_map(hub.panel_registry.records)
            ),
            "parser_anomalies": [dict(anomaly) for anomaly in hub.parser_anomalies],
            "thermostat_count": len(hub.thermostats),
            "panels": _anonymous_thermostats(hub),
            "health": runtime.health.snapshot(),
        },
    }


def _command_state(status: object) -> str:
    """Reduce a command status to its stable state prefix for diagnostics."""
    if not isinstance(status, str):
        return "unknown"
    state = status.partition(":")[0]
    return state if state in _COMMAND_STATES else "unknown"


def _protocol_status(status: object) -> str:
    """Return the small supported protocol-status vocabulary only."""
    return status if isinstance(status, str) and status in _PROTOCOL_STATUSES else "unknown"
