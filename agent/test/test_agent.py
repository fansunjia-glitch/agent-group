"""Offline protocol, lifecycle and real Comm integration regressions."""

from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from agent import Agent
from agent.cron_scheduler import CRON_TIMEZONE
from agent.history import ContextBudgetError, HistoryStore


def chunk(text=None, calls=None, reason=None, finish=None):
    return NS(choices=[NS(delta=NS(content=text, tool_calls=calls, reasoning_content=reason),
                          finish_reason=finish)])


def tool_part(index, name=None, arguments=None, identifier=None):
    return NS(index=index, id=identifier, function=NS(name=name, arguments=arguments))


def done(text="done"):
    return [chunk(text=text), chunk(finish="stop")]


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        for item in self.chunks:
            yield item() if callable(item) else item

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.streams = []
        self.closed = False
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        stream = FakeStream(self.responses.pop(0))
        self.streams.append(stream)
        return stream

    def close(self):
        self.closed = True


class AgentTests(unittest.TestCase):
    def node(self, responses=(), **kwargs):
        directory = tempfile.TemporaryDirectory(prefix="agent-tests-")
        self.addCleanup(directory.cleanup)
        client = FakeClient(responses)
        with patch("agent.agent.OpenAI", return_value=client) as constructor:
            node = Agent(api_key="test-key", workdir=directory.name,
                         host="127.0.0.1", enable_memory=False, **kwargs)
        self.addCleanup(node.close)
        return node, client, constructor

    def test_constructor_uses_explicit_deepseek_settings_and_owns_resources(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "wrong", "OPENAI_BASE_URL": "http://wrong.invalid"}):
            node, client, constructor = self.node()
        constructor.assert_called_once_with(api_key="test-key", base_url="https://api.deepseek.com",
                                            timeout=30.0, max_retries=0)
        self.assertEqual(node.model, "deepseek-flash")
        self.assertTrue(all(tool["type"] == "function" and "parameters" in tool["function"]
                            for tool in node.tools))
        node.close()
        node.close()
        self.assertTrue(client.closed)
        self.assertTrue(node.comm._closed)
        self.assertEqual(node.logger.handlers, [])

    def test_fragmented_parallel_tool_calls_and_reasoning_are_replayed(self):
        first = [
            chunk(reason="reasoning fragment"),
            chunk(calls=[tool_part(1, "write_file", '{"path":"result.txt",', "file"),
                         tool_part(0, "create_topic", '{"name":', "topic")]),
            chunk(calls=[tool_part(1, arguments='"content":"内容"}'),
                         tool_part(0, arguments='"讨论","description":"说明"}')]),
            chunk(finish="tool_calls"),
        ]
        node, client, _ = self.node([first, done()], thinking=True)
        node.messages = [{"role": "user", "content": "work"}]
        output = StringIO()
        with redirect_stdout(output):
            node._run_turn()
        self.assertEqual((node.workdir / "result.txt").read_text(), "内容")
        self.assertEqual(len(client.requests), 2)
        self.assertNotIn("reasoning fragment", output.getvalue())
        second_messages = client.requests[1]["messages"]
        assistant = next(message for message in second_messages if message.get("tool_calls"))
        self.assertEqual(assistant["reasoning_content"], "reasoning fragment")
        self.assertEqual([call["id"] for call in assistant["tool_calls"]], ["topic", "file"])
        results = [message for message in second_messages if message["role"] == "tool"]
        self.assertEqual([result["tool_call_id"] for result in results], ["topic", "file"])
        self.assertEqual(client.requests[0]["extra_body"],
                         {"thinking": {"type": "enabled"}, "user_id": node.user_id})
        self.assertTrue(all(stream.closed for stream in client.streams))
        self.assertEqual(HistoryStore(node.workdir).load(), node.messages)

    def test_stopping_during_stream_prevents_tools_and_closes_the_node(self):
        node, client, _ = self.node()
        def request_stop():
            node.stop()
            return chunk(calls=[tool_part(0, "write_file", '{"path":"never","content":"x"}', "call")])
        client.responses = [[request_stop, chunk(finish="tool_calls")]]
        node.run()
        self.assertFalse((node.workdir / "never").exists())
        self.assertEqual(len(client.requests), 1)
        self.assertTrue(client.streams[0].closed)
        self.assertTrue(client.closed)
        self.assertTrue(node.comm._closed)
        self.assertEqual(node.state, "stopped")

    def test_stop_between_tools_records_cancelled_results_without_another_request(self):
        response = [chunk(calls=[tool_part(0, "stop_here", "{}", "stop"),
                                tool_part(1, "write_file", '{"path":"never","content":"x"}', "skip")]),
                    chunk(finish="tool_calls")]
        node, client, _ = self.node([response])
        node.handlers["stop_here"] = node.stop
        node.run()
        self.assertFalse((node.workdir / "never").exists())
        self.assertEqual(len(client.requests), 1)
        saved = HistoryStore(node.workdir).load()
        self.assertEqual([message["tool_call_id"] for message in saved if message["role"] == "tool"],
                         ["stop", "skip"])
        self.assertIn("not executed", saved[-1]["content"])

    def test_keyboard_interrupt_during_tool_batch_leaves_loadable_history(self):
        response = [chunk(calls=[tool_part(0, "interrupt", "{}", "interrupted")]), chunk(finish="tool_calls")]
        node, _, _ = self.node([response])
        def interrupt():
            raise KeyboardInterrupt
        node.handlers["interrupt"] = interrupt
        node.run()
        saved = HistoryStore(node.workdir).load()
        self.assertEqual(saved[-1]["role"], "tool")
        self.assertIn("interrupted", saved[-1]["content"])

    def test_incomplete_tool_stream_is_never_executed(self):
        response = [chunk(calls=[tool_part(0, "write_file", '{"path":', "partial")]), chunk(finish="length")]
        node, client, _ = self.node([response])
        with self.assertRaisesRegex(RuntimeError, "Incomplete model response"):
            node.run()
        self.assertFalse(any(message.get("tool_calls") for message in node.messages))
        self.assertTrue(client.closed)
        self.assertTrue(node.comm._closed)

    def test_tool_errors_are_returned_with_matching_call_id(self):
        node, _, _ = self.node()
        for args in ("invalid JSON", "[]", '{"path":"a"}'):
            with self.subTest(arguments=args):
                result = node._execute_tool({"id": "call", "function": {"name": "write_file", "arguments": args}})
                self.assertEqual(result["role"], "tool")
                self.assertEqual(result["tool_call_id"], "call")
                self.assertIn("error", json.loads(result["content"]))

    def test_new_topic_message_interrupts_generation_without_executing_partial_tools(self):
        node, client, _ = self.node([[chunk(text="partial", reason="partial reason"), chunk(finish="stop")]], thinking=True)
        node.messages = [{"role": "user", "content": "original"}]
        incoming = [{"topic_id": "topic", "messages": [{"sender_id": "peer", "payload": ' { "x": 1 }\n'}]}]
        with patch.object(node.topic_manager, "read_messages", return_value=incoming), redirect_stdout(StringIO()):
            self.assertIsNone(node._stream_response(node.messages))
        self.assertTrue(client.streams[0].closed)
        self.assertEqual(node.messages[-2]["reasoning_content"], "partial reason")
        encoded = node.messages[-1]["content"].split("New topic messages:\n", 1)[1]
        self.assertEqual(json.loads(encoded), incoming)

    def test_real_comm_peers_discover_join_and_deliver_original_payload(self):
        first, _, _ = self.node()
        second, _, _ = self.node()
        topic = first.topic_manager.create_topic("agent integration", "discovery description")
        self.assertIn({"topic_id": topic, "name": "agent integration", "description": "discovery description"},
                      second.topic_manager.discover_topics())
        second.topic_manager.join_topic(topic)
        payload = ' { "text": "原始消息" }\n'
        first.comm.send(topic, payload)
        self.assertTrue(second._collect_events())
        incoming = json.loads(second.messages[-1]["content"].split("\n", 1)[1])
        self.assertEqual(incoming[0]["messages"], [{"sender_id": first.agent_id, "payload": payload}])
        self.assertEqual(first.comm.receive(topic), [])
        self.assertFalse(second._collect_events())

    def test_idle_does_not_call_model_again_and_stop_releases_resources(self):
        node, client, _ = self.node([done()], idle_interval=0.01)
        errors = []
        def run():
            try:
                node.run()
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=run, daemon=True)
        with redirect_stdout(StringIO()):
            worker.start()
            try:
                deadline = time.monotonic() + 3
                while node.state != "idle" or not client.requests:
                    if time.monotonic() > deadline:
                        self.fail("agent never entered idle")
                    time.sleep(0.01)
                self.assertEqual(len(client.requests), 1)
            finally:
                node.stop()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(node.comm._closed)
        self.assertTrue(client.closed)

    def test_failed_comm_initialization_closes_model_client(self):
        client = FakeClient([])
        with tempfile.TemporaryDirectory() as directory:
            with patch("agent.agent.OpenAI", return_value=client), patch("agent.agent.Comm", side_effect=OSError("bind failed")):
                with self.assertRaisesRegex(OSError, "bind failed"):
                    Agent(api_key="test", workdir=directory)
        self.assertTrue(client.closed)

    def test_pending_cron_jobs_are_not_acknowledged_when_the_batch_exceeds_budget(self):
        node, client, _ = self.node()
        first = node.scheduler.schedule_job("* * * * *", "large task " * 1000, recurring=False)
        second = node.scheduler.schedule_job("* * * * *", "small task", recurring=False)
        node.scheduler.poll_due_jobs(datetime(2026, 1, 1, tzinfo=CRON_TIMEZONE))
        node.max_context_chars = 7000
        with self.assertRaises(ContextBudgetError):
            node._run_turn()
        self.assertEqual(client.requests, [])
        self.assertEqual(set(node.scheduler.scheduled_jobs), {first.id, second.id})
        self.assertTrue(all(job.pending_delivery for job in node.scheduler.scheduled_jobs.values()))

    def test_one_shot_cron_is_acknowledged_after_a_complete_model_response(self):
        node, client, _ = self.node([done()])
        job = node.scheduler.schedule_job("* * * * *", "scheduled work", recurring=False)
        node.scheduler.poll_due_jobs(datetime(2026, 1, 1, tzinfo=CRON_TIMEZONE))
        with redirect_stdout(StringIO()):
            node._run_turn()
        self.assertIn(job.id, json.dumps(client.requests[0]["messages"]))
        self.assertNotIn(job.id, node.scheduler.scheduled_jobs)
        self.assertFalse(node._collect_events())

    def test_stop_during_memory_extraction_does_not_start_consolidation(self):
        node, _, _ = self.node([done()])
        def extract(messages):
            node.stop()
            return 1
        memory = NS(load_memories=Mock(return_value=""), build_system=lambda system, recalled: system,
                    extract_memories=Mock(side_effect=extract), consolidate_memories=Mock())
        node.memory = memory
        with redirect_stdout(StringIO()):
            node.run()
        memory.extract_memories.assert_called_once()
        memory.consolidate_memories.assert_not_called()

    def test_import_has_no_workspace_or_client_side_effects(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            code = f"import sys; sys.path.insert(0, {str(root)!r}); import agent; import agent.agent; print(agent.Agent.__name__)"
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(("AGENT_", "ANTHROPIC_", "OPENAI_", "DEEPSEEK_"))}
            completed = subprocess.run([sys.executable, "-c", code], cwd=directory,
                                       env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "Agent")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_launcher_rejects_missing_explicit_key_even_when_env_has_one(self):
        import launch_agent
        with patch.object(launch_agent, "API_KEY", ""), patch.object(launch_agent, "Agent") as constructor:
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "not-used"}), self.assertRaises(SystemExit):
                launch_agent.main()
            constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
