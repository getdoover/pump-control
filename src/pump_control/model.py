"""Pure logic for Pump Control: no Doover API calls, so it can be tested alone.

The cloud app is a remote control. The edge controller owns the pumps: it runs
them, times them, runs any auto-fill logic locally and reports what they are
actually doing. Everything here works from that reported state.
"""

import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP_NAME = "pump_control"
SETTINGS_SCHEMA = 1

ROLE_PUMP = "Pump"
ROLE_AUTO_FILL = "Auto-fill (storage)"

MODE_TIMED = "Timed"
MODE_LATCHED = "Latched"

STATE_RUNNING = "Running"
STATE_STOPPED = "Stopped"
STATE_UNKNOWN = "Unknown"

FILL_FILLING = "Filling"
FILL_IDLE = "Idle"
FILL_DISABLED = "Disabled"
FILL_UNKNOWN = "Unknown"

# A level at or above Stop with the pump still running has to last this long
# before it is reported: the edge stops the pump itself, and a check landing
# in the seconds between the level arriving and the pump stopping is not a fault.
OVERFILL_GRACE_S = 120
# Slack on Max Shift before a fill counts as having overrun it.
SHIFT_GRACE_S = 120

_TRUE_STRINGS = {"1", "true", "on", "yes", "running", "run", "started"}
_FALSE_STRINGS = {"0", "false", "off", "no", "stopped", "stop", "idle"}


# -- message paths ------------------------------------------------------------


def parse_message_path(value: Any) -> tuple[str, list[str]] | None:
    """Split '$channel.path.to.value' (or '$channel:path.to.value').

    Returns None for an empty path (an optional input left blank); raises
    ValueError for one that is set but malformed.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("A message path must be a string like '$channel.key'.")
    path = value.strip()
    if not path:
        return None
    if not path.startswith("$"):
        raise ValueError(f"Message path {path!r} must start with '$channel'.")

    body = path[1:]
    if ":" in body:
        channel, raw = body.split(":", 1)
    elif "." in body:
        channel, raw = body.split(".", 1)
    else:
        raise ValueError(f"Message path {path!r} needs a value path after the channel.")

    channel = channel.strip()
    parts = [p.strip() for p in raw.split(".") if p.strip()]
    if not channel or not parts:
        raise ValueError(
            f"Message path {path!r} needs both a channel and a value path."
        )
    return channel, parts


_MISSING = object()


def extract_path(data: Any, parts: list[str]) -> Any:
    """The value at ``parts`` in ``data``; ``_MISSING`` when it isn't there."""
    value = data
    for part in parts:
        if isinstance(value, dict):
            if part not in value:
                return _MISSING
            value = value[part]
        elif isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                return _MISSING
        else:
            return _MISSING
    return value


def is_missing(value: Any) -> bool:
    return value is _MISSING


# -- automatic input addresses ----------------------------------------------------

# A blank input path reads from the input's automatic address,
# $io_state.<app_key>.<point id>, which the edge bridge fills in.
AUTO_CHANNEL = "io_state"
IO_POINTS_VERSION = 1


def resolve_path(
    value: Any, app_key: str | None, point_id: str
) -> tuple[tuple[str, list[str]] | None, bool]:
    """(path, auto) for an input: an explicit '$channel.path' wins; a blank one is
    the automatic address. Raises ValueError for a set but malformed path."""
    path = parse_message_path(value)
    if path is not None:
        return path, False
    if not app_key or not point_id:
        return None, False
    return (AUTO_CHANNEL, [str(app_key), str(point_id)]), True


def format_path(path: tuple[str, list[str]] | None) -> str | None:
    if path is None:
        return None
    return "$" + ".".join([path[0], *path[1]])


def fault_point(target: str) -> str:
    return f"{target}_fault"


def io_points(pumps: list[dict], readings: list[dict]) -> dict:
    """The io_points tag: every input this install reads, outputs first.

    ``pumps`` and ``readings`` are the parsed config entries (empty in the
    Auto-fill role). Inputs with no usable path are left out, as is a later
    duplicate id.
    """
    outputs: list[dict] = []
    inputs: list[dict] = []
    for pump in pumps:
        outputs.append(
            _point(
                pump["target"],
                pump["name"],
                "output",
                pump["state_path"],
                pump.get("state_auto"),
            )
        )
        inputs.append(
            _point(
                fault_point(pump["target"]),
                f"{pump['name']} fault",
                "input",
                pump["fault_path"],
                pump.get("fault_auto"),
            )
        )
    for reading in readings:
        inputs.append(
            _point(
                reading["key"],
                reading["name"],
                "input",
                reading["path"],
                reading.get("auto"),
            )
        )
    points: list[dict] = []
    seen: set[str] = set()
    for point in outputs + inputs:
        if point["path"] is None or point["id"] in seen:
            continue
        seen.add(point["id"])
        points.append(point)
    return {"version": IO_POINTS_VERSION, "points": points}


def _point(point_id: str, label: str, kind: str, path, auto) -> dict:
    return {
        "id": point_id,
        "label": label,
        "kind": kind,
        "path": format_path(path),
        "auto": bool(auto),
    }


def as_bool(value: Any) -> bool | None:
    """A raw run / fault input as a bool. Truthy means on; None if unreadable."""
    if value is None or value is _MISSING:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
        try:
            return float(text) != 0
        except ValueError:
            return None
    return None


# -- names --------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^A-Za-z0-9_]+")


def slug(target: str) -> str:
    """A target id made safe for tag and UI element names."""
    s = _SLUG_RE.sub("_", str(target or "").strip()).strip("_")
    return s or "pump"


def tag_key(target: str, field: str) -> str:
    return f"p_{slug(target)}_{field}"


def get_zone(name: str | None):
    try:
        return ZoneInfo(name) if name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def local_day(ts_ms: float, tz) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz).date().isoformat()


def _local_midnight_ms(ts_ms: float, tz) -> float:
    local = datetime.fromtimestamp(ts_ms / 1000, tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp() * 1000


def state_text(running: bool | None) -> str:
    if running is True:
        return STATE_RUNNING
    if running is False:
        return STATE_STOPPED
    return STATE_UNKNOWN


def clock(ts_ms: float, tz) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz).strftime("%H:%M")


# -- run statistics -------------------------------------------------------------


def observe(stats: dict, reported: bool | None, now_ms: float, tz) -> dict:
    """Fold one observation of a pump's reported state into its statistics.

    ``stats`` holds: running (bool | None), accrued_at (ms), day (local date),
    hours_today, hours_total, starts_today, last_started, last_stopped.
    ``reported`` None means "nothing new" (a schedule tick): the last reported
    state stands and run time keeps accruing while it says running.

    Returns a new dict; the input is not modified.
    """
    out = dict(stats)
    prev = out.get("running")
    accrued_at = out.get("accrued_at")
    hours_today = float(out.get("hours_today") or 0.0)
    hours_total = float(out.get("hours_total") or 0.0)
    starts_today = int(out.get("starts_today") or 0)

    today = local_day(now_ms, tz)
    new_day = out.get("day") not in (None, today)

    if prev is True and accrued_at is not None and now_ms > accrued_at:
        run_h = (now_ms - accrued_at) / 3_600_000
        hours_total += run_h
        if new_day:
            # Only the part since local midnight belongs to today.
            midnight = _local_midnight_ms(now_ms, tz)
            hours_today = max(0.0, now_ms - max(midnight, accrued_at)) / 3_600_000
        else:
            hours_today += run_h
    elif new_day:
        hours_today = 0.0

    if new_day:
        starts_today = 0

    if reported is not None and reported != prev:
        if reported:
            starts_today += 1
            out["last_started"] = int(now_ms)
        elif prev is True:
            out["last_stopped"] = int(now_ms)
        out["running"] = reported

    # Only a running pump needs the accrual point moved: leaving it alone while
    # stopped keeps an idle pump's tags (and their history) still.
    if out.get("running") is True:
        out["accrued_at"] = int(now_ms)
    out["day"] = today
    out["hours_today"] = round(hours_today, 4)
    out["hours_total"] = round(hours_total, 4)
    out["starts_today"] = starts_today
    return out


# -- commands -------------------------------------------------------------------


def build_command(
    verb: str,
    target: str,
    duration_s: int | None,
    app_key: str,
    now_ms: float,
    request_id: str | None = None,
) -> dict:
    """One message for the Command Channel (the shared command contract)."""
    return {
        "command": verb,
        "target": target,
        "duration_s": duration_s,
        "params": {},
        "request_id": request_id or str(uuid.uuid4()),
        "app_key": app_key,
        "app": APP_NAME,
        "ts": int(now_ms),
    }


def start_duration_s(
    mode: str, minutes: Any, default_min: float, max_min: float
) -> int | None:
    """The duration a Start carries: None when latched.

    Raises ValueError (with an operator-facing message) for an out-of-range
    run time.
    """
    if mode == MODE_LATCHED:
        return None
    try:
        value = float(default_min if minutes is None or minutes == "" else minutes)
    except (TypeError, ValueError):
        raise ValueError("Run time must be a number of minutes.")
    if value <= 0:
        raise ValueError("Run time must be more than 0 minutes.")
    if max_min and value > float(max_min):
        raise ValueError(f"Run time can be at most {float(max_min):g} minutes.")
    return int(round(value * 60))


def command_label(verb: str, duration_s: int | None) -> str:
    if verb == "start":
        if duration_s:
            return f"Start ({duration_s / 60:g} min)"
        return "Start"
    return "Stop"


def evaluate_pending(
    pending: dict | None, running: bool | None, now_ms: float, timeout_s: float, tz
) -> tuple[dict | None, str | None, bool]:
    """Where a sent command stands against the reported state.

    Returns (pending after this check, status text or None for "unchanged",
    whether to show the no-confirmation warning).
    """
    if not pending:
        return None, None, False

    verb = pending.get("command")
    expect = verb == "start"
    label = command_label(verb, pending.get("duration_s"))
    sent_at = float(pending.get("sent_at") or now_ms)

    if running is expect:
        return None, f"{label} confirmed at {clock(now_ms, tz)}", False

    if now_ms - sent_at > timeout_s * 1000:
        text = (
            f"No confirmation: {label} sent at {clock(sent_at, tz)}, "
            f"pump still reports {state_text(running)}"
        )
        return None, text, True

    want = STATE_RUNNING if expect else STATE_STOPPED
    ack = pending.get("ack")
    prefix = f"{label} accepted by the controller" if ack else f"{label} sent"
    return pending, f"{prefix}, waiting for the pump to report {want}", False


def controls(
    running: bool | None, pending: dict | None, fault: bool | None, problem: bool
) -> tuple[bool, bool]:
    """(show Start, show Stop): offer the control that changes the reported state.

    Stopped shows only Start; Running shows only Stop. When the reported state
    can't be trusted -- no reading yet, a command awaiting confirmation, a fault
    reported, or a command not confirmed or rejected -- both are shown, because
    then an explicit Stop is the safe thing to have to hand.
    """
    if running is None or pending or fault or problem:
        return True, True
    return (not running, bool(running))


# -- auto-fill ------------------------------------------------------------------


def validate_fill_settings(start: Any, stop: Any, max_shift: Any) -> str | None:
    """An operator-facing reason the settings can't be used, or None."""
    try:
        start = float(start)
        stop = float(stop)
        max_shift = float(max_shift)
    except (TypeError, ValueError):
        return "Start, Stop and Max Shift must all be numbers."
    if not 0 <= start <= 100 or not 0 <= stop <= 100:
        return "Start and Stop levels must be between 0 and 100 %."
    if start >= stop:
        return f"Start below ({start:g} %) must be lower than Stop at ({stop:g} %)."
    if max_shift <= 0:
        return "Max shift must be more than 0 minutes."
    return None


def fill_state(enabled: bool | None, pump_running: bool | None) -> str:
    if enabled is False:
        return FILL_DISABLED
    if enabled is None or pump_running is None:
        return FILL_UNKNOWN
    return FILL_FILLING if pump_running else FILL_IDLE


def evaluate_fill(
    track: dict,
    *,
    enabled: bool,
    pump_running: bool | None,
    pump_last_started: float | None,
    pump_last_stopped: float | None,
    level: float | None,
    stop_level: float,
    max_shift_min: float,
    now_ms: float,
) -> tuple[dict, list[str]]:
    """Track fills and decide which notifications are due.

    ``track`` holds fill_active, fill_started, fill_ended, shift_notified,
    overfill_since, overfill_notified. Returns (new track, notifications to
    send) where the notifications are "shift_exceeded" and/or
    "stop_level_pump_running". Nothing is due without a reported pump state.
    """
    out = dict(track)
    due: list[str] = []
    active = bool(enabled and pump_running is True)
    was_active = bool(out.get("fill_active"))

    if active and not was_active:
        started = pump_last_started if pump_last_started else now_ms
        out["fill_started"] = int(started)
        out["shift_notified"] = False
    elif was_active and not active and pump_running is False:
        ended = pump_last_stopped if pump_last_stopped else now_ms
        out["fill_ended"] = int(ended)
    if pump_running is not None or not active:
        out["fill_active"] = active

    if active:
        started = out.get("fill_started") or now_ms
        limit_ms = (float(max_shift_min) * 60 + SHIFT_GRACE_S) * 1000
        if now_ms - started > limit_ms and not out.get("shift_notified"):
            due.append("shift_exceeded")
            out["shift_notified"] = True

    over = active and level is not None and float(level) >= float(stop_level)
    if over:
        since = out.get("overfill_since")
        if since is None:
            out["overfill_since"] = int(now_ms)
        elif now_ms - since >= OVERFILL_GRACE_S * 1000 and not out.get(
            "overfill_notified"
        ):
            due.append("stop_level_pump_running")
            out["overfill_notified"] = True
    else:
        out["overfill_since"] = None
        out["overfill_notified"] = False

    return out, due


# -- settings channel -------------------------------------------------------------


def pump_settings(
    pumps: list[dict], app_key: str, command_channel: str, now_ms: float
) -> dict:
    """The Pump role's settings for the edge to enforce."""
    return {
        "app": APP_NAME,
        "schema": SETTINGS_SCHEMA,
        "role": "pump",
        "app_key": app_key,
        "command_channel": command_channel,
        "pumps": [
            {
                "target": p["target"],
                "name": p["name"],
                "mode": p["mode"].lower(),
                "default_run_s": int(round(float(p["default_run_min"]) * 60)),
                "max_run_s": int(round(float(p["max_run_min"]) * 60)),
            }
            for p in pumps
        ],
        "updated_at": int(now_ms),
    }


def auto_fill_settings(
    *,
    app_key: str,
    enabled: bool,
    start_level: float,
    stop_level: float,
    max_shift_min: float,
    level_app_key: str | None,
    level_tag: str | None,
    pump_device_id: str | None,
    pump_app_key: str | None,
    pump_target: str | None,
    updated_by: str,
    now_ms: float,
) -> dict:
    """The Auto-fill role's settings for the edge's local fill logic."""
    return {
        "app": APP_NAME,
        "schema": SETTINGS_SCHEMA,
        "role": "auto_fill",
        "app_key": app_key,
        "enabled": bool(enabled),
        "start_level_pct": float(start_level),
        "stop_level_pct": float(stop_level),
        "max_shift_s": int(round(float(max_shift_min) * 60)),
        "level": {"app_key": level_app_key or None, "tag": level_tag or None},
        "pump": {
            "device_id": str(pump_device_id) if pump_device_id else None,
            "app_key": pump_app_key or None,
            "target": pump_target or None,
        },
        "updated_by": updated_by,
        "updated_at": int(now_ms),
    }


# -- readings -------------------------------------------------------------------
#
# Telemetry shown alongside the pumps (pressures, engine data, ...). A reading
# that exists on site is always shown: a faulted or stale one shows its fault
# or age, never disappears. Only its config Enabled switch removes it.

INPUT_ANALOG = "Analog"
INPUT_SCALED = "Scaled"

# How far outside the input range an Analog input may go before it counts as a
# sensor fault, as a fraction of the input span. For a 4-20 mA loop that is
# below 3.6 mA or above 21 mA (the usual NAMUR NE 43 limits); readings between
# those limits and the range ends are clamped to the value range.
UNDER_RANGE_FRACTION = 0.025
OVER_RANGE_FRACTION = 0.0625

READING_NO_DATA = "No reading yet"
READING_OK = "OK"
READING_LOW = "Low"
READING_HIGH = "High"
READING_FAULT = "Sensor fault"
READING_STALE = "Stale"


def reading_key(key: str) -> str:
    """A reading key made safe (and lower-case) for tag and UI element names."""
    return slug(key).lower() if str(key or "").strip() else ""


def reading_tag(key: str, field: str | None = None) -> str:
    k = reading_key(key)
    return f"r_{k}_{field}" if field else f"r_{k}"


def as_number(value: Any) -> float | None:
    """A raw input as a float: bools count as 0/1; None if unreadable."""
    if value is None or value is _MISSING:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return None if math.isnan(value) else float(value)  # NaN is unreadable
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return None if math.isnan(number) else number
    return None


def _fmt(value: float, precision: int = 2) -> str:
    return f"{value:.{max(0, int(precision))}f}"


def scale_reading(raw: Any, spec: dict) -> tuple[float | None, str | None]:
    """(scaled value, fault text) for one raw input.

    ``spec`` holds input_type and, for Analog, input_min / input_max /
    value_min / value_max; for Scaled, multiplier / offset. A fault returns no
    value: a dead loop must never pass for a real (if odd) reading.
    """
    number = as_number(raw)
    if number is None:
        return None, f"unreadable input {raw!r}"

    if spec.get("input_type") == INPUT_SCALED:
        mult = float(spec.get("multiplier", 1.0))
        offset = float(spec.get("offset", 0.0))
        return number * mult + offset, None

    in_min = float(spec.get("input_min", 4.0))
    in_max = float(spec.get("input_max", 20.0))
    v_min = float(spec.get("value_min", 0.0))
    v_max = float(spec.get("value_max", 100.0))
    span = in_max - in_min
    if span == 0:
        return None, "input range is empty (input min equals input max)"
    lo, hi = min(in_min, in_max), max(in_min, in_max)
    width = abs(span)
    if number < lo - UNDER_RANGE_FRACTION * width:
        return None, (f"input {_fmt(number)} is under the {lo:g}-{hi:g} input range")
    if number > hi + OVER_RANGE_FRACTION * width:
        return None, (f"input {_fmt(number)} is over the {lo:g}-{hi:g} input range")
    clamped = min(max(number, lo), hi)
    value = v_min + (clamped - in_min) / span * (v_max - v_min)
    return value, None


def format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "under a minute"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def format_value(value: float, spec: dict) -> str:
    text = _fmt(value, spec.get("precision", 1))
    units = (spec.get("units") or "").strip()
    return f"{text} {units}" if units else text


def evaluate_reading(
    spec: dict,
    raw: Any,
    ts_ms: float | None,
    now_ms: float,
    stale_after_s: float | None,
) -> dict:
    """Everything shown and published for one reading.

    ``raw`` is the last raw input (None when nothing has arrived) and ``ts_ms``
    when it arrived. Returns value (None on a fault), fault (bool), fault_text,
    level ("low" / "high" / None), stale (bool), age_s, status and warning
    (operator-facing, "" when there is nothing to say).
    """
    name = spec.get("name") or spec.get("key") or "Reading"
    out = {
        "value": None,
        "fault": False,
        "fault_text": None,
        "level": None,
        "stale": False,
        "age_s": None,
        "status": READING_NO_DATA,
        "warning": "",
    }
    if raw is None or raw is _MISSING:
        return out

    value, fault_text = scale_reading(raw, spec)
    out["value"] = value
    out["fault"] = fault_text is not None
    out["fault_text"] = fault_text

    if ts_ms is not None:
        age_s = max(0.0, (now_ms - float(ts_ms)) / 1000)
        out["age_s"] = age_s
        out["stale"] = bool(stale_after_s) and age_s > float(stale_after_s)

    warn_low = spec.get("warn_low")
    warn_high = spec.get("warn_high")
    if value is not None:
        if warn_low is not None and value < float(warn_low):
            out["level"] = "low"
        elif warn_high is not None and value > float(warn_high):
            out["level"] = "high"

    parts = []
    if out["fault"]:
        out["status"] = READING_FAULT
        parts.append(f"sensor fault, {fault_text}")
    elif out["level"] == "low":
        out["status"] = READING_LOW
        parts.append(
            f"{format_value(value, spec)} is below the low warning "
            f"({format_value(float(warn_low), spec)})"
        )
    elif out["level"] == "high":
        out["status"] = READING_HIGH
        parts.append(
            f"{format_value(value, spec)} is above the high warning "
            f"({format_value(float(warn_high), spec)})"
        )
    if out["stale"]:
        if not parts:
            out["status"] = READING_STALE
        parts.append(f"no new reading for {format_age(out['age_s'])}")
    elif out["status"] == READING_NO_DATA:
        out["status"] = READING_OK

    if parts:
        out["warning"] = f"{name}: " + "; ".join(parts)
    return out


def readings_summary(statuses: list[str]) -> str:
    """The Readings section's status line."""
    if not statuses:
        return READING_NO_DATA
    faults = sum(1 for s in statuses if s == READING_FAULT)
    stale = sum(1 for s in statuses if s == READING_STALE)
    alerts = sum(1 for s in statuses if s in (READING_LOW, READING_HIGH))
    waiting = sum(1 for s in statuses if s == READING_NO_DATA)
    parts = []
    if faults:
        parts.append(f"{faults} sensor fault{'s' if faults != 1 else ''}")
    if alerts:
        parts.append(f"{alerts} outside limits")
    if stale:
        parts.append(f"{stale} stale")
    if waiting == len(statuses):
        return READING_NO_DATA
    if waiting:
        parts.append(f"{waiting} waiting")
    return ", ".join(parts) if parts else READING_OK
