from pathlib import Path

from pydoover import config
from pydoover.processor import (
    ExtendedPermissionsConfig,
    ManySubscriptionConfig,
    ScheduleConfig,
)

from .model import (
    INPUT_ANALOG,
    INPUT_SCALED,
    MODE_LATCHED,
    MODE_TIMED,
    ROLE_AUTO_FILL,
    ROLE_PUMP,
)


class PumpConfig(config.Object):
    name = config.String(
        "Name",
        name="name",
        description="What operators call this pump, e.g. 'Pump 1'.",
        default="Pump 1",
        position=1,
    )
    target = config.String(
        "Target ID",
        name="target",
        description=(
            "The pump's output id on the edge controller. Sent as 'target' in "
            "commands, e.g. 'pump_1'."
        ),
        default="pump_1",
        position=2,
    )
    run_state_path = config.String(
        "Run State Input Path",
        name="run_state_path",
        description=(
            "The pump's reported run state, truthy while it is running. Leave "
            "blank to use this input's automatic address, which the edge bridge "
            "fills in. Or give a message path, e.g. '$io_state.x'."
        ),
        default="",
        required=False,
        position=3,
    )
    fault_path = config.String(
        "Fault Input Path",
        name="fault_path",
        description=(
            "The pump's fault input, truthy while in fault; shown once a value "
            "has arrived. Leave blank to use this input's automatic address, "
            "which the edge bridge fills in. Or give a message path, e.g. "
            "'$io_state.x'."
        ),
        default="",
        required=False,
        position=4,
    )
    default_run_min = config.Number(
        "Default Run Duration (min)",
        name="default_run_min",
        description="Run time a Timed start offers by default.",
        default=60.0,
        minimum=1.0,
        position=5,
    )
    max_run_min = config.Number(
        "Max Run Duration (min)",
        name="max_run_min",
        description="Longest run time an operator can ask for. The edge enforces it too.",
        default=720.0,
        minimum=1.0,
        position=6,
    )
    mode = config.Enum(
        "Mode",
        name="mode",
        choices=[MODE_TIMED, MODE_LATCHED],
        default=MODE_TIMED,
        description=(
            "Timed: Start runs the pump for a set time, then the edge stops it. "
            "Latched: Start runs it until Stop."
        ),
        position=7,
    )


class ReadingConfig(config.Object):
    name = config.String(
        "Name",
        name="name",
        description="What operators call this reading, e.g. 'Suction Pressure' or 'Engine RPM'.",
        default="Reading 1",
        position=1,
    )
    key = config.String(
        "Key",
        name="key",
        description=(
            "Short id used in this reading's tags (r_<key>, r_<key>_raw, "
            "r_<key>_fault), e.g. 'suction_pressure'. Letters, digits and "
            "underscores. Left blank, it is made from the name."
        ),
        default="",
        required=False,
        position=2,
    )
    enabled = config.Boolean(
        "Enabled",
        name="enabled",
        description=(
            "Switch the reading off where it doesn't exist on this device. An "
            "enabled reading is always shown, even while faulted or stale."
        ),
        default=True,
        position=3,
    )
    input_path = config.String(
        "Input Message Path",
        name="input_path",
        description=(
            "The raw value. Leave blank to use this input's automatic address, "
            "which the edge bridge fills in. Or give a message path, e.g. "
            "'$io_state.x'."
        ),
        default="",
        required=False,
        position=4,
    )
    input_type = config.Enum(
        "Input Type",
        name="input_type",
        choices=[INPUT_ANALOG, INPUT_SCALED],
        default=INPUT_ANALOG,
        description=(
            "Analog: a raw signal (e.g. 4-20 mA) mapped linearly from the input "
            "range to the value range; well outside the input range is a sensor "
            "fault. Scaled: value = raw x Multiplier + Offset."
        ),
        position=5,
    )
    input_min = config.Number(
        "Input Min",
        name="input_min",
        description="Analog: raw input at the bottom of the range, e.g. 4 (mA).",
        default=4.0,
        position=6,
    )
    input_max = config.Number(
        "Input Max",
        name="input_max",
        description="Analog: raw input at the top of the range, e.g. 20 (mA).",
        default=20.0,
        position=7,
    )
    value_min = config.Number(
        "Value Min",
        name="value_min",
        description="Analog: the value at Input Min, in Units.",
        default=0.0,
        position=8,
    )
    value_max = config.Number(
        "Value Max",
        name="value_max",
        description="Analog: the value at Input Max, in Units.",
        default=100.0,
        position=9,
    )
    multiplier = config.Number(
        "Multiplier",
        name="multiplier",
        description="Scaled: value = raw x Multiplier + Offset.",
        default=1.0,
        position=10,
    )
    offset = config.Number(
        "Offset",
        name="offset",
        description="Scaled: added after the Multiplier.",
        default=0.0,
        position=11,
    )
    units = config.String(
        "Units",
        name="units",
        description="Shown after the value, e.g. 'PSI' or 'rpm'. Blank for none.",
        default="",
        required=False,
        position=12,
    )
    precision = config.Integer(
        "Decimal Precision",
        name="precision",
        description="Decimal places shown.",
        default=1,
        minimum=0,
        maximum=6,
        position=13,
    )
    warn_low = config.Number(
        "Low Warning",
        name="warn_low",
        description="Optional: show a warning while the value is below this.",
        default=None,
        required=False,
        position=14,
    )
    warn_high = config.Number(
        "High Warning",
        name="warn_high",
        description="Optional: show a warning while the value is above this.",
        default=None,
        required=False,
        position=15,
    )


class LevelSource(config.Object):
    app_name = config.ApplicationInstall(
        "Application",
        description="App on this device that publishes the tank level, e.g. an Analog Level Sensor Processor.",
        name="app_name",
        default="analog_level_sensor_processor_1",
    )
    tag_name = config.String(
        "Tag Name",
        description="The level tag, in percent full.",
        name="tag_name",
        default="level_filled_percentage",
    )


_pump_role = config.equal("role", ROLE_PUMP)
_fill_role = config.equal("role", ROLE_AUTO_FILL)


class PumpControlConfig(config.Schema):
    role = config.Enum(
        "Role",
        name="role",
        choices=[ROLE_PUMP, ROLE_AUTO_FILL],
        default=ROLE_PUMP,
        description=(
            "Pump: remote control of the pumps on this device. Auto-fill (storage): "
            "the fill settings of the tank or storage on this device, which a "
            "pump on another device fills."
        ),
        position=1,
    )

    # -- Pump role ------------------------------------------------------------
    pumps = config.Array(
        "Pumps",
        name="pumps",
        element=PumpConfig("Pump"),
        description="The pumps this device controls.",
        default=[],
        show_if=_pump_role,
        position=2,
    )
    auto_fill_device = config.Device(
        "Auto-filled By",
        name="auto_fill_device",
        description=(
            "Optional: the tank or storage device whose Pump Control (Auto-fill) "
            "install controls these pumps. Also add it under Devices below."
        ),
        default=None,
        required=False,
        show_if=_pump_role,
        position=3,
    )
    auto_fill_app_key = config.String(
        "Auto-fill App Key",
        name="auto_fill_app_key",
        description="The Pump Control install's app key on that device, e.g. 'pump_control_1'.",
        default=None,
        required=False,
        show_if=_pump_role,
        position=4,
    )

    readings = config.Array(
        "Readings",
        name="readings",
        element=ReadingConfig("Reading"),
        description=(
            "Optional telemetry shown under the pumps, e.g. pressures or engine "
            "data, each published as tags for other apps."
        ),
        default=[],
        required=False,
        show_if=_pump_role,
        position=13,
    )
    reading_stale_after_min = config.Number(
        "Reading Stale After (min)",
        name="reading_stale_after_min",
        description=(
            "A reading with no new value for this long is shown as stale, with "
            "its age. It stays on the page with its last value."
        ),
        default=120.0,
        minimum=1.0,
        show_if=_pump_role,
        advanced=True,
        position=14,
    )

    # -- Auto-fill role -------------------------------------------------------
    level_source = LevelSource(
        "Level Source",
        name="level_source",
        description="The tag on this device that carries the tank level (%).",
        default={
            "app_name": "analog_level_sensor_processor_1",
            "tag_name": "level_filled_percentage",
        },
        required=False,
        show_if=_fill_role,
        position=5,
    )
    pump_device = config.Device(
        "Pump Device",
        name="pump_device",
        description="The device with the pump that fills this storage. Also add it under Devices below.",
        default=None,
        required=False,
        show_if=_fill_role,
        position=6,
    )
    pump_app_key = config.String(
        "Pump App Key",
        name="pump_app_key",
        description="The Pump Control install's app key on the pump device, e.g. 'pump_control_1'.",
        default=None,
        required=False,
        show_if=_fill_role,
        position=7,
    )
    pump_target = config.String(
        "Pump Target ID",
        name="pump_target",
        description="The pump's Target ID in that install, e.g. 'pump_1'.",
        default="pump_1",
        show_if=_fill_role,
        position=8,
    )
    start_level = config.Number(
        "Start Level (%)",
        name="start_level",
        description="Default: start filling when the level falls below this. Operators can change it on the page.",
        default=60.0,
        minimum=0.0,
        maximum=100.0,
        show_if=_fill_role,
        position=9,
    )
    stop_level = config.Number(
        "Stop Level (%)",
        name="stop_level",
        description="Default: stop filling when the level reaches this. Operators can change it on the page.",
        default=90.0,
        minimum=0.0,
        maximum=100.0,
        show_if=_fill_role,
        position=10,
    )
    max_shift_min = config.Number(
        "Max Shift (min)",
        name="max_shift_min",
        description="Default: the longest the pump may run in one fill. Operators can change it on the page.",
        default=60.0,
        minimum=1.0,
        show_if=_fill_role,
        position=11,
    )
    auto_fill_enabled = config.Boolean(
        "Enabled by Default",
        name="auto_fill_enabled",
        description="Whether auto-fill starts enabled. Operators can switch it on the page.",
        default=True,
        show_if=_fill_role,
        position=12,
    )

    # -- Channels ---------------------------------------------------------------
    command_channel = config.String(
        "Command Channel",
        name="command_channel",
        description="Where Start and Stop commands go to the edge controller.",
        default="io_commands",
        advanced=True,
        position=20,
    )
    settings_channel = config.String(
        "Settings Channel",
        name="settings_channel",
        description=(
            "Whose aggregate carries the settings the edge enforces, under this "
            "app's key."
        ),
        default="control_settings",
        advanced=True,
        position=21,
    )
    confirm_timeout_s = config.Integer(
        "Confirm Timeout (s)",
        name="confirm_timeout_s",
        description="How long a command may wait for the reported state to match before a warning shows.",
        default=120,
        minimum=10,
        advanced=True,
        position=22,
    )
    timezone = config.String(
        "Timezone",
        name="timezone",
        description="IANA timezone for 'today' in run hours and starts, e.g. 'Australia/Brisbane'.",
        default="Australia/Brisbane",
        advanced=True,
        position=23,
    )

    subscription = ManySubscriptionConfig(
        default=["io_state", "ui_cmds", "io_commands"],
        description=(
            "Channels that invoke this app: the channels in the input paths, "
            "ui_cmds for the buttons, and the Command Channel for controller replies."
        ),
        advanced=True,
        position=24,
    )
    schedule = ScheduleConfig(
        default="rate(5 minutes)",
        description="Periodic refresh: run hours, command timeouts, reading ages and the linked device's state.",
        advanced=True,
        position=25,
    )
    extended_permissions = ExtendedPermissionsConfig(
        extra_fields=["id", "name", "display_name"],
        default={
            "devices": [],
            "groups": [],
            "apps_installed": [],
            "all_devices": False,
        },
        required=False,
        position=26,
    )

    position = config.ApplicationPosition(default=20, position=27)
    default_open = config.ApplicationDefaultOpen(default=True, position=28)


def export():
    PumpControlConfig.export(
        Path(__file__).parents[2] / "doover_config.json",
        "pump_control",
    )
