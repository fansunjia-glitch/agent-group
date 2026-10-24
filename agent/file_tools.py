"""LCC s02 file tools, restricted to one agent's configured workspace."""

import fnmatch
import os
import stat
from pathlib import Path, PureWindowsPath


MAX_OUTPUT = 50000
MAX_MATCHES = 200


class FileTools:
    def __init__(self, workdir):
        # Capture the boundary once; changes to cwd never change permissions.
        self.workdir = Path(workdir).resolve()
        self.handlers = {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
            "glob": self.glob,
        }

    def safe_path(self, path: str) -> Path:
        if not isinstance(path, str) or not path or "\0" in path:
            raise ValueError("Path must be a non-empty string")
        candidate = Path(path)
        if os.name == "nt":
            # Reject device namespaces, drive-relative paths and NTFS streams.
            windows = PureWindowsPath(path)
            if path.replace("/", "\\").startswith(("\\\\?\\", "\\\\.\\")) or (windows.drive and not windows.is_absolute()):
                raise ValueError("Device and drive-relative paths are not allowed")
            if any(":" in part or PureWindowsPath(part).is_reserved()
                   for part in windows.parts if part != windows.anchor):
                raise ValueError("Device names and alternate data streams are not allowed")
        resolved = (self.workdir / candidate).resolve()
        if not resolved.is_relative_to(self.workdir):
            raise ValueError(f"Path escapes workspace: {path}")
        if resolved.exists():
            info = resolved.stat()
            if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
                raise ValueError("Only regular files and directories are allowed")
            # A hard link can alias a file outside the workspace without changing
            # its resolved path. Reject linked files rather than guessing targets.
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise ValueError("Hard-linked files are not allowed")
        return resolved

    def read_file(self, path: str, limit: int | None = None) -> str:
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("limit must be a positive integer")
        lines = self.safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:MAX_OUTPUT]

    def write_file(self, path: str, content: str) -> str:
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        size = len(content.encode("utf-8"))
        target = self.safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = self.safe_path(path)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {size} bytes to {path}"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        if not old_text:
            raise ValueError("old_text must not be empty")
        target = self.safe_path(path)
        text = target.read_text(encoding="utf-8")
        if old_text not in text:
            raise ValueError(f"Text not found in {path}")
        self.safe_path(path).write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"

    def glob(self, pattern: str) -> str:
        if not isinstance(pattern, str) or not pattern or "\0" in pattern:
            raise ValueError("pattern must be a non-empty relative glob")
        normalized = pattern.replace("\\", "/") if os.name == "nt" else pattern
        parts = Path(normalized).parts
        if (Path(normalized).is_absolute() or PureWindowsPath(normalized).drive
                or ".." in parts or not parts):
            raise ValueError("Glob patterns must stay inside the workspace")
        if os.name == "nt" and ":" in normalized:
            raise ValueError("Alternate data streams are not allowed")
        matches = set()
        visited = set()

        def walk(directory, index):
            if len(matches) > MAX_MATCHES or (directory, index) in visited:
                return
            visited.add((directory, index))
            segment = parts[index]
            if segment == "**" and index + 1 < len(parts):
                walk(directory, index + 1)
            # Do not traverse links and then filter results: that would already
            # enumerate directories outside the workspace. Prune before descent.
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
            for entry in children:
                if len(matches) > MAX_MATCHES:
                    break
                info = entry.stat(follow_symlinks=False)
                if (stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & 0x400
                        or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1)):
                    continue
                if entry.name.startswith(".") and not segment.startswith("."):
                    continue
                if segment != "**" and not fnmatch.fnmatchcase(
                    os.path.normcase(entry.name), os.path.normcase(segment),
                ):
                    continue
                target = self.safe_path(entry.path)
                if stat.S_ISDIR(info.st_mode):
                    if segment == "**":
                        walk(target, index)
                    elif index + 1 < len(parts):
                        walk(target, index + 1)
                elif stat.S_ISREG(info.st_mode) and index == len(parts) - 1:
                    matches.add(target.relative_to(self.workdir).as_posix())

        walk(self.safe_path("."), 0)
        shown = sorted(matches)[:MAX_MATCHES]
        if len(matches) > MAX_MATCHES:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"


TOOLS = [
    {"name": "read_file", "description": "Read a UTF-8 file inside your workspace; limit is a line count.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write a UTF-8 file inside your workspace, creating parent directories.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace the first exact occurrence of old_text in a workspace file.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "old_text": {"type": "string", "minLength": 1},
         "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find workspace files by relative pattern, e.g. **/*.py. Returns up to 200 paths; skips links.",
     "input_schema": {"type": "object", "properties": {
         "pattern": {"type": "string"}}, "required": ["pattern"]}},
]
