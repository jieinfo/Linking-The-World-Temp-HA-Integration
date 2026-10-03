"""Bounded transport and independently monitored total-status regressions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.linking_the_world_temp_ha import hub as hub_module
from custom_components.linking_the_world_temp_ha import protocol
from custom_components.linking_the_world_temp_ha.health import HealthTracker
from custom_components.linking_the_world_temp_ha.hub import LinkingTempHub
from custom_components.linking_the_world_temp_ha.protocol import AsyncMoorgenClient, tlv
from tests.native.test_entities import _system_status, _thermostat_status


async def _stall() -> None:
    await asyncio.Event().wait()


def _stalled_client(monkeypatch):
    monkeypatch.setattr(protocol, "WRITE_TIMEOUT", 0.01, raising=False)
    monkeypatch.setattr(protocol, "CLOSE_TIMEOUT", 0.01, raising=False)
    client = AsyncMoorgenClient("127.0.0.1", 9000, "admin", "password")
    writer = Mock()
    writer.drain = AsyncMock(side_effect=_stall)
    writer.wait_closed = AsyncMock(side_effect=_stall)
    client._writer = writer
    client._ready = True
    return client, writer


async def test_stalled_write_aborts_transport_and_releases_lock(monkeypatch):
    client, writer = _stalled_client(monkeypatch)
    with pytest.raises(ConnectionError, match="write timed out"):
        await asyncio.wait_for(client.heartbeat(), 0.1)
    writer.transport.abort.assert_called_once()
    assert not client._ready
    assert not client._write_lock.locked()
    with pytest.raises(ConnectionError, match="not ready"):
        await client.send_command(protocol.TECH_SYSTEM_MAC, 2)


async def test_stalled_close_is_bounded_and_detaches_socket(monkeypatch):
    client, writer = _stalled_client(monkeypatch)
    await asyncio.wait_for(client.close(), 0.1)
    writer.transport.abort.assert_called_once()
    assert client._writer is None
    assert not client._ready
    await client.close()
    assert writer.close.call_count == 1


async def test_cancelled_close_aborts_without_swallowing_cancellation(monkeypatch):
    client, writer = _stalled_client(monkeypatch)
    monkeypatch.setattr(protocol, "CLOSE_TIMEOUT", 1)
    entered = asyncio.Event()

    async def wait_closed():
        entered.set()
        await _stall()

    writer.wait_closed = AsyncMock(side_effect=wait_closed)
    task = asyncio.create_task(client.close())
    await asyncio.wait_for(entered.wait(), 0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    writer.transport.abort.assert_called_once()
    assert client._writer is None


async def test_close_while_a_sender_waits_does_not_write_a_stale_socket(monkeypatch):
    client, writer = _stalled_client(monkeypatch)
    writer.wait_closed = AsyncMock()
    await client._write_lock.acquire()
    task = asyncio.create_task(client.heartbeat())
    await asyncio.sleep(0)
    await client.close()
    client._write_lock.release()
    with pytest.raises(ConnectionError, match="closed before write"):
        await task
    writer.write.assert_not_called()


async def test_write_lock_wait_has_the_same_deadline(monkeypatch):
    client, writer = _stalled_client(monkeypatch)
    await client._write_lock.acquire()
    try:
        with pytest.raises(ConnectionError, match="write timed out"):
            await asyncio.wait_for(client.heartbeat(), 0.1)
        writer.write.assert_not_called()
        writer.transport.abort.assert_called_once()
    finally:
        client._write_lock.release()


async def test_room_reports_do_not_refresh_total_status(hass, mock_config_entry):
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub.controller_silence_timeout = 30
    hub._last_system_status_at = 100.0
    hub._last_valid_status_at = 140.0
    client = Mock()
    client.request_status = AsyncMock()
    await hub._async_check_system_freshness(client, 140.0)
    client.request_status.assert_awaited_once()
    await hub._async_check_system_freshness(client, 141.0)
    client.request_status.assert_awaited_once()
    with pytest.raises(ConnectionError, match="total-system status"):
        await hub._async_check_system_freshness(client, 151.0)


async def test_pending_poll_can_satisfy_freshness_query(hass, mock_config_entry):
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub.controller_silence_timeout = 30
    hub._last_system_status_at = 100.0
    hub._last_status_query_at = 140.0
    client = Mock()
    client.request_status = AsyncMock()
    await hub._async_check_system_freshness(client, 140.0)
    client.request_status.assert_not_awaited()
    assert hub._system_status_query_at == 140.0


async def test_valid_total_report_clears_freshness_probe(hass, mock_config_entry):
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub._system_status_query_at = 100.0
    body = tlv(0x0004, hub.tech_system_mac) + tlv(0x000B, b"\x01")
    body += tlv(0x000A, bytes.fromhex("0101000000013e003200f0010000"))
    await hub._async_status_received(body)
    assert hub._last_system_status_at is not None
    assert hub._system_status_query_at is None
    before = hub._last_system_status_at
    await hub._async_status_received(b"invalid")
    assert hub._last_system_status_at == before


async def test_real_room_and_malformed_reports_never_refresh_total_clock(
    hass, mock_config_entry, monkeypatch
):
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    clock = [100.0]
    monkeypatch.setattr(hub_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    await hub._async_status_received(_system_status(hub, power=True))
    clock[0] = 140.0
    await hub._async_status_received(_thermostat_status(hub, "ff00ffffffff01ff"))
    assert hub._last_valid_status_at == 140.0
    assert hub._last_system_status_at == 100.0
    assert hub.status_freshness()["system_status_age_seconds"] == 40.0
    hub._system_status_query_at = 140.0
    await hub._async_status_received(b"bad")
    assert hub._system_status_query_at == 140.0
    await hub._async_status_received(_system_status(hub, power=True))
    assert hub._system_status_query_at is None
    assert hub._last_system_status_at == 140.0


async def test_first_total_status_uses_session_start_and_response_during_query(
    hass, mock_config_entry
):
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub.controller_silence_timeout = 30
    hub._session_started_at = 100.0
    client = Mock()
    client.request_status = AsyncMock()
    await hub._async_check_system_freshness(client, 129.0)
    client.request_status.assert_not_awaited()

    async def query():
        await hub._async_status_received(_system_status(hub, power=True))

    client.request_status = AsyncMock(side_effect=query)
    await hub._async_check_system_freshness(client, 130.0)
    assert hub._system_status_query_at is None
    assert hub._last_system_status_at is not None


async def test_freshness_probe_disables_cached_command_shortcut(hass, mock_config_entry):
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub.state.power = "ON"
    assert hub._matches_verified_system_state({"power": "ON"})
    hub._system_status_query_at = 100.0
    assert not hub._matches_verified_system_state({"power": "ON"})
