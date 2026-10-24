"""Persist the current conversation snapshot and restore it at startup."""

import json
import os
from pathlib import Path


WORKDIR = Path(os.getenv("AGENT_WORKDIR", Path(__file__).resolve().parent)).resolve()
HISTORY_PATH = WORKDIR / ".logs" / "history.json"


def _serialize(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    raise TypeError(f"Cannot persist {type(value).__name__} in conversation history")


def save_history(messages: list):
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = HISTORY_PATH.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps(messages, ensure_ascii=False, indent=2, default=_serialize),
            encoding="utf-8",
        )
        temporary.replace(HISTORY_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def load_history() -> list:
    if not HISTORY_PATH.exists():
        return []
    messages = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    if not isinstance(messages, list) or any(
        not isinstance(message, dict)
        or message.get("role") not in {"user", "assistant"}
        or not isinstance(message.get("content"), (str, list))
        for message in messages
    ):
        raise ValueError(f"Invalid conversation history: {HISTORY_PATH}")
    return messages
