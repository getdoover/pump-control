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


@pytest.mark.parametrize(
    "running, pending, fault, problem, expected",
    [
        (False, None, None, False, (True, False)),  # stopped: Start only
        (True, None, None, False, (False, True)),  # running: Stop only
        (None, None, None, False, (True, True)),  # no reading yet
        (False, {"command": "start"}, None, False, (True, True)),  # pending
        (True, {"command": "stop"}, False, False, (True, True)),  # pending
        (True, None, True, False, (True, True)),  # fault reported
        (False, None, None, True, (True, True)),  # not confirmed / rejected
    ],
)
def test_controls_offer_the_action_that_changes_the_state(
    running, pending, fault, problem, expected
):
    assert model.controls(running, pending, fault, problem) == expected


# -- readings -------------------------------------------------------------------

PSI = {
    "name": "Pressure 1",
    "key": "pressure_1",
    "input_type": model.INPUT_ANALOG,
    "input_min": 4.0,
    "input_max": 20.0,
    "value_min": 0.0,
    "value_max": 145.0,
    "units": "PSI",
    "precision": 1,
    "warn_low": None,
    "warn_high": None,
}
RPM = {
    "name": "Engine RPM",
    "key": "engine_rpm",
    "input_type": model.INPUT_SCALED,
    "multiplier": 1.0,
    "offset": 0.0,
    "units": "",
    "precision": 0,
}


def test_analog_scaling_matches_site_formula():
    for ma in (4.0, 8.0, 12.5, 20.0):
        value, fault = model.scale_reading(ma, PSI)
        assert fault is None
        assert value == pytest.approx((ma - 4) * 9.0625)


def test_analog_under_and_over_range_are_sensor_faults():
    value, fault = model.scale_reading(0.0, PSI)  # 0 mA: dead loop
    assert value is None and "under" in fault
    value, fault = model.scale_reading(3.59, PSI)
    assert value is None and "under" in fault
    value, fault = model.scale_reading(21.5, PSI)
    assert value is None and "over" in fault


def test_analog_slightly_outside_range_is_clamped_not_faulted():
    assert model.scale_reading(3.7, PSI) == (0.0, None)
    assert model.scale_reading(20.6, PSI) == (145.0, None)


def test_analog_inverted_and_empty_ranges():
    inverted = {**PSI, "input_min": 20.0, "input_max": 4.0}
    assert model.scale_reading(20.0, inverted)[0] == pytest.approx(0.0)
    assert model.scale_reading(4.0, inverted)[0] == pytest.approx(145.0)
    value, fault = model.scale_reading(12.0, {**PSI, "input_max": 4.0})
    assert value is None and "empty" in fault


def test_scaled_reading():
    assert model.scale_reading(1580, RPM) == (1580.0, None)
    spec = {**RPM, "multiplier": 0.1, "offset": -2}
    assert model.scale_reading("285", spec)[0] == pytest.approx(26.5)
    assert model.scale_reading(True, spec)[0] == pytest.approx(-1.9)


def test_unreadable_input_is_a_fault():
    value, fault = model.scale_reading("n/a", RPM)
    assert value is None and "unreadable" in fault


def test_evaluate_reading_no_data_is_quiet():
    out = model.evaluate_reading(PSI, None, None, 1_000, 3600)
    assert out["status"] == model.READING_NO_DATA
    assert out["warning"] == "" and out["fault"] is False and out["value"] is None


def test_evaluate_reading_fault_is_shown_not_dropped():
    out = model.evaluate_reading(PSI, 0.0, 0, 60_000, 3600)
    assert out["status"] == model.READING_FAULT
    assert out["fault"] is True and out["value"] is None
    assert out["warning"].startswith("Pressure 1: sensor fault")


def test_evaluate_reading_flat_zero_scaled_is_a_value():
    # Zero is a value, not a missing field.
    out = model.evaluate_reading(RPM, 0, 0, 60_000, 3600)
    assert out["status"] == model.READING_OK and out["value"] == 0.0
    assert out["warning"] == ""


def test_evaluate_reading_stale_keeps_its_value():
    hour = 3_600_000
    out = model.evaluate_reading(RPM, 1580, 0, 3 * hour, 7200)
    assert out["stale"] is True and out["value"] == 1580.0
    assert out["status"] == model.READING_STALE
    assert "no new reading for 3 h" in out["warning"]
    # faulted and stale: both said
    out = model.evaluate_reading(PSI, 0.0, 0, 3 * hour, 7200)
    assert out["status"] == model.READING_FAULT
    assert "sensor fault" in out["warning"] and "3 h" in out["warning"]


def test_evaluate_reading_limits():
    spec = {**PSI, "warn_low": 10.0, "warn_high": 100.0}
    low = model.evaluate_reading(spec, 4.5, 0, 0, 3600)
    assert (
        low["status"] == model.READING_LOW and "below the low warning" in low["warning"]
    )
    high = model.evaluate_reading(spec, 19.0, 0, 0, 3600)
    assert high["status"] == model.READING_HIGH
    assert "above the high warning (100.0 PSI)" in high["warning"]
    ok = model.evaluate_reading(spec, 10.0, 0, 0, 3600)
    assert ok["status"] == model.READING_OK


def test_reading_keys_and_summary():
    assert model.reading_key("Engine RPM (provisional)") == "engine_rpm_provisional"
    assert model.reading_tag("pressure_1", "raw") == "r_pressure_1_raw"
    assert model.readings_summary([]) == model.READING_NO_DATA
    assert model.readings_summary([model.READING_OK]) == model.READING_OK
    assert (
        model.readings_summary(
            [
                model.READING_FAULT,
                model.READING_FAULT,
                model.READING_OK,
                model.READING_STALE,
            ]
        )
        == "2 sensor faults, 1 stale"
    )
    assert model.format_age(90 * 60) == "1 h 30 min"
    assert model.format_age(26 * 3600) == "1 d 2 h"


def test_resolve_path_blank_is_automatic_explicit_wins():
    assert model.resolve_path("", "pump_control_2", "pump_1") == (
        ("io_state", ["pump_control_2", "pump_1"]),
        True,
    )
    assert model.resolve_path(None, "pump_control_2", "pump_1")[1] is True
    assert model.resolve_path("  ", "pump_control_2", "pump_1")[1] is True
    assert model.resolve_path("$io_state.pump_1", "pump_control_2", "pump_1") == (
        ("io_state", ["pump_1"]),
        False,
    )
    assert model.resolve_path("$on_dm_event.x", "k", "p") == (
        ("on_dm_event", ["x"]),
        False,
    )
    with pytest.raises(ValueError):
        model.resolve_path("io_state.pump_1", "k", "p")


def test_format_path_round_trips():
    path, _ = model.resolve_path("", "pump_control_1", "pressure_1")
    assert model.format_path(path) == "$io_state.pump_control_1.pressure_1"
    assert model.parse_message_path(model.format_path(path)) == path


def test_io_points_duplicate_ids_keep_the_first():
    pumps = [
        {
            "target": "a",
            "name": "A",
            "state_path": ("io_state", ["k", "a"]),
            "state_auto": True,
            "fault_path": ("io_state", ["k", "a_fault"]),
            "fault_auto": True,
        },
    ]
    readings = [
        {"key": "a_fault", "name": "Dup", "path": ("io_state", ["x"]), "auto": False}
    ]
    points = model.io_points(pumps, readings)["points"]
    assert [p["id"] for p in points] == ["a", "a_fault"]
    assert points[1]["label"] == "A fault"
