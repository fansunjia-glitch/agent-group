"""Generic provider compatibility against the real SDK, with no Comm discovery."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from openai import OpenAI

from agent import Agent
from agent.model_config import resolve_api_mode
from agent.memory import MemoryStore
from agent.test.quiet_comm import QuietComm, local_model_network
from agent.test.test_model_http import chunk, model_server
from agent.test.test_user_identity import model_capture


class ModelProviderTests(unittest.TestCase):
    def setUp(self):
        network = local_model_network()
        network.start()
        self.addCleanup(network.stop)
        communication = patch("agent.agent.Comm", QuietComm)
        communication.start()
        self.addCleanup(communication.stop)

    def test_generic_stream_and_all_memory_operations_use_standard_identity_and_configured_model(self):
        with TemporaryDirectory() as directory, model_capture() as (url, requests):
            with Agent(api_key="provider-test-key", base_url=url, model="vendor/custom-model",
                       profile_id="resident_1", workdir=directory, thinking=True) as node:
                self.assertEqual(node.api_mode, "openai")
                self.assertEqual(node.memory.api_mode, "openai")
                self.assertIs(node.memory.client, node.client)
                self.assertEqual(node.memory.model, "vendor/custom-model")
                with redirect_stdout(StringIO()):
                    node._generate_response([{"role": "user", "content": "Hello"}])
                for index in range(10):
                    node.memory.write_memory_file(f"Fact {index}", "project", f"Stable fact {index}", f"Fact {index}")
                self.assertTrue(node.memory.load_memories([{"role": "user", "content": "Recall facts"}]))
                node.memory.extract_memories([{"role": "user", "content": "Remember a new stable fact"}])
                node.memory.consolidate_memories()
                self.assertEqual([body["stream"] for body in requests], [True, False, False, False])
                for body in requests:
                    self.assertEqual(body["model"], "vendor/custom-model")
                    self.assertEqual(body["user"], "agent_resident_1")
                    self.assertNotIn("thinking", body)
                    self.assertNotIn("user_id", body)
                    self.assertNotIn("extra_body", body)

    def test_generic_tool_stream_strips_reasoning_from_replay_without_mutating_saved_history(self):
        responses = [
            [chunk({"reasoning_content": "Private provider reasoning"}),
             chunk({"tool_calls": [{"index": 0, "id": "write", "type": "function", "function": {
                 "name": "write_file", "arguments": '{"path":"saved.txt","content":"Saved"}'}}]}),
             chunk({}, "tool_calls")],
            [chunk({"content": "Ready"}), chunk({}, "stop")],
        ]
        with TemporaryDirectory() as directory, model_server(responses) as (url, requests):
            with Agent(api_key="provider-test-key", base_url=url, model="custom-model",
                       api_mode="openai", enable_memory=False, workdir=directory) as node:
                node.messages = [
                    {"role": "user", "content": "Old request"},
                    {"role": "assistant", "content": "Old reply", "reasoning_content": "Old DeepSeek reasoning"},
                    {"role": "user", "content": "Save a file"},
                ]
                with redirect_stdout(StringIO()):
                    node._run_turn()
                self.assertEqual(len(requests), 2)
                self.assertEqual((Path(directory) / "saved.txt").read_text(), "Saved")
                self.assertEqual(node.messages[1]["reasoning_content"], "Old DeepSeek reasoning")
                for _, body in requests:
                    self.assertNotIn("thinking", body)
                    self.assertNotIn("user_id", body)
                    self.assertTrue(all("reasoning_content" not in message for message in body["messages"]))
                assistant = next(m for m in requests[1][1]["messages"] if m.get("tool_calls"))
                result = next(m for m in requests[1][1]["messages"] if m.get("role") == "tool")
                self.assertEqual(assistant["tool_calls"][0]["id"], result["tool_call_id"])
                self.assertNotIn("reasoning_content", node.messages[-1])

    def test_generic_memory_store_infers_client_endpoint_and_reuses_workspace_identity(self):
        with TemporaryDirectory() as directory, model_capture() as (url, requests):
            with OpenAI(api_key="provider-test-key", base_url=url) as client:
                memory = MemoryStore(Path(directory), client, "deepseek-served-via-generic-api")
                self.assertEqual(memory.api_mode, "openai")
                memory.extract_memories([{"role": "user", "content": "Remember"}])
                self.assertEqual(requests[0]["user"], memory.user_id)
                self.assertNotIn("thinking", requests[0])
                self.assertNotIn("user_id", requests[0])

    def test_api_mode_inference_is_exact_and_overridable(self):
        for url in ["https://api.deepseek.com", "https://api.deepseek.com/v1/"]:
            self.assertEqual(resolve_api_mode(None, url), "deepseek")
        for url in ["https://api.openai.com/v1", "https://api.deepseek.com.attacker.invalid", "http://localhost:1234/v1"]:
            self.assertEqual(resolve_api_mode(None, url), "openai")
        self.assertEqual(resolve_api_mode("openai", "https://api.deepseek.com"), "openai")
        self.assertEqual(resolve_api_mode("deepseek", "https://proxy.invalid/v1"), "deepseek")
        with self.assertRaisesRegex(ValueError, "api_mode"):
            resolve_api_mode("unsupported", "http://localhost:1234")
