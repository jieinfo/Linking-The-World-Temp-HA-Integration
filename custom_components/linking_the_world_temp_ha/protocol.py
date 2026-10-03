"""Async MC7021 YAS HCP protocol implementation."""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from .const import DEFAULT_CLIENT_ID, MODE_VALUES, SCENE_VALUES

_LOGGER = logging.getLogger(__name__)

MAGIC = b"dooyashcp"
VERSION = 1
TRAILER = b"#"
MAX_PAYLOAD_LENGTH = 16_384
PARSER_ANOMALY_LIMIT = 8
PARSER_RECOVERY_GRACE = 5.0
CONNECT_TIMEOUT = 8
HELLO_TIMEOUT = 8
LOGIN_TIMEOUT = 8
WRITE_TIMEOUT = 8
CLOSE_TIMEOUT = 3
TECH_SYSTEM_MAC = bytes.fromhex("ff00ffffffff00ff")

CLIENT_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCbubcnMbVxGmjp2Sc22azesb08
T1MlidtdZpEJYG6OL/PMhwV4z+B/Trf1aQ5G560/4Xs9f2Vgox36DSUs6pvYOql+
Fjc/WfyEB80l5op4M7AhblPr171spbbxkF4Gk2S8DWlf0YouBl3XDk0ZaW/6QArD
z/tjVw5AVVI7+stdPQIDAQAB
-----END PUBLIC KEY-----
""".rstrip(b"\n")

COMMAND_POWER_OFF = 1
COMMAND_POWER_ON = 2
COMMAND_MODE = 3
COMMAND_SCENE = 4
COMMAND_WINTER_HUMIDIFIER = 5
COMMAND_ENERGY_SWITCH = 6

MODE_NAMES = {value: name for name, value in MODE_VALUES.items()}
SCENE_NAMES = {value: name for name, value in SCENE_VALUES.items()}


class ProtocolError(Exception):
    """Base protocol error."""


class MoorgenConnectionError(Exception):
    """The controller connection could not complete."""


class TcpConnectError(MoorgenConnectionError):
    """The TCP socket could not be opened."""


class HandshakeTimeout(MoorgenConnectionError):
    """The controller did not complete the hello exchange."""


class LoginTimeout(MoorgenConnectionError):
    """The controller did not complete the login exchange."""


class AuthenticationRejected(MoorgenConnectionError):
    """The controller explicitly rejected the supplied credentials."""


class IncompatibleProtocol(MoorgenConnectionError):
    """The controller sent a response this client does not understand."""


# Retain the historical catch-all name while callers move to typed failures.
CannotConnect = MoorgenConnectionError


@dataclass(frozen=True)
class YasHcpFrame:
    """Decoded YAS HCP frame."""

    kind: int
    opcode: int
    sequence: int
    body: bytes

    def encode(self) -> bytes:
        header = MAGIC + bytes((VERSION, self.kind, self.opcode))
        header += struct.pack("<HH", self.sequence, len(self.body))
        payload = header + self.body + TRAILER
        return b"#" + struct.pack("<H", len(payload)) + payload


class YasHcpDecoder:
    """Incrementally decode the App's YAS HCP TCP stream."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._parser_anomalies: deque[dict[str, object]] = deque(
            maxlen=PARSER_ANOMALY_LIMIT
        )
        self.frames_decoded = 0
        self.frames_malformed = 0
        self.frames_resynchronized = 0
        self.bytes_discarded = 0

    @property
    def parser_anomalies(self) -> list[dict[str, object]]:
        """Return bounded, payload-free descriptions of malformed frames."""
        return [dict(anomaly) for anomaly in self._parser_anomalies]

    def _record_parser_anomaly(
        self,
        *,
        reason: str,
        outer_length: int,
        raw: bytes | None,
    ) -> None:
        """Retain structural metadata without retaining protocol payload bytes."""
        for anomaly in self._parser_anomalies:
            if anomaly["recovery"] == "pending":
                anomaly["additional_anomalies_before_recovery"] = (
                    cast(int, anomaly["additional_anomalies_before_recovery"]) + 1
                )

        magic_header_valid = raw is not None and raw.startswith(MAGIC)
        header_available = (
            magic_header_valid and len(raw) >= len(MAGIC) + 7
            if raw is not None
            else False
        )
        body_length = (
            struct.unpack_from("<H", raw, len(MAGIC) + 5)[0]
            if raw is not None and header_available
            else None
        )
        expected_length = (
            len(MAGIC) + 7 + body_length + len(TRAILER)
            if body_length is not None
            else None
        )
        self._parser_anomalies.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "reason": reason,
                "outer_length_declared": outer_length,
                "payload_bytes_consumed": len(raw) if raw is not None else None,
                "body_length_declared": body_length,
                "payload_length_expected": expected_length,
                "magic_header_valid": magic_header_valid,
                "trailer_present": raw.endswith(TRAILER) if raw is not None else None,
                "kind": (
                    raw[len(MAGIC) + 1]
                    if raw is not None and header_available
                    else None
                ),
                "opcode": (
                    raw[len(MAGIC) + 2]
                    if raw is not None and header_available
                    else None
                ),
                "sequence": (
                    struct.unpack_from("<H", raw, len(MAGIC) + 3)[0]
                    if raw is not None and header_available
                    else None
                ),
                "recovery": "pending",
                "immediate_recovery": None,
                "additional_anomalies_before_recovery": 0,
                "recovery_kind": None,
                "recovery_opcode": None,
                "recovery_sequence": None,
            }
        )

    def _record_parser_recovery(self, frame: YasHcpFrame) -> None:
        """Mark whether the next successfully parsed frame restored alignment."""
        for anomaly in self._parser_anomalies:
            if anomaly["recovery"] != "pending":
                continue
            additional = cast(int, anomaly["additional_anomalies_before_recovery"])
            anomaly["recovery"] = (
                "next_valid_frame" if additional == 0 else "after_additional_anomalies"
            )
            anomaly["immediate_recovery"] = additional == 0
            anomaly["recovery_kind"] = frame.kind
            anomaly["recovery_opcode"] = frame.opcode
            anomaly["recovery_sequence"] = frame.sequence

    def feed(self, data: bytes) -> list[YasHcpFrame]:
        self._buffer.extend(data)
        output: list[YasHcpFrame] = []
        while True:
            start = self._buffer.find(b"#")
            if start < 0:
                self.bytes_discarded += len(self._buffer)
                if self._buffer:
                    self.frames_resynchronized += 1
                self._buffer.clear()
                return output
            if start:
                self.bytes_discarded += start
                self.frames_resynchronized += 1
                del self._buffer[:start]
            if len(self._buffer) < 3:
                return output
            prefix_length = min(len(MAGIC), len(self._buffer) - 3)
            if bytes(self._buffer[3 : 3 + prefix_length]) != MAGIC[:prefix_length]:
                del self._buffer[0]
                self.bytes_discarded += 1
                self.frames_resynchronized += 1
                continue
            if prefix_length < len(MAGIC):
                return output
            payload_length = struct.unpack_from("<H", self._buffer, 1)[0]
            if payload_length > MAX_PAYLOAD_LENGTH:
                self._record_parser_anomaly(
                    reason="outer_length_exceeds_limit",
                    outer_length=payload_length,
                    raw=None,
                )
                del self._buffer[0]
                self.frames_malformed += 1
                self.bytes_discarded += 1
                self.frames_resynchronized += 1
                continue
            frame_length = 3 + payload_length
            if len(self._buffer) < frame_length:
                return output
            raw = bytes(self._buffer[3:frame_length])
            del self._buffer[:frame_length]
            if (
                not raw.startswith(MAGIC)
                or not raw.endswith(TRAILER)
                or len(raw) < len(MAGIC) + 8
            ):
                self._record_parser_anomaly(
                    reason="invalid_envelope",
                    outer_length=payload_length,
                    raw=raw,
                )
                self.frames_malformed += 1
                continue
            body_length = struct.unpack_from("<H", raw, len(MAGIC) + 5)[0]
            expected_length = len(MAGIC) + 7 + body_length + len(TRAILER)
            if len(raw) != expected_length:
                self._record_parser_anomaly(
                    reason="body_length_mismatch",
                    outer_length=payload_length,
                    raw=raw,
                )
                self.frames_malformed += 1
                continue
            frame = YasHcpFrame(
                kind=raw[len(MAGIC) + 1],
                opcode=raw[len(MAGIC) + 2],
                sequence=struct.unpack_from("<H", raw, len(MAGIC) + 3)[0],
                body=raw[len(MAGIC) + 7 : len(MAGIC) + 7 + body_length],
            )
            self._record_parser_recovery(frame)
            output.append(frame)
            self.frames_decoded += 1


@dataclass
class TechSystemState:
    """Last verified total-control state."""

    power: str | None = None
    mode: str | None = None
    scene: str | None = None
    winter_humidifier: str | None = None
    energy_saving: str | None = None
    temperature: float | None = None
    humidity: int | None = None
    pm25: float | None = None
    co2: int | None = None
    system_fault_code: int | None = None
    filter_fault_code: int | None = None

    @property
    def can_change_mode(self) -> bool:
        return self.power == "OFF"


@dataclass
class ThermostatState:
    """Last verified room thermostat state."""

    mac: bytes
    room_id: str
    target_temperature: float | None = None
    current_temperature: float | None = None
    power: str | None = None
    humidity: int | None = None
    last_seen: float = 0.0
    available: bool = False


def preserve_valid_thermostat_measurements(
    current: ThermostatState, previous: ThermostatState | None
) -> bool:
    """Keep the last credible measurements when MC7021 reports placeholders."""
    temperature = current.current_temperature
    humidity = current.humidity
    valid = (
        temperature is not None
        and humidity is not None
        and 0 <= temperature <= 60
        and 0 <= humidity <= 100
    )
    if valid:
        return True
    current.current_temperature = (
        previous.current_temperature if previous is not None else None
    )
    current.humidity = previous.humidity if previous is not None else None
    return False


def parse_device_mac(value: str) -> bytes:
    normalized = value.replace(":", "").replace("-", "").strip()
    try:
        mac = bytes.fromhex(normalized)
    except ValueError as error:
        raise ValueError("MAC must contain 16 hexadecimal characters") from error
    if len(mac) != 8:
        raise ValueError("MAC must contain 16 hexadecimal characters")
    return mac


def decode_text(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.decode("gb18030", errors="replace")


def tlv(tag: int, value: bytes) -> bytes:
    return struct.pack("<HH", tag, len(value)) + value


def iter_tlvs(data: bytes):
    offset = 0
    while offset + 4 <= len(data):
        tag, length = struct.unpack_from("<HH", data, offset)
        offset += 4
        if offset + length > len(data):
            return
        yield tag, data[offset : offset + length]
        offset += length


def is_complete_tlv_body(data: bytes) -> bool:
    """Return whether *all* bytes form complete TLVs without a trailing fragment."""
    offset = 0
    while offset < len(data):
        if len(data) - offset < 4:
            return False
        _tag, length = struct.unpack_from("<HH", data, offset)
        offset += 4
        if length > len(data) - offset:
            return False
        offset += length
    return True


def parse_tlvs(data: bytes) -> dict[int, bytes]:
    if not is_complete_tlv_body(data):
        return {}
    return dict(iter_tlvs(data))


def decode_tech_system_status(
    body: bytes, tech_system_mac: bytes
) -> dict[str, str | float | int]:
    fields = parse_tlvs(body)
    packed = fields.get(0x000A)
    if fields.get(0x0004) != tech_system_mac or len(packed or b"") != 14:
        return {}
    assert packed is not None
    state: dict[str, str | float | int] = {}
    if power := fields.get(0x000B):
        state["power"] = "ON" if power[0] else "OFF"
    if mode := MODE_NAMES.get(packed[0]):
        state["mode"] = mode
    if scene := SCENE_NAMES.get(packed[1]):
        state["scene"] = scene
    state["winter_humidifier"] = "ON" if packed[2] else "OFF"
    state["energy_saving"] = "ON" if packed[3] else "OFF"
    state["temperature"] = int.from_bytes(packed[4:6], "little") / 10
    state["humidity"] = int.from_bytes(packed[6:8], "little")
    state["pm25"] = int.from_bytes(packed[8:10], "little") / 10
    state["co2"] = int.from_bytes(packed[10:12], "little")
    state["system_fault_code"] = packed[12]
    state["filter_fault_code"] = packed[13]
    return state


def decode_thermostat_status(
    body: bytes, tech_system_mac: bytes
) -> ThermostatState | None:
    fields = parse_tlvs(body)
    mac = fields.get(0x0004)
    packed = fields.get(0x000A)
    power = fields.get(0x000B)
    if (
        len(mac or b"") != 8
        or fields.get(0x0075) != tech_system_mac
        or len(packed or b"") != 5
        or not power
    ):
        return None
    assert mac is not None and packed is not None and power is not None
    return ThermostatState(
        mac=mac,
        room_id=decode_text(fields.get(0x0030, b"")),
        target_temperature=packed[0] // 2,
        current_temperature=int.from_bytes(packed[1:3], "little") / 10,
        power="ON" if power[0] else "OFF",
        humidity=packed[3],
        last_seen=time.monotonic(),
        available=True,
    )


FrameCallback = Callable[[YasHcpFrame], Awaitable[None] | None]
StatusCallback = Callable[[bytes], Awaitable[None] | None]
StageCallback = Callable[[str], Awaitable[None] | None]
ParserEventCallback = Callable[
    [str, int, list[dict[str, object]]], Awaitable[None] | None
]


class AsyncMoorgenClient:
    """One authenticated asynchronous MC7021 connection."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        client_id: str = DEFAULT_CLIENT_ID,
        *,
        parser_recovery_grace: float = PARSER_RECOVERY_GRACE,
    ) -> None:
        if len(client_id) != 16 or any(
            char not in "0123456789abcdefABCDEF" for char in client_id
        ):
            raise ValueError("client_id must contain 16 hexadecimal characters")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client_id = client_id.lower()
        self._parser_recovery_grace = parser_recovery_grace
        self.on_frame: FrameCallback | None = None
        self.on_status: StatusCallback | None = None
        self.on_stage: StageCallback | None = None
        self.on_parser_event: ParserEventCallback | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._parser_warning_task: asyncio.Task[None] | None = None
        self._warned_parser_anomalies: set[str] = set()
        self._decoder = YasHcpDecoder()
        self._inbox: asyncio.Queue[YasHcpFrame | None] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._sequence = 0
        self._ready = False
        self.last_received_at = 0.0
        self.reader_error: Exception | None = None

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def reader_alive(self) -> bool:
        return self._reader_task is not None and not self._reader_task.done()

    async def connect(self) -> None:
        try:
            await self._emit_stage("connecting")
            await self._async_open_socket()
            await self._emit_stage("handshaking")
            await self._async_complete_hello()
            await self._emit_stage("authenticating")
            await self._async_complete_login()
            await self._send_initial_queries()
            self._ready = True
            await self._emit_stage("ready")
        except MoorgenConnectionError:
            await self.close()
            raise
        except asyncio.CancelledError:
            await self.close()
            raise
        except (OSError, ConnectionError) as error:
            await self.close()
            raise IncompatibleProtocol(
                "MC7021 disconnected while starting the authenticated session"
            ) from error

    async def _async_open_socket(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=CONNECT_TIMEOUT
            )
            self.last_received_at = time.monotonic()
            self.reader_error = None
            self._decoder = YasHcpDecoder()
            self._cancel_parser_warning_task()
            self._warned_parser_anomalies.clear()
            self._inbox = asyncio.Queue()
            self._reader_task = asyncio.create_task(
                self._read_loop(self._inbox), name="linking-temp-mc7021-reader"
            )
        except (OSError, TimeoutError, asyncio.TimeoutError) as error:
            raise TcpConnectError(
                f"Could not connect to MC7021 at {self.host}:{self.port}"
            ) from error

    async def _async_complete_hello(self) -> None:
        try:
            await self._send_hello()
            await self._wait_for(1, 3, HELLO_TIMEOUT)
        except IncompatibleProtocol:
            raise
        except (OSError, TimeoutError, asyncio.TimeoutError, ConnectionError) as error:
            raise HandshakeTimeout(
                "MC7021 did not complete the hello exchange"
            ) from error

    async def _async_complete_login(self) -> None:
        try:
            self._discard_pre_login_frames()
            await self._send_login()
            reply = await self._wait_for_opcodes(2, {5, 6}, LOGIN_TIMEOUT)
        except IncompatibleProtocol:
            raise
        except (OSError, TimeoutError, asyncio.TimeoutError, ConnectionError) as error:
            raise LoginTimeout("MC7021 did not complete the login exchange") from error

        if reply.opcode == 6:
            return
        if any(
            tag == 0x031C and value == b"\x01" for tag, value in iter_tlvs(reply.body)
        ):
            raise AuthenticationRejected("MC7021 rejected the supplied credentials")
        raise IncompatibleProtocol("MC7021 sent an unknown login rejection response")

    def _discard_pre_login_frames(self) -> None:
        """Discard stale login replies before sending credentials for this session."""
        deferred: list[YasHcpFrame | None] = []
        while True:
            try:
                frame = self._inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if frame is None or frame.kind != 2:
                deferred.append(frame)
        for frame in deferred:
            self._inbox.put_nowait(frame)

    async def close(self) -> None:
        self._ready = False
        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is not None:
            writer.close()
        task = self._reader_task
        self._reader_task = None
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT):
                if task is not None and task is not asyncio.current_task():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self._flush_pending_parser_warning("connection closed before recovery")
                warning_task = self._parser_warning_task
                self._cancel_parser_warning_task()
                if (
                    warning_task is not None
                    and warning_task is not asyncio.current_task()
                ):
                    await asyncio.gather(warning_task, return_exceptions=True)
                if writer is not None:
                    await writer.wait_closed()
        except TimeoutError:
            if writer is not None:
                writer.transport.abort()
            _LOGGER.debug("MC7021 socket close timed out; transport aborted")
        except asyncio.CancelledError:
            if writer is not None:
                writer.transport.abort()
            raise
        except OSError:
            pass

    async def send_command(
        self,
        mac: bytes,
        command: int,
        value: int | None = None,
        *,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        if not self._ready:
            raise ConnectionError("MC7021 session is not ready")
        body = tlv(0x0010, b"\x01") + tlv(0x0004, mac) + tlv(0x0009, bytes((command,)))
        if value is not None:
            body += tlv(0x000A, bytes((value,)))
        await self._send(4, 9, body, before_write=before_write)

    async def heartbeat(self) -> None:
        if self._ready:
            await self._send(6, 0x0E, b"")

    async def request_status(self) -> None:
        if self._ready:
            await self._send(3, 7, tlv(0x000F, b"\x21"))

    async def _send_hello(self) -> None:
        body = bytes.fromhex("12020f01") + CLIENT_PUBLIC_KEY
        body += bytes.fromhex("13021000") + self.client_id.encode("ascii")
        await self._send(1, 1, body)

    async def _emit_stage(self, stage: str) -> None:
        """Forward lifecycle boundaries without exposing protocol payloads."""
        if self.on_stage is None:
            return
        result = self.on_stage(stage)
        if result is not None:
            await result

    async def _emit_parser_changes(self, before: tuple[int, int, int, int]) -> None:
        """Report parser counters only, never the bytes that produced them."""
        if self.on_parser_event is None:
            return
        after = (
            self._decoder.frames_decoded,
            self._decoder.frames_malformed,
            self._decoder.frames_resynchronized,
            self._decoder.bytes_discarded,
        )
        parser_anomalies = self._decoder.parser_anomalies
        for name, change in zip(
            (
                "frames_decoded",
                "frames_malformed",
                "frames_resynchronized",
                "bytes_discarded",
            ),
            (current - previous for current, previous in zip(after, before)),
            strict=True,
        ):
            if change > 0:
                result = self.on_parser_event(name, change, parser_anomalies)
                if result is not None:
                    await result

    def _pending_parser_anomalies(self) -> list[dict[str, object]]:
        """Return anomalies that have not yet been followed by a valid frame."""
        return [
            anomaly
            for anomaly in self._decoder.parser_anomalies
            if anomaly["recovery"] == "pending"
        ]

    def _cancel_parser_warning_task(self) -> None:
        """Cancel a deferred parser warning after the stream has recovered."""
        task = self._parser_warning_task
        self._parser_warning_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _warn_parser_anomalies(
        self, anomalies: list[dict[str, object]], circumstance: str
    ) -> None:
        """Warn once for one unresolved burst without retaining payload bytes."""
        unwarned = [
            anomaly
            for anomaly in anomalies
            if str(anomaly["timestamp"]) not in self._warned_parser_anomalies
        ]
        if not unwarned:
            return
        first = unwarned[0]
        _LOGGER.warning(
            "MC7021 parser anomaly did not recover normally: %s; "
            "count=%d reason=%s outer_length=%s kind=%s opcode=%s sequence=%s",
            circumstance,
            len(anomalies),
            first["reason"],
            first["outer_length_declared"],
            first["kind"],
            first["opcode"],
            first["sequence"],
        )
        self._warned_parser_anomalies.update(
            str(anomaly["timestamp"]) for anomaly in anomalies
        )

    async def _warn_if_parser_not_recovered(self, timestamp: str) -> None:
        """Promote one still-pending anomaly after the recovery grace period."""
        try:
            await asyncio.sleep(self._parser_recovery_grace)
            pending = self._pending_parser_anomalies()
            if any(str(anomaly["timestamp"]) == timestamp for anomaly in pending):
                self._warn_parser_anomalies(
                    pending,
                    f"no valid frame within {self._parser_recovery_grace:g}s",
                )
        except asyncio.CancelledError:
            raise
        finally:
            if self._parser_warning_task is asyncio.current_task():
                self._parser_warning_task = None

    def _update_parser_warning_state(self) -> None:
        """Apply the deferred warning policy after each decoder pass."""
        anomalies = self._decoder.parser_anomalies
        self._warned_parser_anomalies.intersection_update(
            str(anomaly["timestamp"]) for anomaly in anomalies
        )
        pending = [
            anomaly for anomaly in anomalies if anomaly["recovery"] == "pending"
        ]
        if not pending:
            self._cancel_parser_warning_task()
            return

        if any(
            cast(int, anomaly["additional_anomalies_before_recovery"]) > 0
            for anomaly in pending
        ):
            self._cancel_parser_warning_task()
            self._warn_parser_anomalies(pending, "consecutive malformed frames")
            return

        timestamp = str(pending[0]["timestamp"])
        if timestamp in self._warned_parser_anomalies:
            return
        if self._parser_warning_task is None or self._parser_warning_task.done():
            self._parser_warning_task = asyncio.create_task(
                self._warn_if_parser_not_recovered(timestamp),
                name="linking-temp-parser-recovery-watch",
            )

    def _flush_pending_parser_warning(self, circumstance: str) -> None:
        """Surface an anomaly when the transport ends before parser recovery."""
        pending = self._pending_parser_anomalies()
        if pending:
            self._warn_parser_anomalies(pending, circumstance)

    async def _send_login(self) -> None:
        await self._send(
            2,
            4,
            tlv(0x000C, self.username.encode()) + tlv(0x000D, self.password.encode()),
        )

    async def _send_initial_queries(self) -> None:
        for category in (0x0B, 0x1F, 0x01, 0x11, 0x09, 0x0D, 0x03, 0x07, 0x1B):
            await self._send(3, 7, tlv(0x000F, bytes((category,))))
            await asyncio.sleep(0.15)
        await self._send(
            3,
            7,
            tlv(0x000F, b"\x17") + tlv(0x0077, self.client_id.encode("ascii")),
        )

    async def _send(
        self,
        kind: int,
        opcode: int,
        body: bytes,
        *,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        writer = self._writer
        if writer is None:
            raise ConnectionError("MC7021 socket is not connected")
        try:
            async with asyncio.timeout(WRITE_TIMEOUT):
                async with self._write_lock:
                    if self._writer is not writer:
                        raise ConnectionError("MC7021 socket was closed before write")
                    if before_write is not None:
                        before_write()
                    frame = YasHcpFrame(kind, opcode, self._sequence, body)
                    self._sequence = (self._sequence + 1) & 0xFFFF
                    writer.write(frame.encode())
                    await writer.drain()
                    _LOGGER.debug(
                        "Sent MC7021 kind=%02x opcode=%02x seq=%d body_length=%d",
                        kind,
                        opcode,
                        frame.sequence,
                        len(body),
                    )
        except TimeoutError as error:
            self._ready = False
            writer.transport.abort()
            raise ConnectionError("MC7021 socket write timed out") from error

    async def _wait_for(self, kind: int, opcode: int, timeout: float) -> YasHcpFrame:
        return await self._wait_for_opcodes(kind, {opcode}, timeout)

    async def _wait_for_opcodes(
        self, kind: int, opcodes: set[int], timeout: float
    ) -> YasHcpFrame:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"MC7021 did not return kind={kind:#x}, opcode={opcodes}"
                )
            try:
                frame = await asyncio.wait_for(self._inbox.get(), remaining)
            except asyncio.TimeoutError as error:
                raise TimeoutError(
                    f"MC7021 did not return kind={kind:#x}, opcode={opcodes}"
                ) from error
            if frame is None:
                if self.reader_error is not None:
                    raise self.reader_error
                raise ConnectionError("MC7021 closed the TCP stream")
            if frame.kind == kind:
                if frame.opcode in opcodes:
                    return frame
                raise IncompatibleProtocol(
                    "MC7021 returned an unexpected response "
                    f"kind={frame.kind:#x}, opcode={frame.opcode:#x}"
                )

    async def _read_loop(self, inbox: asyncio.Queue[YasHcpFrame | None]) -> None:
        assert self._reader is not None
        try:
            while data := await self._reader.read(4096):
                self.last_received_at = time.monotonic()
                before = (
                    self._decoder.frames_decoded,
                    self._decoder.frames_malformed,
                    self._decoder.frames_resynchronized,
                    self._decoder.bytes_discarded,
                )
                frames = self._decoder.feed(data)
                self._update_parser_warning_state()
                await self._emit_parser_changes(before)
                for frame in frames:
                    if frame.kind in (1, 2):
                        inbox.put_nowait(frame)
                for frame in frames:
                    _LOGGER.debug(
                        "Received MC7021 kind=%02x opcode=%02x seq=%d body_length=%d",
                        frame.kind,
                        frame.opcode,
                        frame.sequence,
                        len(frame.body),
                    )
                    if self.on_frame is not None:
                        result = self.on_frame(frame)
                        if result is not None:
                            await result
                    if (
                        frame.kind == 5
                        and frame.opcode == 0x0C
                        and self.on_status is not None
                    ):
                        result = self.on_status(frame.body)
                        if result is not None:
                            await result
            raise ConnectionError("MC7021 closed the TCP stream")
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - preserve any reader failure for reconnect diagnostics.
            self.reader_error = error
        finally:
            self._flush_pending_parser_warning("connection closed before recovery")
            self._cancel_parser_warning_task()
            self._ready = False
            inbox.put_nowait(None)
