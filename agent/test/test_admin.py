"""Administrator enrollment, signed broadcasts and runtime event integration."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent import Agent
from agent.admin import sign_notice, verify_notice
from agent.history import HistoryStore
from agent.test.test_agent import FakeClient, chunk, done, tool_part
from comm import Comm


class AdminTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="agent-admin-")
        self.addCleanup(directory.cleanup)
        self.workdir = Path(directory.name)
        self.events = []
        self.admin = Comm(host="127.0.0.1")
        self.addCleanup(self.admin.close)
        self.topic = self.admin.create_topic("管理员通知", "测试管理员频道")
        self.secret = "test-only-enrollment-secret"

    def node(self, **kwargs):
        client = FakeClient(kwargs.pop("responses", ()))
        with patch("agent.agent.OpenAI", return_value=client):
            node = Agent(api_key="test-key", profile_id="profile-worker",
                         workdir=self.workdir, host="127.0.0.1",
                         enable_memory=False, event_sink=kwargs.pop("event_sink", self.events.append),
                         **kwargs)
        self.addCleanup(node.close)
        return node, client

    def enroll(self, node):
        node.invite_admin_topic(self.topic, self.admin.agent_id, self.secret)
        self.assertNotIn(self.topic, node.topic_manager.joined_topics)
        self.assertFalse(node._collect_events())
        self.assertIn(self.topic, node.topic_manager.joined_topics)
        self.assertTrue(any(event["type"] == "admin_joined" for event in self.events))

    def envelope(self, **updates):
        notice = {"kind": "admin.notice", "notice_id": "notice_001",
                  "content": "完成当前步骤后，优先处理测试任务。", "targets": ["profile-worker"]}
        notice.update(updates)
        notice["signature"] = sign_notice(notice, self.secret)
        return notice

    def send(self, notice, sender=None, topic=None):
        (sender or self.admin).send(topic or self.topic, json.dumps(notice, ensure_ascii=False))

    def test_canonical_signature_and_mutation(self):
        notice = self.envelope()
        self.assertTrue(verify_notice(dict(reversed(list(notice.items()))), self.secret))
        notice["content"] = "changed"
        self.assertFalse(verify_notice(notice, self.secret))
        self.assertFalse(verify_notice({"kind": "admin.notice"}, self.secret))

    def test_invitation_is_queued_persona_and_receipts_follow_history_commit(self):
        persisted_at_receipt = []

        def record(event):
            self.events.append(event)
            if event["type"] == "notice_applied":
                persisted_at_receipt.extend(HistoryStore(self.workdir).load())

        node, _ = self.node(persona="你是一名谨慎的研究员。", event_sink=record)
        self.assertIn("你是一名谨慎的研究员。", node._system_prompt())
        self.enroll(node)
        self.send(self.envelope())
        self.assertTrue(node._collect_events())
        self.assertIn("[Administrator notice=notice_001]", node.messages[-1]["content"])
        self.assertEqual(persisted_at_receipt[-1], node.messages[-1])
        kinds = [event["type"] for event in self.events]
        self.assertLess(kinds.index("notice_received"), kinds.index("notice_applied"))
        self.assertNotIn(self.secret, json.dumps(self.events))
        self.assertNotIn("signature", node.messages[-1]["content"])

    def test_rejects_other_sender_bad_signature_and_other_target(self):
        node, _ = self.node()
        self.enroll(node)
        attacker = Comm(host="127.0.0.1")
        self.addCleanup(attacker.close)
        attacker.join_topic(self.topic)
        self.send(self.envelope(), sender=attacker)
        self.send({**self.envelope(), "signature": "0" * 64})
        self.send(self.envelope(targets=["some-other-profile"]))
        self.assertFalse(node._collect_events())
        self.assertEqual(node.messages, [])
        self.assertFalse(any(event["type"] == "notice_received" for event in self.events))

    def test_duplicate_and_restart_do_not_reapply(self):
        node, _ = self.node()
        self.enroll(node)
        notice = self.envelope()
        self.send(notice)
        self.assertTrue(node._collect_events())
        self.send(notice)
        self.assertFalse(node._collect_events())
        self.assertEqual(len(node.messages), 1)
        self.assertTrue(self.events[-1]["duplicate"])
        node.close()
        restored, _ = self.node()
        self.enroll(restored)
        self.send(notice)
        self.assertFalse(restored._collect_events())
        self.assertEqual(len(restored.messages), 1)
        self.assertTrue(self.events[-1]["duplicate"])

    def test_model_cannot_send_or_leave_admin_topic_and_replacement_is_runtime_owned(self):
        node, _ = self.node()
        self.enroll(node)
        with self.assertRaises(PermissionError):
            node.topic_manager.handlers["send"](self.topic, "spoofed")
        with self.assertRaises(PermissionError):
            node.topic_manager.handlers["leave_topic"](self.topic)
        newer = self.admin.create_topic("新管理员频道")
        node.invite_admin_topic(newer, self.admin.agent_id, self.secret)
        node._collect_events()
        self.assertEqual(node.topic_manager.admin_topic_id, newer)
        self.assertNotIn(self.topic, node.topic_manager.joined_topics)
        with self.assertRaises(PermissionError):
            node.topic_manager.join_topic(self.topic)
        business = node.topic_manager.create_topic("普通协作")
        node.topic_manager.send(business, "ordinary")
        node.topic_manager.leave_topic(business)
        self.assertTrue(any(e["type"] == "message_sent" for e in self.events))

    def test_invitation_during_generation_and_notice_interrupts_partial_tool_call(self):
        node, client = self.node()

        def invite():
            node.invite_admin_topic(self.topic, self.admin.agent_id, self.secret)
            return chunk(text="partial", calls=[tool_part(0, "write_file", '{"path":', "incomplete")])

        def publish():
            self.send(self.envelope())
            return chunk(reason="private reasoning")

        client.responses = [[invite, publish, chunk(finish="stop")]]
        node.messages = [{"role": "user", "content": "original task"}]
        with redirect_stdout(StringIO()):
            self.assertIsNone(node._stream_response(node.messages))
        self.assertTrue(client.streams[0].closed)
        self.assertEqual(node.messages[-1]["role"], "user")
        self.assertIn("[Administrator notice=notice_001]", node.messages[-1]["content"])
        self.assertFalse(any(message.get("tool_calls") for message in node.messages))
        self.assertEqual(HistoryStore(self.workdir).load(), node.messages)
        self.assertNotIn("private reasoning", json.dumps(self.events))
        self.assertEqual(self.events[-1]["type"], "model_finished")
        self.assertEqual(self.events[-1]["status"], "interrupted")

    def test_duplicate_during_generation_does_not_interrupt(self):
        node, client = self.node()
        self.enroll(node)
        self.send(self.envelope())
        node._collect_events()
        self.send(self.envelope())
        client.responses = [done("completed")]
        with redirect_stdout(StringIO()):
            response = node._stream_response(node.messages)
        self.assertEqual(response["content"], "completed")
        self.assertEqual(len(node.messages), 1)

    def test_observer_failure_does_not_break_agent_or_business_tool(self):
        def fail(event):
            raise RuntimeError("observer disconnected")

        node, client = self.node(event_sink=fail, responses=[done()])
        topic = node.topic_manager.create_topic("business")
        node.topic_manager.send(topic, "still works")
        with redirect_stdout(StringIO()):
            self.assertEqual(node._stream_response([])["content"], "done")

    def test_bad_invitation_reports_failure_without_replacing_valid_channel(self):
        node, _ = self.node()
        self.enroll(node)
        node.invite_admin_topic("missing-topic", self.admin.agent_id, self.secret)
        self.assertFalse(node._collect_events())
        self.assertEqual(node.topic_manager.admin_topic_id, self.topic)
        self.assertEqual(self.events[-1]["type"], "admin_join_failed")


if __name__ == "__main__":
    unittest.main()
