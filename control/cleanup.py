"""Bounded cleanup of files owned by the control service.

Workspace locks also protect agents left running after a control-server crash.
Never resolve a supplied workspace through a symlink into another directory.
"""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
from uuid import uuid4


class CleanupBlocked(Exception):
    pass


def _directory(path):
    if path.is_symlink():
        raise CleanupBlocked(f"Refusing cleanup through a symbolic link: {path.name}")
    if path.exists() and not path.is_dir():
        raise CleanupBlocked(f"Managed directory is not a directory: {path.name}")


@contextmanager
def locked_workspace(root, profile_id):
    """Hold the same exclusive lock as the local worker until cleanup finishes."""
    if not isinstance(profile_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", profile_id):
        raise CleanupBlocked("Invalid managed workspace identifier")
    base = Path(root) / "workspaces"
    workspace = base / profile_id
    _directory(base)
    _directory(workspace)
    _directory(Path(root) / "logs")
    if not workspace.exists():
        yield workspace
        return
    lock_path = workspace / ".control.lock"
    if lock_path.is_symlink():
        raise CleanupBlocked("Refusing cleanup through a symbolic workspace lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CleanupBlocked("Managed workspace lock is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CleanupBlocked("Agent workspace is still in use; stop the local worker before deleting it") from None
        # Check again after opening the lock so a substituted parent cannot
        # turn a stale path check into recursive deletion of a foreign tree.
        _directory(base)
        _directory(workspace)
        if os.stat(lock_path, follow_symlinks=False).st_ino != os.fstat(fd).st_ino:
            raise CleanupBlocked("Managed workspace changed during cleanup; retry")
        yield workspace
    finally:
        os.close(fd)


def remove_profile_files(root, profile_id, workspace):
    if workspace.exists():
        shutil.rmtree(workspace)
    logs = Path(root) / "logs"
    _directory(logs)
    for path in logs.glob(f"{profile_id}-*.log"):
        if path.is_dir() and not path.is_symlink():
            raise CleanupBlocked("Managed run log unexpectedly contains a directory")
        path.unlink(missing_ok=True)


def scrub_event_logs(root, matches):
    """Rewrite every rotation atomically; malformed unrelated lines survive."""
    for path in sorted(Path(root).glob("events*.jsonl")):
        if path.is_symlink() or not path.is_file():
            raise CleanupBlocked("Refusing cleanup of a non-regular event log")
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        changed = False
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as target, path.open("r", encoding="utf-8") as source:
                for line in source:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        event = None
                    if isinstance(event, dict) and matches(event):
                        changed = True
                        continue
                    target.write(line)
                target.flush()
                os.fsync(target.fileno())
            if changed:
                os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
