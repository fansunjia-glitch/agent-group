"""Exercise the real OpenAI SDK against a local Chat Completions SSE server."""

from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from agent import Agent
from agent.test.quiet_comm import QuietComm, local_model_network


def chunk(delta, finish_reason=None):
    return {
        "id": "chatcmpl-local-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


@contextmanager
def model_server(responses):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            # Keep only protocol payloads, never authorization headers.
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            index = len(requests) - 1
            if self.path != "/chat/completions" or index >= len(responses):
                self.send_error(400, "Unexpected model request")
                return
            events = "".join(
                f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                for event in responses[index]
            ) + "data: [DONE]\n\n"
            payload = events.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if thread.is_alive():
            raise AssertionError("Local model HTTP server did not stop")


class ModelHTTPTests(unittest.TestCase):
    def setUp(self):
        network = local_model_network()
        network.start()
        self.addCleanup(network.stop)
        communication = patch("agent.agent.Comm", QuietComm)
        communication.start()
        self.addCleanup(communication.stop)

    def test_real_sdk_reassembles_tool_and_reasoning_deltas_and_replays_results(self):
        responses = [
            [
                chunk({"role": "assistant", "reasoning_content": "inspect "}),
                chunk({"reasoning_content": "workspace"}),
                chunk({"tool_calls": [{
                    "index": 0, "id": "call_write_1", "type": "function",
                    "function": {"name": "write_", "arguments": '{"path":"'},
                }]}),
                chunk({"tool_calls": [{
                    "index": 0,
                    "function": {"name": "file", "arguments": 'answer.txt","content":"hello '},
                }]}),
                chunk({"tool_calls": [{
                    "index": 0, "function": {"arguments": '世界"}'},
                }]}),
                chunk({}, "tool_calls"),
            ],
            [
                chunk({"role": "assistant", "content": "File "}),
                chunk({"content": "saved."}),
                chunk({}, "stop"),
            ],
        ]
        with TemporaryDirectory(prefix="agent-model-http-") as directory:
            with model_server(responses) as (base_url, requests):
                output = StringIO()
                with Agent(
                    api_key="test", base_url=base_url, host="127.0.0.1",
                    enable_memory=False, thinking=True, api_mode="deepseek", workdir=directory,
                    request_timeout=2.0,
                ) as node:
                    node._append_user("Write hello 世界 to answer.txt")
                    with redirect_stdout(output):
                        node._run_turn()
                    self.assertEqual((Path(directory) / "answer.txt").read_text(), "hello 世界")
                    self.assertEqual(output.getvalue(), "File saved.\n")
                    self.assertEqual([path for path, _ in requests], ["/chat/completions"] * 2)
                    for _, body in requests:
                        self.assertEqual(body["model"], "deepseek-flash")
                        self.assertEqual(body["thinking"], {"type": "enabled"})
                        self.assertTrue(body["stream"])
                        self.assertTrue(body["tools"])
                        for tool in body["tools"]:
                            self.assertEqual(tool["type"], "function")
                            self.assertIn("parameters", tool["function"])
                            self.assertNotIn("input_schema", tool["function"])

                    second_messages = requests[1][1]["messages"]
                    assistant = next(message for message in second_messages if message.get("tool_calls"))
                    self.assertEqual(assistant["reasoning_content"], "inspect workspace")
                    call = assistant["tool_calls"][0]
                    self.assertEqual(call["id"], "call_write_1")
                    self.assertEqual(call["function"]["name"], "write_file")
                    self.assertEqual(json.loads(call["function"]["arguments"]), {
                        "path": "answer.txt", "content": "hello 世界",
                    })
                    results = [message for message in second_messages if message["role"] == "tool"]
                    self.assertEqual(len(results), 1)
                    self.assertEqual(results[0]["tool_call_id"], call["id"])
                    self.assertIn("Wrote", results[0]["content"])
                    self.assertEqual(node.messages[-1]["content"], "File saved.")
                self.assertTrue(node.client.is_closed())
                self.assertTrue(node.comm._closed)
                self.assertTrue(node.scheduler._closed)
                self.assertEqual(node.logger.handlers, [])

    def test_real_sdk_sends_disabled_thinking_explicitly(self):
        responses = [[chunk({"role": "assistant", "content": "Ready"}), chunk({}, "stop")]]
        with TemporaryDirectory(prefix="agent-model-http-") as directory:
            with model_server(responses) as (base_url, requests):
                with Agent(
                    api_key="test", base_url=base_url, host="127.0.0.1",
                    enable_memory=False, thinking=False, api_mode="deepseek", workdir=directory,
                    request_timeout=2.0,
                ) as node:
                    node._append_user("Say Ready")
                    with redirect_stdout(StringIO()):
                        node._run_turn()
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0][1]["thinking"], {"type": "disabled"})
                    self.assertEqual(node.messages[-1], {"role": "assistant", "content": "Ready"})
                self.assertTrue(node.client.is_closed())
                self.assertTrue(node.comm._closed)


if __name__ == "__main__":
    unittest.main()
