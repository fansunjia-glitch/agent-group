"""Per-agent history and bounded OpenAI chat context snapshots."""

from copy import deepcopy
import json
from pathlib import Path


class ContextBudgetError(ValueError):
    """The current conversation cannot fit the configured character budget."""


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
        isinstance(block, dict) and block.get("type") == "text"
        and isinstance(block.get("text"), str) for block in content
    ):
        return "\n".join(block["text"] for block in content)
    raise ValueError("History contains unsupported non-text content")


def _normalise(messages: list[dict]) -> list[dict]:
    """Convert the old text/tool block format without discarding tool results."""
    if not isinstance(messages, list):
        raise ValueError("History must be a list of messages")
    converted = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("History messages must be objects")
        role = message.get("role")
        content = message.get("content")
        if role == "tool":
            converted.append({
                "role": "tool", "tool_call_id": message.get("tool_call_id"),
                "content": _text(content),
            })
            continue
        if role not in ("user", "assistant"):
            raise ValueError(f"Unsupported history role: {role}")
        if isinstance(content, list):
            text = []
            calls = []
            results = []
            for block in content:
                if not isinstance(block, dict):
                    raise ValueError("History content blocks must be objects")
                kind = block.get("type")
                if kind == "text" and isinstance(block.get("text"), str):
                    text.append(block["text"])
                elif role == "assistant" and kind == "tool_use":
                    calls.append({
                        "id": block.get("id"), "type": "function",
                        "function": {
                            "name": block.get("name"),
                            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                        },
                    })
                elif role == "user" and kind == "tool_result":
                    results.append({
                        "role": "tool", "tool_call_id": block.get("tool_use_id"),
                        "content": _text(block.get("content", "")),
                    })
                else:
                    raise ValueError(f"Unsupported history content block: {kind}")
            if role == "user":
                converted.extend(results)
                if text or not results:
                    converted.append({"role": "user", "content": "\n".join(text)})
                continue
            if calls and message.get("tool_calls"):
                raise ValueError("History mixes two tool-call formats")
            content = "\n".join(text) or None
        else:
            calls = []
            if not isinstance(content, str) and not (role == "assistant" and content is None):
                raise ValueError("History text must be a string")
        item = {"role": role, "content": content}
        if role == "assistant":
            calls = calls or deepcopy(message.get("tool_calls", []))
            if calls:
                item["tool_calls"] = calls
            if "_interrupted" in message:
                if type(message["_interrupted"]) is not bool:
                    raise ValueError("History _interrupted must be a boolean")
                item["_interrupted"] = message["_interrupted"]
            reasoning = message.get("reasoning_content")
            if reasoning is not None:
                if not isinstance(reasoning, str):
                    raise ValueError("History reasoning_content must be a string")
                item["reasoning_content"] = reasoning
        converted.append(item)
    _validate_tool_pairs(converted)
    return converted


def _validate_tool_pairs(messages: list[dict]) -> None:
    pending = set()
    for message in messages:
        role = message["role"]
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise ValueError("History contains an orphan or duplicate tool result")
            pending.remove(call_id)
            continue
        if pending:
            raise ValueError("History tool calls are missing results before the next message")
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list):
            raise ValueError("History tool_calls must be a list")
        for call in calls:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise ValueError("History contains an unsupported tool call")
            call_id = call.get("id")
            function = call.get("function")
            if not isinstance(call_id, str) or not call_id or call_id in pending:
                raise ValueError("History contains an invalid or duplicate tool-call ID")
            if (
                not isinstance(function, dict)
                or not isinstance(function.get("name"), str) or not function["name"]
                or not isinstance(function.get("arguments"), str)
            ):
                raise ValueError("History contains an invalid function call")
            pending.add(call_id)
    if pending:
        raise ValueError("History ends with tool calls that have no results")


class HistoryStore:
    def __init__(self, workdir: Path):
        self.path = Path(workdir).resolve() / ".logs" / "history.json"

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return _normalise(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, messages: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)


def build_context(
    messages: list[dict], system: str, tools: list[dict], max_chars: int,
) -> list[dict]:
    """Keep recent complete user turns; count system and tools in the budget.

    The budget is serialized Unicode characters, not provider tokens. The full
    history is never mutated. Tool calls and their results stay in one turn.
    """
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if not isinstance(system, str):
        raise TypeError("system must be a string")
    normalised = _normalise(messages)
    system_message = {"role": "system", "content": system}

    def public_messages(context):
        return [
            {key: value for key, value in message.items() if key != "_interrupted"}
            for message in context
        ]

    def size(context):
        return len(json.dumps(
            {"messages": [system_message, *public_messages(context)], "tools": tools},
            ensure_ascii=False, separators=(",", ":"),
        ))

    if size([]) > max_chars:
        raise ContextBudgetError("System prompt and tools exceed the context character budget")
    groups = []
    answered = False
    for message in normalised:
        # Several freshly collected topic/cron events can precede one model
        # response. They form one pending turn and must never trim each other.
        if not groups or (message["role"] == "user" and answered):
            groups.append([])
            answered = False
        groups[-1].append(message)
        if message["role"] == "assistant":
            answered = not message.get("tool_calls") and not message.get("_interrupted")
    if not groups:
        return [system_message]
    selected = groups[-1]
    if size(selected) > max_chars:
        raise ContextBudgetError(
            "The latest turn exceeds the context character budget; "
            "shorten its input or increase max_context_chars"
        )
    for group in reversed(groups[:-1]):
        candidate = group + selected
        if size(candidate) > max_chars:
            break
        selected = candidate
    return [system_message, *public_messages(selected)]
