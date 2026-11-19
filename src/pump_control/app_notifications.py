from pathlib import Path

from pydoover import notifications as n


class PumpControlNotifications(n.Notifications):
    shift_exceeded = n.Notification(
        "An auto-fill has run longer than its max shift",
        description=(
            "The pump filling this storage has run past the Max Shift set for "
            "auto-fill and is still running."
        ),
        severity=n.NotificationSeverity.Warn,
    )
    stop_level_pump_running = n.Notification(
        "The level has reached Stop but the pump is still running",
        description=(
            "The storage is at or above its Stop level and the pump filling it "
            "still reports running two minutes later."
        ),
        severity=n.NotificationSeverity.Warn,
    )


def export():
    PumpControlNotifications.export(
        Path(__file__).parents[2] / "doover_config.json", "pump_control"
    )
