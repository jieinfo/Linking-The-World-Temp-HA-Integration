"""Connection and state coordinator for Linking The World Temp HA."""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .command_queue import (
    STATUS_POLL_INTERVAL,
    PendingCommand,
    QueuedCommand,
    coalesce_queued,
    command_intent,
    first_status_poll_at,
    temperature_retry_is_allowed,
)
from .const import (
    CONF_ALLOW_CONTROL,
    CONF_CLIENT_ID,
    CONF_COMMAND_CONFIRMATION_TIMEOUT,
    CONF_COMMAND_MIN_INTERVAL,
    CONF_CONTROLLER_SILENCE_TIMEOUT,
    CONF_TECH_SYSTEM_MAC,
    CONF_THERMOSTAT_OFFLINE_AFTER,
    DEFAULT_ALLOW_CONTROL,
    DEFAULT_CLIENT_ID,
    DEFAULT_COMMAND_CONFIRMATION_TIMEOUT,
    DEFAULT_COMMAND_MIN_INTERVAL,
    DEFAULT_CONTROLLER_SILENCE_TIMEOUT,
    DEFAULT_TECH_SYSTEM_MAC,
    DEFAULT_THERMOSTAT_OFFLINE_AFTER,
    DOMAIN,
    MODE_VALUES,
    SCENE_VALUES,
    THERMOSTAT_MAX_TEMPERATURE,
    THERMOSTAT_MIN_TEMPERATURE,
)
from .health import HealthTracker
from .panel_registry import STALE_PANEL_SECONDS, PanelRegistry
from .protocol import (
    COMMAND_ENERGY_SWITCH,
    COMMAND_MODE,
    COMMAND_POWER_OFF,
    COMMAND_POWER_ON,
    COMMAND_SCENE,
    COMMAND_WINTER_HUMIDIFIER,
    AsyncMoorgenClient,
    AuthenticationRejected,
    CannotConnect,
    HandshakeTimeout,
    IncompatibleProtocol,
    LoginTimeout,
    TcpConnectError,
    TechSystemState,
    ThermostatState,
    YasHcpFrame,
    decode_tech_system_status,
    decode_text,
    decode_thermostat_status,
    is_complete_tlv_body,
    iter_tlvs,
    parse_device_mac,
    preserve_valid_thermostat_measurements,
)
from .repairs import RepairManager
from .runtime import ConnectionStage, FailureKind
from .thermostat_policy import room_thermostat_block_reason

_LOGGER = logging.getLogger(__name__)

REAUTH_FLOW_WATCH_INTERVAL = 1.0
_FLOW_SOURCE_REAUTH = "reauth"
_FLOW_SOURCE_RECONFIGURE = "reconfigure"
SESSION_IDLE_INTERVAL = 1.0
SESSION_ACTIVE_INTERVAL = 0.25


@dataclass
class FilteredMeasurements:
    """Smoothed values intended for automations."""

    temperatures: deque[float]
    humidities: deque[int]
    temperature: float | None = None
    humidity: int | None = None


class LinkingTempHub:
    """Own the controller session and expose push state to HA entities."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, health: HealthTracker
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.host = entry.data["host"]
        self.port = int(entry.data["port"])
        self.username = entry.data["username"]
        self.password = entry.data["password"]
        self.client_id = entry.data.get(CONF_CLIENT_ID, DEFAULT_CLIENT_ID)
        self.tech_system_mac = parse_device_mac(
            entry.data.get(CONF_TECH_SYSTEM_MAC, DEFAULT_TECH_SYSTEM_MAC)
        )
        options = entry.options
        self.allow_control = bool(
            options.get(CONF_ALLOW_CONTROL, DEFAULT_ALLOW_CONTROL)
        )
        self.command_min_interval = float(
            options.get(CONF_COMMAND_MIN_INTERVAL, DEFAULT_COMMAND_MIN_INTERVAL)
        )
        self.command_confirmation_timeout = float(
            options.get(
                CONF_COMMAND_CONFIRMATION_TIMEOUT,
                DEFAULT_COMMAND_CONFIRMATION_TIMEOUT,
            )
        )
        self.controller_silence_timeout = float(
            options.get(
                CONF_CONTROLLER_SILENCE_TIMEOUT, DEFAULT_CONTROLLER_SILENCE_TIMEOUT
            )
        )
        self.thermostat_offline_after = float(
            options.get(CONF_THERMOSTAT_OFFLINE_AFTER, DEFAULT_THERMOSTAT_OFFLINE_AFTER)
        )

        self.state = TechSystemState()
        self.thermostats: dict[str, ThermostatState] = {}
        self.room_names: dict[str, str] = {}
        self.panel_registry = PanelRegistry(
            hass,
            entry.entry_id,
            status_gap_timeout=self.controller_silence_timeout,
        )
        self.filtered: dict[str, FilteredMeasurements] = {}
        self.health = health
        self.repairs = RepairManager(hass, entry)
        self.controller_device_id: str | None = None
        self.connected = False
        self.protocol_verified = False
        self.protocol_status = "waiting"
        self.last_connection_error = "starting"
        self.last_command_status = "idle"
        self.parser_anomalies: list[dict[str, object]] = []

        self._client: AsyncMoorgenClient | None = None
        self._listeners: set[Callable[[], None]] = set()
        self._runner: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._command_lock = asyncio.Lock()
        self._mode_transition_lock = asyncio.Lock()
        # Panel reports and Repair-driven deletion share this lock. A report
        # either invalidates an open Repair before its confirmation is checked,
        # or arrives after deletion and is treated as a fresh discovery.
        self._panel_lifecycle_lock = asyncio.Lock()
        self._last_command_at: float | None = None
        self._pending: dict[str, PendingCommand] = {}
        self._queued: dict[str, list[QueuedCommand]] = {}
        self._has_attempted_connection = False
        self._session_authenticated = False
        self._reauth_required = False
        self._reauth_watcher: asyncio.Task[None] | None = None
        self._last_valid_status_at: float | None = None
        self._last_system_status_at: float | None = None
        self._session_started_at: float | None = None
        self._system_status_query_at: float | None = None
        self._last_status_query_at: float | None = None
        self._last_status_query_completed_at: float | None = None

    async def async_start(self) -> None:
        """Restore known panels and start the supervised TCP session."""
        await self._async_restore_panels()
        await self.panel_registry.async_pause_monitoring(datetime.now(UTC))
        self._runner = self.entry.async_create_background_task(
            self.hass,
            self._async_run(),
            f"{DOMAIN}_{self.entry.entry_id}",
        )

    async def async_stop(self) -> None:
        """Stop the session and mark all state unavailable."""
        self._stop.set()
        watcher = self._reauth_watcher
        self._reauth_watcher = None
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if self._runner is not None:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
            self._runner = None
        await self._async_disconnect()
        await self.panel_registry.async_flush()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)

        @callback
        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    @property
    def available(self) -> bool:
        stage = getattr(getattr(self, "health", None), "stage", None)
        return (
            (stage is None or stage is ConnectionStage.READY)
            and self.connected
            and self.protocol_verified
        )

    @property
    def control_permission(self) -> str:
        if not self.allow_control:
            return "read_only"
        if (
            getattr(getattr(self, "health", None), "stage", None)
            is ConnectionStage.DISCONNECTED
        ):
            return "disconnected"
        if not self.protocol_verified:
            return "waiting_for_protocol"
        return "ready"

    def thermostat_name(self, thermostat: ThermostatState) -> str:
        room_name = self.room_names.get(thermostat.room_id, thermostat.room_id)
        return f"{room_name or thermostat.mac.hex()} 温控面板"

    @property
    def can_change_system_mode(self) -> bool:
        """Return whether the UI can start one safe mode transition."""
        return (
            self.state.power in ("ON", "OFF")
            and not self._mode_transition_lock.locked()
            and not self._has_system_intent("power")
        )

    def _has_system_intent(self, intent: str) -> bool:
        """Return whether one pending or queued system command changes intent."""
        commands = (
            self._pending.get("system"),
            *self._queued.get("system", ()),
        )
        return any(
            command is not None and command_intent(command.expected) == intent
            for command in commands
        )

    def _mode_change_block_reason(self) -> str | None:
        if not self.state.can_change_mode or self._has_system_intent("power"):
            return "请先关闭科技系统，再切换运行模式"
        return None

    def _mode_dispatch_block_reason(self) -> str | None:
        """Revalidate confirmed state without inspecting later queued intents."""
        if not self.state.can_change_mode:
            return "请先关闭科技系统，再切换运行模式"
        return None

    def _winter_humidifier_block_reason(self) -> str | None:
        if self.state.mode != "heat":
            return "冬季加湿仅能在制热模式下使用"
        commands = [
            command
            for command in (
                self._pending.get("system"),
                *self._queued.get("system", ()),
            )
            if command is not None
        ]
        if any(
            (mode := command.expected.get("mode")) is not None and mode != "heat"
            for command in commands
        ):
            return "科技系统正在离开制热模式，冬季加湿不能操作"
        return None

    def _winter_humidifier_dispatch_block_reason(self) -> str | None:
        """Revalidate confirmed mode without blocking on later queue entries."""
        if self.state.mode != "heat":
            return "冬季加湿仅能在制热模式下使用"
        return None

    async def async_set_system_power(self, enabled: bool) -> None:
        await self._async_send_tracked(
            "system",
            "总控开关",
            {"power": "ON" if enabled else "OFF"},
            self.tech_system_mac,
            COMMAND_POWER_ON if enabled else COMMAND_POWER_OFF,
            coalesce=True,
        )

    async def async_set_mode(self, mode: str) -> None:
        if reason := self._mode_change_block_reason():
            raise HomeAssistantError(reason)
        if mode not in MODE_VALUES:
            raise HomeAssistantError(f"不支持的运行模式: {mode}")
        await self._async_send_tracked(
            "system",
            "总控模式",
            {"mode": mode},
            self.tech_system_mac,
            COMMAND_MODE,
            MODE_VALUES[mode],
            coalesce=True,
            send_guard=self._mode_dispatch_block_reason,
        )

    async def async_select_mode(self, mode: str) -> None:
        """Select a mode, safely power-cycling an enabled system when required."""
        if mode not in MODE_VALUES:
            raise HomeAssistantError(f"不支持的运行模式: {mode}")
        async with self._mode_transition_lock:
            if self.state.mode == mode:
                return
            if self.state.power not in ("ON", "OFF"):
                raise HomeAssistantError("科技系统总开关状态尚未确认")
            if self._has_system_intent("power"):
                raise HomeAssistantError(
                    "总控开关状态正在变化，请稍后再切换模式"
                )

            restore_power = self.state.power == "ON"
            if restore_power:
                await self.async_set_system_power(False)
                await self._async_wait_for_system_value(
                    "power",
                    "OFF",
                    "科技系统关闭未得到主机确认，模式切换已中止",
                )

            await self.async_set_mode(mode)
            await self._async_wait_for_system_value(
                "mode",
                mode,
                "模式切换未得到主机确认，科技系统保持关闭",
            )

            if restore_power:
                await self.async_set_system_power(True)
                await self._async_wait_for_system_value(
                    "power",
                    "ON",
                    "模式已切换，但科技系统恢复开启未得到主机确认",
                )

    async def _async_wait_for_system_value(
        self, intent: str, expected: str, error_message: str
    ) -> None:
        """Wait until a tracked system command is confirmed by a real status report."""
        changed = asyncio.Event()
        remove_listener = self.async_add_listener(changed.set)
        command_count = 1 + int("system" in self._pending) + len(
            self._queued.get("system", ())
        )
        timeout = self.command_confirmation_timeout * command_count + 2
        try:
            async with asyncio.timeout(timeout):
                while True:
                    if (
                        getattr(self.state, intent, None) == expected
                        and not self._has_system_intent(intent)
                    ):
                        return
                    if not self.available:
                        raise HomeAssistantError(
                            f"{error_message}：主机连接已经断开"
                        )
                    if not self._has_system_intent(intent):
                        raise HomeAssistantError(error_message)
                    changed.clear()
                    if (
                        getattr(self.state, intent, None) == expected
                        and not self._has_system_intent(intent)
                    ):
                        return
                    await changed.wait()
        except TimeoutError as error:
            raise HomeAssistantError(error_message) from error
        finally:
            remove_listener()

    async def async_set_scene(self, scene: str) -> None:
        if scene not in SCENE_VALUES:
            raise HomeAssistantError(f"不支持的场景: {scene}")
        await self._async_send_tracked(
            "system",
            "总控场景",
            {"scene": scene},
            self.tech_system_mac,
            COMMAND_SCENE,
            SCENE_VALUES[scene],
            coalesce=True,
        )

    async def async_set_winter_humidifier(self, enabled: bool) -> None:
        if reason := self._winter_humidifier_block_reason():
            raise HomeAssistantError(reason)
        await self._async_send_tracked(
            "system",
            "冬季加湿",
            {"winter_humidifier": "ON" if enabled else "OFF"},
            self.tech_system_mac,
            COMMAND_WINTER_HUMIDIFIER,
            1 if enabled else 0,
            coalesce=True,
            send_guard=self._winter_humidifier_dispatch_block_reason,
        )

    async def async_set_energy_saving(self, enabled: bool) -> None:
        """Set the energy-saving flag and await controller confirmation."""
        await self._async_send_tracked(
            "system",
            "节能控制",
            {"energy_saving": "ON" if enabled else "OFF"},
            self.tech_system_mac,
            COMMAND_ENERGY_SWITCH,
            1 if enabled else 0,
            coalesce=True,
        )

    async def async_set_thermostat_power(self, mac_hex: str, enabled: bool) -> None:
        thermostat = self._require_thermostat(mac_hex)
        target = f"thermostat_{mac_hex}"
        send_guard = self._room_thermostat_block_reason if enabled else None
        if send_guard is not None and (reason := send_guard()):
            self.health.increment("commands_blocked")
            raise HomeAssistantError(reason)
        expected_power = "ON" if enabled else "OFF"
        label = f"{self.thermostat_name(thermostat)} 开关"
        if thermostat.power == expected_power and await self._async_confirm_unchanged(
            target, label, {"power": expected_power}
        ):
            return
        await self._async_send_tracked(
            target,
            label,
            {"power": expected_power},
            thermostat.mac,
            COMMAND_POWER_ON if enabled else COMMAND_POWER_OFF,
            coalesce=True,
            send_guard=send_guard,
        )

    async def _async_confirm_unchanged(
        self, target: str, label: str, expected: dict[str, str]
    ) -> bool:
        """Confirm an unchanged state and cancel only its stale queued intent."""
        async with self._command_lock:
            if target in self._pending:
                return False
            intent = command_intent(expected)
            retained = [
                command
                for command in self._queued.get(target, ())
                if command_intent(command.expected) != intent
            ]
            if retained:
                self._queued[target] = retained
            else:
                self._queued.pop(target, None)
            self._record_command_queue_depth()
            self.last_command_status = f"confirmed:{label}"
            self._notify()
            return True

    def _room_thermostat_block_reason(self) -> str | None:
        """Return why a room panel cannot be enabled at send time."""
        pending_system = self._pending.get("system")
        if pending_system is not None and pending_system.expected.get("power") == "OFF":
            return "科技系统总开关正在关闭，房间温控面板不能开启"
        return room_thermostat_block_reason(self.state.power, self.state.mode)

    async def async_set_thermostat_temperature(
        self, mac_hex: str, temperature: float
    ) -> None:
        thermostat = self._require_thermostat(mac_hex)
        temperature = float(temperature)
        if not temperature.is_integer() or not (
            THERMOSTAT_MIN_TEMPERATURE <= temperature <= THERMOSTAT_MAX_TEMPERATURE
        ):
            raise HomeAssistantError(
                f"温度必须是 {THERMOSTAT_MIN_TEMPERATURE} 至 {THERMOSTAT_MAX_TEMPERATURE}°C 的整数"
            )
        await self._async_send_tracked(
            f"thermostat_{mac_hex}",
            f"{self.thermostat_name(thermostat)} 设定温度",
            {"target_temperature": f"{int(temperature)}"},
            thermostat.mac,
            COMMAND_MODE,
            int(temperature) * 2,
            coalesce=True,
        )

    def _require_thermostat(self, mac_hex: str) -> ThermostatState:
        if thermostat := self.thermostats.get(mac_hex):
            return thermostat
        raise HomeAssistantError("该房间温控面板尚未被主机发现")

    @staticmethod
    def _command_target_type(target: str) -> str:
        """Return a stable command category without household identity."""
        return "thermostat" if target.startswith("thermostat_") else "system"

    def _record_command_queue_depth(self) -> None:
        """Publish bounded command pressure without retaining command details."""
        health = getattr(self, "health", None)
        if health is None:
            return
        health.record_command_queue_depth(
            len(self._pending) + sum(len(commands) for commands in self._queued.values())
        )

    def _matches_verified_system_state(self, expected: dict[str, str]) -> bool:
        """Return whether one total-control intent already matches verified state."""
        return self._system_status_query_at is None and all(
            getattr(self.state, key, None) == value for key, value in expected.items()
        )

    async def _async_run(self) -> None:
        retry_delay = 5
        while not self._stop.is_set():
            try:
                self.health.increment("connection_attempts")
                if self._has_attempted_connection:
                    self.health.increment("reconnects")
                self._has_attempted_connection = True
                self._mark_stage(ConnectionStage.CONNECTING)
                self.last_connection_error = "connecting"
                self._notify()
                client = AsyncMoorgenClient(
                    self.host,
                    self.port,
                    self.username,
                    self.password,
                    self.client_id,
                )
                client.on_frame = self._async_frame_received
                client.on_status = self._async_status_received
                client.on_stage = self._async_protocol_stage_received
                client.on_parser_event = self._async_parser_event
                self._session_authenticated = False
                self._reauth_required = False
                self._client = client
                await client.connect()
                self.connected = True
                self.health.increment("connection_successes")
                self.health.clear_failure()
                await self.repairs.async_set_login_timeout(False)
                await self.repairs.async_set_protocol_incompatible(False)
                self.last_connection_error = "none"
                self._last_valid_status_at = time.monotonic()
                self._session_started_at = self._last_valid_status_at
                retry_delay = 5
                self._notify()
                await self._async_session_loop(client)
                if client.reader_error is not None:
                    raise ConnectionError(str(client.reader_error))
                raise ConnectionError("MC7021 TCP reader stopped")
            except asyncio.CancelledError:
                raise
            except AuthenticationRejected as error:
                self._record_connection_failure(error)
                self.last_connection_error = str(error) or error.__class__.__name__
                self._reauth_required = True
                _LOGGER.warning(
                    "MC7021 rejected the configured credentials; reauthentication is required"
                )
                self._async_request_reauth()
            except (CannotConnect, ConnectionError, OSError, TimeoutError) as error:
                self._record_connection_failure(error)
                self.last_connection_error = str(error) or error.__class__.__name__
                _LOGGER.warning(
                    "MC7021 session unavailable: failure_kind=%s retry_delay_seconds=%s",
                    self.health.failure_kind.value,
                    retry_delay,
                )
            finally:
                await self._async_disconnect()
            if self._reauth_required:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), retry_delay)
            except asyncio.TimeoutError:
                pass
            retry_delay = min(30, retry_delay * 2)

    def _async_request_reauth(self) -> None:
        """Start reauth now, or wait quietly for a conflicting flow to finish."""
        flows = tuple(
            self.entry.async_get_active_flows(
                self.hass, {_FLOW_SOURCE_REAUTH, _FLOW_SOURCE_RECONFIGURE}
            )
        )
        if any(flow["context"].get("source") == _FLOW_SOURCE_REAUTH for flow in flows):
            return
        if not flows:
            self.entry.async_start_reauth(self.hass)
            return
        if self._reauth_watcher is None or self._reauth_watcher.done():
            self._reauth_watcher = self.entry.async_create_background_task(
                self.hass,
                self._async_wait_for_reauth_slot(),
                f"{DOMAIN}_{self.entry.entry_id}_reauth_waiter",
            )

    async def _async_wait_for_reauth_slot(self) -> None:
        """Wait at a low rate for an existing reconfigure flow to end.

        Config-entry flows expose no completion callback.  This task only exists
        while a rejected session is paused, performs no network I/O, and is
        owned by the entry so unload reliably cancels it.
        """
        try:
            while self._reauth_required and not self._stop.is_set():
                flows = tuple(
                    self.entry.async_get_active_flows(
                        self.hass, {_FLOW_SOURCE_REAUTH, _FLOW_SOURCE_RECONFIGURE}
                    )
                )
                if any(
                    flow["context"].get("source") == _FLOW_SOURCE_REAUTH
                    for flow in flows
                ):
                    return
                if not flows:
                    self.entry.async_start_reauth(self.hass)
                    await asyncio.sleep(0)
                    if any(
                        flow["context"].get("source") == _FLOW_SOURCE_REAUTH
                        for flow in self.entry.async_get_active_flows(
                            self.hass,
                            {_FLOW_SOURCE_REAUTH, _FLOW_SOURCE_RECONFIGURE},
                        )
                    ):
                        return
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), REAUTH_FLOW_WATCH_INTERVAL
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            self._reauth_watcher = None

    async def _async_protocol_stage_received(self, stage: str) -> None:
        """Reflect protocol lifecycle callbacks in the shared health runtime."""
        connection_stage = ConnectionStage(stage)
        if connection_stage is ConnectionStage.AUTHENTICATING:
            self.health.increment("handshake_successes")
        elif connection_stage is ConnectionStage.READY:
            self.connected = True
            self._session_authenticated = True
            self.health.increment("login_successes")
        self._mark_stage(connection_stage)
        self._notify()

    async def _async_parser_event(
        self,
        name: str,
        count: int,
        parser_anomalies: list[dict[str, object]] | None = None,
    ) -> None:
        """Record decoder counters only; raw protocol data stays in the client."""
        self.health.increment(name, count)
        if parser_anomalies is not None:
            self.parser_anomalies = [dict(anomaly) for anomaly in parser_anomalies]
        if name == "frames_malformed":
            await self.panel_registry.async_pause_monitoring(datetime.now(UTC))

    def _increment_health(self, name: str, count: int = 1) -> None:
        """Keep focused compatibility tests independent from runtime construction."""
        if health := getattr(self, "health", None):
            health.increment(name, count)

    def _mark_stage(self, stage: ConnectionStage) -> None:
        """Update the lifecycle before entities are notified of a disconnect."""
        if self.health.stage is stage:
            return
        self.health.mark_stage(stage)
        if stage is ConnectionStage.DISCONNECTED:
            self.connected = False

    def _record_connection_failure(self, error: BaseException) -> None:
        """Map typed connection failures to stable, user-safe health categories."""
        kind = FailureKind.TCP_TIMEOUT
        if isinstance(error, TcpConnectError):
            if isinstance(error.__cause__, ConnectionRefusedError):
                kind = FailureKind.TCP_REFUSED
            else:
                kind = FailureKind.TCP_TIMEOUT
        elif isinstance(error, HandshakeTimeout):
            kind = FailureKind.HANDSHAKE
            self.health.increment("handshake_failures")
        elif isinstance(error, LoginTimeout):
            kind = FailureKind.LOGIN_TIMEOUT
            self.health.increment("login_failures")
            if repairs := getattr(self, "repairs", None):
                self._schedule_repair(repairs.async_set_login_timeout(True))
        elif isinstance(error, AuthenticationRejected):
            kind = FailureKind.AUTH_REJECTED
            self.health.increment("login_failures")
        elif isinstance(error, IncompatibleProtocol):
            kind = FailureKind.PROTOCOL
            if self.health.stage is ConnectionStage.HANDSHAKING:
                self.health.increment("handshake_failures")
            elif self.health.stage is ConnectionStage.AUTHENTICATING:
                self.health.increment("login_failures")
            if repairs := getattr(self, "repairs", None):
                self._schedule_repair(repairs.async_set_protocol_incompatible(True))
        elif "silent" in str(error).lower():
            kind = FailureKind.STATUS_SILENCE
        self.health.record_failure(
            kind,
            error,
            secrets={
                "host": self.host,
                "username": self.username,
                "password": self.password,
                "client_id": self.client_id,
            },
        )

    def _schedule_repair(self, coroutine: Any) -> None:
        """Run issue updates without blocking the reconnect failure path."""
        if not hasattr(self, "hass") or not hasattr(self, "entry"):
            coroutine.close()
            return
        self.entry.async_create_background_task(
            self.hass, coroutine, "linking-temp-connection-repair"
        )

    async def _async_session_loop(self, client: AsyncMoorgenClient) -> None:
        heartbeat_at = 0.0
        availability_at = 0.0
        while not self._stop.is_set() and client.reader_alive:
            now = time.monotonic()
            await self._async_poll_pending_status(now)
            await self._async_check_system_freshness(client, now)
            await self._async_expire_pending(now)
            await self._async_dispatch_queued()
            if now >= heartbeat_at:
                await client.heartbeat()
                heartbeat_at = now + 15
            if now >= availability_at:
                await self._async_refresh_thermostat_availability(now)
                availability_at = now + 15
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    SESSION_ACTIVE_INTERVAL
                    if self._pending or self._queued
                    else SESSION_IDLE_INTERVAL,
                )
            except asyncio.TimeoutError:
                pass

    async def _async_check_system_freshness(
        self, client: AsyncMoorgenClient, now: float
    ) -> None:
        """Room reports cannot keep a silent total-system state healthy forever."""
        last_total = self._last_system_status_at
        anchor = last_total if last_total is not None else self._session_started_at
        if anchor is None or now - anchor < self.controller_silence_timeout:
            return
        if self._system_status_query_at is None:
            # A pending command's query in this same loop refreshes all devices.
            self._system_status_query_at = now
            if self._last_status_query_at != now:
                self.health.increment("system_status_refresh_queries")
                await client.request_status()
                self._last_status_query_at = now
                self._last_status_query_completed_at = time.monotonic()
            # A response can clear the probe while drain/lock wait is in progress.
            if self._system_status_query_at is not None:
                self._system_status_query_at = self._last_status_query_completed_at
            return
        grace = max(10.0, self.command_confirmation_timeout)
        if now - self._system_status_query_at >= grace:
            raise ConnectionError(
                "MC7021 total-system status has been silent for "
                f"{now - anchor:.0f} seconds after a refresh query"
            )

    def status_freshness(self) -> dict[str, float | bool | None]:
        """Export stream ages, independently of socket or room activity."""
        now = time.monotonic()
        return {
            "any_status_age_seconds": (
                round(max(0.0, now - self._last_valid_status_at), 3)
                if self._last_valid_status_at is not None
                else None
            ),
            "system_status_age_seconds": (
                round(max(0.0, now - self._last_system_status_at), 3)
                if self._last_system_status_at is not None
                else None
            ),
            "system_refresh_pending": self._system_status_query_at is not None,
        }

    async def _async_disconnect(self) -> None:
        client = self._client
        self._client = None
        was_authenticated = getattr(self, "_session_authenticated", False)
        self._session_authenticated = False
        self._mark_stage(ConnectionStage.DISCONNECTED)
        self.protocol_verified = False
        self.protocol_status = "disconnected"
        self._last_valid_status_at = None
        self._last_system_status_at = None
        self._session_started_at = None
        self._system_status_query_at = None
        self._last_status_query_at = None
        self._last_status_query_completed_at = None
        if was_authenticated:
            self.health.increment("disconnects")
        if panel_registry := getattr(self, "panel_registry", None):
            await panel_registry.async_pause_monitoring(datetime.now(UTC))
        for thermostat in self.thermostats.values():
            thermostat.available = False
        self.filtered.clear()
        for pending in self._pending.values():
            self.health.command_result(pending.trace_id, "disconnected")
        for commands in self._queued.values():
            for queued in commands:
                self.health.command_result(queued.trace_id, "disconnected")
        self._pending.clear()
        self._queued.clear()
        self._record_command_queue_depth()
        if client is not None:
            await client.close()
        self._notify()

    async def _async_send_tracked(
        self,
        target: str,
        label: str,
        expected: dict[str, str],
        mac: bytes,
        command: int,
        value: int | None = None,
        *,
        coalesce: bool = False,
        send_guard: Callable[[], str | None] | None = None,
    ) -> None:
        async with self._command_lock:
            await self._async_send_tracked_locked(
                target,
                label,
                expected,
                mac,
                command,
                value,
                coalesce=coalesce,
                send_guard=send_guard,
            )

    async def _async_send_tracked_locked(
        self,
        target: str,
        label: str,
        expected: dict[str, str],
        mac: bytes,
        command: int,
        value: int | None = None,
        *,
        coalesce: bool = False,
        send_guard: Callable[[], str | None] | None = None,
        from_queue: bool = False,
        trace_id: int | None = None,
    ) -> None:
        """Send or queue one command while the per-hub command lock is held."""
        if not self.allow_control:
            self.health.increment("commands_blocked")
            raise HomeAssistantError("集成当前处于只读模式")
        if not self.available or self._client is None:
            self.health.increment("commands_blocked")
            raise HomeAssistantError("主机尚未连接或协议状态尚未验证")

        def validate_send_guard() -> None:
            if send_guard is not None and (reason := send_guard()):
                self._increment_health("commands_blocked")
                raise HomeAssistantError(reason)

        if trace_id is None:
            trace_id = self.health.start_command(target, command_intent(expected))
        replacement = QueuedCommand(
            label, target, expected, mac, command, value, send_guard, trace_id
        )
        if target in self._pending:
            if not coalesce:
                self.health.increment("commands_blocked")
                self.health.command_result(trace_id, "blocked")
                raise HomeAssistantError(
                    f"仍在等待主机确认: {self._pending[target].label}"
                )
            pending = self._pending[target]
            queued = coalesce_queued(pending, self._queued.get(target, ()), replacement)
            if (
                target == "system"
                and command_intent(pending.expected) != command_intent(expected)
                and self._matches_verified_system_state(expected)
            ):
                queued = [
                    queued_command
                    for queued_command in queued
                    if command_intent(queued_command.expected)
                    != command_intent(expected)
                ]
            self._record_coalesced_history(target, queued, replacement)
            if queued:
                self._queued[target] = queued
                self.last_command_status = f"queued:{label}"
                self.health.increment("commands_coalesced")
            else:
                self._queued.pop(target, None)
                self.last_command_status = f"waiting:{pending.label}"
            self._record_command_queue_depth()
            self._notify()
            return
        if coalesce and not from_queue and self._queued.get(target):
            queued = coalesce_queued(None, self._queued[target], replacement)
            if target == "system" and self._matches_verified_system_state(expected):
                queued = [
                    queued_command
                    for queued_command in queued
                    if command_intent(queued_command.expected)
                    != command_intent(expected)
                ]
            self._record_coalesced_history(target, queued, replacement)
            if not queued:
                self._queued.pop(target, None)
                self.last_command_status = f"confirmed:{label}"
                self._record_command_queue_depth()
                self._notify()
                return
            self._queued[target] = queued
            self.last_command_status = f"queued:{label}"
            self.health.increment("commands_coalesced")
            self._record_command_queue_depth()
            self._notify()
            return
        if target == "system" and self._matches_verified_system_state(expected):
            self.health.command_result(trace_id, "unchanged")
            self.last_command_status = f"confirmed:{label}"
            self._record_command_queue_depth()
            self._notify()
            return
        now = time.monotonic()
        if self._last_command_at is not None:
            remaining = self.command_min_interval - (now - self._last_command_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
        try:
            validate_send_guard()
        except HomeAssistantError:
            self.health.command_result(trace_id, "blocked")
            raise
        now = time.monotonic()
        pending = PendingCommand(
            label,
            target,
            expected,
            now,
            now + self.command_confirmation_timeout,
            first_status_poll_at(now, self.command_confirmation_timeout),
            mac,
            command,
            value,
            trace_id=trace_id,
        )
        self._pending[target] = pending
        self._record_command_queue_depth()
        self.last_command_status = f"waiting:{label}"
        self._notify()
        try:
            self.health.command_sent(trace_id)
            if send_guard is None:
                await self._client.send_command(mac, command, value)
            else:
                await self._client.send_command(
                    mac,
                    command,
                    value,
                    before_write=validate_send_guard,
                )
            self.health.increment("commands_sent")
            self._last_command_at = time.monotonic()
        except Exception:
            self.health.command_result(trace_id, "failed")
            self._pending.pop(target, None)
            self._record_command_queue_depth()
            self.last_command_status = f"failed:{label}"
            _LOGGER.warning(
                "MC7021 command send failed: target_type=%s command_code=%d attempt=1",
                self._command_target_type(target),
                command,
            )
            self._notify()
            raise

    def _record_coalesced_history(
        self, target: str, retained: list[QueuedCommand], replacement: QueuedCommand
    ) -> None:
        """Finish every discarded queued intent, without recording a fake send."""
        retained_ids = {command.trace_id for command in retained}
        for old in self._queued.get(target, ()):
            if old.trace_id not in retained_ids:
                self.health.command_result(old.trace_id, "superseded")
        if replacement.trace_id not in retained_ids:
            self.health.command_result(replacement.trace_id, "unchanged")

    async def _async_dispatch_queued(self) -> None:
        async with self._command_lock:
            ready = [target for target in self._queued if target not in self._pending]
            if not ready:
                return
            target = ready[0]
            queued = self._queued[target].pop(0)
            if not self._queued[target]:
                self._queued.pop(target, None)
            self._record_command_queue_depth()
            try:
                await self._async_send_tracked_locked(
                    queued.target,
                    queued.label,
                    queued.expected,
                    queued.mac,
                    queued.command,
                    queued.value,
                    coalesce=True,
                    send_guard=queued.send_guard,
                    from_queue=True,
                    trace_id=queued.trace_id,
                )
            except HomeAssistantError:
                self.health.command_result(queued.trace_id, "blocked")
                self.last_command_status = f"failed:{queued.label}"
                _LOGGER.warning(
                    "Queued command was not sent: target_type=%s command_code=%d",
                    self._command_target_type(queued.target),
                    queued.command,
                )
                self._notify()

    async def _async_poll_pending_status(self, now: float) -> None:
        """Request a fresh status report while commands await confirmation."""
        due = [
            pending
            for pending in self._pending.values()
            if now >= pending.next_status_poll_at and now < pending.deadline
        ]
        if not due or self._client is None:
            return
        self.health.increment("status_fallback_queries")
        for pending in due:
            pending.status_queries += 1
            self.health.command_queried(pending.trace_id)
        await self._client.request_status()
        self._last_status_query_at = now
        self._last_status_query_completed_at = time.monotonic()
        for pending in due:
            pending.next_status_poll_at = now + STATUS_POLL_INTERVAL
        _LOGGER.debug(
            "Requested MC7021 status for pending confirmations: pending_count=%d "
            "thermostat_count=%d system_count=%d",
            len(due),
            sum(
                self._command_target_type(pending.target) == "thermostat"
                for pending in due
            ),
            sum(
                self._command_target_type(pending.target) == "system" for pending in due
            ),
        )

    def _confirm_pending(self, target: str, actual: dict[str, str | None]) -> None:
        pending = self._pending.get(target)
        if pending is None or not all(
            actual.get(key) == value for key, value in pending.expected.items()
        ):
            return
        self._pending.pop(target, None)
        self.health.command_result(pending.trace_id, "confirmed")
        self._record_command_queue_depth()
        self.last_command_status = f"confirmed:{pending.label}"
        self.health.increment("commands_confirmed")
        self.health.increment(
            "commands_confirmed_after_query"
            if pending.status_queries
            else "commands_confirmed_by_push"
        )
        self.health.record_confirmation_latency(time.monotonic() - pending.sent_at)
        self.health.record_command_confirmation()
        self.repairs.set_command_timeout(False)

    async def _async_expire_pending(self, now: float) -> None:
        """Advance queued commands, retry setpoints once, or report final failure."""
        expired = [
            target
            for target, pending in self._pending.items()
            if now >= pending.deadline
        ]
        for target in expired:
            async with self._command_lock:
                pending = self._pending.get(target)
                if pending is None or now < pending.deadline:
                    continue
                pending_intent = command_intent(pending.expected)
                has_newer_same_intent = any(
                    command_intent(command.expected) == pending_intent
                    for command in self._queued.get(target, ())
                )
                if has_newer_same_intent:
                    self.health.command_result(pending.trace_id, "superseded")
                    self._pending.pop(target, None)
                    self.health.increment("commands_timed_out")
                    self.last_command_status = f"timeout_continuing:{pending.label}"
                    _LOGGER.info(
                        "Command confirmation timed out; continuing a newer queued "
                        "intent: target_type=%s command_code=%d attempt=%d "
                        "elapsed_seconds=%.1f confirmation_timeout_seconds=%.1f",
                        self._command_target_type(target),
                        pending.command,
                        pending.attempts,
                        now - pending.sent_at,
                        self.command_confirmation_timeout,
                    )
                elif temperature_retry_is_allowed(pending):
                    self.health.increment("commands_timed_out")
                    await self._async_retry_temperature_command(pending)
                else:
                    self.health.command_result(pending.trace_id, "timeout")
                    self._pending.pop(target, None)
                    self.health.increment("commands_timed_out")
                    self.last_command_status = f"timeout:{pending.label}"
                    consecutive_timeouts = self.health.record_final_command_timeout()
                    self.repairs.set_command_timeout(True)
                    log = _LOGGER.error if consecutive_timeouts >= 3 else _LOGGER.warning
                    log(
                        "MC7021 command confirmation timed out: target_type=%s "
                        "command_code=%d attempt=%d elapsed_seconds=%.1f "
                        "confirmation_timeout_seconds=%.1f consecutive_timeouts=%d",
                        self._command_target_type(target),
                        pending.command,
                        pending.attempts,
                        now - pending.sent_at,
                        self.command_confirmation_timeout,
                        consecutive_timeouts,
                    )
                self._record_command_queue_depth()
        if expired:
            self._notify()

    async def _async_retry_temperature_command(self, pending: PendingCommand) -> None:
        """Retry one unconfirmed thermostat setpoint without reporting failure yet."""
        client = self._client
        if client is None or not self.available:
            raise ConnectionError("MC7021 session unavailable during command retry")
        now = time.monotonic()
        if self._last_command_at is not None:
            remaining = self.command_min_interval - (now - self._last_command_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
        if self._pending.get(pending.target) is not pending:
            return
        retry_at = time.monotonic()
        pending.attempts += 1
        self.health.increment("commands_retried")
        pending.deadline = retry_at + self.command_confirmation_timeout
        pending.next_status_poll_at = first_status_poll_at(
            retry_at, self.command_confirmation_timeout
        )
        self.last_command_status = f"retrying:{pending.label}"
        _LOGGER.warning(
            "Thermostat command confirmation delayed; retrying once: "
            "target_type=%s command_code=%d attempt=%d "
            "confirmation_timeout_seconds=%.1f",
            self._command_target_type(pending.target),
            pending.command,
            pending.attempts,
            self.command_confirmation_timeout,
        )
        try:
            self.health.command_sent(pending.trace_id)
            await client.send_command(pending.mac, pending.command, pending.value)
            self.health.increment("commands_sent")
            self._last_command_at = time.monotonic()
        except Exception:
            self.health.command_result(pending.trace_id, "failed")
            if self._pending.get(pending.target) is pending:
                self._pending.pop(pending.target, None)
                self.last_command_status = f"failed:{pending.label}"
                _LOGGER.warning(
                    "MC7021 command retry send failed: target_type=%s "
                    "command_code=%d attempt=%d",
                    self._command_target_type(pending.target),
                    pending.command,
                    pending.attempts,
                )
            raise

    async def _async_frame_received(self, frame: YasHcpFrame) -> None:
        if frame.kind != 3 or frame.opcode != 8:
            return
        if not is_complete_tlv_body(frame.body):
            self.health.increment("ignored_statuses")
            return
        room_id = ""
        changed = False
        for tag, value in iter_tlvs(frame.body):
            if tag == 0x0030:
                room_id = decode_text(value)
            elif tag == 0x0036 and room_id:
                name = decode_text(value)
                if await self.panel_registry.async_set_room_name(room_id, name):
                    self.room_names[room_id] = name
                    changed = True
        if changed:
            self._notify()

    async def _async_status_received(self, body: bytes) -> None:
        changed = False
        now_utc = datetime.now(UTC)
        now_monotonic = time.monotonic()
        if not is_complete_tlv_body(body):
            self.health.increment("ignored_statuses")
            await self.panel_registry.async_pause_monitoring(now_utc)
            return
        total = decode_tech_system_status(body, self.tech_system_mac)
        if total:
            self._last_system_status_at = now_monotonic
            self._system_status_query_at = None
            self.protocol_verified = True
            self.protocol_status = "verified"
            for name, value in total.items():
                if getattr(self.state, name) != value:
                    setattr(self.state, name, value)
                    changed = True
            if self.state.system_fault_code is not None:
                self.repairs.set_fault_code("system", self.state.system_fault_code)
            if self.state.filter_fault_code is not None:
                self.repairs.set_fault_code("filter", self.state.filter_fault_code)
            self._confirm_pending(
                "system",
                {
                    "power": self.state.power,
                    "mode": self.state.mode,
                    "scene": self.state.scene,
                    "winter_humidifier": self.state.winter_humidifier,
                    "energy_saving": self.state.energy_saving,
                },
            )
        thermostat = decode_thermostat_status(body, self.tech_system_mac)
        valid_status = bool(total) or thermostat is not None
        if valid_status:
            previous_status_at = self._last_valid_status_at
            status_is_contiguous = (
                previous_status_at is None
                or now_monotonic >= previous_status_at
                and now_monotonic - previous_status_at
                <= self.controller_silence_timeout
            )
            self._last_valid_status_at = now_monotonic
        if (
            valid_status
            and self.connected
            and self.health.stage is ConnectionStage.READY
        ):
            if not status_is_contiguous:
                await self.panel_registry.async_pause_monitoring(now_utc)
            await self.panel_registry.async_note_status_stream(
                now_utc, now_monotonic=now_monotonic
            )
            await self.async_sync_stale_panel_repairs()
        elif not valid_status:
            await self.panel_registry.async_pause_monitoring(now_utc)
        if thermostat is not None:
            mac_hex = thermostat.mac.hex()
            previous = self.thermostats.get(mac_hex)
            await self.async_note_panel_report(
                mac_hex,
                thermostat.room_id,
                now_utc,
                now_monotonic=now_monotonic,
            )
            reported_temperature = thermostat.current_temperature
            reported_humidity = thermostat.humidity
            measurements_valid = preserve_valid_thermostat_measurements(
                thermostat, previous
            )
            if not measurements_valid:
                self.health.increment("invalid_measurements")
                _LOGGER.debug(
                    "Ignored implausible thermostat measurements: "
                    "temperature=%s humidity=%s",
                    reported_temperature,
                    reported_humidity,
                )
            self.thermostats[mac_hex] = thermostat
            if measurements_valid:
                self._update_filtered(thermostat)
            self._confirm_pending(
                f"thermostat_{mac_hex}",
                {
                    "target_temperature": f"{thermostat.target_temperature:g}",
                    "power": thermostat.power,
                },
            )
            changed = True
        if not total and thermostat is None:
            self.health.increment("ignored_statuses")
        if total or changed:
            self._notify()

    async def async_sync_stale_panel_repairs(self) -> None:
        """Expose each observed-thirty-day absence as one actionable Repair."""
        async with self._panel_lifecycle_lock:
            for mac_hex in self.panel_registry.stale_macs:
                record = self.panel_registry.records.get(mac_hex)
                if record is not None:
                    await self.repairs.async_set_stale_panel(
                        record, self.panel_registry.room_names.get(record.room_id)
                    )

    async def async_note_panel_report(
        self,
        mac_hex: str,
        room_id: str,
        now_utc: datetime,
        *,
        now_monotonic: float | None = None,
    ) -> bool:
        """Record a panel report and resolve its stale Repair, if any."""
        async with self._panel_lifecycle_lock:
            is_new = await self.panel_registry.async_note_panel_report(
                mac_hex, room_id, now_utc, now_monotonic=now_monotonic
            )
            await self.repairs.async_clear_stale_panel(mac_hex)
            return is_new

    async def async_forget_panel(self, mac_hex: str) -> None:
        """Remove a user-deleted panel from runtime state after registry cleanup."""
        async with self._panel_lifecycle_lock:
            await self._async_forget_panel_locked(mac_hex)

    async def async_remove_stale_panel_if_current(
        self,
        mac_hex: str,
        *,
        issue_is_current: Callable[[], bool],
        cleanup_owned_records: Callable[[], Awaitable[None]],
    ) -> bool:
        """Atomically revalidate and remove one stale panel.

        The caller supplies the Home Assistant registry cleanup so the source
        record stays intact if registry removal raises. Keeping it inside the
        lifecycle lock makes a concurrent report deterministically either
        invalidate this Repair or rediscover the panel after removal.
        """
        async with self._panel_lifecycle_lock:
            record = self.panel_registry.records.get(mac_hex)
            if (
                record is None
                or record.available
                or record.monitored_absence_seconds < STALE_PANEL_SECONDS
                or not issue_is_current()
            ):
                return False
            await cleanup_owned_records()
            await self._async_forget_panel_locked(mac_hex)
            return True

    async def _async_forget_panel_locked(self, mac_hex: str) -> None:
        """Forget one panel while the panel lifecycle lock is held."""
        await self.panel_registry.async_delete_panel(mac_hex)
        self.thermostats.pop(mac_hex, None)
        self.filtered.pop(mac_hex, None)

    def _update_filtered(self, thermostat: ThermostatState) -> None:
        if thermostat.current_temperature is None or thermostat.humidity is None:
            return
        mac_hex = thermostat.mac.hex()
        values = self.filtered.get(mac_hex)
        if values is None:
            values = FilteredMeasurements(deque(maxlen=3), deque(maxlen=3))
            self.filtered[mac_hex] = values
        values.temperatures.append(thermostat.current_temperature)
        values.humidities.append(thermostat.humidity)
        temperature = round(statistics.median(values.temperatures), 1)
        humidity = round(statistics.median(values.humidities))
        if values.temperature is None or abs(temperature - values.temperature) >= 0.2:
            values.temperature = temperature
        if values.humidity is None or abs(humidity - values.humidity) >= 2:
            values.humidity = humidity

    async def _async_refresh_thermostat_availability(self, now: float) -> None:
        if self.thermostat_offline_after == 0:
            return
        changed = False
        for thermostat in self.thermostats.values():
            if (
                thermostat.available
                and now - thermostat.last_seen >= self.thermostat_offline_after
            ):
                thermostat.available = False
                await self.panel_registry.async_set_panel_available(
                    thermostat.mac.hex(), False
                )
                changed = True
        if changed:
            self._notify()

    async def _async_restore_panels(self) -> None:
        await self.panel_registry.async_load()
        self.room_names.update(self.panel_registry.room_names)
        for record in self.panel_registry.records.values():
            mac = parse_device_mac(record.mac_hex)
            self.thermostats[mac.hex()] = ThermostatState(
                mac=mac,
                room_id=record.room_id,
                available=False,
            )
