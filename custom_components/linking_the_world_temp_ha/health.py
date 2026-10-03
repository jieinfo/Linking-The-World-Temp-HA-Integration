"""Bounded, privacy-safe runtime health metrics."""

from __future__ import annotations

import re
import time
from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import fmean
from typing import Any

from .runtime import ConnectionStage, FailureKind

_COUNTER_NAMES = (
    "connection_attempts",
    "connection_successes",
    "disconnects",
    "reconnects",
    "handshake_successes",
    "handshake_failures",
    "login_successes",
    "login_failures",
    "frames_decoded",
    "frames_malformed",
    "frames_resynchronized",
    "bytes_discarded",
    "invalid_measurements",
    "ignored_statuses",
    "commands_sent",
    "commands_confirmed",
    "commands_confirmed_by_push",
    "commands_confirmed_after_query",
    "status_fallback_queries",
    "system_status_refresh_queries",
    "commands_retried",
    "commands_coalesced",
    "commands_blocked",
    "commands_timed_out",
)
_SECRET_VALUE = re.compile(
    r"(?i)\b(password|username|host|client_id|mac|token|key|body)\s*=\s*[^\s,;]+"
)
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HEX_IDENTIFIER = re.compile(r"(?i)\b[0-9a-f]{16,}\b")
_COLON_MAC = re.compile(r"(?i)\b(?:[0-9a-f]{2}:){5,7}[0-9a-f]{2}\b")
_BRACKETED_IPV6_ENDPOINT = re.compile(r"\[[0-9a-fA-F:.%]+\](?::\d{1,5})?")
_BARE_IPV6 = re.compile(
    r"(?<![0-9a-fA-F:])(?:[0-9a-fA-F]{1,4}:){2,}[0-9a-fA-F:]+(?![0-9a-fA-F:])"
)
_DNS_ENDPOINT = re.compile(
    r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,63}|local)(?::\d{1,5})?\b"
)
_AT_HOST_PORT = re.compile(
    r"(?i)\bat\s+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?::\d{1,5})\b"
)


@dataclass
class _CommandTrace:
    """An in-memory reference; household targets are redacted at export."""

    id: int
    target: str
    intent: str
    timestamp: str
    submitted_at: float
    sent_at: float | None = None
    finished_at: float | None = None
    attempts: int = 0
    status_queries: int = 0
    result: str = "queued"


def _sanitize_message(
    message: object, secrets: Mapping[str, object] | None = None
) -> str:
    """Retain an actionable short message without secrets or transport data."""
    text = " ".join(str(message).split())[:512]
    if secrets:
        ordered_secrets = sorted(
            secrets.items(), key=lambda item: len(str(item[1])), reverse=True
        )
        for name, value in ordered_secrets:
            if value is not None and (secret := str(value)):
                text = text.replace(secret, f"<redacted-{name}>")
    text = _SECRET_VALUE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _BRACKETED_IPV6_ENDPOINT.sub("<redacted-ipv6>", text)
    text = _BARE_IPV6.sub("<redacted-ipv6>", text)
    text = _DNS_ENDPOINT.sub("<redacted-host>", text)
    text = _AT_HOST_PORT.sub("at <redacted-host>", text)
    text = _IPV4.sub("<redacted-ip>", text)
    text = _COLON_MAC.sub("<redacted-mac>", text)
    return _HEX_IDENTIFIER.sub("<redacted-id>", text)


class HealthTracker:
    """Collect bounded health data suitable for a future diagnostics export."""

    def __init__(self, *, history_size: int = 32, latency_size: int = 100) -> None:
        self._counters: Counter[str] = Counter({name: 0 for name in _COUNTER_NAMES})
        self.stage = ConnectionStage.DISCONNECTED
        self.failure_kind = FailureKind.NONE
        self._stage_history: deque[dict[str, str]] = deque(maxlen=history_size)
        self._failure_history: deque[dict[str, str]] = deque(maxlen=history_size)
        self._confirmation_latencies: deque[float] = deque(maxlen=latency_size)
        self._current_command_queue_depth = 0
        self._peak_command_queue_depth = 0
        self._consecutive_command_timeouts = 0
        self._command_timeout_recoveries = 0
        self._command_history: deque[_CommandTrace] = deque(maxlen=30)
        self._next_command_id = 1

    def start_command(self, target: str, intent: str) -> int:
        """Assign one bounded trace to a submitted command, including queue time."""
        trace = _CommandTrace(
            self._next_command_id,
            target,
            intent,
            datetime.now(UTC).isoformat(),
            time.monotonic(),
        )
        self._next_command_id += 1
        self._command_history.append(trace)
        return trace.id

    def _command_trace(self, trace_id: int | None) -> _CommandTrace | None:
        return next(
            (item for item in self._command_history if item.id == trace_id), None
        )

    def command_sent(self, trace_id: int | None) -> None:
        """Mark a write attempt before a matching push can race its drain."""
        if (trace := self._command_trace(trace_id)) is not None:
            if trace.sent_at is None:
                trace.sent_at = time.monotonic()
            trace.attempts += 1
            trace.result = "waiting"

    def command_queried(self, trace_id: int | None) -> None:
        """Count fallback queries for this command, not unrelated room reports."""
        if (trace := self._command_trace(trace_id)) is not None:
            trace.status_queries += 1

    def command_result(self, trace_id: int | None, result: str) -> None:
        """Complete a retained trace; evicted traces need no further bookkeeping."""
        if (trace := self._command_trace(trace_id)) is not None:
            trace.result = result
            trace.finished_at = time.monotonic()

    def command_history(self, panel_labels: Mapping[str, str]) -> list[dict[str, Any]]:
        """Return fresh dictionaries with the same anonymous labels as panels."""
        return [
            {
                "id": trace.id,
                "timestamp": trace.timestamp,
                "target": (
                    "system"
                    if trace.target == "system"
                    else panel_labels.get(
                        trace.target.removeprefix("thermostat_"), "panel_unknown"
                    )
                ),
                "intent": trace.intent,
                "result": trace.result,
                "attempts": trace.attempts,
                "status_queries": trace.status_queries,
                "queue_delay_seconds": (
                    round(max(0.0, trace.sent_at - trace.submitted_at), 3)
                    if trace.sent_at is not None
                    else None
                ),
                "confirmation_seconds": (
                    round(max(0.0, trace.finished_at - trace.sent_at), 3)
                    if trace.result == "confirmed"
                    and trace.sent_at is not None
                    and trace.finished_at is not None
                    else None
                ),
            }
            for trace in self._command_history
        ]

    def increment(self, name: str, value: int = 1) -> None:
        """Increment one named counter without retaining event payloads."""
        self._counters[name] += value

    def record_failure(
        self,
        kind: FailureKind,
        message: object,
        *,
        secrets: Mapping[str, object] | None = None,
    ) -> None:
        """Store a sanitized, bounded summary of the newest failure."""
        self.failure_kind = kind
        self._failure_history.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "kind": kind.value,
                "message": _sanitize_message(message, secrets),
            }
        )

    def clear_failure(self) -> None:
        """Mark the active controller session healthy without erasing history."""
        self.failure_kind = FailureKind.NONE

    def record_confirmation_latency(self, seconds: float) -> None:
        """Track a bounded summary of command acknowledgement delays."""
        self._confirmation_latencies.append(round(max(0.0, float(seconds)), 3))

    def record_command_queue_depth(self, depth: int) -> None:
        """Track current and peak in-memory command pressure."""
        self._current_command_queue_depth = max(0, int(depth))
        self._peak_command_queue_depth = max(
            self._peak_command_queue_depth, self._current_command_queue_depth
        )

    def record_final_command_timeout(self) -> int:
        """Count one command that exhausted every recovery path."""
        self._consecutive_command_timeouts += 1
        return self._consecutive_command_timeouts

    def record_command_confirmation(self) -> bool:
        """Reset final failures and report whether this is a recovery."""
        if not self._consecutive_command_timeouts:
            return False
        self._consecutive_command_timeouts = 0
        self._command_timeout_recoveries += 1
        return True

    def mark_stage(self, stage: ConnectionStage) -> None:
        """Record the current lifecycle stage, retaining a bounded history."""
        self.stage = stage
        self._stage_history.append(
            {"timestamp": datetime.now(UTC).isoformat(), "stage": stage.value}
        )

    def snapshot(self) -> dict[str, Any]:
        """Return copies only; callers cannot mutate retained health history."""
        latencies = list(self._confirmation_latencies)
        return {
            "stage": self.stage.value,
            "failure_kind": self.failure_kind.value,
            "counters": dict(self._counters),
            "stage_history": [dict(item) for item in self._stage_history],
            "failure_history": [dict(item) for item in self._failure_history],
            "confirmation_latencies": latencies,
            "confirmation_latency_summary": {
                "count": len(latencies),
                "minimum": min(latencies) if latencies else None,
                "maximum": max(latencies) if latencies else None,
                "mean": round(fmean(latencies), 3) if latencies else None,
            },
            "command_runtime": {
                "current_queue_depth": self._current_command_queue_depth,
                "peak_queue_depth": self._peak_command_queue_depth,
                "consecutive_timeouts": self._consecutive_command_timeouts,
                "recoveries": self._command_timeout_recoveries,
            },
        }
