"""Actionable connection Repair lifecycle tests."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.linking_the_world_temp_ha.const import DOMAIN
from custom_components.linking_the_world_temp_ha.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.linking_the_world_temp_ha.health import HealthTracker
from custom_components.linking_the_world_temp_ha.hub import LinkingTempHub
from custom_components.linking_the_world_temp_ha.panel_registry import (
    STALE_PANEL_SECONDS,
    PanelRecord,
    PanelRegistry,
)
from custom_components.linking_the_world_temp_ha.protocol import (
    AuthenticationRejected,
    TcpConnectError,
    ThermostatState,
)
from custom_components.linking_the_world_temp_ha.repairs import (
    RepairManager,
    async_create_fix_flow,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _issue(hass, issue_id: str):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


def _stale_issue_id(entry_id: str, mac_hex: str) -> str:
    return f"stale_panel_{entry_id}_{mac_hex}"


def _fault_issue_id(entry_id: str, fault_type: str) -> str:
    return f"{fault_type}_fault_{entry_id}"


def _panel_unique_id_prefix(entry_id: str, mac_hex: str) -> str:
    return f"{entry_id}_thermostat_{mac_hex}_"


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


async def test_fix_flow_rejects_incomplete_or_unknown_issue_metadata(hass) -> None:
    """Repair routing refuses malformed issue metadata before changing HA state."""
    with pytest.raises(ValueError, match="config entry"):
        await async_create_fix_flow(hass, "login_timeout_x", None)
    with pytest.raises(ValueError, match="panel identity"):
        await async_create_fix_flow(
            hass, "stale_panel_x", {"entry_id": "entry"}
        )
    with pytest.raises(ValueError, match="Unsupported"):
        await async_create_fix_flow(hass, "unknown_x", {"entry_id": "entry"})


@pytest.mark.parametrize("fault_type", ["system", "filter"])
def test_fault_repair_is_error_scoped_and_clears_on_zero(
    hass, mock_config_entry, fault_type: str, caplog
) -> None:
    """A controller fault stays visible until a valid zero report clears it."""
    manager = RepairManager(hass, mock_config_entry)
    issue_id = _fault_issue_id(mock_config_entry.entry_id, fault_type)

    with caplog.at_level(logging.INFO):
        manager.set_fault_code(fault_type, 7)
        issue = _issue(hass, issue_id)
        assert issue is not None
        assert issue.severity is ir.IssueSeverity.ERROR
        assert issue.is_persistent
        assert issue.translation_placeholders == {"code": "7"}

        manager.set_fault_code(fault_type, 0)

    assert _issue(hass, issue_id) is None
    assert any("fault cleared" in record.message for record in caplog.records)


def test_fault_repair_deduplicates_repeats_and_updates_changed_code(
    hass, mock_config_entry, caplog
) -> None:
    """Repeated status pushes do not spam logs; a changed code remains visible."""
    manager = RepairManager(hass, mock_config_entry)
    issue_id = _fault_issue_id(mock_config_entry.entry_id, "system")

    with caplog.at_level(logging.ERROR):
        manager.set_fault_code("system", 7)
        manager.set_fault_code("system", 7)
        manager.set_fault_code("system", 9)

    issue = _issue(hass, issue_id)
    assert issue is not None
    assert issue.translation_placeholders == {"code": "9"}
    fault_logs = [
        record for record in caplog.records if "reported system fault" in record.message
    ]
    assert len(fault_logs) == 2


async def _make_stale_hub(hass, mock_config_entry):
    """Create one hub with a panel exactly at the observed-absence threshold."""
    now = _utc("2026-08-24T00:00:00")
    monotonic = [0.0]
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub.panel_registry = PanelRegistry(
        hass,
        mock_config_entry.entry_id,
        clock=lambda: now,
        monotonic_clock=lambda: monotonic[0],
    )
    await hub.panel_registry.async_load()
    await hub.panel_registry.async_note_panel_report(
        "ff00ffffffff01ff", "r0100", now
    )
    await hub.panel_registry.async_set_room_name("r0100", "客餐厅")
    await hub.panel_registry.async_note_status_stream(now)
    now += timedelta(seconds=STALE_PANEL_SECONDS)
    monotonic[0] += STALE_PANEL_SECONDS
    await hub.panel_registry.async_note_status_stream(now)
    await hub.panel_registry.async_set_panel_available(
        "ff00ffffffff01ff", False
    )
    return hub


async def test_stale_panel_repair_is_created_once_at_threshold_and_clears_on_report(
    hass, mock_config_entry
) -> None:
    """A valid return report resolves the one deduplicated stale-panel Repair."""
    hub = await _make_stale_hub(hass, mock_config_entry)
    mac_hex = "ff00ffffffff01ff"
    issue_id = _stale_issue_id(mock_config_entry.entry_id, mac_hex)

    await hub.async_sync_stale_panel_repairs()
    issue = _issue(hass, issue_id)
    assert issue is not None
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.data == {"entry_id": mock_config_entry.entry_id, "mac_hex": mac_hex}
    assert mac_hex not in str(issue.translation_placeholders)

    await hub.async_sync_stale_panel_repairs()
    assert len([key for key in ir.async_get(hass).issues if key[1] == issue_id]) == 1

    await hub.async_note_panel_report(mac_hex, "r0100", _utc("2026-09-24T00:00:00"))
    assert _issue(hass, issue_id) is None


async def test_stale_panel_repair_does_not_exist_one_second_before_threshold(
    hass, mock_config_entry
) -> None:
    """Observed absence needs the full thirty days before it becomes a Repair."""
    now = _utc("2026-08-24T00:00:00")
    monotonic = [0.0]
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub.panel_registry = PanelRegistry(
        hass,
        mock_config_entry.entry_id,
        clock=lambda: now,
        monotonic_clock=lambda: monotonic[0],
    )
    await hub.panel_registry.async_load()
    await hub.panel_registry.async_note_panel_report(
        "ff00ffffffff01ff", "r0100", now
    )
    await hub.panel_registry.async_note_status_stream(now)
    now += timedelta(seconds=STALE_PANEL_SECONDS - 1)
    monotonic[0] += STALE_PANEL_SECONDS - 1
    await hub.panel_registry.async_note_status_stream(now)
    await hub.async_sync_stale_panel_repairs()

    assert _issue(
        hass, _stale_issue_id(mock_config_entry.entry_id, "ff00ffffffff01ff")
    ) is None


async def test_stale_panel_repair_cancel_keeps_registry_and_home_assistant_records(
    hass, mock_config_entry
) -> None:
    """Opening then cancelling a repair never changes a panel or registry records."""
    hub = await _make_stale_hub(hass, mock_config_entry)
    mac_hex = "ff00ffffffff01ff"
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.runtime_data = SimpleNamespace(hub=hub)
    await hub.async_sync_stale_panel_repairs()
    issue_id = _stale_issue_id(mock_config_entry.entry_id, mac_hex)

    flow = await async_create_fix_flow(
        hass,
        issue_id,
        {"entry_id": mock_config_entry.entry_id, "mac_hex": mac_hex},
    )
    flow.hass = hass
    flow.handler = DOMAIN
    result = await flow.async_step_confirm()

    assert result["type"] == "form"
    assert mac_hex in hub.panel_registry.records
    assert _issue(hass, issue_id) is not None


async def test_diagnostics_do_not_export_full_panel_macs_for_stale_panel_work(
    hass, mock_config_entry
) -> None:
    """Stale-panel diagnostics retain state facts without household MAC addresses."""
    hub = await _make_stale_hub(hass, mock_config_entry)
    mac_hex = "ff00ffffffff01ff"
    hub.thermostats[mac_hex] = ThermostatState(
        mac=bytes.fromhex(mac_hex), room_id="r0100", available=False
    )
    mock_config_entry.runtime_data = SimpleNamespace(hub=hub, health=hub.health)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert mac_hex not in str(diagnostics)
    assert "r0100" not in str(diagnostics)


async def test_diagnostics_export_only_safe_runtime_status_categories(
    hass, mock_config_entry
) -> None:
    """Runtime text cannot leak controller or household identities to diagnostics."""
    hub = await _make_stale_hub(hass, mock_config_entry)
    mac_hex = "ff00ffffffff01ff"
    room_name = "客餐厅"
    host_canaries = ("10.10.1.246", "house-controller.lan", "2001:db8::246")
    hub.last_connection_error = (
        "Could not connect to 10.10.1.246, house-controller.lan, "
        "or [2001:db8::246]:9000"
    )
    hub.last_command_status = f"waiting:{room_name} 温控面板 mac={mac_hex}"
    hub.thermostats[mac_hex] = ThermostatState(
        mac=bytes.fromhex(mac_hex), room_id="r0100", available=False
    )
    mock_config_entry.runtime_data = SimpleNamespace(hub=hub, health=hub.health)

    serialized = json.dumps(
        await async_get_config_entry_diagnostics(hass, mock_config_entry),
        ensure_ascii=False,
        sort_keys=True,
    )

    for canary in (*host_canaries, mac_hex, room_name, "r0100"):
        assert canary not in serialized
    assert "connection_failure_kind" in serialized
    assert "last_command_state" in serialized


async def test_stale_panel_confirm_aborts_when_a_report_resolves_old_form(
    hass, mock_config_entry
) -> None:
    """A stale Repair form cannot delete a panel that reported before submit."""
    hub = await _make_stale_hub(hass, mock_config_entry)
    mac_hex = "ff00ffffffff01ff"
    entry_id = mock_config_entry.entry_id
    issue_id = _stale_issue_id(entry_id, mac_hex)
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.runtime_data = SimpleNamespace(hub=hub)
    await hub.async_sync_stale_panel_repairs()

    flow = await async_create_fix_flow(
        hass, issue_id, {"entry_id": entry_id, "mac_hex": mac_hex}
    )
    flow.hass = hass
    flow.handler = DOMAIN
    assert (await flow.async_step_confirm())["type"] == "form"

    await hub.async_note_panel_report(mac_hex, "r0100", _utc("2026-09-24T00:00:00"))
    result = await flow.async_step_confirm({})

    assert result["type"] == "abort"
    assert result["reason"] == "panel_no_longer_stale"
    assert mac_hex in hub.panel_registry.records
    assert _issue(hass, issue_id) is None


async def test_stale_panel_confirm_and_report_are_serialized_for_rediscovery(
    hass, mock_config_entry
) -> None:
    """A report racing a confirmed cleanup waits, then recreates the panel."""
    hub = await _make_stale_hub(hass, mock_config_entry)
    mac_hex = "ff00ffffffff01ff"
    entry_id = mock_config_entry.entry_id
    issue_id = _stale_issue_id(entry_id, mac_hex)
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.runtime_data = SimpleNamespace(hub=hub)
    await hub.async_sync_stale_panel_repairs()

    flow = await async_create_fix_flow(
        hass, issue_id, {"entry_id": entry_id, "mac_hex": mac_hex}
    )
    flow.hass = hass
    flow.handler = DOMAIN

    async with hub._panel_lifecycle_lock:
        confirm_task = asyncio.create_task(flow.async_step_confirm({}))
        await asyncio.sleep(0)
        report_task = asyncio.create_task(
            hub.async_note_panel_report(mac_hex, "r0100", _utc("2026-09-24T00:00:00"))
        )
        await asyncio.sleep(0)
        assert not confirm_task.done()
        assert not report_task.done()

    assert (await confirm_task)["type"] == "create_entry"
    assert await report_task
    assert mac_hex in hub.panel_registry.records
    assert _issue(hass, issue_id) is None


async def test_stale_panel_repair_confirm_removes_only_owned_records_then_rediscovery(
    hass, mock_config_entry
) -> None:
    """Confirmed cleanup removes owned records and the same MAC can reappear once."""
    hub = await _make_stale_hub(hass, mock_config_entry)
    mac_hex = "ff00ffffffff01ff"
    entry_id = mock_config_entry.entry_id
    issue_id = _stale_issue_id(entry_id, mac_hex)
    prefix = _panel_unique_id_prefix(entry_id, mac_hex)
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.runtime_data = SimpleNamespace(hub=hub)

    device_registry = dr.async_get(hass)
    panel_device = device_registry.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, f"{entry_id}_{mac_hex}")},
    )
    entity_registry = er.async_get(hass)
    owned_entities = [
        entity_registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{prefix}{suffix}",
            config_entry=mock_config_entry,
            device_id=panel_device.id,
        )
        for domain, suffix in (
            ("climate", "climate"),
            ("sensor", "automation_temperature"),
            ("sensor", "automation_humidity"),
        )
    ]
    unrelated = entity_registry.async_get_or_create(
        "sensor",
        "unrelated_platform",
        "unrelated_panel_entity",
        device_id=panel_device.id,
    )
    await hub.async_sync_stale_panel_repairs()

    flow = await async_create_fix_flow(
        hass,
        issue_id,
        {"entry_id": entry_id, "mac_hex": mac_hex},
    )
    flow.hass = hass
    flow.handler = DOMAIN
    result = await flow.async_step_confirm({})

    assert result["type"] == "create_entry"
    assert mac_hex not in hub.panel_registry.records
    assert _issue(hass, issue_id) is None
    assert all(entity_registry.async_get(item.entity_id) is None for item in owned_entities)
    assert entity_registry.async_get(unrelated.entity_id) is not None
    assert device_registry.async_get(panel_device.id) is not None

    entity_registry.async_remove(unrelated.entity_id)
    is_new = await hub.async_note_panel_report(
        mac_hex, "r0100", _utc("2026-09-24T00:00:00")
    )
    assert is_new
    replacement = device_registry.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, f"{entry_id}_{mac_hex}")},
    )
    recreated = [
        entity_registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{prefix}{suffix}",
            config_entry=mock_config_entry,
            device_id=replacement.id,
        )
        for domain, suffix in (
            ("climate", "climate"),
            ("sensor", "automation_temperature"),
            ("sensor", "automation_humidity"),
        )
    ]
    assert len({item.entity_id for item in recreated}) == 3
    assert len(
        [
            item
            for item in er.async_entries_for_config_entry(entity_registry, entry_id)
            if item.unique_id.startswith(prefix)
        ]
    ) == 3
    assert _issue(hass, issue_id) is None


async def test_stale_panel_confirm_removes_device_when_no_unrelated_entities(
    hass, mock_config_entry, monkeypatch
) -> None:
    """A panel-only device is removed after every owned entity registry entry."""
    hub = await _make_stale_hub(hass, mock_config_entry)
    mac_hex = "ff00ffffffff01ff"
    entry_id = mock_config_entry.entry_id
    prefix = _panel_unique_id_prefix(entry_id, mac_hex)
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.runtime_data = SimpleNamespace(hub=hub)
    device_registry = dr.async_get(hass)
    panel_device = device_registry.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, f"{entry_id}_{mac_hex}")},
    )
    monkeypatch.setattr(
        device_registry,
        "async_get_device",
        lambda *_args, **_kwargs: pytest.fail(
            "stale-panel cleanup used the deprecated ambiguous device lookup"
        ),
    )
    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        "climate",
        DOMAIN,
        f"{prefix}climate",
        config_entry=mock_config_entry,
        device_id=panel_device.id,
    )
    await hub.async_sync_stale_panel_repairs()
    mock_config_entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    reloads: list[str] = []
    monkeypatch.setattr(
        hass.config_entries,
        "async_schedule_reload",
        lambda entry_id: reloads.append(entry_id),
    )
    flow = await async_create_fix_flow(
        hass,
        _stale_issue_id(entry_id, mac_hex),
        {"entry_id": entry_id, "mac_hex": mac_hex},
    )
    flow.hass = hass
    flow.handler = DOMAIN

    await flow.async_step_confirm({})

    assert device_registry.async_get(panel_device.id) is None
    assert mac_hex not in hub.panel_registry.records
    assert reloads == [entry_id]


async def test_stale_panel_registry_failure_keeps_source_record_for_retry(
    hass, mock_config_entry, monkeypatch
) -> None:
    """A partial registry cleanup never discards the persisted panel source."""
    hub = await _make_stale_hub(hass, mock_config_entry)
    mac_hex = "ff00ffffffff01ff"
    entry_id = mock_config_entry.entry_id
    prefix = _panel_unique_id_prefix(entry_id, mac_hex)
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.runtime_data = SimpleNamespace(hub=hub)
    device_registry = dr.async_get(hass)
    panel_device = device_registry.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, f"{entry_id}_{mac_hex}")},
    )
    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        "climate",
        DOMAIN,
        f"{prefix}climate",
        config_entry=mock_config_entry,
        device_id=panel_device.id,
    )
    await hub.async_sync_stale_panel_repairs()
    flow = await async_create_fix_flow(
        hass,
        _stale_issue_id(entry_id, mac_hex),
        {"entry_id": entry_id, "mac_hex": mac_hex},
    )
    flow.hass = hass
    flow.handler = DOMAIN

    def fail_remove(_entity_id: str) -> None:
        raise RuntimeError("entity registry unavailable")

    monkeypatch.setattr(entity_registry, "async_remove", fail_remove)
    with pytest.raises(RuntimeError, match="entity registry unavailable"):
        await flow.async_step_confirm({})

    assert mac_hex in hub.panel_registry.records
    assert _issue(hass, _stale_issue_id(entry_id, mac_hex)) is not None


async def test_login_timeout_repair_starts_after_three_consecutive_failures(
    hass, mock_config_entry
) -> None:
    """Ambiguous login timeouts become actionable only after three attempts."""
    manager = RepairManager(hass, mock_config_entry)
    issue_id = f"login_timeout_{mock_config_entry.entry_id}"

    await manager.async_set_login_timeout(True)
    await manager.async_set_login_timeout(True)
    assert _issue(hass, issue_id) is None

    await manager.async_set_login_timeout(True)
    issue = _issue(hass, issue_id)
    assert issue is not None
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.data == {"entry_id": mock_config_entry.entry_id}

    await manager.async_set_login_timeout(True)
    assert len(
        [item for item in ir.async_get(hass).issues if item[1] == issue_id]
    ) == 1


async def test_command_timeout_repair_starts_after_three_and_clears_on_recovery(
    hass, mock_config_entry
) -> None:
    """Repeated final control failures become visible and one success resolves them."""
    manager = RepairManager(hass, mock_config_entry)
    issue_id = f"command_timeout_{mock_config_entry.entry_id}"

    manager.set_command_timeout(True)
    manager.set_command_timeout(True)
    assert _issue(hass, issue_id) is None

    manager.set_command_timeout(True)
    issue = _issue(hass, issue_id)
    assert issue is not None
    assert not issue.is_fixable
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.data == {"entry_id": mock_config_entry.entry_id}

    manager.set_command_timeout(False)
    assert _issue(hass, issue_id) is None
    assert manager.consecutive_command_timeouts == 0


async def test_successful_login_clears_login_and_protocol_repairs(
    hass, mock_config_entry
) -> None:
    """A verified authenticated session resolves the two connection Repairs."""
    manager = RepairManager(hass, mock_config_entry)
    login_issue_id = f"login_timeout_{mock_config_entry.entry_id}"
    protocol_issue_id = f"protocol_incompatible_{mock_config_entry.entry_id}"

    for _ in range(3):
        await manager.async_set_login_timeout(True)
    await manager.async_set_protocol_incompatible(True)
    assert _issue(hass, login_issue_id) is not None
    assert _issue(hass, protocol_issue_id) is not None

    await manager.async_set_login_timeout(False)
    await manager.async_set_protocol_incompatible(False)
    assert _issue(hass, login_issue_id) is None
    assert _issue(hass, protocol_issue_id) is None
    assert manager.consecutive_login_timeouts == 0


async def test_tcp_failure_does_not_create_connection_repairs(
    hass, mock_config_entry
) -> None:
    """Network failures remain reconnect/diagnostic events, not credential Repairs."""
    assert _issue(hass, f"login_timeout_{mock_config_entry.entry_id}") is None
    assert _issue(hass, f"protocol_incompatible_{mock_config_entry.entry_id}") is None


async def test_login_timeout_repair_opens_entry_linked_reauth(
    hass, mock_config_entry, monkeypatch
) -> None:
    """The fix button directs users to reauthenticate this exact config entry."""
    manager = RepairManager(hass, mock_config_entry)
    for _ in range(3):
        await manager.async_set_login_timeout(True)
    reauth_calls: list[object] = []

    def start_reauth(hass_arg) -> None:
        reauth_calls.append(hass_arg)

    mock_config_entry.add_to_hass(hass)
    monkeypatch.setattr(mock_config_entry, "async_start_reauth", start_reauth)
    flow = await async_create_fix_flow(
        hass,
        f"login_timeout_{mock_config_entry.entry_id}",
        {"entry_id": mock_config_entry.entry_id},
    )
    flow.hass = hass
    flow.handler = DOMAIN

    result = await flow.async_step_init({})
    assert result["type"] == "create_entry"
    assert reauth_calls == [hass]


async def test_runtime_authentication_rejection_starts_reauth_and_stops_retrying(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Known-invalid credentials pause reconnects until the entry is reloaded."""
    import custom_components.linking_the_world_temp_ha.hub as hub_module

    calls = 0
    reauth_calls: list[object] = []

    class RejectingClient:
        def __init__(self, *_args) -> None:
            nonlocal calls
            calls += 1
            self.reader_error = None
            self.on_frame = None
            self.on_status = None
            self.on_stage = None
            self.on_parser_event = None

        async def connect(self) -> None:
            raise AuthenticationRejected("MC7021 rejected the supplied credentials")

        async def close(self) -> None:
            return None

    def start_reauth(hass_arg) -> None:
        reauth_calls.append(hass_arg)

    monkeypatch.setattr(hub_module, "AsyncMoorgenClient", RejectingClient)
    monkeypatch.setattr(mock_config_entry, "async_start_reauth", start_reauth)
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    await hub._async_run()

    assert calls == 1
    assert reauth_calls == [hass]
    assert hub._client is None
    assert hub._reauth_required
    assert not hub.connected


async def test_runtime_rejection_waits_for_conflicting_reconfigure_then_starts_reauth(
    hass, mock_config_entry, monkeypatch
) -> None:
    """A cancelled reconfigure flow must not strand an entry with rejected auth."""
    import custom_components.linking_the_world_temp_ha.hub as hub_module

    calls = 0

    class RejectingClient:
        def __init__(self, *_args) -> None:
            nonlocal calls
            calls += 1
            self.reader_error = None
            self.on_frame = None
            self.on_status = None
            self.on_stage = None
            self.on_parser_event = None

        async def connect(self) -> None:
            raise AuthenticationRejected("MC7021 rejected the supplied credentials")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(hub_module, "AsyncMoorgenClient", RejectingClient)
    monkeypatch.setattr(hub_module, "REAUTH_FLOW_WATCH_INTERVAL", 0.01)
    mock_config_entry.add_to_hass(hass)

    reconfigure = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": mock_config_entry.entry_id},
        data=None,
    )
    assert reconfigure["type"] == "form"
    assert reconfigure["step_id"] == "reconfigure"

    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    await hub._async_run()
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(
        DOMAIN, match_context={"entry_id": mock_config_entry.entry_id}
    )
    assert [flow["context"]["source"] for flow in flows] == ["reconfigure"]
    assert hub._reauth_required
    assert calls == 1

    hass.config_entries.flow.async_abort(reconfigure["flow_id"])
    for _ in range(50):
        await asyncio.sleep(0.01)
        flows = hass.config_entries.flow.async_progress_by_handler(
            DOMAIN, match_context={"entry_id": mock_config_entry.entry_id}
        )
        if (
            [flow["context"]["source"] for flow in flows] == ["reauth"]
            and hub._reauth_watcher is None
        ):
            break
    else:
        pytest.fail("reauth did not start after the conflicting flow was cancelled")

    assert calls == 1
    assert hub._reauth_watcher is None
    await hub.async_stop()
    hass.config_entries.flow.async_abort(flows[0]["flow_id"])
    await hass.async_block_till_done()


async def test_removing_entry_clears_entry_linked_connection_repairs(
    hass, mock_config_entry
) -> None:
    """Entry removal cannot leave an unfixable connection Repair behind."""
    mock_config_entry.add_to_hass(hass)
    manager = RepairManager(hass, mock_config_entry)
    for _ in range(3):
        await manager.async_set_login_timeout(True)
    await manager.async_set_protocol_incompatible(True)
    stale_mac = "ff00ffffffff01ff"
    await manager.async_set_stale_panel(
        PanelRecord(
            mac_hex=stale_mac,
            room_id="r0100",
            first_seen_utc=_utc("2026-08-01T00:00:00"),
            last_report_utc=_utc("2026-08-02T00:00:00"),
            available=False,
            monitored_absence_seconds=STALE_PANEL_SECONDS,
            checkpoint_utc=None,
            last_temperature=None,
            last_humidity=None,
        ),
        "客餐厅",
    )

    login_issue_id = f"login_timeout_{mock_config_entry.entry_id}"
    protocol_issue_id = f"protocol_incompatible_{mock_config_entry.entry_id}"
    assert _issue(hass, login_issue_id) is not None
    assert _issue(hass, protocol_issue_id) is not None
    assert _issue(hass, _stale_issue_id(mock_config_entry.entry_id, stale_mac)) is not None

    await hass.config_entries.async_remove(mock_config_entry.entry_id)

    assert _issue(hass, login_issue_id) is None
    assert _issue(hass, protocol_issue_id) is None
    assert _issue(hass, _stale_issue_id(mock_config_entry.entry_id, stale_mac)) is None


async def test_runtime_tcp_failure_does_not_create_login_or_protocol_repairs(
    hass, mock_config_entry
) -> None:
    """A network failure must not be misrepresented as credentials or protocol work."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub._record_connection_failure(TcpConnectError("connection refused"))
    await hass.async_block_till_done()

    assert _issue(hass, f"login_timeout_{mock_config_entry.entry_id}") is None
    assert _issue(hass, f"protocol_incompatible_{mock_config_entry.entry_id}") is None
