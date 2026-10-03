"""Health lifecycle accounting regression tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.linking_the_world_temp_ha.health import HealthTracker
from custom_components.linking_the_world_temp_ha.hub import LinkingTempHub
from custom_components.linking_the_world_temp_ha.protocol import (
    AuthenticationRejected,
    HandshakeTimeout,
    IncompatibleProtocol,
    LoginTimeout,
    TcpConnectError,
)
from custom_components.linking_the_world_temp_ha.runtime import ConnectionStage


def _hub_with_health() -> LinkingTempHub:
    """Create the small coordinator surface needed for accounting tests."""
    hub = object.__new__(LinkingTempHub)
    hub.health = HealthTracker()
    hub.connected = False
    hub.protocol_verified = False
    hub.protocol_status = "waiting"
    hub._client = None
    hub._pending = {}
    hub._queued = {}
    hub.thermostats = {}
    hub.filtered = {}
    hub._listeners = {}
    hub._session_authenticated = False
    hub.host = "house-controller.lan"
    hub.username = "admin"
    hub.password = "secret"
    hub.client_id = "test-client"
    return hub


@pytest.mark.parametrize(
    "error",
    [
        TcpConnectError("refused"),
        HandshakeTimeout("hello timed out"),
        LoginTimeout("login timed out"),
        AuthenticationRejected("rejected"),
    ],
)
async def test_failed_connection_attempts_do_not_count_as_disconnects(error) -> None:
    """Only a session that reached authentication can produce a disconnect."""
    hub = _hub_with_health()
    hub._client = AsyncMock()

    hub._record_connection_failure(error)
    await hub._async_disconnect()

    assert hub.health.snapshot()["counters"]["disconnects"] == 0


async def test_authenticated_session_disconnect_is_counted_once() -> None:
    """A successfully authenticated session records one, and only one, disconnect."""
    hub = _hub_with_health()
    hub._client = None
    hub._session_authenticated = True

    await hub._async_disconnect()
    await hub._async_disconnect()

    assert hub.health.snapshot()["counters"]["disconnects"] == 1


@pytest.mark.parametrize(
    ("stage", "counter"),
    [
        (ConnectionStage.HANDSHAKING, "handshake_failures"),
        (ConnectionStage.AUTHENTICATING, "login_failures"),
    ],
)
def test_incompatible_protocol_counts_the_stage_that_failed(stage, counter) -> None:
    """Malformed hello/login replies belong to their actual lifecycle stage."""
    hub = _hub_with_health()
    hub.health.mark_stage(stage)

    hub._record_connection_failure(IncompatibleProtocol("unexpected frame"))

    counters = hub.health.snapshot()["counters"]
    assert counters[counter] == 1
    assert counters["handshake_failures"] + counters["login_failures"] == 1
