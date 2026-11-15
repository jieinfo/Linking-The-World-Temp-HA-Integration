# Independent Room Climate Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make each room Climate depend only on its own panel power and measurements, while preserving the standard `off`/`auto` Home Assistant and HomeKit representation.

**Architecture:** Keep the existing MC7021 command queue and confirmed-state model. Remove the room Climate and room-power dispatch guards that consult the system-wide operating mode, and stop notifying room Climate entities when only the system mode changes. Retain the one-time platform-setup notification so existing HomeKit accessories receive their initial capabilities after registration.

**Tech Stack:** Python 3.14, Home Assistant 2026.9+, pytest-homeassistant-custom-component, unittest, existing native integration test fixtures.

**Spec:** User-approved v1.5.0 baseline refactor described in the conversation: room Climate exposes only panel open/auto, off, temperature, humidity, and target temperature; the controller's actual report remains authoritative.

## Global Constraints

- Keep integration version metadata at `1.5.0` until a release version is explicitly chosen.
- Preserve entity IDs, unique IDs, room discovery, MC7021 command confirmation, 16–28 °C limits, and 1 °C HA step with existing HomeKit half-degree normalization.
- Keep the standard Home Assistant HVAC values `off` and `auto`; do not introduce a non-standard `open` mode.
- A panel power-on command may be sent regardless of the system-wide mode, but the final Climate state must follow the panel's actual host report.
- Do not promise that this removes all HomeKit/HAP transport failures; this change only removes project-level mode coupling and unnecessary Climate notifications.

## Review Focus

- Panel power state changes in cooling, heating, ventilation, dehumidify, and total-power-off states: the Climate state follows only the panel report.
- An enable request while the system mode is unknown or unsupported: the command is not blocked by the removed system-mode policy, but still requires normal host availability and confirmation.
- A system-mode-only update: no room Climate entity is written, preventing unnecessary HomeKit notifications.
- A panel power update: the corresponding room Climate is written and no unrelated room or sensor is refreshed.
- HomeKit mapping: the entity always advertises exactly `off` and `auto` and never emits a dynamic HVAC mode.

---

### Task 1: Lock the independent Climate behavior with failing tests

**Files:**
- Modify: `tests/native/test_entities.py`
- Modify: `tests/native/test_entity_refresh.py`
- Modify: `tests/native/test_thermostat_policy.py`

**Interfaces:**
- Consumes: existing `RoomThermostat`, `LinkingTempHub`, and fake-controller fixtures.
- Produces: regression tests proving system mode no longer controls room Climate state, enable dispatch, or entity refresh.

- [ ] **Step 1: Add failing assertions** for an enabled panel reporting `auto` in ventilation/dehumidify, enabling while those modes are active, and no Climate refresh on a system-mode-only status update.
- [ ] **Step 2: Run the focused tests and verify they fail because current production code still consults `can_operate_room_thermostat` and emits `room_controls`.
- [ ] **Step 3: Update/remove policy-specific test expectations so the obsolete policy is no longer a required interface.

### Task 2: Remove system-mode coupling from room Climate and panel power dispatch

**Files:**
- Modify: `custom_components/linking_the_world_temp_ha/climate.py`
- Modify: `custom_components/linking_the_world_temp_ha/hub.py`
- Modify: `custom_components/linking_the_world_temp_ha/__init__.py`
- Delete: `custom_components/linking_the_world_temp_ha/thermostat_policy.py`
- Delete: `tests/native/test_thermostat_policy.py`

**Interfaces:**
- Consumes: Task 1's failing behavior tests.
- Produces: `RoomThermostat.hvac_mode` based only on panel power, `async_set_hvac_mode(AUTO)` dispatch without a system-mode guard, and scoped notifications that ignore system-mode-only changes.

- [ ] **Step 1: Make the minimum production change:** remove `can_operate_room_thermostat` from Climate state and service handling; keep `hvac_modes == [OFF, AUTO]` and send only the existing panel power command.
- [ ] **Step 2: Remove `_room_thermostat_block_reason` and its dispatch guard; retain ordinary command availability, serialization, confirmation, and queue behavior.
- [ ] **Step 3: Stop adding `room_controls` for system power/mode changes and remove the now-unused policy module and setup-only callback plumbing, while preserving normal platform setup and panel discovery.
- [ ] **Step 4: Run Task 1 focused tests and verify they pass.

### Task 3: Update user-facing behavior documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/TROUBLESHOOTING.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 2's final behavior.
- Produces: documentation that says room Climate is panel-local and that the controller's confirmed report overrides a requested power state.

- [ ] **Step 1: Add an Unreleased changelog entry without changing manifest/README current release metadata.
- [ ] **Step 2: Replace statements that room Climate is forced off or blocked by ventilation/dehumidify mode with the independent-panel behavior and host-authoritative caveat.
- [ ] **Step 3: State that system operating mode remains a separate total-system control and is not emitted by room Climate.
- [ ] **Step 4: Run repository metadata and documentation contract tests.

### Task 4: Full verification and review

**Files:**
- Test: native integration suite, repository contract suite, static checks.

- [ ] **Step 1: Run Ruff and mypy.
- [ ] **Step 2: Run focused native tests and confirm the RED→GREEN behavior was observed.
- [ ] **Step 3: Run the full native suite with coverage and repository contract tests.
- [ ] **Step 4: Run `git diff --check`, inspect the final diff, and verify no v1.5.1 tag or release remains.

