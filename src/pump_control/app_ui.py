"""The page. Built from config at deployment: one section per pump in the Pump
role, the fill settings and status in the Auto-fill role.

The schema is published once per deployment, so everything that changes is
bound to tags; buttons and inputs arrive on ui_cmds and are handled in
application.py.
"""

from pathlib import Path

from pydoover import ui

from .app_tags import PumpControlTags
from .model import MODE_TIMED, ROLE_AUTO_FILL, slug, tag_key


def _tag(key: str, tag_type: str, default=None):
    return ui.tag_ref(key, tag_type, default)


def pump_elements(pump: dict) -> list:
    """One pump's controls and readings. ``pump`` is a parsed config entry."""
    s = slug(pump["target"])
    timed = pump["mode"] == MODE_TIMED
    children = [
        ui.WarningIndicator(
            f"{pump['name']}: fault reported",
            name=f"p_{s}_fault_warning",
            hidden=_tag(tag_key(s, "fault_hidden"), "boolean", True),
            can_cancel=False,
            position=1,
        ),
        ui.WarningIndicator(
            _tag(tag_key(s, "cmd_warning"), "string", ""),
            name=f"p_{s}_cmd_warning",
            hidden=_tag(tag_key(s, "cmd_warning_hidden"), "boolean", True),
            can_cancel=False,
            position=2,
        ),
        ui.TextVariable(
            "State",
            name=f"p_{s}_state",
            value=_tag(tag_key(s, "state"), "string", "Unknown"),
            position=3,
        ),
        ui.TextVariable(
            "Command",
            name=f"p_{s}_command",
            value=_tag(tag_key(s, "cmd_text"), "string", "None sent"),
            position=4,
        ),
    ]
    if timed:
        children.append(
            ui.FloatInput(
                "Start For (min)",
                name=f"p_{s}_minutes",
                min_val=1,
                max_val=float(pump["max_run_min"]),
                default=float(pump["default_run_min"]),
                position=5,
            )
        )
    children += [
        ui.Button(
            "Start",
            name=f"p_{s}_start",
            requires_confirm=True,
            colour=ui.Colour.green,
            position=6,
        ),
        ui.Button(
            "Stop",
            name=f"p_{s}_stop",
            colour=ui.Colour.red,
            position=7,
        ),
    ]
    if timed:
        children.append(
            ui.Timestamp(
                "Time Remaining",
                name=f"p_{s}_ends_at",
                value=_tag(tag_key(s, "ends_at"), "number"),
                precision="second",
                position=8,
            )
        )
    children += [
        ui.Timestamp(
            "Last Started",
            name=f"p_{s}_last_started",
            value=_tag(tag_key(s, "last_started"), "number"),
            position=9,
        ),
        ui.Timestamp(
            "Last Stopped",
            name=f"p_{s}_last_stopped",
            value=_tag(tag_key(s, "last_stopped"), "number"),
            position=10,
        ),
        ui.NumericVariable(
            "Run Hours Today",
            name=f"p_{s}_hours_today",
            value=_tag(tag_key(s, "hours_today"), "number"),
            units="h",
            precision=1,
            position=11,
        ),
        ui.NumericVariable(
            "Run Hours Total",
            name=f"p_{s}_hours_total",
            value=_tag(tag_key(s, "hours_total"), "number"),
            units="h",
            precision=1,
            position=12,
        ),
        ui.NumericVariable(
            "Starts Today",
            name=f"p_{s}_starts_today",
            value=_tag(tag_key(s, "starts_today"), "number"),
            precision=0,
            position=13,
        ),
    ]
    return children


class PumpControlUI(ui.UI):
    async def setup(self):
        from .application import parse_pumps  # avoid an import cycle

        if self.config.role.value == ROLE_AUTO_FILL:
            self._setup_auto_fill()
            return

        if self.config.auto_fill_device.value:
            self.add_element(
                ui.TextVariable(
                    "Auto-Fill",
                    name="af_banner",
                    value=PumpControlTags.af_banner,
                    position=1,
                )
            )

        for index, pump in enumerate(parse_pumps(self.config)):
            self.add_element(
                ui.Submodule(
                    pump["name"],
                    name=f"p_{slug(pump['target'])}",
                    status=_tag(tag_key(pump["target"], "state"), "string", "Unknown"),
                    children=pump_elements(pump),
                    position=10 + index,
                )
            )

    def _setup_auto_fill(self):
        cfg = self.config
        level_app = (cfg.level_source.app_name.value or "").strip()
        level_tag = (cfg.level_source.tag_name.value or "").strip()

        self.add_element(
            ui.WarningIndicator(
                PumpControlTags.af_warning,
                name="af_warning",
                hidden=PumpControlTags.af_warning_hidden,
                can_cancel=False,
                position=1,
            )
        )
        self.add_element(
            ui.TextVariable(
                "Auto-Fill", name="af_state", value=PumpControlTags.af_state, position=2
            )
        )
        self.add_element(
            ui.Switch(
                "Auto-Fill Enabled",
                name="af_enabled",
                # No schema default: its value is seeded into ui_cmds on
                # deployment from the persisted setting.
                position=3,
            )
        )
        if level_app and level_tag:
            self.add_element(
                ui.NumericVariable(
                    "Current Level",
                    name="af_level",
                    # Another app's tag on this device, read by the page directly.
                    value=f"$tag.{level_app}.{level_tag}:number",
                    units="%",
                    precision=1,
                    position=4,
                )
            )
        self.add_element(
            ui.FloatInput(
                "Start Below (%)",
                name="af_start",
                min_val=0,
                max_val=100,
                default=float(cfg.start_level.value),
                position=5,
            )
        )
        self.add_element(
            ui.FloatInput(
                "Stop At (%)",
                name="af_stop",
                min_val=0,
                max_val=100,
                default=float(cfg.stop_level.value),
                position=6,
            )
        )
        self.add_element(
            ui.FloatInput(
                "Max Shift (min)",
                name="af_max_shift",
                min_val=1,
                default=float(cfg.max_shift_min.value),
                position=7,
            )
        )
        self.add_element(
            ui.TextVariable(
                "Linked Pump",
                name="af_pump_name",
                value=_tag("af_pump_name", "string", "Not linked"),
                position=8,
            )
        )
        self.add_element(
            ui.TextVariable(
                "Pump State",
                name="af_pump_state",
                value=PumpControlTags.af_pump_state,
                position=9,
            )
        )
        self.add_element(
            ui.Timestamp(
                "Last Fill Started",
                name="af_fill_started",
                value=PumpControlTags.af_fill_started,
                position=10,
            )
        )
        self.add_element(
            ui.Timestamp(
                "Last Fill Ended",
                name="af_fill_ended",
                value=PumpControlTags.af_fill_ended,
                position=11,
            )
        )


def export():
    PumpControlUI(None, None, None).export(
        Path(__file__).parents[2] / "doover_config.json",
        "pump_control",
    )
