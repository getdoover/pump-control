from pydoover.processor import run_app

from .application import PumpControlApplication


def handler(event, context):
    """Lambda handler entry point."""
    return run_app(
        PumpControlApplication(),
        event,
        context,
    )
