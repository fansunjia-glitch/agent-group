"""Conservative recovery of clipped chat bodies from saved successful tool calls.

Runtime snapshots may contain more text than old control events, but they are
not an independent message ledger: attempts can fail, and snapshots have no
per-call timestamps. Only repair existing send events with unambiguous text
evidence. Never manufacture messages or deduplicate repeated sends.
"""

from collections import Counter, defaultdict
from copy import deepcopy
import json
from pathlib import Path
import re


def _successful_sends(messages):
    """Yield (call ID, topic ID, payload) only for completed send -> null pairs."""
    if not isinstance(messages, list):
        return
    for index, message in enumerate(messages):
        if (not isinstance(message, dict) or message.get("role") != "assistant"
                or message.get("_interrupted")):
            continue
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list):
            continue
        counts = Counter(call.get("id") for call in calls
                         if isinstance(call, dict) and isinstance(call.get("id"), str))
        results = defaultdict(list)
        result_index = index + 1
        while result_index < len(messages):
            result = messages[result_index]
            if not isinstance(result, dict) or result.get("role") != "tool":
                break
            identifier = result.get("tool_call_id")
            if isinstance(identifier, str):
                results[identifier].append(result.get("content"))
            result_index += 1
        for call in calls:
            if not isinstance(call, dict):
                continue
            identifier, function = call.get("id"), call.get("function")
            if (not isinstance(identifier, str) or not identifier or counts[identifier] != 1
                    or call.get("type") != "function" or not isinstance(function, dict)
                    or function.get("name") != "send"):
                continue
            outputs = results.get(identifier, [])
            if len(outputs) != 1 or not isinstance(outputs[0], str):
                continue
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                continue
            try:
                # TopicManager.send returns None. Error, stopped and interrupted
                # executions return an error object, not the JSON value null.
                if json.loads(outputs[0]) is not None:
                    continue
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                continue
            if (isinstance(arguments, dict) and isinstance(arguments.get("topic_id"), str)
                    and arguments["topic_id"] and isinstance(arguments.get("payload"), str)):
                yield identifier, arguments["topic_id"], arguments["payload"]


def recover_truncated_sends(events, workspace_root):
    """Return copied events and recovery counts without changing any source file.

    A body is repaired only when successful saved sends for the same profile
    and topic identify exactly one longer body with the recorded prefix. If the
    event carries a call ID, it must match too. Different possible suffixes stay
    explicitly truncated. Repeated equal sends are safe because event IDs,
    timestamps and multiplicity are never changed.
    """
    result = deepcopy(list(events))
    summary = {"checked": 0, "recovered": 0, "ambiguous": 0,
               "no_match": 0, "unavailable": 0, "files_read": 0}
    root = Path(workspace_root).resolve()
    histories = {}
    for event in result:
        if not isinstance(event, dict) or event.get("type") != "message_sent" or not event.get("truncated"):
            continue
        summary["checked"] += 1
        profile_id, topic_id, prefix = event.get("profile_id"), event.get("topic_id"), event.get("payload")
        if (not isinstance(profile_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", profile_id)
                or not isinstance(topic_id, str) or not isinstance(prefix, str) or not prefix):
            summary["unavailable"] += 1
            continue
        if profile_id not in histories:
            path = root / profile_id / ".logs" / "history.json"
            try:
                if not path.resolve().is_relative_to(root):
                    raise ValueError("History is outside the managed workspace")
                messages = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(messages, list):
                    raise ValueError("History must be a message list")
                histories[profile_id] = list(_successful_sends(messages))
                summary["files_read"] += 1
            except (OSError, ValueError, UnicodeError):
                histories[profile_id] = None
        candidates = histories[profile_id]
        if candidates is None:
            summary["unavailable"] += 1
            continue
        possible = {payload for call_id, topic, payload in candidates
                    if topic == topic_id and payload.startswith(prefix) and len(payload) > len(prefix)
                    and (not event.get("call_id") or event["call_id"] == call_id)}
        if len(possible) == 1:
            event.update(payload=possible.pop(), truncated=False, recovered=True,
                         recovery_source="agent_history")
            summary["recovered"] += 1
        else:
            summary["ambiguous" if possible else "no_match"] += 1
    return result, summary
