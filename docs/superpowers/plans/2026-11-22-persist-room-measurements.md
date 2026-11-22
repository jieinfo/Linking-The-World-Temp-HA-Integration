# Persist Room Measurements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Preserve each room panel's last valid temperature and humidity across HA restarts so HomeKit Bridge can create the humidity characteristic during cold startup without changing HomeKit Bridge or device control behavior.

**Architecture:** Extend the existing panel registry persistence with optional last-valid measurements and migrate existing records without changing panel identities. Restore those values into the runtime `ThermostatState` before climate entities are forwarded. During the initial connection/protocol bootstrap only, a cached panel is exposed as an assumed cached state so HomeKit can create the humidity characteristic; after protocol verification, connection failure, or disconnect, normal availability semantics apply. Update the persisted cache only after the existing protocol validation accepts a report.

**Tech Stack:** Python, Home Assistant custom integration, `homeassistant.helpers.storage.Store`, pytest and existing native integration fixtures.

**Spec:** The approved project-only design in the conversation: persist last valid room temperature/humidity, restore them for startup capability discovery, keep the panel unavailable until a fresh report, and update with live data.

## Global Constraints

- Do not modify Home Assistant Core or HomeKit Bridge.
- Do not send control commands, reload HomeKit, alter pairing, or mark a restored panel as freshly reported or verified online.
- Only valid temperature/humidity reports may replace the persisted cache.
- Existing v1/v2 panel records and MAC/room identities must remain loadable.
- Invalid persisted measurements must be ignored without invalidating an otherwise valid panel record.

## Review Focus

- A restored panel exposes cached temperature/humidity during the bounded startup cache window; test that it returns unavailable after disconnect.
- A first-ever panel with no cache remains `None` until its first valid report; test the no-cache path.
- A malformed or out-of-range persisted measurement is discarded while lifecycle metadata survives; test partial corruption.
- An invalid live measurement does not overwrite a previously valid cache; test the protocol validation boundary.
- A valid live report updates both runtime state and persisted cache without changing panel availability semantics; test reload after flush.

### Task 1: Persisted Measurement Model

**Files:**
- Modify: `custom_components/linking_the_world_temp_ha/panel_registry.py`
- Test: `tests/native/test_panel_registry.py`

**Interfaces:**
- Produces `PanelRecord.last_temperature: float | None` and `PanelRecord.last_humidity: int | None`.
- Produces `PanelRegistry.async_note_valid_measurements(mac_hex: str, temperature: float, humidity: int) -> bool`.
- Store version advances from 2 to 3; v1 and v2 records migrate with empty measurement fields.

- [x] Write migration, validation, cache-update, and flush/reload tests first.
- [x] Run the focused tests and confirm they fail because the new fields/API do not exist.
- [x] Add v3 record fields, v2 migration defaults, finite/range validation, serialization, and the idempotent cache update method.
- [x] Run `tests/native/test_panel_registry.py` and confirm it passes.

### Task 2: Restore and Update Runtime Climate State

**Files:**
- Modify: `custom_components/linking_the_world_temp_ha/hub.py`
- Test: `tests/native/test_setup.py` or the closest existing hub startup test.

**Interfaces:**
- Consumes `PanelRecord.last_temperature` and `last_humidity` from Task 1.
- Restores cached values into `ThermostatState` inside `_async_restore_panels` with `available=False`.
- Provides a bounded `startup_cache_active` state used only by room entities to expose cached values before verification.
- Calls `async_note_valid_measurements` only after `preserve_valid_thermostat_measurements` accepts the live report.

- [x] Add a regression test proving restored values appear in the runtime state and the Climate entity is available only during bootstrap.
- [x] Run it and confirm failure before implementation.
- [x] Implement restore and live-cache update at the existing validation boundary.
- [x] Run focused setup/hub tests and confirm live recovery still marks the panel available.

### Task 3: Entity and Full Verification

**Files:**
- Modify: `tests/native/test_entities.py` only if entity-level assertions are needed.
- Modify: `CHANGELOG.md` only if the repository release workflow requires an unreleased entry; do not bump the project version unless explicitly requested.

- [x] Add or update the entity assertion that cached humidity is exposed during bootstrap and disappears from HA state attributes after disconnect.
- [x] Run focused entity and panel-registry tests.
- [x] Run Ruff, mypy, repository contract tests, and the full pytest suite with coverage threshold.
- [x] Review the final diff for no HA Core/HomeKit files, no control commands, and no version bump.
