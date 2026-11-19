from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pump_control import model

TZ = ZoneInfo("Australia/Brisbane")


def ms(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp() * 1000


def test_parse_message_path():
    assert model.parse_message_path("$io_state.pump_1") == ("io_state", ["pump_1"])
    assert model.parse_message_path("$io_state:a.b") == ("io_state", ["a", "b"])
    assert model.parse_message_path("") is None
    assert model.parse_message_path(None) is None
    with pytest.raises(ValueError):
        model.parse_message_path("io_state.pump_1")
    with pytest.raises(ValueError):
        model.parse_message_path("$io_state")


def test_extract_and_as_bool():
    data = {"pumps": [{"run": 1}], "x": None}
    assert model.extract_path(data, ["pumps", "0", "run"]) == 1
    assert model.is_missing(model.extract_path(data, ["nope"]))
    assert model.extract_path(data, ["x"]) is None
    assert model.as_bool(1) is True
    assert model.as_bool(0.0) is False
    assert model.as_bool("Running") is True
    assert model.as_bool("off") is False
    assert model.as_bool("2") is True
    assert model.as_bool("garbage") is None
    assert model.as_bool(None) is None


def test_slug_and_tag_key():
    assert model.slug("pump 1") == "pump_1"
    assert model.slug("") == "pump"
    assert model.tag_key("eastern_pump", "running") == "p_eastern_pump_running"


def test_observe_counts_run_hours_and_starts():
    stats = {}
    t0 = ms(2026, 11, 19, 8)
    stats = model.observe(stats, False, t0, TZ)
    assert stats["running"] is False
    assert stats["starts_today"] == 0
    assert stats.get("last_stopped") is None  # unknown -> stopped is not a stop

    stats = model.observe(stats, True, t0 + 60_000, TZ)
    assert stats["starts_today"] == 1
    assert stats["last_started"] == int(t0 + 60_000)

    # a schedule tick with nothing new: keeps accruing
    stats = model.observe(stats, None, t0 + 60_000 + 1_800_000, TZ)
    assert stats["hours_today"] == pytest.approx(0.5)

    stats = model.observe(stats, False, t0 + 60_000 + 3_600_000, TZ)
    assert stats["hours_today"] == pytest.approx(1.0)
    assert stats["hours_total"] == pytest.approx(1.0)
    assert stats["last_stopped"] == int(t0 + 60_000 + 3_600_000)

    # stopped: no accrual
    stats = model.observe(stats, None, t0 + 7_200_000, TZ)
    assert stats["hours_total"] == pytest.approx(1.0)


def test_observe_splits_at_local_midnight():
    stats = model.observe({}, True, ms(2026, 11, 19, 23), TZ)
    stats = model.observe(stats, None, ms(2026, 11, 20, 1), TZ)
    assert stats["hours_total"] == pytest.approx(2.0)
    assert stats["hours_today"] == pytest.approx(1.0)
    assert stats["starts_today"] == 0
    assert stats["day"] == "2026-11-20"


def test_observe_no_data_changes_nothing_but_day():
    stats = model.observe({}, None, ms(2026, 11, 19, 8), TZ)
    assert stats.get("running") is None
    assert stats["hours_total"] == 0
    assert "accrued_at" not in stats or stats["accrued_at"] is None


def test_start_duration():
    assert model.start_duration_s(model.MODE_TIMED, None, 60, 720) == 3600
    assert model.start_duration_s(model.MODE_TIMED, "10", 60, 720) == 600
    assert model.start_duration_s(model.MODE_LATCHED, 30, 60, 720) is None
    with pytest.raises(ValueError):
        model.start_duration_s(model.MODE_TIMED, 800, 60, 720)
    with pytest.raises(ValueError):
        model.start_duration_s(model.MODE_TIMED, 0, 60, 720)


def test_build_command_shape():
    cmd = model.build_command(
        "start", "pump_1", 600, "pump_control_1", 1_795_050_000_000
    )
    assert set(cmd) == {
        "command",
        "target",
        "duration_s",
        "params",
        "request_id",
        "app_key",
        "app",
        "ts",
    }
    assert cmd["app"] == "pump_control"
    assert cmd["duration_s"] == 600
    assert len(cmd["request_id"]) == 36


def test_evaluate_pending():
    sent = ms(2026, 11, 19, 8)
    pending = {
        "command": "start",
        "request_id": "r",
        "sent_at": sent,
        "duration_s": 600,
    }
    after, text, warn = model.evaluate_pending(pending, False, sent + 10_000, 120, TZ)
    assert after == pending and not warn and "waiting" in text
    after, text, warn = model.evaluate_pending(pending, True, sent + 10_000, 120, TZ)
    assert after is None and not warn and "confirmed" in text
    after, text, warn = model.evaluate_pending(pending, None, sent + 200_000, 120, TZ)
    assert after is None and warn and "No confirmation" in text and "Unknown" in text
    assert model.evaluate_pending(None, True, sent, 120, TZ) == (None, None, False)


def test_validate_fill_settings():
    assert model.validate_fill_settings(60, 85, 10) is None
    assert "lower" in model.validate_fill_settings(90, 85, 10)
    assert model.validate_fill_settings(60, 101, 10)
    assert model.validate_fill_settings(60, 85, 0)
    assert model.validate_fill_settings(None, 85, 10)


def test_fill_state():
    assert model.fill_state(False, True) == model.FILL_DISABLED
    assert model.fill_state(True, None) == model.FILL_UNKNOWN
    assert model.fill_state(True, True) == model.FILL_FILLING
    assert model.fill_state(True, False) == model.FILL_IDLE


def _fill(track, now, running, level=None, enabled=True, started=None):
    return model.evaluate_fill(
        track,
        enabled=enabled,
        pump_running=running,
        pump_last_started=started,
        pump_last_stopped=None,
        level=level,
        stop_level=85,
        max_shift_min=10,
        now_ms=now,
    )


def test_evaluate_fill_no_data_never_notifies():
    track, due = _fill({}, 0, None, level=None)
    assert due == []
    track, due = _fill(track, 10_000_000, None, level=99)
    assert due == []


def test_evaluate_fill_shift_exceeded_once():
    t0 = 1_000_000
    track, due = _fill({}, t0, True, started=t0)
    assert track["fill_active"] and track["fill_started"] == t0 and due == []
    track, due = _fill(track, t0 + 11 * 60_000, True)
    assert due == []  # within grace
    track, due = _fill(track, t0 + 13 * 60_000, True)
    assert due == ["shift_exceeded"]
    track, due = _fill(track, t0 + 20 * 60_000, True)
    assert due == []
    track, due = _fill(track, t0 + 21 * 60_000, False)
    assert not track["fill_active"] and track["fill_ended"] == t0 + 21 * 60_000


def test_evaluate_fill_stop_level_needs_to_persist():
    t0 = 1_000_000
    track, due = _fill({}, t0, True, level=86)
    assert due == []
    track, due = _fill(track, t0 + 60_000, True, level=86)
    assert due == []
    track, due = _fill(track, t0 + 130_000, True, level=86)
    assert due == ["stop_level_pump_running"]
    track, due = _fill(track, t0 + 400_000, True, level=86)
    assert due == []
    track, due = _fill(track, t0 + 500_000, False, level=86)
    assert track["overfill_since"] is None and not track["overfill_notified"]


def test_settings_shapes():
    s = model.pump_settings(
        [
            {
                "target": "pump_1",
                "name": "Pump 1",
                "mode": "Timed",
                "default_run_min": 10,
                "max_run_min": 120,
            }
        ],
        "pump_control_1",
        "io_commands",
        5,
    )
    assert s["pumps"][0] == {
        "target": "pump_1",
        "name": "Pump 1",
        "mode": "timed",
        "default_run_s": 600,
        "max_run_s": 7200,
    }
    assert s["app"] == "pump_control" and s["schema"] == 1
    a = model.auto_fill_settings(
        app_key="pump_control_1",
        enabled=True,
        start_level=60,
        stop_level=85,
        max_shift_min=10,
        level_app_key="analog_level_sensor_processor_1",
        level_tag="level_filled_percentage",
        pump_device_id=248969700854236933,
        pump_app_key="pump_control_1",
        pump_target="pump_1",
        updated_by="operator",
        now_ms=5,
    )
    assert a["max_shift_s"] == 600
    assert a["pump"] == {
        "device_id": "248969700854236933",
        "app_key": "pump_control_1",
        "target": "pump_1",
    }
