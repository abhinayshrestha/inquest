"""Process lifecycle signal shared by the runners.

A task cancelled because the user cancelled the turn records it as cancelled. A task
cancelled because the backend is shutting down must not: its turn and assignments stay
running with their checkpoints, so recovery resumes them after the restart.
"""

_shutting_down = False


def begin_shutdown() -> None:
    global _shutting_down
    _shutting_down = True


def reset() -> None:
    global _shutting_down
    _shutting_down = False


def shutting_down() -> bool:
    return _shutting_down
