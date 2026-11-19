from types import SimpleNamespace

import pytest
from pydoover.rpc import RPCError
from pydoover.tags.manager import TagsManagerProcessor

from pump_control import model
from pump_control.app_config import PumpControlConfig
from pump_control.app_tags import PumpControlTags
from pump_control.application import PumpControlApplication

PUMP_DEVICE = "248969700854236933"
TANK_DEVICE = "248969699172321036"


class FakeApi:
    def __init__(self, aggregates=None, forbidden=()):
        self.messages = []
        self.aggregate_updates = []
        self.aggregates = aggregates or {}
        self.forbidden = set(forbidden)

    async def create_message(self, channel, data, **kwargs):
        self.messages.append((channel, data))

    async def update_channel_aggregate(self, channel, data, **kwargs):
        self.aggregate_updates.append((channel, data, kwargs))

    async def fetch_channel_aggregate(self, channel, agent_id=None, **kwargs):
        key = (str(agent_id) if agent_id else None, channel)
        if agent_id and str(agent_id) in self.forbidden:
            raise PermissionError("403")
        if key not in self.aggregates:
            raise LookupError("404")
        return SimpleNamespace(data=self.aggregates[key])


class FakeUIManager:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_value(self, key):
        return self.values[key]

    async def set_value(self, key, value, log_update=True):
        self.values[key] = value


class FakeNotification:
    def __init__(self):
        self.sent = []

    async def send(self, message=None, **kwargs):
        self.sent.append(message)


class FakeCtx:
    def __init__(self, method):
        self.method = method
        self.values = []

    async def set_value(self, value, max_age=None, log_update=True):
        self.values.append(value)


async def make_app(config: dict, tag_values=None, api=None, ui_values=None):
    app = object.__new__(PumpControlApplication)
    app.config = PumpControlConfig()
    app.config._inject_deployment_config(config)
    app.app_key = "pump_control_1"
    app.api = api or FakeApi()
    app.tag_manager = TagsManagerProcessor(
        "pump_control_1", app.api, 1, tag_values or {}
    )
    app.tags = PumpControlTags("pump_control_1", app.tag_manager, app.config)
    app.ui_manager = FakeUIManager(ui_values)
    app.notifications = SimpleNamespace(
        shift_exceeded=FakeNotification(), stop_level_pump_running=FakeNotification()
    )
    app.received_deployment_config = {
        "DEVICE_MAP": {
            PUMP_DEVICE: {"name": "pump", "display_name": "Main Office Fill Pump"},
            TANK_DEVICE: {"name": "tank", "display_name": "Main Office Tank"},
        }
    }
    await app.setup()
    return app


PUMP_CONFIG = {
    "role": "Pump",
    "pumps": [
        {
            "name": "Pump 1",
            "target": "pump_1",
            "run_state_path": "$io_state.pump_1",
            "fault_path": "$io_state.pump_1_fault",
            "default_run_min": 10.0,
            "max_run_min": 120.0,
        },
        {
            "name": "Pump 2",
            "target": "pump_2",
            "run_state_path": "$io_state.pump_2",
            "mode": "Latched",
        },
    ],
    "auto_fill_device": TANK_DEVICE,
    "auto_fill_app_key": "pump_control_1",
}

FILL_CONFIG = {
    "role": "Auto-fill (storage)",
    "level_source": {
        "app_name": "analog_level_sensor_processor_1",
        "tag_name": "level_filled_percentage",
    },
    "pump_device": PUMP_DEVICE,
    "pump_app_key": "pump_control_1",
    "pump_target": "pump_1",
    "start_level": 60.0,
    "stop_level": 85.0,
    "max_shift_min": 10.0,
    "auto_fill_enabled": True,
}


def msg(channel, data):
    return SimpleNamespace(
        channel=SimpleNamespace(name=channel), message=SimpleNamespace(data=data)
    )


def tag(app, key):
    return app.tag_manager.get_tag(key)


@pytest.mark.asyncio
async def test_deploy_with_no_data_is_clean():
    app = await make_app(PUMP_CONFIG)
    await app.on_deployment(None)
    assert tag(app, "p_pump_1_state") == "Unknown"
    assert tag(app, "p_pump_1_name") == "Pump 1"
    assert tag(app, "p_pump_1_starts_today") == 0
    # settings published under the app key, merged not clobbered
    channel, data, kwargs = app.api.aggregate_updates[0]
    assert channel == "control_settings"
    assert data["pump_control_1"]["pumps"][0]["max_run_s"] == 7200
    assert kwargs["replace_keys"] == ["pump_control_1"]
    # auto-fill device unreadable -> says so, no exception
    assert tag(app, "af_banner").startswith("Controlled by Main Office Tank")


@pytest.mark.asyncio
async def test_state_messages_update_pumps():
    app = await make_app(PUMP_CONFIG)
    await app.on_message_create(msg("io_state", {"pump_1": 1}))
    assert tag(app, "p_pump_1_state") == "Running"
    assert tag(app, "p_pump_1_starts_today") == 1
    assert tag(app, "p_pump_2_state") is None  # not in this message
    await app.on_message_create(msg("io_state", {"pump_1_fault": True}))
    assert tag(app, "p_pump_1_fault_hidden") is False
    await app.on_message_create(msg("other", {"pump_1": 0}))
    assert tag(app, "p_pump_1_state") == "Running"


@pytest.mark.asyncio
async def test_start_sends_command_and_confirms():
    app = await make_app(PUMP_CONFIG, ui_values={"p_pump_1_minutes": 5})
    await app.on_start(FakeCtx("p_pump_1_start"), None)
    channel, cmd = app.api.messages[-1]
    assert channel == "io_commands"
    assert (
        cmd["command"] == "start"
        and cmd["target"] == "pump_1"
        and cmd["duration_s"] == 300
    )
    assert cmd["app_key"] == "pump_control_1"
    assert tag(app, "p_pump_1_pending")["request_id"] == cmd["request_id"]
    assert "waiting" in tag(app, "p_pump_1_cmd_text")

    # edge acknowledges, then reports running
    await app.on_message_create(
        msg("io_commands", {"request_id": cmd["request_id"], "ok": True, "error": None})
    )
    assert "accepted" in tag(app, "p_pump_1_cmd_text")
    await app.on_message_create(msg("io_state", {"pump_1": True}))
    assert tag(app, "p_pump_1_pending") is None
    assert "confirmed" in tag(app, "p_pump_1_cmd_text")
    assert tag(app, "p_pump_1_ends_at") == cmd["ts"] + 300_000


@pytest.mark.asyncio
async def test_start_rejects_out_of_range_and_latched_has_no_duration():
    app = await make_app(PUMP_CONFIG, ui_values={"p_pump_1_minutes": 500})
    with pytest.raises(RPCError):
        await app.on_start(FakeCtx("p_pump_1_start"), None)
    assert app.api.messages == []
    await app.on_start(FakeCtx("p_pump_2_start"), None)
    assert app.api.messages[-1][1]["duration_s"] is None
    await app.on_stop(FakeCtx("p_pump_2_stop"), None)
    assert app.api.messages[-1][1]["command"] == "stop"


@pytest.mark.asyncio
async def test_rejected_reply_shows_warning():
    app = await make_app(PUMP_CONFIG)
    await app.on_stop(FakeCtx("p_pump_1_stop"), None)
    rid = app.api.messages[-1][1]["request_id"]
    await app.on_message_create(
        msg("io_commands", {"request_id": rid, "ok": False, "error": "interlock"})
    )
    assert tag(app, "p_pump_1_cmd_warning_hidden") is False
    assert "interlock" in tag(app, "p_pump_1_cmd_warning")


@pytest.mark.asyncio
async def test_banner_reads_auto_fill_device():
    api = FakeApi(
        {
            (TANK_DEVICE, "tag_values"): {
                "pump_control_1": {"af_enabled": True, "af_pump_target": "pump_1"}
            }
        }
    )
    app = await make_app(PUMP_CONFIG, api=api)
    await app.on_message_create(msg("io_state", {"pump_1": 1}))
    assert tag(app, "af_banner") == "Controlled by Main Office Tank: Enabled, Filling"


@pytest.mark.asyncio
async def test_fill_deploy_no_data():
    app = await make_app(FILL_CONFIG)
    await app.on_deployment(None)
    assert app.tags.af_enabled.value is True
    assert app.tags.af_start.value == 60.0
    assert app.tags.af_state.value == "Unknown"
    assert app.tags.af_warning_hidden.value is False  # pump device unreadable
    assert app.ui_manager.values["af_stop"] == 85.0
    settings = app.api.aggregate_updates[0][1]["pump_control_1"]
    assert settings["role"] == "auto_fill" and settings["stop_level_pct"] == 85.0
    assert app.notifications.shift_exceeded.sent == []
    assert app.notifications.stop_level_pump_running.sent == []


@pytest.mark.asyncio
async def test_fill_reads_pump_and_notifies():
    pump_tags = {
        "pump_control_1": {
            "p_pump_1_running": True,
            "p_pump_1_name": "Pump 1",
            "p_pump_1_last_started": 1,
        }
    }
    api = FakeApi({(PUMP_DEVICE, "tag_values"): pump_tags})
    tags = {"analog_level_sensor_processor_1": {"level_filled_percentage": 90.0}}
    app = await make_app(FILL_CONFIG, tag_values=tags, api=api)
    await app.on_deployment(None)
    assert app.tags.af_state.value == "Filling"
    assert app.tags.af_pump_name.value == "Main Office Fill Pump: Pump 1"
    assert app.tags.af_warning_hidden.value is True
    # fill started long ago -> max shift exceeded right away
    assert len(app.notifications.shift_exceeded.sent) == 1


@pytest.mark.asyncio
async def test_operator_edits_validate_and_publish():
    app = await make_app(FILL_CONFIG)
    await app.on_deployment(None)
    ctx = FakeCtx("af_start")
    with pytest.raises(RPCError):
        await app.on_fill_start(ctx, 90)
    assert ctx.values == [60.0]  # reverted
    assert app.tags.af_start.value == 60.0

    ctx = FakeCtx("af_stop")
    await app.on_fill_stop(ctx, 95)
    assert app.tags.af_stop.value == 95.0
    assert app.api.aggregate_updates[-1][1]["pump_control_1"]["stop_level_pct"] == 95.0
    assert (
        app.api.aggregate_updates[-1][1]["pump_control_1"]["updated_by"] == "operator"
    )

    await app.on_fill_enabled(FakeCtx("af_enabled"), False)
    assert app.tags.af_state.value == "Disabled"

    # redeploy with unchanged config keeps the operator's values
    app2 = await make_app(FILL_CONFIG, tag_values=app.tag_manager._tag_values)
    await app2.on_deployment(None)
    assert app2.tags.af_stop.value == 95.0
    assert app2.tags.af_enabled.value is False


def test_tag_keys_used_by_peers():
    assert model.tag_key("pump_call", "running") == "p_pump_call_running"


def shown(app, t="pump_1"):
    return (
        not (tag(app, f"p_{t}_start_hidden") or False),
        not (tag(app, f"p_{t}_stop_hidden") or False),
    )


@pytest.mark.asyncio
async def test_controls_follow_the_reported_state():
    app = await make_app(PUMP_CONFIG, ui_values={"p_pump_1_minutes": 5})
    await app.on_schedule(None)
    assert shown(app) == (True, True)  # no data: both

    await app.on_message_create(msg("io_state", {"pump_1": 0}))
    assert shown(app) == (True, False)  # stopped: Start only

    await app.on_start(FakeCtx("p_pump_1_start"), None)
    assert shown(app) == (True, True)  # pending: both

    await app.on_message_create(msg("io_state", {"pump_1": 1}))
    assert shown(app) == (False, True)  # running: Stop only

    await app.on_message_create(msg("io_state", {"pump_1_fault": 1}))
    assert shown(app) == (True, True)  # fault: both
    await app.on_message_create(msg("io_state", {"pump_1_fault": 0}))
    assert shown(app) == (False, True)

    await app.on_stop(FakeCtx("p_pump_1_stop"), None)
    rid = app.api.messages[-1][1]["request_id"]
    await app.on_message_create(
        msg("io_commands", {"request_id": rid, "ok": False, "error": "interlock"})
    )
    assert shown(app) == (True, True)  # rejected: both
    await app.on_message_create(msg("io_state", {"pump_1": 0}))
    assert shown(app) == (True, False)  # it moved since: trusted again
    assert tag(app, "p_pump_1_cmd_warning_hidden") is True


@pytest.mark.asyncio
async def test_start_and_stop_are_bound_to_their_hidden_tags():
    app = await make_app(PUMP_CONFIG)
    from pump_control.app_ui import pump_elements
    from pump_control.application import parse_pumps

    elements = {e.name: e.to_dict() for e in pump_elements(parse_pumps(app.config)[0])}
    assert "p_pump_1_start_hidden" in str(elements["p_pump_1_start"]["hidden"])
    assert "p_pump_1_start_hidden" in str(elements["p_pump_1_minutes"]["hidden"])
    assert "p_pump_1_stop_hidden" in str(elements["p_pump_1_stop"]["hidden"])


READINGS_CONFIG = {
    **PUMP_CONFIG,
    "auto_fill_device": None,
    "auto_fill_app_key": None,
    "readings": [
        {
            "name": "Pressure 1",
            "key": "pressure_1",
            "input_path": "$io_state.pressure_1",
            "input_type": "Analog",
            "input_min": 4,
            "input_max": 20,
            "value_min": 0,
            "value_max": 145,
            "units": "PSI",
            "precision": 1,
        },
        {
            "name": "Engine RPM (provisional)",
            "input_path": "$io_state.modbus_7",
            "input_type": "Scaled",
            "multiplier": 1,
            "offset": 0,
            "precision": 0,
        },
        {
            "name": "Switched Off",
            "key": "off",
            "enabled": False,
            "input_path": "$io_state.off",
        },
    ],
}


@pytest.mark.asyncio
async def test_readings_parse_and_toggle():
    app = await make_app(READINGS_CONFIG)
    keys = [r["key"] for r in app.readings]
    assert keys == ["pressure_1", "engine_rpm_provisional"]  # only the toggle drops one


@pytest.mark.asyncio
async def test_readings_deploy_with_no_data_is_clean():
    app = await make_app(READINGS_CONFIG)
    await app.on_deployment(None)
    assert tag(app, "r_pressure_1") is None
    assert tag(app, "r_pressure_1_status") == "No reading yet"
    assert tag(app, "r_pressure_1_warning_hidden") is True
    assert tag(app, "readings_status") == "No reading yet"


@pytest.mark.asyncio
async def test_readings_publish_value_raw_and_fault():
    app = await make_app(READINGS_CONFIG)
    await app.on_message_create(msg("io_state", {"pressure_1": 12.0, "modbus_7": 1580}))
    assert tag(app, "r_pressure_1") == pytest.approx(72.5)
    assert tag(app, "r_pressure_1_raw") == 12.0
    assert tag(app, "r_pressure_1_fault") is False
    assert tag(app, "r_engine_rpm_provisional") == 1580
    assert tag(app, "readings_status") == "OK"

    # the loop goes dead: still published, as a fault with no value
    await app.on_message_create(msg("io_state", {"pressure_1": 0}))
    assert tag(app, "r_pressure_1") is None
    assert tag(app, "r_pressure_1_raw") == 0
    assert tag(app, "r_pressure_1_fault") is True
    assert tag(app, "r_pressure_1_warning_hidden") is False
    assert "sensor fault" in tag(app, "r_pressure_1_warning")
    assert tag(app, "readings_status") == "1 sensor fault"
    # a message without the field leaves it alone
    await app.on_message_create(msg("io_state", {"modbus_7": 1590}))
    assert tag(app, "r_pressure_1_fault") is True


@pytest.mark.asyncio
async def test_readings_go_stale_on_schedule_and_keep_value():
    app = await make_app(READINGS_CONFIG)
    await app.on_message_create(msg("io_state", {"modbus_7": 1580}))
    sent = tag(app, "r_engine_rpm_provisional_ts")
    app._now = lambda: sent + 3 * 3_600_000
    await app.on_schedule(None)
    assert tag(app, "r_engine_rpm_provisional") == 1580
    assert tag(app, "r_engine_rpm_provisional_status") == "Stale"
    assert "no new reading for 3 h" in tag(app, "r_engine_rpm_provisional_warning")


@pytest.mark.asyncio
async def test_readings_seeded_from_aggregate_on_deploy():
    api = FakeApi(aggregates={(None, "io_state"): {"pressure_1": 0.0, "modbus_7": 362}})
    app = await make_app(READINGS_CONFIG, api=api)
    await app.on_deployment(None)
    assert tag(app, "r_pressure_1_fault") is True
    assert tag(app, "r_engine_rpm_provisional") == 362
    assert tag(app, "readings_status") == "1 sensor fault"


@pytest.mark.asyncio
async def test_readings_ui_section():
    app = await make_app(READINGS_CONFIG)
    from pump_control.app_ui import reading_elements

    elements = {e.name: e.to_dict() for e in reading_elements(app.readings)}
    assert set(elements) == {
        "r_pressure_1",
        "r_pressure_1_warning_ind",
        "r_engine_rpm_provisional",
        "r_engine_rpm_provisional_warning_ind",
    }
    assert elements["r_pressure_1"]["units"] == "PSI"
    assert "r_pressure_1_warning_hidden" in str(
        elements["r_pressure_1_warning_ind"]["hidden"]
    )
