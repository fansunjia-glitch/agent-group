"""Topic history retains the entire delivered chat payload, including long text."""

from io import StringIO
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent import Agent
from agent.admin import sign_notice
from agent.test.test_agent import FakeClient
from comm import Comm
from comm.shared_memory import SharedMemoryTransport
from control import worker


class FullTopicMessageTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="full-topic-message-")
        self.addCleanup(self.directory.cleanup)
        self.events = []
        with patch("agent.agent.OpenAI", return_value=FakeClient([])):
            self.node = Agent(api_key="offline-key", profile_id="resident", host="127.0.0.1",
                              workdir=Path(self.directory.name), enable_memory=False,
                              event_sink=self.events.append)
        self.addCleanup(self.node.close)
        self.peer = Comm(host="127.0.0.1")
        self.addCleanup(self.peer.close)

    def test_sent_and_received_events_keep_near_inbox_capacity_payload(self):
        topic = self.node.topic_manager.create_topic("Full transcript")
        self.peer.join_topic(topic)
        payload = "x" * (SharedMemoryTransport.INBOX_BYTES - 2048) + "\n末尾 END"
        self.node.topic_manager.send(topic, payload)
        sent = next(event for event in reversed(self.events) if event["type"] == "message_sent")
        self.assertEqual(sent["payload"], payload)
        self.assertFalse(sent["truncated"])
        self.assertEqual(self.peer.receive(topic)[0].payload, payload)
        self.peer.send(topic, payload)
        groups = self.node._read_topics()
        self.assertEqual(groups[0]["messages"][0]["payload"], payload)
        received = next(event for event in reversed(self.events) if event["type"] == "message_received")
        self.assertEqual(received["payload"], payload)
        self.assertFalse(received["truncated"])

    def test_authenticated_long_notice_keeps_entire_body_without_signature(self):
        topic = self.peer.create_topic("管理员通知")
        secret = "offline-notice-signature-secret"
        self.node.invite_admin_topic(topic, self.peer.agent_id, secret)
        self.node._read_topics()
        body = "关于周末活动的安排。\n" * 600 + "最后一条说明。"
        notice = {"kind": "admin.notice", "notice_id": "long-notice", "targets": ["resident"], "content": body}
        notice["signature"] = sign_notice(notice, secret)
        self.peer.send(topic, json.dumps(notice, ensure_ascii=False))
        groups = self.node._read_topics()
        self.assertEqual(groups[0]["_admin_notice"]["content"], body)
        received = next(event for event in reversed(self.events) if event["type"] == "message_received")
        self.assertEqual(received["payload"], body)
        self.assertFalse(received["truncated"])
        self.assertNotIn(secret, json.dumps(self.events))
        self.assertNotIn(notice["signature"], json.dumps(self.events))


class FullWorkerEventTests(unittest.TestCase):
    def test_stdout_keeps_large_escaped_payload_and_redacts_credentials(self):
        # The supervisor line cap must accommodate escaped characters, not just
        # the original plain-text length. No model or network calls are made.
        key = "test-worker-secret-value"
        payload = ("\x01\n\\\"中文" * 80000) + key + "正文末尾"
        with TemporaryDirectory(prefix="full-worker-event-") as directory:
            config = {"api_key": key, "workdir": directory}
            output = StringIO()

            class EventOnlyAgent:
                state = "idle"
                logger = logging.Logger("event-only-worker")

                def __init__(self, *, event_sink, **_):
                    self.emit = event_sink

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    pass

                def stop(self):
                    pass

                def run(self):
                    self.emit({"type": "message_sent", "topic_id": "topic", "payload": payload,
                               "truncated": False})

            stdin = StringIO(json.dumps({"type": "start", "config": config}) + "\n")
            with patch.object(worker, "Agent", EventOnlyAgent), patch.object(worker.sys, "stdin", stdin), \
                    patch.object(worker.sys, "stdout", output), patch.object(worker.sys, "argv", ["worker"]), \
                    patch.object(worker.signal, "signal"):
                worker.main()
            lines = output.getvalue().splitlines()
            message = next(json.loads(line) for line in lines if json.loads(line)["type"] == "message_sent")
            self.assertNotIn(key, output.getvalue())
            self.assertEqual(message["payload"], payload.replace(key, "[redacted]"))
            self.assertFalse(message["truncated"])
            self.assertTrue(message["payload"].endswith("正文末尾"))
            self.assertGreater(len(lines[0]), 1048576)


if __name__ == "__main__":
    unittest.main()
