"""Callbacks at fixed points in the loop, following learn-claude-code s04."""

HOOKS = {
    "BeforeModelCall": [],
    "ModelInterrupted": [],
    "AfterModelResponse": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "AfterToolBatch": [],
    "AfterTurn": [],
    "Stop": [],
}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None
