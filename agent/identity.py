"""Stable, opaque model-request identities shared by dialogue and memory calls."""

import fcntl
import os
from pathlib import Path
import re
from uuid import uuid4


def validate_user_id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,512}", value) is None:
        raise ValueError("user_id must contain 1–512 ASCII letters, digits, hyphens or underscores")
    return value


def profile_user_id(profile_id: str) -> str:
    """Managed profile IDs are opaque UUIDs, independent of display names."""
    return validate_user_id(f"agent_{profile_id}")


def resolve_user_id(workdir: Path, *, user_id: str | None = None,
                    profile_id: str | None = None) -> str:
    if user_id is not None:
        return validate_user_id(user_id)
    if profile_id is not None:
        return profile_user_id(profile_id)
    # Standalone Agent/MemoryStore instances reuse an anonymous workspace ID.
    # A short file lock also makes simultaneous first reads choose one identity.
    workdir.mkdir(parents=True, exist_ok=True)
    fd = os.open(workdir / ".agent-user-id", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="ascii") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        raw = stream.read(514)
        if len(raw) > 513:
            raise ValueError("Stored user_id exceeds its maximum length")
        value = raw.strip()
        if value:
            return validate_user_id(value)
        value = "agent_" + uuid4().hex
        stream.seek(0)
        stream.write(value + "\n")
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
        return value
