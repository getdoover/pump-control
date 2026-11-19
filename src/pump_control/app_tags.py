from pydoover.tags import Tag, Tags

# Per-pump tags are keyed by target (model.tag_key): p_<target>_<field>, e.g.
# p_pump_1_running. They are written straight through the tag manager because
# the set of pumps comes from config. Peers (an Auto-fill install on another
# device) read p_<target>_running / _state / _name / _last_started /
# _last_stopped.


class PumpControlTags(Tags):
    # Pump role: the "Auto-fill: controlled by ..." banner.
    af_banner = Tag("string", default=None)

    # Auto-fill role: operator settings (defaults from config).
    af_enabled = Tag("boolean", default=None)
    af_start = Tag("number", default=None)
    af_stop = Tag("number", default=None)
    af_max_shift_min = Tag("number", default=None)
    # Fingerprint of the config defaults the settings were last reset from.
    af_config_sig = Tag("string", default=None)

    # Auto-fill role: status. Peers read af_enabled and af_pump_target.
    af_state = Tag("string", default="Unknown")
    af_pump_target = Tag("string", default=None)
    af_pump_name = Tag("string", default=None)
    af_pump_state = Tag("string", default="Unknown")
    af_pump_running = Tag("boolean", default=None)
    af_fill_active = Tag("boolean", default=False)
    af_fill_started = Tag("number", default=None)
    af_fill_ended = Tag("number", default=None)
    af_shift_notified = Tag("boolean", default=False)
    af_overfill_since = Tag("number", default=None)
    af_overfill_notified = Tag("boolean", default=False)
    af_warning = Tag("string", default="")
    af_warning_hidden = Tag("boolean", default=True)
