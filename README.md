
# Pump Control

<img src="https://doover.com/wp-content/uploads/Doover-Logo-Landscape-Navy-padded-small.png" alt="App Icon" style="max-width: 300px;">

**Start, stop and monitor pumps from anywhere, and set the auto-fill levels of the tanks they fill.**

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/getdoover/pump-control/blob/main/LICENSE)

[Overview](#overview) • [Roles](#roles) • [Readings](#readings) • [Configuration](#configuration) • [Input Addresses](#input-addresses) • [Tags](#tags) • [Channels](#channels)

<br/>

## Overview

Pump Control is a cloud processor that acts as the remote control for pumps run by an
edge controller: a Doovit app, a PLC behind a bridge, a radio or LoRaWAN node, or anything
else that reports pump states on a channel and accepts commands on another.

The edge controller is always the source of truth. It switches the pumps, times their
runs, runs any auto-fill logic locally and reports what each pump is actually doing. This
app sends it commands, hands it the settings it must enforce, and shows the reported state
— never an assumed one. After a command the page shows it as pending until the reported
state matches; if it doesn't within the confirm timeout, a warning says so.

Key capabilities:

- **Start / Stop per pump** -- Timed (run for a set number of minutes, capped by a
  maximum) or Latched (run until stopped)
- **Live state** -- Running / Stopped / Unknown from the pump's run-state input, plus an
  optional fault input
- **Run statistics** -- last started and stopped, time remaining on a timed run, run hours
  today and in total, and starts today, all worked out from the reported state
- **Readings** -- pressures, engine data or any other value on the pump's device, scaled
  from the raw signal, shown under the pumps with its fault or stale state, and published
  as tags for other apps (a pump analysis, a report)
- **Auto-fill** -- installed on a tank, the same app holds the fill settings (enable,
  start level, stop level, max shift) that operators change on the page, and shows whether
  the linked pump is filling
- **Notifications** -- a fill that runs past its max shift, and a level at Stop while the
  pump keeps running

<br/>

## Roles

The same app is installed on both ends of a fill.

### Pump

Install it on the device that has the pumps. Each configured pump gets its own section:

| Element | Shows |
|---------|-------|
| **State** | Running, Stopped or Unknown, from the Run State Input Path |
| **Fault** | A warning while the pump reports a fault |
| **Command** | The last command and where it stands: sent, accepted by the controller, confirmed, or not confirmed in time |
| **Start For (min)** / **Start** | A Timed start for the entered minutes (Latched pumps have Start only) |
| **Stop** | Stops the pump |
| **Time Remaining** | When a timed run sent from here is due to end |
| **Last Started / Last Stopped** | When the reported state last changed |
| **Run Hours Today / Total**, **Starts Today** | Counted from the reported state, in the configured timezone |

Only the control that changes the reported state is offered: a pump reporting
**Stopped** shows only **Start For (min)** / **Start**, one reporting **Running**
shows only **Stop** (with **Time Remaining** for a timed run). While the state is
Unknown (no reading yet), a command is waiting for confirmation, a fault is
reported, or a command was not confirmed or was rejected, both are shown: when the
reported state can't be trusted, an explicit Stop is the safe action to have to hand.

If **Auto-filled By** names a tank's device, an **Auto-Fill** line reads, e.g.,
"Controlled by North Tank: Enabled, Filling". It is read-only: the fill settings
belong to the tank.

### Auto-fill (storage)

Install it on the tank or storage that a pump fills. It owns the fill settings; the edge
controller runs the fill logic. On the page:

| Element | Shows / does |
|---------|--------------|
| **Auto-Fill** | Filling, Idle, Disabled or Unknown |
| **Auto-Fill Enabled** | Switches auto-fill on or off |
| **Current Level** | The level tag named in Level Source |
| **Start Below (%)**, **Stop At (%)**, **Max Shift (min)** | The fill settings; Start must be below Stop |
| **Linked Pump** / **Pump State** | The pump that fills this storage and its reported state |
| **Last Fill Started / Ended** | When the pump last started and stopped filling with auto-fill on |

Operator changes apply immediately, without a redeploy, and are kept across redeploys.
Changing the matching config values and redeploying resets them to the new config.

<br/>

## Readings

The Pump role can also show the telemetry that belongs with the pumps: suction and
discharge pressure, engine speed and hours, coolant temperature, fuel, and so on. Each
entry in **Readings** turns one raw input into a value:

- **Analog** maps the input range onto the value range, e.g. 4–20 mA onto 0–145 PSI. An
  input well outside its range -- below 2.5 % of the span under the minimum, or above
  6.25 % of the span over the maximum (3.6 mA and 21 mA on a 4–20 mA loop) -- is a
  **sensor fault**: 0 mA on a 4–20 mA loop is a dead loop, not a reading of −36 PSI.
  Inputs between those limits and the range ends are clamped to the value range.
- **Scaled** computes value = raw × Multiplier + Offset, for values that arrive already in
  engineering units or need only a factor (Modbus registers, counters, voltages).

Every enabled reading has a line in the **Readings** section under the pumps, with its
value and units. A reading is never dropped for what it reads: zero is a value, a faulted
reading shows its fault, and one with no new value for **Reading Stale After** shows its
age and keeps its last value. Below each value, a warning appears while the reading is
faulted, stale or outside its optional low / high warning. The section's status sums it
up, e.g. "2 sensor faults, 1 stale". To leave a reading off a device where it doesn't
exist, switch its **Enabled** off in config.

<br/>

## Configuration

| Setting | Key | Description | Default |
|---------|-----|-------------|---------|
| **Role** | `role` | `Pump` or `Auto-fill (storage)` | `Pump` |
| **Pumps** | `pumps` | Pump role: the pumps on this device (below) | `[]` |
| **Auto-filled By** | `auto_fill_device` | Pump role, optional: the tank device whose Auto-fill install controls these pumps | none |
| **Auto-fill App Key** | `auto_fill_app_key` | Pump role: that install's app key, e.g. `pump_control_1` | none |
| **Level Source** | `level_source` | Auto-fill role: `{app_name, tag_name}` of the level tag (%) on this device | `analog_level_sensor_processor_1` / `level_filled_percentage` |
| **Pump Device** | `pump_device` | Auto-fill role: the device with the filling pump | none |
| **Pump App Key** | `pump_app_key` | Auto-fill role: the Pump Control install's app key on that device | none |
| **Pump Target ID** | `pump_target` | Auto-fill role: the pump's Target ID in that install | `pump_1` |
| **Start Level (%)** | `start_level` | Auto-fill role: default Start Below | `60.0` |
| **Stop Level (%)** | `stop_level` | Auto-fill role: default Stop At | `90.0` |
| **Max Shift (min)** | `max_shift_min` | Auto-fill role: default Max Shift | `60.0` |
| **Enabled by Default** | `auto_fill_enabled` | Auto-fill role: whether auto-fill starts enabled | `true` |
| **Readings** | `readings` | Pump role, optional: telemetry to scale, show and publish (below) | `[]` |
| **Reading Stale After (min)** | `reading_stale_after_min` | Pump role: how long without a new value before a reading shows as stale | `120` |
| **Command Channel** | `command_channel` | Where commands go | `io_commands` |
| **Settings Channel** | `settings_channel` | Whose aggregate carries the settings | `control_settings` |
| **Confirm Timeout (s)** | `confirm_timeout_s` | How long a command may wait for the reported state | `120` |
| **Timezone** | `timezone` | IANA timezone for "today" | `Australia/Brisbane` |
| **Subscription** | `dv_proc_subscriptions` | Channels that invoke the app | `io_state`, `ui_cmds`, `io_commands` |
| **Schedule** | `dv_proc_schedules` | Periodic refresh of run hours, timeouts and the linked device | `rate(5 minutes)` |
| **Devices** | `dv_proc_extended_permissions` | Other devices this app may read: add the linked tank or pump device here | none |
| **Position** | `dv_app_position` | Hidden. Where the app sits on the device page; smaller is higher | `20` |
| **Default Open** | `dv_app_default_open` | Hidden. Whether the app's panel starts expanded | `true` |

Each entry in **Pumps**:

| Setting | Key | Description | Default |
|---------|-----|-------------|---------|
| **Name** | `name` | What operators call the pump | `Pump 1` |
| **Target ID** | `target` | The pump's output id on the edge controller | `pump_1` |
| **Run State Input Path** | `run_state_path` | The run state, truthy while running. Blank uses the automatic address | blank |
| **Fault Input Path** | `fault_path` | The fault input, truthy in fault. Blank uses the automatic address | blank |
| **Default Run Duration (min)** | `default_run_min` | Run time a Timed start offers | `60.0` |
| **Max Run Duration (min)** | `max_run_min` | Longest run time allowed | `720.0` |
| **Mode** | `mode` | `Timed` or `Latched` | `Timed` |

Each entry in **Readings**:

| Setting | Key | Description | Default |
|---------|-----|-------------|---------|
| **Name** | `name` | What operators call the reading, e.g. `Suction Pressure` | `Reading 1` |
| **Key** | `key` | Id used in the reading's tags; made from the name when blank | blank |
| **Enabled** | `enabled` | Off leaves the reading out entirely; on, it is always shown | `true` |
| **Input Message Path** | `input_path` | The raw value. Blank uses the automatic address | blank |
| **Input Type** | `input_type` | `Analog` or `Scaled` | `Analog` |
| **Input Min / Input Max** | `input_min`, `input_max` | Analog: the raw input range | `4`, `20` |
| **Value Min / Value Max** | `value_min`, `value_max` | Analog: the values at the ends of the input range | `0`, `100` |
| **Multiplier / Offset** | `multiplier`, `offset` | Scaled: value = raw × Multiplier + Offset | `1`, `0` |
| **Units** | `units` | Shown after the value | blank |
| **Decimal Precision** | `precision` | Decimal places shown | `1` |
| **Low Warning / High Warning** | `warn_low`, `warn_high` | Optional limits that raise a warning on the page | none |

Leave an input path blank and the app reads it from its automatic address (see
[Input Addresses](#input-addresses)). To read it from somewhere else, give a path of the
form `$channel.path.to.value` (or `$channel:path.to.value`), read from each message on a
subscribed channel; keep every channel used in a path in **Subscription**. A value is
truthy when it is `true`, a non-zero number, or a string such as `on`, `running` or `1`.

To link a pump and a tank, install the Pump role on the pump's device first, then the
Auto-fill role on the tank with **Pump Device**, **Pump App Key** and **Pump Target ID**
pointing at it. Add each side's device under the other's **Devices** so it can read it.

<br/>

## Input Addresses

Every input has a fixed point id, and with its path left blank the app reads it from

```
$io_state.<app key>.<point id>        e.g.  $io_state.pump_control_1.pump_1
```

so the edge bridge only has to publish each value under this install's app key, and the
inputs are wired up in one place: the bridge's map. The point ids are:

| Input | Point id |
|-------|----------|
| A pump's run state | its Target ID, e.g. `pump_1` (the same id commands use) |
| A pump's fault | its Target ID + `_fault`, e.g. `pump_1_fault` |
| A reading | its Key (made from the name when blank), e.g. `suction_pressure` |

A pump's fault on its automatic address shows nothing until a value first arrives;
from then on it is shown, like any other input. An explicit path always wins over the
automatic address.

The app lists its inputs in the `io_points` tag, so a bridge can offer them by name:

```json
{"version": 1, "points": [
  {"id": "pump_1", "label": "Pump 1", "kind": "output",
   "path": "$io_state.pump_control_1.pump_1", "auto": true},
  {"id": "pump_1_fault", "label": "Pump 1 fault", "kind": "input",
   "path": "$io_state.pump_control_1.pump_1_fault", "auto": true},
  {"id": "suction_pressure", "label": "Suction Pressure", "kind": "input",
   "path": "$io_state.pump_control_1.suction_pressure", "auto": true}
]}
```

`kind` is `output` for a pump (commands carry it as `target`, and its run state arrives
on `path`) and `input` for anything read-only. `path` is the path the app reads and `auto`
whether it is the automatic address. Pumps come first, then faults and readings, in
config order; a reading switched off is left out. The Auto-fill role reads no inputs and
publishes an empty list.

<br/>

## Tags

Pump role, one set per pump, keyed by its Target ID (`p_<target>_<field>`):

| Tag | Meaning |
|-----|---------|
| `p_<target>_running` | Reported run state (`true` / `false`, null until reported) |
| `p_<target>_state` | `Running`, `Stopped` or `Unknown` |
| `p_<target>_fault` | Reported fault state |
| `p_<target>_fault_seen` | `true` once a fault value has ever arrived |
| `p_<target>_name` | The pump's name |
| `p_<target>_last_started`, `p_<target>_last_stopped` | Unix ms |
| `p_<target>_ends_at` | Unix ms a timed run sent from here is due to end |
| `p_<target>_hours_today`, `p_<target>_hours_total`, `p_<target>_starts_today` | Run statistics |
| `p_<target>_pending` | The command awaiting confirmation |

Readings, one set per enabled reading, keyed by its Key (`r_<key>_<field>`):

| Tag | Meaning |
|-----|---------|
| `r_<key>` | The scaled value; null until a reading arrives and while the sensor is faulted |
| `r_<key>_raw` | The last raw input, as received |
| `r_<key>_fault` | `true` while the input is a sensor fault (under / over range or unreadable) |
| `r_<key>_ts` | Unix ms the last raw input arrived |
| `r_<key>_status` | `OK`, `Low`, `High`, `Sensor fault`, `Stale` or `No reading yet` |
| `r_<key>_name` | The reading's name |

`io_points` lists the inputs (see [Input Addresses](#input-addresses)).

Another app on the device consumes a reading through `{app_name, tag_name}`, e.g. a pump
analysis taking suction pressure from `pump_control_1` / `r_pressure_1`. Because a faulted
reading publishes null rather than a scaled dead-loop value, consumers never compute from
a disconnected sensor.

Auto-fill role:

| Tag | Meaning |
|-----|---------|
| `af_enabled`, `af_start`, `af_stop`, `af_max_shift_min` | The settings in force |
| `af_state` | `Filling`, `Idle`, `Disabled` or `Unknown` |
| `af_pump_target`, `af_pump_name`, `af_pump_running`, `af_pump_state` | The linked pump |
| `af_fill_started`, `af_fill_ended` | Unix ms |

<br/>

## Channels

### Inputs

Raw run-state, fault and reading values arrive as messages on `io_state`, under this
install's app key (or on the channels in any explicit input paths), and buttons on `ui_cmds`. The Auto-fill role reads its level from
another app's tag on the same device, and the linked pump's tags from the pump device.

### Commands

One message per command on the **Command Channel**:

```json
{"command": "start", "target": "pump_1", "duration_s": 600, "params": {},
 "request_id": "<uuid4>", "app_key": "pump_control_1", "app": "pump_control",
 "ts": 1795050000000}
```

`command` is `start` or `stop`; `duration_s` is null for a Latched start and for a stop.
The controller may answer on the same channel with
`{"request_id": "<uuid4>", "ok": true|false, "error": "<reason>"}`; the page shows it when
it does, and works without it.

### Settings

Written to the **Settings Channel** aggregate under the app's key on every deployment and
every operator change, leaving other apps' keys alone.

Pump role:

```json
{"pump_control_1": {"app": "pump_control", "schema": 1, "role": "pump",
  "app_key": "pump_control_1", "command_channel": "io_commands",
  "pumps": [{"target": "pump_1", "name": "Pump 1", "mode": "timed",
             "default_run_s": 600, "max_run_s": 7200}],
  "updated_at": 1795050000000}}
```

Auto-fill role:

```json
{"pump_control_1": {"app": "pump_control", "schema": 1, "role": "auto_fill",
  "app_key": "pump_control_1", "enabled": true,
  "start_level_pct": 60.0, "stop_level_pct": 85.0, "max_shift_s": 600,
  "level": {"app_key": "analog_level_sensor_processor_1", "tag": "level_filled_percentage"},
  "pump": {"device_id": "123456789012345678", "app_key": "pump_control_1", "target": "pump_1"},
  "updated_by": "operator", "updated_at": 1795050000000}}
```

`updated_by` is `config` (a deployment) or `operator` (a change on the page).

<br/>

## Notifications

| Notification | When |
|--------------|------|
| **Shift exceeded** | Auto-fill is on and the linked pump has been running longer than Max Shift (plus two minutes) |
| **Stop level pump running** | The level has been at or above Stop for two minutes with the pump still running |

Both need a reported pump state, so neither fires before data arrives.
