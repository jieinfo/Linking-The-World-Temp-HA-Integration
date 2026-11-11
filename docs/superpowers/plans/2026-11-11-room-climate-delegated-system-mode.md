# Room Climate Delegated System Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each room Climate entity control only room-panel power and temperature while the system mode select owns cooling/heating/ventilation/dehumidification.

**Architecture:** Keep the existing `RoomThermostat` entity and entity IDs. Replace its dynamic cooling/heating capability list with a stable `[off, heat_cool]` list, map the live room state to that stable capability, and retain the existing `LinkingTempHub` command queue and `SystemModeSelect` unchanged.

**Tech Stack:** Python 3.14, Home Assistant 2026.11 Climate API, pytest, the repository's native integration test harness.

**Spec:** `docs/superpowers/specs/2026-11-11-room-climate-delegated-system-mode-design.md`

## Global Constraints

- Keep one native room Climate entity; do not add a second Climate entity.
- `RoomThermostat.hvac_modes` must always be `[HVACMode.OFF, HVACMode.HEAT_COOL]`.
- The system mode select remains the only entity that changes cooling, heating, ventilation, and dehumidification.
- Preserve room discovery, temperature bounds, half-degree normalization, command confirmation, and shared TCP state.

## Review Focus

- A room panel is on in cooling or heating: Climate reports `heat_cool` and does not send a mode command.
- A room panel is off in a valid temperature mode: Climate reports `off`, and turning it on sends only panel power.
- The system is in ventilation or dehumidification: Climate reports `off`, and an enable request is rejected.
- HA/HomeKit sends a stale direct `cool` or `heat` request: it is rejected without changing the system mode.
- The controller has not reported a usable system state: HA marks Climate unavailable, its internal mode remains `off`, and it cannot be enabled.

### Task 1: Pin the delegated-mode contract with tests

**Files:**
- Modify: `tests/native/test_entities.py`
- Modify: `tests/native/test_climate_temperature_api.py` if direct Climate mode assertions need coverage

**Interfaces:**
- Consumes the existing `RoomThermostat` and `SystemModeSelect` entities.
- Produces regression tests for stable mode exposure, live state mapping, and command rejection.

- [x] **Step 1: Add/update failing tests** for `hvac_modes == ["off", "heat_cool"]`, live `hvac_mode`, power-only enable/disable, and rejection of direct `cool`/`heat`/unsupported mode requests.
- [x] **Step 2: Run the focused tests** and confirm they fail because production code still exposes `cool` and `heat`.
- [x] **Step 3: Update expected mode assertions** in existing room thermostat tests while preserving all command and state assertions.
- [x] **Step 4: Run the focused tests** and confirm the intended failures remain limited to the production mode contract.

### Task 2: Implement stable room Climate mode semantics

**Files:**
- Modify: `custom_components/linking_the_world_temp_ha/climate.py`

**Interfaces:**
- Consumes `can_operate_room_thermostat(system_power, system_mode)`.
- Keeps `RoomThermostat.async_set_temperature` and the existing hub power commands unchanged.

- [x] **Step 1: Change `hvac_modes`** to return `[HVACMode.OFF, HVACMode.HEAT_COOL]` for every room and every controller mode.
- [x] **Step 2: Map `hvac_mode`** to `HEAT_COOL` only when the room panel is on and the system policy permits operation; otherwise return `OFF`.
- [x] **Step 3: Make `async_set_hvac_mode`** accept only `OFF` and `HEAT_COOL`; reject all other direct mode requests and preserve the existing policy error for enabling outside cooling/heating.
- [x] **Step 4: Run focused tests** and confirm all delegated-mode tests pass.

### Task 3: Verify the integration and documentation surface

**Files:**
- Review: `custom_components/linking_the_world_temp_ha/strings.json`
- Review: `README.md`, `TROUBLESHOOTING.md`, and `CHANGELOG.md`

**Interfaces:**
- Uses the final Climate state semantics from Task 2.
- Produces no new entity IDs or configuration fields.

- [x] **Step 1: Update user-facing documentation** only where it currently promises room Climate mode selection; describe the system mode select as the mode control.
- [x] **Step 2: Run the complete native test suite, lint, mypy, and metadata checks.**
- [x] **Step 3: Inspect the diff and verify no second Climate entity, entity-ID churn, or unrelated behavior change was introduced.
- [x] **Step 4: Commit the implementation** with a focused conventional commit.
