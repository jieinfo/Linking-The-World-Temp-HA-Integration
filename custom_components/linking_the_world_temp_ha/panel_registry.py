"""Versioned persistence for discovered room thermostat panels."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN

PANEL_STORE_VERSION = 3
STALE_PANEL_SECONDS = 30 * 24 * 60 * 60
SAVE_DELAY_SECONDS = 1


def _panel_store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.panels"


async def async_remove_panel_storage(hass: HomeAssistant, entry_id: str) -> None:
    """Delete panel history only when its config entry is permanently removed."""
    await Store[dict[str, Any]](
        hass, PANEL_STORE_VERSION, _panel_store_key(entry_id)
    ).async_remove()


@dataclass(slots=True)
class PanelRecord:
    """Persisted lifecycle facts for a dynamically discovered panel."""

    mac_hex: str
    room_id: str
    first_seen_utc: datetime
    last_report_utc: datetime | None
    available: bool
    monitored_absence_seconds: float
    checkpoint_utc: datetime | None
    last_temperature: float | None
    last_humidity: int | None


class _PanelStore(Store[dict[str, Any]]):
    """Store versioned panel records while preserving the original key."""

    def __init__(
        self, hass: HomeAssistant, key: str, clock: Callable[[], datetime]
    ) -> None:
        super().__init__(hass, PANEL_STORE_VERSION, key)
        self._clock = clock

    async def _async_migrate_func(
        self, old_major_version: int, _old_minor_version: int, old_data: Any
    ) -> dict[str, Any]:
        """Migrate older records without changing panel identities."""
        if old_major_version not in (1, 2) or not isinstance(old_data, dict):
            raise NotImplementedError
        now_utc = _utc(self._clock())
        rooms = old_data.get("rooms", {})
        panels = old_data.get("panels", [])
        if old_major_version == 2:
            migrated_panels = []
            for panel in panels if isinstance(panels, list) else []:
                if isinstance(panel, dict):
                    migrated_panels.append(
                        {
                            **panel,
                            "last_temperature": panel.get("last_temperature"),
                            "last_humidity": panel.get("last_humidity"),
                        }
                    )
            return {
                "rooms": rooms if isinstance(rooms, dict) else {},
                "panels": migrated_panels,
            }
        return {
            "rooms": rooms if isinstance(rooms, dict) else {},
            "panels": [
                {
                    "mac_hex": panel.get("mac"),
                    "room_id": panel.get("room_id", ""),
                    "first_seen_utc": now_utc.isoformat(),
                    "last_report_utc": None,
                    "available": False,
                    "monitored_absence_seconds": 0.0,
                    "checkpoint_utc": None,
                    "last_temperature": None,
                    "last_humidity": None,
                }
                for panel in panels
                if isinstance(panel, dict)
            ],
        }


def _utc(value: datetime) -> datetime:
    """Normalize a clock value to an aware UTC timestamp."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Panel registry clock must return a timezone-aware timestamp")
    return value.astimezone(UTC)


def _parse_utc(value: object) -> datetime | None:
    """Parse only timezone-aware persisted ISO-8601 timestamps."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _normalize_mac(value: object) -> str | None:
    """Accept only the existing eight-byte lower-case MAC identity format."""
    if not isinstance(value, str):
        return None
    normalized = value.lower().replace(":", "").replace("-", "").strip()
    try:
        raw = bytes.fromhex(normalized)
    except ValueError:
        return None
    return normalized if len(raw) == 8 else None


class PanelRegistry:
    """Keep panel identity and observed absence accounting across restarts."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        status_gap_timeout: float | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._status_gap_timeout = status_gap_timeout
        self._store = _PanelStore(hass, _panel_store_key(entry_id), self._clock)
        self.records: dict[str, PanelRecord] = {}
        self.room_names: dict[str, str] = {}
        self._monitoring_active = False
        self._checkpoint_monotonic: float | None = None

    async def async_load(self) -> None:
        """Restore valid records, isolating individual damaged records."""
        stored = await self._store.async_load() or {}
        if not isinstance(stored, dict):
            return
        rooms = stored.get("rooms", {})
        if isinstance(rooms, dict):
            self.room_names = {
                room_id: name
                for room_id, name in rooms.items()
                if isinstance(room_id, str) and isinstance(name, str)
            }
        panels = stored.get("panels", [])
        if not isinstance(panels, list):
            return
        for item in panels:
            record = self._record_from_storage(item)
            if record is not None:
                self.records[record.mac_hex] = record

    async def async_note_status_stream(
        self, now_utc: datetime, *, now_monotonic: float | None = None
    ) -> None:
        """Advance absence only across contiguous, valid controller status traffic."""
        now_utc = _utc(now_utc)
        monotonic_now = self._monotonic(now_monotonic)
        if not self._monitoring_active:
            self._start_monitoring(now_utc, monotonic_now)
            return
        if self._must_rebaseline(monotonic_now):
            await self.async_pause_monitoring(now_utc)
            self._start_monitoring(now_utc, monotonic_now)
            return
        self._advance_observed(now_utc, monotonic_now)

    async def async_note_panel_report(
        self,
        mac_hex: str,
        room_id: str,
        now_utc: datetime,
        *,
        now_monotonic: float | None = None,
    ) -> bool:
        """Record a fresh valid panel report and return whether it is new."""
        now_utc = _utc(now_utc)
        monotonic_now = self._monotonic(now_monotonic)
        normalized_mac = _normalize_mac(mac_hex)
        if normalized_mac is None:
            raise ValueError("Panel MAC must contain 16 hexadecimal characters")
        if not isinstance(room_id, str):
            raise ValueError("Panel room ID must be a string")
        if self._monitoring_active:
            if self._must_rebaseline(monotonic_now):
                await self.async_pause_monitoring(now_utc)
                self._start_monitoring(now_utc, monotonic_now)
            else:
                self._advance_observed(now_utc, monotonic_now)
        record = self.records.get(normalized_mac)
        is_new = record is None
        if record is None:
            record = PanelRecord(
                mac_hex=normalized_mac,
                room_id=room_id,
                first_seen_utc=now_utc,
                last_report_utc=now_utc,
                available=True,
                monitored_absence_seconds=0.0,
                checkpoint_utc=now_utc if self._monitoring_active else None,
                last_temperature=None,
                last_humidity=None,
            )
            self.records[normalized_mac] = record
        else:
            record.room_id = room_id
            record.last_report_utc = now_utc
            record.available = True
            record.monitored_absence_seconds = 0.0
            record.checkpoint_utc = now_utc if self._monitoring_active else None
        self._schedule_save()
        return is_new

    async def async_pause_monitoring(self, now_utc: datetime) -> None:
        """Stop absence time while controller evidence is unavailable."""
        _utc(now_utc)
        changed = self._monitoring_active
        self._monitoring_active = False
        self._checkpoint_monotonic = None
        for record in self.records.values():
            if record.available or record.checkpoint_utc is not None:
                changed = True
            record.available = False
            record.checkpoint_utc = None
        if changed:
            self._schedule_save()

    async def async_set_panel_available(self, mac_hex: str, available: bool) -> bool:
        """Persist one panel's short-term availability without changing absence facts."""
        normalized_mac = _normalize_mac(mac_hex)
        if normalized_mac is None:
            raise ValueError("Panel MAC must contain 16 hexadecimal characters")
        record = self.records.get(normalized_mac)
        if record is None or record.available is available:
            return False
        record.available = available
        self._schedule_save()
        return True

    async def async_note_valid_measurements(
        self, mac_hex: str, temperature: float, humidity: int
    ) -> bool:
        """Persist one panel's last valid measurements without changing availability."""
        normalized_mac = _normalize_mac(mac_hex)
        if normalized_mac is None:
            raise ValueError("Panel MAC must contain 16 hexadecimal characters")
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not isfinite(temperature)
            or not 0 <= temperature <= 60
        ):
            raise ValueError("Panel temperature must be a finite value from 0 to 60")
        if (
            isinstance(humidity, bool)
            or not isinstance(humidity, int)
            or not 0 <= humidity <= 100
        ):
            raise ValueError("Panel humidity must be an integer from 0 to 100")
        record = self.records.get(normalized_mac)
        if record is None:
            return False
        normalized_temperature = float(temperature)
        if (
            record.last_temperature == normalized_temperature
            and record.last_humidity == humidity
        ):
            return False
        record.last_temperature = normalized_temperature
        record.last_humidity = humidity
        self._schedule_save()
        return True

    async def async_set_room_name(self, room_id: str, name: str) -> bool:
        """Persist room metadata without altering any panel identity."""
        if not isinstance(room_id, str) or not isinstance(name, str):
            return False
        if self.room_names.get(room_id) == name:
            return False
        self.room_names[room_id] = name
        self._schedule_save()
        return True

    async def async_flush(self) -> None:
        """Persist the newest registry state before a clean unload."""
        await self._store.async_save(self._serialize())

    async def async_delete_panel(self, mac_hex: str) -> None:
        """Delete one explicitly selected panel record."""
        normalized_mac = _normalize_mac(mac_hex)
        if normalized_mac is None or self.records.pop(normalized_mac, None) is None:
            return
        self._schedule_save()

    @property
    def stale_macs(self) -> tuple[str, ...]:
        """Return panels with enough continuously observed absence for a Repair."""
        return tuple(
            mac_hex
            for mac_hex, record in self.records.items()
            if record.monitored_absence_seconds >= STALE_PANEL_SECONDS
        )

    @callback
    def _advance_observed(self, now_utc: datetime, monotonic_now: float) -> None:
        changed = False
        checkpoint = self._checkpoint_monotonic
        elapsed = 0.0 if checkpoint is None else monotonic_now - checkpoint
        if elapsed < 0:
            elapsed = 0.0
        for record in self.records.values():
            if elapsed:
                record.monitored_absence_seconds += elapsed
                changed = True
            record.checkpoint_utc = now_utc
        self._checkpoint_monotonic = monotonic_now
        if changed:
            self._schedule_save()

    def _must_rebaseline(self, monotonic_now: float) -> bool:
        """A restart, clock regression, or broken status continuity starts a new interval."""
        checkpoint = self._checkpoint_monotonic
        if not self._monitoring_active or checkpoint is None or monotonic_now < checkpoint:
            return True
        return (
            self._status_gap_timeout is not None
            and monotonic_now - checkpoint > self._status_gap_timeout
        )

    def _start_monitoring(self, now_utc: datetime, monotonic_now: float) -> None:
        """Establish an in-process baseline; UTC exists only for persisted diagnostics."""
        self._monitoring_active = True
        self._checkpoint_monotonic = monotonic_now
        for record in self.records.values():
            record.checkpoint_utc = now_utc
        self._schedule_save()

    def _monotonic(self, value: float | None) -> float:
        monotonic_now = self._monotonic_clock() if value is None else value
        if not isinstance(monotonic_now, (int, float)) or not isfinite(monotonic_now):
            raise ValueError("Panel registry monotonic clock must return a finite number")
        return float(monotonic_now)

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._serialize, SAVE_DELAY_SECONDS)

    @callback
    def _serialize(self) -> dict[str, Any]:
        return {
            "rooms": dict(self.room_names),
            "panels": [
                {
                    **asdict(record),
                    "first_seen_utc": record.first_seen_utc.isoformat(),
                    "last_report_utc": (
                        record.last_report_utc.isoformat()
                        if record.last_report_utc is not None
                        else None
                    ),
                    "checkpoint_utc": (
                        record.checkpoint_utc.isoformat()
                        if record.checkpoint_utc is not None
                        else None
                    ),
                }
                for record in self.records.values()
            ],
        }

    @staticmethod
    def _record_from_storage(item: object) -> PanelRecord | None:
        if not isinstance(item, dict):
            return None
        mac_hex = _normalize_mac(item.get("mac_hex"))
        first_seen_utc = _parse_utc(item.get("first_seen_utc"))
        last_report_utc = _parse_utc(item.get("last_report_utc"))
        checkpoint_utc = _parse_utc(item.get("checkpoint_utc"))
        room_id = item.get("room_id")
        absence = item.get("monitored_absence_seconds")
        if (
            mac_hex is None
            or first_seen_utc is None
            or not isinstance(room_id, str)
            or not isinstance(item.get("available"), bool)
            or not isinstance(absence, (int, float))
            or isinstance(absence, bool)
            or absence < 0
            or not isfinite(absence)
            or (item.get("last_report_utc") is not None and last_report_utc is None)
            or (item.get("checkpoint_utc") is not None and checkpoint_utc is None)
        ):
            return None
        return PanelRecord(
            mac_hex=mac_hex,
            room_id=room_id,
            first_seen_utc=first_seen_utc,
            last_report_utc=last_report_utc,
            available=item["available"],
            monitored_absence_seconds=float(absence),
            checkpoint_utc=checkpoint_utc,
            last_temperature=_parse_temperature(item.get("last_temperature")),
            last_humidity=_parse_humidity(item.get("last_humidity")),
        )


def _parse_temperature(value: object) -> float | None:
    """Parse a persisted credible room temperature, ignoring bad cache data."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or not 0 <= value <= 60
    ):
        return None
    return float(value)


def _parse_humidity(value: object) -> int | None:
    """Parse a persisted integer room humidity, ignoring bad cache data."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        return None
    return value
