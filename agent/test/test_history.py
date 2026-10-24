from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent.file_tools import TOOLS as FILE_TOOLS
from agent.history import ContextBudgetError, HistoryStore, build_context
from agent.topic_tools import TOOLS as TOPIC_TOOLS


def tool_turn(label="current"):
    return [
        {"role": "user", "content": label},
        {
            "role": "assistant", "content": None, "reasoning_content": "tool decision",
            "tool_calls": [
                {"id": "first", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a"}'}},
                {"id": "second", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"b"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "first", "content": "a contents"},
        {"role": "tool", "tool_call_id": "second", "content": "b contents"},
        {"role": "assistant", "content": "finished"},
    ]


def request_size(messages, system="system", tools=None):
    return len(json.dumps(
        {"messages": [{"role": "system", "content": system}, *messages], "tools": tools or []},
        ensure_ascii=False, separators=(",", ":"),
    ))


class HistoryTests(unittest.TestCase):
    def test_stores_are_isolated_and_construction_does_not_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = HistoryStore(root / "one"), HistoryStore(root / "two")
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(first.load(), [])
            first.save(tool_turn())
            self.assertEqual(first.load(), tool_turn())
            self.assertEqual(second.load(), [])
            second.save([{"role": "user", "content": "独立消息"}])
            self.assertEqual(first.load(), tool_turn())
            self.assertFalse(first.path.with_suffix(".tmp").exists())

    def test_load_migrates_anthropic_tool_calls_and_results(self):
        legacy = [
            {"role": "user", "content": "original task"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "reading"},
                {"type": "tool_use", "id": "call-1", "name": "read_file", "input": {"path": "a"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call-1", "content": "unchanged\n  payload"},
                {"type": "text", "text": "follow-up message"},
            ]},
        ]
        with TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory))
            store.save(legacy)
            converted = store.load()
            self.assertEqual([item["role"] for item in converted], ["user", "assistant", "tool", "user"])
            self.assertEqual(converted[1]["content"], "reading")
            call = converted[1]["tool_calls"][0]
            self.assertEqual(call["id"], "call-1")
            self.assertEqual(call["function"]["name"], "read_file")
            self.assertEqual(json.loads(call["function"]["arguments"]), {"path": "a"})
            self.assertEqual(converted[2]["content"], "unchanged\n  payload")
            self.assertEqual(converted[3]["content"], "follow-up message")
            # Loading alone does not overwrite the original snapshot.
            self.assertEqual(json.loads(store.path.read_text()), legacy)
            store.save(converted)
            self.assertEqual(store.load(), converted)

    def test_context_trims_complete_old_turns_without_mutating_history(self):
        old = [{"role": "user", "content": "x" * 2000}, {"role": "assistant", "content": "old answer"}]
        recent = tool_turn()
        history = old + recent
        snapshot = deepcopy(history)
        context = build_context(history, "system", [], request_size(recent))
        self.assertEqual(context, [{"role": "system", "content": "system"}, *recent])
        self.assertEqual(history, snapshot)
        # Request construction must not alias nested tool-call objects either.
        context[2]["tool_calls"][0]["function"]["arguments"] = "changed"
        self.assertEqual(history, snapshot)

    def test_context_keeps_latest_input_after_dropping_whole_tool_turn(self):
        recent = [{"role": "user", "content": "next task"}]
        context = build_context(tool_turn() + recent, "system", [], request_size(recent))
        self.assertEqual(context[1:], recent)

    def test_pending_topic_and_cron_inputs_cannot_trim_each_other(self):
        pending = [
            {"role": "user", "content": "topic message " + "x" * 100},
            {"role": "user", "content": "scheduled task " + "y" * 100},
        ]
        context = build_context(tool_turn() + pending, "system", [], request_size(pending))
        self.assertEqual(context[1:], pending)
        with self.assertRaises(ContextBudgetError):
            build_context(tool_turn() + pending, "system", [], request_size(pending[-1:]))

    def test_interrupted_cron_and_new_topic_remain_one_pending_turn(self):
        pending = [
            {"role": "user", "content": "[Scheduled cron_1] original task"},
            {"role": "assistant", "content": "partial reply", "_interrupted": True},
            {"role": "user", "content": "new topic message"},
        ]
        public = deepcopy(pending)
        public[1].pop("_interrupted")
        context = build_context(tool_turn() + pending, "system", [], request_size(public))
        self.assertEqual(context[1:], public)
        self.assertTrue(pending[1]["_interrupted"])
        self.assertNotIn("_interrupted", json.dumps(context))
        with self.assertRaises(ContextBudgetError):
            build_context(tool_turn() + pending, "system", [], request_size(public[-1:]))
        with TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory))
            store.save(pending)
            self.assertEqual(store.load(), pending)

    def test_tool_response_without_final_answer_does_not_end_pending_turn(self):
        pending = tool_turn()[:-1] + [{"role": "user", "content": "new message during work"}]
        context = build_context(pending, "system", [], request_size(pending))
        self.assertEqual(context[1:], pending)
        with self.assertRaises(ContextBudgetError):
            build_context(pending, "system", [], request_size(pending[-1:]))

    def test_context_budget_includes_system_and_tool_definitions(self):
        messages = [{"role": "user", "content": "hello"}]
        tools = FILE_TOOLS + TOPIC_TOOLS
        exact = request_size(messages, tools=tools)
        self.assertEqual(build_context(messages, "system", tools, exact)[1:], messages)
        with self.assertRaises(ContextBudgetError):
            build_context(messages, "system", tools, exact - 1)
        with self.assertRaises(ContextBudgetError):
            build_context([], "long system", tools, 20)

    def test_oversized_latest_turn_is_rejected_instead_of_dropped(self):
        history = [{"role": "user", "content": "old"}, {"role": "user", "content": "x" * 300}]
        with self.assertRaisesRegex(ContextBudgetError, "latest turn"):
            build_context(history, "system", [], 200)
        self.assertEqual(len(history[-1]["content"]), 300)

    def test_invalid_tool_history_is_rejected(self):
        examples = [
            [{"role": "tool", "tool_call_id": "orphan", "content": "result"}],
            tool_turn()[:2],
            tool_turn()[:3] + [{"role": "user", "content": "missing second result"}],
            tool_turn()[:4] + [tool_turn()[2]],
        ]
        for messages in examples:
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                build_context(messages, "system", [], 10000)

    def test_unsupported_legacy_blocks_are_not_silently_discarded(self):
        messages = [{"role": "user", "content": [{"type": "image", "source": "unsupported"}]}]
        with TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory))
            store.save(messages)
            with self.assertRaisesRegex(ValueError, "Unsupported history content block"):
                store.load()

    def test_tool_schemas_use_openai_function_format(self):
        tools = FILE_TOOLS + TOPIC_TOOLS
        names = {tool["function"]["name"] for tool in tools}
        self.assertIn("discover_topics", names)
        self.assertIn("write_file", names)
        for tool in tools:
            self.assertEqual(tool["type"], "function")
            self.assertEqual(tool["function"]["parameters"]["type"], "object")
            self.assertNotIn("input_schema", tool)


if __name__ == "__main__":
    unittest.main()
