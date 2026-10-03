"""Command confirmation and latest-value coalescing helpers."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

STATUS_POLL_INTERVAL = 2.0
PUSH_CONFIRMATION_GRACE = 1.0
MAX_TEMPERATURE_COMMAND_ATTEMPTS = 2


@dataclass
class PendingCommand:
    """A sent command awaiting a matching state report."""

    label: str
    target: str
    expected: dict[str, str]
    sent_at: float
    deadline: float
    next_status_poll_at: float
    mac: bytes
    command: int
    value: int | None
    attempts: int = 1
    status_queries: int = 0
    trace_id: int | None = None


@dataclass(frozen=True)
class QueuedCommand:
    """The final command retained while another command awaits confirmation."""

    label: str
    target: str
    expected: dict[str, str]
    mac: bytes
    command: int
    value: int | None
    send_guard: Callable[[], str | None] | None = None
    trace_id: int | None = None


def command_intent(expected: dict[str, str]) -> str:
    """Return the single state property changed by a tracked command."""
    if len(expected) != 1:
        raise ValueError("tracked commands must contain exactly one expected property")
    return next(iter(expected))


def coalesce_queued(
    pending: PendingCommand | None,
    queued: Sequence[QueuedCommand],
    replacement: QueuedCommand,
) -> list[QueuedCommand]:
    """Keep the latest command per property while preserving cross-property order."""
    intent = command_intent(replacement.expected)
    retained = [
        command for command in queued if command_intent(command.expected) != intent
    ]
    if pending is not None and replacement.expected == pending.expected:
        return retained
    retained.append(replacement)
    return retained


def temperature_retry_is_allowed(pending: PendingCommand) -> bool:
    """Return whether an idempotent thermostat setpoint may be sent once more."""
    return (
        pending.target.startswith("thermostat_")
        and "target_temperature" in pending.expected
        and pending.value is not None
        and pending.attempts < MAX_TEMPERATURE_COMMAND_ATTEMPTS
    )


def first_status_poll_at(sent_at: float, confirmation_timeout: float) -> float:
    """Leave time for a controller push before falling back to a status query."""
    return sent_at + min(PUSH_CONFIRMATION_GRACE, confirmation_timeout / 2)
