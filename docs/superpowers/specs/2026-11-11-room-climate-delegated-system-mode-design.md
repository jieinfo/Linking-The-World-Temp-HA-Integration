# Room Climate Delegated System Mode

## Intent

Keep one native Home Assistant Climate entity per room and keep HomeKit's native
thermostat accessory, while making the system-wide mode selector the only place
where cooling, heating, ventilation, and dehumidification are changed.

## Goals

- Room Climate controls only panel power and target temperature.
- The system mode `select` remains the source of truth for the operating mode.
- Home Assistant and HomeKit receive a stable Climate mode capability so
  HomeKit does not lose its cached mode mapping after reconnects.
- Preserve the existing room-panel interlocks, command confirmation, and
  temperature behavior.

## Non-goals

- Do not add a second Climate entity.
- Do not change the system mode selector or its four options.
- Do not send a cooling/heating mode command from a room Climate entity.
- Do not change temperature limits, half-degree HomeKit normalization, or room
  discovery.

## Behavior

`RoomThermostat.hvac_modes` is always `[off, heat_cool]`.

- `off` means the room panel is off, the controller state is not usable, or
  the system mode is ventilation/dehumidification.
- `heat_cool` means the room panel is on and the current system mode allows
  room operation. It means “on; the system mode selector decides whether the
  controller is cooling or heating”, not an independent room mode.
- Before the controller has reported a usable state, Home Assistant marks the
  entity unavailable; the entity's internal HVAC mode remains `off` and it
  cannot be enabled.
- Setting `off` sends the existing room-panel power-off command.
- Setting `heat_cool` sends the existing room-panel power-on command only when
  the current system mode is cooling or heating; otherwise the operation is
  rejected by the existing room-panel policy.
- Direct requests for `cool`, `heat`, `fan_only`, or `dry` are rejected as
  unsupported rather than changing the system mode.

The system mode selector continues to control all four controller modes. In
Home Assistant, the room Climate card shows power/temperature controls and a
single stable non-off mode; the system mode selector shows the actual Chinese
operating mode. HomeKit keeps the native thermostat dial with a stable
off/heat-cool mapping, while the system mode selector remains a separate
control.

## Constraints

- Keep the existing entity IDs and the shared `LinkingTempHub` connection.
- Keep the HomeKit-facing Climate mode set stable across unavailable/reconnect
  transitions.
- Test both temperature modes and forced-off ventilation/dehumidification
  modes.
