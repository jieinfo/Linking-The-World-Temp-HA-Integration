"""Real Home Assistant integration setup tests."""

import asyncio
import importlib
import sys
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.linking_the_world_temp_ha.binary_sensor import (
    ControllerConnectionSensor,
)
from custom_components.linking_the_world_temp_ha.const import DOMAIN
from custom_components.linking_the_world_temp_ha.health import HealthTracker
from custom_components.linking_the_world_temp_ha.hub import LinkingTempHub
from custom_components.linking_the_world_temp_ha.protocol import (
    AsyncMoorgenClient,
    YasHcpDecoder,
    YasHcpFrame,
    tlv,
)
from custom_components.linking_the_world_temp_ha.runtime import (
    ConnectionStage,
    LinkingTempRuntime,
)
from tests.helpers import FakeControllerBehavior, FakeMC7021Server

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _system_status(hub, *, power: bool = True) -> bytes:
    return (
        tlv(0x0004, hub.tech_system_mac)
        + tlv(0x000B, bytes((power,)))
        + tlv(0x000A, bytes.fromhex("0101000049013e003200f0010000"))
    )


def _thermostat_status(hub, mac_hex: str) -> bytes:
    return (
        tlv(0x0004, bytes.fromhex(mac_hex))
        + tlv(0x0075, hub.tech_system_mac)
        + tlv(0x0030, b"r0100")
        + tlv(0x000A, bytes((44, 0xF6, 0x00, 58, 0)))
        + tlv(0x000B, b"\x01")
    )


async def _wait_for(predicate, *, timeout: float = 1) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


def _integration_tasks() -> list[asyncio.Task[object]]:
    """Return every task name the integration creates during normal operation."""
    prefixes = (
        f"{DOMAIN}_",
        "linking-temp-mc7021-reader",
        "linking-temp-connection-repair",
    )
    return [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith(prefixes)
        and not task.done()
    ]


@pytest_asyncio.fixture
async def managed_integration(
    hass, setup_integration, mock_config_entry, fake_controller
):
    """Ensure a failing lifecycle assertion cannot strand its TCP peer."""
    try:
        yield setup_integration
    finally:
        try:
            if mock_config_entry.state is ConfigEntryState.LOADED:
                await asyncio.wait_for(
                    hass.config_entries.async_unload(mock_config_entry.entry_id), timeout=1
                )
        finally:
            await fake_controller.async_stop()


async def test_setup_uses_real_home_assistant(
    hass, mock_config_entry, fake_controller
):
    """Load the integration through Home Assistant's config-entry lifecycle."""
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_setup_registers_controller_device_before_forwarding_platforms(
    hass, mock_config_entry, monkeypatch
):
    """Child entities always receive a stable controller device id during setup."""
    integration = importlib.import_module("custom_components.linking_the_world_temp_ha")
    forwarded = False

    class TrackingHub:
        def __init__(self, _hass, entry, health) -> None:
            self.entry = entry
            self.health = health
            self.panel_registry = object()
            self.controller_device_id = None

        async def async_start(self) -> None:
            return None

        async def async_stop(self) -> None:
            return None

    async def verify_parent_exists(*_args) -> None:
        nonlocal forwarded
        forwarded = True
        device_id = dr.async_get_device_id_by_identifier(
            hass,
            (DOMAIN, mock_config_entry.entry_id),
            config_entry_id=mock_config_entry.entry_id,
        )
        assert mock_config_entry.runtime_data.hub.controller_device_id == device_id

    mock_config_entry.add_to_hass(hass)
    monkeypatch.setattr(integration, "LinkingTempHub", TrackingHub)
    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", verify_parent_exists
    )

    assert await integration.async_setup_entry(hass, mock_config_entry)
    assert forwarded


async def test_setup_removes_legacy_energy_sensor_and_experimental_option(
    hass, mock_config_entry, fake_controller
):
    """Upgrading leaves one permanent energy switch and no retired artifacts."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy_unique_id = f"{mock_config_entry.entry_id}_energy_saving"
    legacy = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        legacy_unique_id,
        config_entry=mock_config_entry,
        original_name="节能状态",
    )
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            **mock_config_entry.options,
            "enable_experimental_energy_control": True,
        },
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(legacy.entity_id) is None
    assert "enable_experimental_energy_control" not in mock_config_entry.options
    assert registry.async_get_entity_id(
        "switch", DOMAIN, f"{mock_config_entry.entry_id}_energy_control"
    ) is not None


async def test_setup_preserves_restored_fault_code_sensor_identity(
    hass, mock_config_entry, fake_controller
):
    """Restored raw-code entities retain their original registry identities."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy_entities = [
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{mock_config_entry.entry_id}_{key}",
            config_entry=mock_config_entry,
        )
        for key in ("system_fault_code", "filter_fault_code")
    ]

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert all(registry.async_get(entity.entity_id) is not None for entity in legacy_entities)
    assert {
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{mock_config_entry.entry_id}_{key}"
        )
        for key in ("system_fault_code", "filter_fault_code")
    } == {entity.entity_id for entity in legacy_entities}
    assert registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_system_fault"
    ) is not None
    assert registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{mock_config_entry.entry_id}_filter_fault"
    ) is not None


async def test_setup_uses_typed_runtime_data_and_never_stores_hub_in_hass_data(
    hass, mock_config_entry, fake_controller
):
    """Platforms receive the entry runtime rather than a global hub lookup."""
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    runtime = mock_config_entry.runtime_data
    assert isinstance(runtime, LinkingTempRuntime)
    assert runtime.hub.entry is mock_config_entry
    assert runtime.health is runtime.hub.health
    assert mock_config_entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_setup_restores_v1_panel_identity_through_runtime_registry(
    hass, hass_storage, mock_config_entry
):
    """Legacy panels remain discoverable with their exact existing unique IDs."""
    key = f"{DOMAIN}.{mock_config_entry.entry_id}.panels"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "rooms": {"r0100": "客餐厅"},
            "panels": [{"mac": "ff00ffffffff01ff", "room_id": "r0100"}],
        },
    }
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    runtime = mock_config_entry.runtime_data
    assert runtime.panel_registry.records["ff00ffffffff01ff"].room_id == "r0100"
    assert runtime.hub.room_names["r0100"] == "客餐厅"
    assert "ff00ffffffff01ff" in runtime.hub.thermostats

    entries = [
        entity
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), mock_config_entry.entry_id
        )
        if entity.unique_id
        == f"{mock_config_entry.entry_id}_thermostat_ff00ffffffff01ff_climate"
    ]
    assert len(entries) == 1


async def test_setup_stops_hub_when_platform_forward_fails(
    hass, mock_config_entry, monkeypatch
):
    """A failed HA platform setup cannot leave the controller runner alive."""
    integration = importlib.import_module("custom_components.linking_the_world_temp_ha")

    class TrackingHub:
        instance: "TrackingHub | None" = None

        def __init__(self, _hass, _entry, _health) -> None:
            self.started = False
            self.stopped = False
            self.panel_registry = object()
            TrackingHub.instance = self

        async def async_start(self) -> None:
            self.started = True

        async def async_stop(self) -> None:
            self.stopped = True

    async def fail_forward(*_args) -> None:
        raise RuntimeError("platform forward failed")

    mock_config_entry.add_to_hass(hass)
    monkeypatch.setattr(integration, "LinkingTempHub", TrackingHub)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", fail_forward)

    with pytest.raises(RuntimeError, match="platform forward failed"):
        await integration.async_setup_entry(hass, mock_config_entry)

    assert TrackingHub.instance is not None
    assert TrackingHub.instance.started
    assert TrackingHub.instance.stopped
    assert mock_config_entry.runtime_data is None


async def test_setup_cancellation_stops_hub_before_propagating(
    hass, mock_config_entry, monkeypatch
):
    """Cancellation during platform forwarding must clean up before re-raising."""
    integration = importlib.import_module("custom_components.linking_the_world_temp_ha")
    forward_started = asyncio.Event()

    class TrackingHub:
        instance: "TrackingHub | None" = None

        def __init__(self, _hass, _entry, _health) -> None:
            self.started = False
            self.stopped = False
            self.panel_registry = object()
            TrackingHub.instance = self

        async def async_start(self) -> None:
            self.started = True

        async def async_stop(self) -> None:
            self.stopped = True

    async def block_forward(*_args) -> None:
        forward_started.set()
        await asyncio.Future()

    mock_config_entry.add_to_hass(hass)
    monkeypatch.setattr(integration, "LinkingTempHub", TrackingHub)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", block_forward)
    setup = asyncio.create_task(integration.async_setup_entry(hass, mock_config_entry))
    await asyncio.wait_for(forward_started.wait(), timeout=1)
    setup.cancel()

    with pytest.raises(asyncio.CancelledError):
        await setup

    assert TrackingHub.instance is not None
    assert TrackingHub.instance.stopped
    assert mock_config_entry.runtime_data is None


async def test_setup_stops_hub_when_runtime_assignment_fails(hass, monkeypatch):
    """A runtime-data assignment error cannot strand a newly started hub."""
    integration = importlib.import_module("custom_components.linking_the_world_temp_ha")

    class TrackingHub:
        instance: "TrackingHub | None" = None

        def __init__(self, _hass, _entry, _health) -> None:
            self.stopped = False
            self.panel_registry = object()
            TrackingHub.instance = self

        async def async_start(self) -> None:
            return None

        async def async_stop(self) -> None:
            self.stopped = True

    class RuntimeDataFailureEntry:
        entry_id = "runtime-data-failure"
        options: dict[str, object] = {}

        @property
        def runtime_data(self):
            return None

        @runtime_data.setter
        def runtime_data(self, _value) -> None:
            raise RuntimeError("runtime assignment failed")

    monkeypatch.setattr(integration, "LinkingTempHub", TrackingHub)

    with pytest.raises(RuntimeError, match="runtime assignment failed"):
        await integration.async_setup_entry(hass, RuntimeDataFailureEntry())

    assert TrackingHub.instance is not None
    assert TrackingHub.instance.stopped


async def test_command_health_tracks_send_confirmation_and_coalescing(
    hass, mock_config_entry
):
    """The core tracked-command flow records usable health metrics."""
    health = HealthTracker()
    hub = LinkingTempHub(hass, mock_config_entry, health)
    hub.allow_control = True
    hub.connected = True
    hub.protocol_verified = True
    health.mark_stage(ConnectionStage.READY)
    hub._client = AsyncMock()

    await hub._async_send_tracked(
        "thermostat_test",
        "测试温控面板 设定温度",
        {"target_temperature": "22"},
        b"test-panel-mac",
        3,
        44,
        coalesce=True,
    )
    await hub._async_send_tracked(
        "thermostat_test",
        "测试温控面板 设定温度",
        {"target_temperature": "23"},
        b"test-panel-mac",
        3,
        46,
        coalesce=True,
    )
    hub._confirm_pending("thermostat_test", {"target_temperature": "22"})

    counters = health.snapshot()["counters"]
    assert counters["commands_sent"] == 1
    assert counters["commands_coalesced"] == 1
    assert counters["commands_confirmed"] == 1


async def test_connection_sensor_waits_for_ready_status_stream(
    hass, setup_integration, fake_controller
):
    """A completed TCP/login exchange alone is not controller availability."""
    runtime = setup_integration
    sensor = ControllerConnectionSensor(runtime.hub)

    async with asyncio.timeout(1):
        while runtime.health.stage is not ConnectionStage.READY:
            await asyncio.sleep(0)
    assert not runtime.hub.available
    assert not sensor.is_on

    await fake_controller.async_send_status(
        tlv(0x0004, runtime.hub.tech_system_mac)
        + tlv(0x000B, b"\x00")
        + tlv(0x000A, bytes.fromhex("0101000049013e003200f0010000"))
    )
    async with asyncio.timeout(1):
        while not runtime.hub.available:
            await asyncio.sleep(0)

    assert sensor.is_on


async def test_connection_stage_transitions_follow_protocol_setup(
    hass, setup_integration
):
    """The lifecycle is observable in protocol order for one successful session."""
    runtime = setup_integration
    async with asyncio.timeout(1):
        while runtime.health.stage is not ConnectionStage.READY:
            await asyncio.sleep(0)

    stages = runtime.health.snapshot()["stage_history"]
    assert [item["stage"] for item in stages][-4:] == [
        ConnectionStage.CONNECTING.value,
        ConnectionStage.HANDSHAKING.value,
        ConnectionStage.AUTHENTICATING.value,
        ConnectionStage.READY.value,
    ]


async def test_setup_fixture_waits_for_connected_authenticated_controller(
    setup_integration, fake_controller
) -> None:
    """Push-driven tests receive a live session rather than a pending runner."""
    assert fake_controller.client_connected.is_set()
    assert fake_controller.handshake_complete.is_set()
    assert setup_integration.hub.health.stage is ConnectionStage.READY


async def test_fake_controller_handles_fragmented_malformed_and_status_frames(
    socket_enabled,
):
    """Exercise the transport conditions shared by native integration tests."""
    received_statuses: list[bytes] = []
    received_status = asyncio.Event()
    fake_controller = FakeMC7021Server(
        FakeControllerBehavior(fragment_size=3)
    )
    await fake_controller.async_start()
    client = AsyncMoorgenClient(
        fake_controller.host,
        fake_controller.port,
        "admin",
        "secret",
    )

    async def record_status(body: bytes) -> None:
        received_statuses.append(body)
        received_status.set()

    client.on_status = record_status
    try:
        await client.connect()
        received_statuses.clear()
        received_status.clear()
        await fake_controller.async_send_malformed(b"not a YAS HCP frame")
        await fake_controller.async_send_status(b"status body")
        await asyncio.wait_for(received_status.wait(), timeout=1)
    finally:
        await client.close()
        await fake_controller.async_stop()

    assert b"status body" in received_statuses
    assert [(frame.kind, frame.opcode) for frame in fake_controller.received_frames][
        :2
    ] == [(1, 1), (2, 4)]


async def test_runtime_status_stream_does_not_accumulate_in_login_queue(
    socket_enabled,
):
    """Thousands of runtime pushes are callback-only after authentication."""
    received = 0
    fake_controller = FakeMC7021Server()
    await fake_controller.async_start()
    client = AsyncMoorgenClient(
        fake_controller.host,
        fake_controller.port,
        "admin",
        "secret",
    )

    async def record_status(_body: bytes) -> None:
        nonlocal received
        received += 1

    client.on_status = record_status
    try:
        await client.connect()
        received = 0
        frames = tuple(
            YasHcpFrame(5, 0x0C, sequence, b"status")
            for sequence in range(3_000)
        )
        await fake_controller.async_send_frames(*frames)
        await _wait_for(lambda: received == len(frames), timeout=3)

        assert client._inbox.qsize() == 0
    finally:
        await client.close()
        await fake_controller.async_stop()


async def test_invalid_panel_mac_does_not_break_status_stream(
    hass, setup_integration, fake_controller
):
    """A malformed panel identity is ignored without rebuilding the session."""
    hub = setup_integration.hub
    client = hub._client
    assert client is not None
    ignored_before = hub.health.snapshot()["counters"]["ignored_statuses"]
    invalid = (
        tlv(0x0004, b"\x01")
        + tlv(0x0075, hub.tech_system_mac)
        + tlv(0x0030, b"r1100")
        + tlv(0x000A, bytes((44, 0x1A, 0x01, 63, 0)))
        + tlv(0x000B, b"\x01")
    )

    await fake_controller.async_send_status(invalid)
    await _wait_for(
        lambda: hub.health.snapshot()["counters"]["ignored_statuses"]
        >= ignored_before + 1
    )
    assert client.reader_alive
    assert client.reader_error is None

    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_thermostat_status(hub, mac_hex))
    await _wait_for(lambda: mac_hex in hub.thermostats)
    await hass.async_block_till_done()

    assert hub.thermostats[mac_hex].current_temperature == 24.6


async def test_fake_controller_delays_a_configured_stage_response(socket_enabled):
    """Hold a hello reply until the configured server-side delay expires."""
    fake_controller = FakeMC7021Server(
        FakeControllerBehavior(stage_delays={"hello": 0.05})
    )
    await fake_controller.async_start()
    reader, writer = await asyncio.open_connection(
        fake_controller.host, fake_controller.port
    )
    hello = YasHcpFrame(1, 1, 7, b"")
    response = YasHcpFrame(1, 3, 7, b"")
    response_task = asyncio.create_task(reader.readexactly(len(response.encode())))
    try:
        writer.write(hello.encode())
        await writer.drain()
        await asyncio.wait_for(fake_controller.received_frame_event.wait(), timeout=1)

        assert not response_task.done()
        assert YasHcpDecoder().feed(await response_task) == [response]
    finally:
        response_task.cancel()
        await asyncio.gather(response_task, return_exceptions=True)
        writer.close()
        await writer.wait_closed()
        await fake_controller.async_stop()


async def test_fake_controller_closes_after_a_configured_stage(socket_enabled):
    """Return EOF after the configured stage without a test-level delay."""
    fake_controller = FakeMC7021Server(
        FakeControllerBehavior(hello_reply=None, close_after_stage="hello")
    )
    await fake_controller.async_start()
    reader, writer = await asyncio.open_connection(
        fake_controller.host, fake_controller.port
    )
    try:
        writer.write(YasHcpFrame(1, 1, 0, b"").encode())
        await writer.drain()

        assert await asyncio.wait_for(reader.read(), timeout=1) == b""
    finally:
        writer.close()
        await writer.wait_closed()
        await fake_controller.async_stop()


async def test_fake_controller_writes_multiple_status_frames_together(
    socket_enabled,
):
    """Deliver concatenated frames through the production client decoder."""
    statuses: list[bytes] = []
    received_statuses = asyncio.Event()
    fake_controller = FakeMC7021Server()
    await fake_controller.async_start()
    client = AsyncMoorgenClient(
        fake_controller.host,
        fake_controller.port,
        "admin",
        "secret",
    )

    async def record_status(body: bytes) -> None:
        if body in {b"first", b"second"}:
            statuses.append(body)
        if len(statuses) == 2:
            received_statuses.set()

    client.on_status = record_status
    try:
        await client.connect()
        await fake_controller.async_send_frames(
            YasHcpFrame(5, 0x0C, 10, b"first"),
            YasHcpFrame(5, 0x0C, 11, b"second"),
        )
        await asyncio.wait_for(received_statuses.wait(), timeout=1)
    finally:
        await client.close()
        await fake_controller.async_stop()

    assert statuses == [b"first", b"second"]


async def test_restart_preserves_dynamic_panel_identity_and_restores_unavailable(
    hass, managed_integration, fake_controller, mock_config_entry
):
    """Reload retains panel identity but waits for a new controller report."""
    first_runtime = managed_integration
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(first_runtime.hub))
    await fake_controller.async_send_status(_thermostat_status(first_runtime.hub, mac_hex))
    await _wait_for(lambda: mac_hex in first_runtime.hub.thermostats)
    await hass.async_block_till_done()
    entity_registry = er.async_get(hass)
    unique_id = f"{mock_config_entry.entry_id}_thermostat_{mac_hex}_climate"
    original_entity_id = entity_registry.async_get_entity_id("climate", DOMAIN, unique_id)
    assert original_entity_id is not None

    assert await asyncio.wait_for(
        hass.config_entries.async_unload(mock_config_entry.entry_id), timeout=1
    )
    assert await asyncio.wait_for(
        hass.config_entries.async_setup(mock_config_entry.entry_id), timeout=3
    )
    reloaded = mock_config_entry.runtime_data
    await _wait_for(
        lambda: reloaded.health.stage is ConnectionStage.READY,
        timeout=3,
    )
    assert reloaded is not first_runtime
    assert mac_hex in reloaded.hub.thermostats
    assert not reloaded.hub.thermostats[mac_hex].available
    assert (
        entity_registry.async_get_entity_id("climate", DOMAIN, unique_id)
        == original_entity_id
    )

    await fake_controller.async_send_status(_system_status(reloaded.hub))
    await fake_controller.async_send_status(_thermostat_status(reloaded.hub, mac_hex))
    await _wait_for(lambda: reloaded.hub.thermostats[mac_hex].available)
    assert await asyncio.wait_for(
        hass.config_entries.async_unload(mock_config_entry.entry_id), timeout=1
    )
    await hass.async_block_till_done()
    assert reloaded.hub._runner is None
    await fake_controller.async_stop()


@pytest.mark.skipif(
    sys.version_info < (3, 13),
    reason="CI's supported Python 3.13 Home Assistant runtime owns config reload coverage",
)
async def test_config_entry_reload_restarts_the_native_runtime(
    hass, managed_integration, mock_config_entry
):
    """The supported HA runtime can reload a live entry without retaining its hub."""
    first_runtime = managed_integration
    assert await asyncio.wait_for(
        hass.config_entries.async_reload(mock_config_entry.entry_id), timeout=5
    )
    reloaded = mock_config_entry.runtime_data
    await _wait_for(
        lambda: reloaded.health.stage is ConnectionStage.READY,
        timeout=3,
    )
    assert reloaded is not first_runtime


async def test_unload_closes_background_tasks(
    hass, setup_integration, mock_config_entry, fake_controller
):
    """An explicit HA unload completes promptly and closes the TCP workers."""
    runtime = setup_integration
    assert await asyncio.wait_for(
        hass.config_entries.async_unload(mock_config_entry.entry_id), timeout=1
    )
    await hass.async_block_till_done()
    assert runtime.hub._runner is None
    assert not _integration_tasks()
    await fake_controller.async_stop()


async def test_remove_unloads_workers_and_clears_entry_repairs_and_panel_storage(
    hass, hass_storage, setup_integration, mock_config_entry, fake_controller
):
    """Permanent removal clears entry-linked Repairs and private panel storage."""
    runtime = setup_integration
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_thermostat_status(runtime.hub, mac_hex))
    await _wait_for(lambda: mac_hex in runtime.hub.thermostats)
    await runtime.panel_registry.async_flush()
    storage_key = f"{DOMAIN}.{mock_config_entry.entry_id}.panels"
    assert storage_key in hass_storage
    await runtime.hub.repairs.async_set_protocol_incompatible(True)
    issue_id = f"protocol_incompatible_{mock_config_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    assert await asyncio.wait_for(
        hass.config_entries.async_remove(mock_config_entry.entry_id), timeout=1
    )
    await hass.async_block_till_done()
    assert runtime.hub._runner is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert storage_key not in hass_storage
    assert not _integration_tasks()
    await fake_controller.async_stop()
