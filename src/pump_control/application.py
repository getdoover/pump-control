"""Pump Control: a remote control for pumps, with an optional auto-fill role.

The edge controller is the source of truth. This app sends it Start / Stop
commands on the Command Channel, writes the settings it must enforce to the
Settings Channel aggregate, and shows what the pumps report back.

The same app runs in two roles:

* Pump: on the device with the pumps. Reads each pump's run state (and fault)
  from message paths, tracks run hours and starts, and sends commands.
* Auto-fill (storage): on the tank the pump fills. Owns the fill settings,
  which operators edit on the page, and shows the fill state from the linked
  pump's reported state (read from the pump device with extended permissions).
"""

import hashlib
import json
import logging
import re
import time
from typing import Any

from pydoover import ui
from pydoover.models import (
    DeploymentEvent,
    MessageCreateEvent,
    ScheduleEvent,
)
from pydoover.processor import Application
from pydoover.rpc import RPCError
from pydoover.tags.manager import LogMode

from . import model
from .app_config import PumpControlConfig
from .app_notifications import PumpControlNotifications
from .app_tags import PumpControlTags
from .app_ui import PumpControlUI

log = logging.getLogger(__name__)

# Per-pump statistics kept in tags (model.observe works on these).
STAT_FIELDS = (
    "running",
    "accrued_at",
    "day",
    "hours_today",
    "hours_total",
    "starts_today",
    "last_started",
    "last_stopped",
)


def _parse_path(value: Any, what: str):
    try:
        return model.parse_message_path(value)
    except ValueError as e:
        log.warning("Ignoring %s: %s", what, e)
        return None


def parse_pumps(cfg: PumpControlConfig) -> list[dict]:
    """The configured pumps as plain dicts. Entries without a target are skipped,
    and a later duplicate target is dropped (targets key the tags and buttons)."""
    pumps: list[dict] = []
    seen: set[str] = set()
    for elem in cfg.pumps.elements:
        target = (elem.target.value or "").strip()
        if not target:
            continue
        s = model.slug(target)
        if s in seen:
            log.warning("Duplicate pump target %r ignored.", target)
            continue
        seen.add(s)
        default_min = float(elem.default_run_min.value or 60.0)
        max_min = float(elem.max_run_min.value or 720.0)
        pumps.append(
            {
                "name": (elem.name.value or "").strip() or target,
                "target": target,
                "slug": s,
                "state_path": _parse_path(
                    elem.run_state_path.value, f"{target} run state path"
                ),
                "fault_path": _parse_path(
                    elem.fault_path.value, f"{target} fault path"
                ),
                "default_run_min": min(default_min, max_min),
                "max_run_min": max_min,
                "mode": elem.mode.value or model.MODE_TIMED,
            }
        )
    return pumps


def _as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PumpControlApplication(Application):
    config: PumpControlConfig
    tags: PumpControlTags
    ui: PumpControlUI
    notifications: PumpControlNotifications

    config_cls = PumpControlConfig
    tags_cls = PumpControlTags
    ui_cls = PumpControlUI
    notifications_cls = PumpControlNotifications

    async def setup(self):
        # Log only what changed: the tag history then shows transitions, not
        # every unchanged value on each schedule tick.
        if self.tag_manager is not None:
            self.tag_manager.log_mode = LogMode.ONLY_CHANGED

        cfg = self.config
        self.role = cfg.role.value or model.ROLE_PUMP
        self.tz = model.get_zone(cfg.timezone.value)
        self.command_channel = (cfg.command_channel.value or "io_commands").strip()
        self.settings_channel = (
            cfg.settings_channel.value or "control_settings"
        ).strip()
        self.confirm_timeout_s = float(cfg.confirm_timeout_s.value or 120)
        self.pumps = parse_pumps(cfg) if self.role == model.ROLE_PUMP else []
        self.pumps_by_slug = {p["slug"]: p for p in self.pumps}
        self._aggregates: dict[str, dict] = {}

    # ------------------------------------------------------------------ events

    async def on_deployment(self, event: DeploymentEvent):
        now = self._now()
        if self.role == model.ROLE_AUTO_FILL:
            await self._init_fill_settings(now)
            await self._publish_fill_settings(now, updated_by="config")
            await self._refresh_fill(now)
        else:
            for pump in self.pumps:
                await self._set(model.tag_key(pump["target"], "name"), pump["name"])
            await self._publish_pump_settings(now)
            await self._refresh_pumps_from_aggregates(now)
            await self._refresh_banner()

    async def on_schedule(self, event: ScheduleEvent):
        now = self._now()
        if self.role == model.ROLE_AUTO_FILL:
            await self._refresh_fill(now)
        else:
            await self._refresh_pumps_from_aggregates(now)
            await self._refresh_banner()

    async def on_message_create(self, event: MessageCreateEvent):
        now = self._now()
        channel = event.channel.name
        data = event.message.data or {}

        if channel == self.command_channel:
            if self.role == model.ROLE_PUMP:
                await self._handle_reply(data)
            return
        if channel == "ui_cmds":
            # Buttons and inputs are handled by the ui handlers below.
            return

        if self.role == model.ROLE_AUTO_FILL:
            await self._refresh_fill(now)
            return

        touched = False
        for pump in self.pumps:
            running = self._read(pump["state_path"], channel, data)
            fault = self._read(pump["fault_path"], channel, data)
            if running is None and fault is None:
                continue
            await self._update_pump(pump, running, fault, now)
            touched = True
        if touched:
            await self._refresh_banner()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _now() -> float:
        return time.time() * 1000

    def _tag(self, key: str, default=None):
        return self.get_tag(key, default)

    async def _set(self, key: str, value):
        if self.get_tag(key) != value:
            await self.set_tag(key, value)

    @staticmethod
    def _read(path, channel: str, data: dict) -> bool | None:
        """A pump input from a message: None when this message doesn't carry it."""
        if path is None or path[0] != channel:
            return None
        value = model.extract_path(data, path[1])
        if model.is_missing(value):
            return None
        reading = model.as_bool(value)
        if reading is None:
            log.warning(
                "Unreadable value %r at $%s.%s", value, channel, ".".join(path[1])
            )
        return reading

    async def _channel_aggregate(
        self, channel: str, agent_id: int | None = None
    ) -> dict:
        key = f"{agent_id or ''}/{channel}"
        if key not in self._aggregates:
            agg = await self.api.fetch_channel_aggregate(channel, agent_id=agent_id)
            self._aggregates[key] = getattr(agg, "data", None) or {}
        return self._aggregates[key]

    def _device_name(self, device_id) -> str:
        device_map = (self.received_deployment_config or {}).get("DEVICE_MAP") or {}
        entry = device_map.get(str(device_id)) or {}
        return entry.get("display_name") or entry.get("name") or f"device {device_id}"

    # ------------------------------------------------------------------ pump role

    async def _refresh_pumps_from_aggregates(self, now: float):
        """Re-read each pump's inputs from its channel's aggregate (deploy and
        schedule), and move run hours and command timeouts on."""
        for pump in self.pumps:
            running = fault = None
            for which in ("state_path", "fault_path"):
                path = pump[which]
                if path is None:
                    continue
                try:
                    data = await self._channel_aggregate(path[0])
                except Exception as e:  # no channel yet: nothing reported
                    log.info("No aggregate on %s yet: %s", path[0], e)
                    continue
                value = self._read(path, path[0], data)
                if which == "state_path":
                    running = value
                else:
                    fault = value
            await self._update_pump(pump, running, fault, now)

    async def _update_pump(
        self, pump: dict, running: bool | None, fault: bool | None, now: float
    ):
        t = pump["target"]
        stats = {f: self._tag(model.tag_key(t, f)) for f in STAT_FIELDS}
        prev_running = stats.get("running")
        stats = model.observe(stats, running, now, self.tz)
        for f in STAT_FIELDS:
            await self._set(model.tag_key(t, f), stats.get(f))

        current = stats.get("running")
        await self._set(model.tag_key(t, "state"), model.state_text(current))
        if fault is not None:
            await self._set(model.tag_key(t, "fault"), fault)
            await self._set(model.tag_key(t, "fault_hidden"), not fault)

        pending = self._tag(model.tag_key(t, "pending"))
        after, text, warn = model.evaluate_pending(
            pending, current, now, self.confirm_timeout_s, self.tz
        )
        if pending and not after and not warn and pending.get("command") == "start":
            duration = pending.get("duration_s")
            sent_at = float(pending.get("sent_at") or now)
            await self._set(
                model.tag_key(t, "ends_at"),
                int(sent_at + duration * 1000) if duration else None,
            )
        await self._set(model.tag_key(t, "pending"), after)
        if text is not None:
            await self._set(model.tag_key(t, "cmd_text"), text)
        if warn:
            await self._set(model.tag_key(t, "cmd_warning"), text)
            await self._set(model.tag_key(t, "cmd_warning_hidden"), False)

        if current is not True and prev_running is True:
            await self._set(model.tag_key(t, "ends_at"), None)
        elif current is not True:
            ends_at = self._tag(model.tag_key(t, "ends_at"))
            if ends_at is not None:
                await self._set(model.tag_key(t, "ends_at"), None)

    async def _handle_reply(self, data: dict):
        """An edge reply on the Command Channel: {"request_id", "ok", "error"}."""
        if "command" in data or "request_id" not in data or "ok" not in data:
            return
        request_id = data.get("request_id")
        for pump in self.pumps:
            t = pump["target"]
            if self._tag(model.tag_key(t, "last_request_id")) != request_id:
                continue
            pending = self._tag(model.tag_key(t, "pending"))
            label = model.command_label(
                (pending or {}).get("command")
                or self._tag(model.tag_key(t, "last_command")),
                (pending or {}).get("duration_s"),
            )
            if data.get("ok"):
                if pending:
                    pending = {**pending, "ack": True}
                    await self._set(model.tag_key(t, "pending"), pending)
                    _, text, _ = model.evaluate_pending(
                        pending,
                        self._tag(model.tag_key(t, "running")),
                        self._now(),
                        self.confirm_timeout_s,
                        self.tz,
                    )
                    if text:
                        await self._set(model.tag_key(t, "cmd_text"), text)
            else:
                text = f"{label} rejected by the controller: {data.get('error') or 'no reason given'}"
                await self._set(model.tag_key(t, "pending"), None)
                await self._set(model.tag_key(t, "cmd_text"), text)
                await self._set(model.tag_key(t, "cmd_warning"), text)
                await self._set(model.tag_key(t, "cmd_warning_hidden"), False)
            return

    async def _send_command(self, pump: dict, verb: str, duration_s: int | None):
        now = self._now()
        t = pump["target"]
        cmd = model.build_command(verb, t, duration_s, self.app_key, now)
        await self.api.create_message(self.command_channel, cmd)
        log.info("Sent %s", cmd)

        pending = {
            "command": verb,
            "request_id": cmd["request_id"],
            "sent_at": cmd["ts"],
            "duration_s": duration_s,
        }
        want = model.STATE_RUNNING if verb == "start" else model.STATE_STOPPED
        text = (
            f"{model.command_label(verb, duration_s)} sent, waiting for the pump "
            f"to report {want}"
        )
        await self._set(model.tag_key(t, "pending"), pending)
        await self._set(model.tag_key(t, "last_request_id"), cmd["request_id"])
        await self._set(model.tag_key(t, "last_command"), verb)
        await self._set(model.tag_key(t, "cmd_text"), text)
        await self._set(model.tag_key(t, "cmd_warning_hidden"), True)

    def _pump_for(self, method: str, suffix: str) -> dict:
        s = method[len("p_") : -len(suffix)]
        pump = self.pumps_by_slug.get(s)
        if pump is None:
            raise RPCError("unknown_pump", f"No pump {s!r} is configured.")
        return pump

    @ui.handler(re.compile(r"^p_\w+_start$"), auto_update=False)
    async def on_start(self, ctx, _value):
        await ctx.set_value(None, log_update=False)
        pump = self._pump_for(ctx.method, "_start")
        minutes = None
        if pump["mode"] == model.MODE_TIMED:
            try:
                minutes = self.ui_manager.get_value(f"p_{pump['slug']}_minutes")
            except KeyError:
                minutes = None
        try:
            duration_s = model.start_duration_s(
                pump["mode"], minutes, pump["default_run_min"], pump["max_run_min"]
            )
        except ValueError as e:
            await self._set(model.tag_key(pump["target"], "cmd_text"), f"Not sent: {e}")
            raise RPCError("invalid_duration", str(e))
        await self._send_command(pump, "start", duration_s)

    @ui.handler(re.compile(r"^p_\w+_stop$"), auto_update=False)
    async def on_stop(self, ctx, _value):
        await ctx.set_value(None, log_update=False)
        pump = self._pump_for(ctx.method, "_stop")
        await self._send_command(pump, "stop", None)

    async def _publish_pump_settings(self, now: float):
        settings = model.pump_settings(
            self.pumps, self.app_key, self.command_channel, now
        )
        await self._publish_settings(settings)

    async def _publish_settings(self, settings: dict):
        try:
            await self.api.update_channel_aggregate(
                self.settings_channel,
                {self.app_key: settings},
                replace_keys=[self.app_key],
            )
        except Exception as e:
            log.error("Failed to publish settings to %s: %s", self.settings_channel, e)

    async def _refresh_banner(self):
        """Pump role: 'Auto-fill: controlled by <device>' with its fill status."""
        device_id = self.config.auto_fill_device.value
        if not device_id:
            await self._set("af_banner", None)
            return
        name = self._device_name(device_id)
        app_key = (self.config.auto_fill_app_key.value or "").strip()
        text = f"Controlled by {name}"
        if app_key:
            try:
                tags = (
                    await self._channel_aggregate("tag_values", int(device_id))
                ).get(app_key) or {}
            except Exception as e:
                log.warning("Can't read tag_values on %s: %s", device_id, e)
                text += " (its status can't be read: add it under Devices in this app's config)"
            else:
                enabled = tags.get("af_enabled")
                target = tags.get("af_pump_target")
                if enabled is None:
                    text += " (no status reported yet)"
                elif not enabled:
                    text += ": Disabled"
                else:
                    pumps = (
                        [self.pumps_by_slug.get(model.slug(target))]
                        if target
                        else self.pumps
                    )
                    running = [
                        self._tag(model.tag_key(p["target"], "running"))
                        for p in pumps
                        if p
                    ]
                    if any(r is True for r in running):
                        state = model.FILL_FILLING
                    elif running and all(r is False for r in running):
                        state = model.FILL_IDLE
                    else:
                        state = model.FILL_UNKNOWN
                    text += f": Enabled, {state}"
        await self._set("af_banner", text)

    # ------------------------------------------------------------------ auto-fill role

    def _fill_config(self) -> dict:
        cfg = self.config
        return {
            "enabled": bool(cfg.auto_fill_enabled.value),
            "start": float(cfg.start_level.value),
            "stop": float(cfg.stop_level.value),
            "max_shift_min": float(cfg.max_shift_min.value),
        }

    def _fill_values(self) -> dict:
        """The operator settings in force: tags, falling back to config."""
        conf = self._fill_config()
        enabled = self.tags.af_enabled.value
        start = _as_number(self.tags.af_start.value)
        stop = _as_number(self.tags.af_stop.value)
        shift = _as_number(self.tags.af_max_shift_min.value)
        return {
            "enabled": conf["enabled"] if enabled is None else bool(enabled),
            "start": conf["start"] if start is None else start,
            "stop": conf["stop"] if stop is None else stop,
            "max_shift_min": conf["max_shift_min"] if shift is None else shift,
        }

    async def _init_fill_settings(self, now: float):
        """On deployment: a changed config resets the operator settings to it;
        an unchanged one keeps what operators set. Then show them on the page."""
        conf = self._fill_config()
        sig = hashlib.sha1(json.dumps(conf, sort_keys=True).encode()).hexdigest()[:12]
        if self.tags.af_config_sig.value != sig:
            await self.tags.af_enabled.set(conf["enabled"])
            await self.tags.af_start.set(conf["start"])
            await self.tags.af_stop.set(conf["stop"])
            await self.tags.af_max_shift_min.set(conf["max_shift_min"])
            await self.tags.af_config_sig.set(sig)
        await self.tags.af_pump_target.set(
            (self.config.pump_target.value or "").strip() or None
        )
        values = self._fill_values()
        for key, value in (
            ("af_enabled", values["enabled"]),
            ("af_start", values["start"]),
            ("af_stop", values["stop"]),
            ("af_max_shift", values["max_shift_min"]),
        ):
            try:
                await self.ui_manager.set_value(key, value, log_update=False)
            except Exception as e:
                log.warning("Couldn't show %s on the page: %s", key, e)

    async def _publish_fill_settings(self, now: float, updated_by: str):
        cfg = self.config
        values = self._fill_values()
        settings = model.auto_fill_settings(
            app_key=self.app_key,
            enabled=values["enabled"],
            start_level=values["start"],
            stop_level=values["stop"],
            max_shift_min=values["max_shift_min"],
            level_app_key=(cfg.level_source.app_name.value or "").strip(),
            level_tag=(cfg.level_source.tag_name.value or "").strip(),
            pump_device_id=cfg.pump_device.value,
            pump_app_key=(cfg.pump_app_key.value or "").strip(),
            pump_target=(cfg.pump_target.value or "").strip(),
            updated_by=updated_by,
            now_ms=now,
        )
        await self._publish_settings(settings)

    async def _read_level(self) -> float | None:
        app_key = (self.config.level_source.app_name.value or "").strip()
        tag = (self.config.level_source.tag_name.value or "").strip()
        if not app_key or not tag:
            return None
        value = self.tag_manager.get_tag(tag, app_key=app_key)
        if value is None:
            try:
                value = (
                    (await self._channel_aggregate("tag_values")).get(app_key) or {}
                ).get(tag)
            except Exception as e:
                log.info("Couldn't read tag_values: %s", e)
        return _as_number(value)

    async def _refresh_fill(self, now: float):
        cfg = self.config
        values = self._fill_values()
        device_id = cfg.pump_device.value
        app_key = (cfg.pump_app_key.value or "").strip()
        target = (cfg.pump_target.value or "").strip()

        running = last_started = last_stopped = None
        pump_name = None
        warning = ""
        if device_id and app_key and target:
            device = self._device_name(device_id)
            try:
                tags = (
                    await self._channel_aggregate("tag_values", int(device_id))
                ).get(app_key) or {}
            except Exception as e:
                log.warning("Can't read tag_values on %s: %s", device_id, e)
                warning = (
                    f"Can't read the pump on {device}: add it under Devices in this "
                    "app's config"
                )
                tags = {}
            running = tags.get(model.tag_key(target, "running"))
            running = running if isinstance(running, bool) else None
            last_started = _as_number(tags.get(model.tag_key(target, "last_started")))
            last_stopped = _as_number(tags.get(model.tag_key(target, "last_stopped")))
            pump_name = f"{device}: {tags.get(model.tag_key(target, 'name')) or target}"
        elif device_id or app_key:
            warning = (
                "Pump link incomplete: set Pump Device, Pump App Key and Pump Target ID"
            )

        await self.tags.af_pump_name.set(pump_name)
        await self.tags.af_pump_running.set(running)
        await self.tags.af_pump_state.set(model.state_text(running))
        await self.tags.af_state.set(model.fill_state(values["enabled"], running))
        await self.tags.af_warning.set(warning)
        await self.tags.af_warning_hidden.set(not warning)

        track = {
            "fill_active": self.tags.af_fill_active.value,
            "fill_started": self.tags.af_fill_started.value,
            "fill_ended": self.tags.af_fill_ended.value,
            "shift_notified": self.tags.af_shift_notified.value,
            "overfill_since": self.tags.af_overfill_since.value,
            "overfill_notified": self.tags.af_overfill_notified.value,
        }
        level = await self._read_level()
        track, due = model.evaluate_fill(
            track,
            enabled=values["enabled"],
            pump_running=running,
            pump_last_started=last_started,
            pump_last_stopped=last_stopped,
            level=level,
            stop_level=values["stop"],
            max_shift_min=values["max_shift_min"],
            now_ms=now,
        )
        await self.tags.af_fill_active.set(track.get("fill_active"))
        await self.tags.af_fill_started.set(track.get("fill_started"))
        await self.tags.af_fill_ended.set(track.get("fill_ended"))
        await self.tags.af_shift_notified.set(track.get("shift_notified"))
        await self.tags.af_overfill_since.set(track.get("overfill_since"))
        await self.tags.af_overfill_notified.set(track.get("overfill_notified"))

        for name in due:
            if name == "shift_exceeded":
                msg = (
                    f"Auto-fill has run more than {values['max_shift_min']:g} min "
                    f"and {pump_name or 'the pump'} is still running."
                )
                await self.notifications.shift_exceeded.send(msg)
            elif name == "stop_level_pump_running":
                msg = (
                    f"Level {level:.1f} % is at or above Stop ({values['stop']:g} %) "
                    f"but {pump_name or 'the pump'} is still running."
                )
                await self.notifications.stop_level_pump_running.send(msg)

    async def _operator_change(self, ctx, key: str, value) -> None:
        """Apply one operator edit of the fill settings, or reject it."""
        if self.role != model.ROLE_AUTO_FILL:
            raise RPCError("wrong_role", "This install is not in the Auto-fill role.")
        now = self._now()
        values = self._fill_values()
        new = dict(values)
        if key == "af_enabled":
            new["enabled"] = bool(value)
        else:
            number = _as_number(value)
            field = {
                "af_start": "start",
                "af_stop": "stop",
                "af_max_shift": "max_shift_min",
            }[key]
            new[field] = number
            error = model.validate_fill_settings(
                new["start"], new["stop"], new["max_shift_min"]
            )
            if error:
                await ctx.set_value(values[field], log_update=False)
                raise RPCError("invalid_setting", error)

        await self.tags.af_enabled.set(new["enabled"])
        await self.tags.af_start.set(new["start"])
        await self.tags.af_stop.set(new["stop"])
        await self.tags.af_max_shift_min.set(new["max_shift_min"])
        field_value = {
            "af_enabled": new["enabled"],
            "af_start": new["start"],
            "af_stop": new["stop"],
            "af_max_shift": new["max_shift_min"],
        }[key]
        await ctx.set_value(field_value)
        await self._publish_fill_settings(now, updated_by="operator")
        await self._refresh_fill(now)

    @ui.handler("af_enabled", auto_update=False)
    async def on_fill_enabled(self, ctx, value):
        await self._operator_change(ctx, "af_enabled", value)

    @ui.handler("af_start", auto_update=False)
    async def on_fill_start(self, ctx, value):
        await self._operator_change(ctx, "af_start", value)

    @ui.handler("af_stop", auto_update=False)
    async def on_fill_stop(self, ctx, value):
        await self._operator_change(ctx, "af_stop", value)

    @ui.handler("af_max_shift", auto_update=False)
    async def on_fill_max_shift(self, ctx, value):
        await self._operator_change(ctx, "af_max_shift", value)
