"""Pure logic for Pump Control: no Doover API calls, so it can be tested alone.

The cloud app is a remote control. The edge controller owns the pumps: it runs
them, times them, runs any auto-fill logic locally and reports what they are
actually doing. Everything here works from that reported state.
"""

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
