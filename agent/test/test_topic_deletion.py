"""Trusted topic retirement cannot be bypassed by stale discoveries or model tools."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent import Agent
from agent.test.test_agent import FakeClient, done
from comm import Comm


class TopicDeletionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="agent-topic-delete-")
        self.addCleanup(self.directory.cleanup)
        self.external = Comm(host="127.0.0.1")
        self.addCleanup(self.external.close)
        self.topic = self.external.create_topic("Independent member keeps this topic alive")
        self.events = []

    def node(self, **kwargs):
        with patch("agent.agent.OpenAI", return_value=FakeClient([done()])):
            node = Agent(api_key="offline", host="127.0.0.1", workdir=Path(self.directory.name),
                         enable_memory=False, event_sink=self.events.append, **kwargs)
        self.addCleanup(node.close)
        return node

    def test_queued_delete_discards_unread_and_blocks_discovery_rejoin_and_send(self):
        node = self.node()
        node.topic_manager.join_topic(self.topic)
        self.external.send(self.topic, "Unread before deletion")
        node.delete_topic(self.topic)
        self.assertIn(self.topic, node.topic_manager.joined_topics)  # Queue, not a cross-thread mutation.
        self.assertFalse(node._collect_events())
        self.assertNotIn(self.topic, node.topic_manager.joined_topics)
        self.assertNotIn(self.topic, {t["topic_id"] for t in node.topic_manager.discover_topics()})
        self.assertIn(self.topic, {t.topic_id for t in self.external.discover_topics()})
        with self.assertRaises(PermissionError):
            node.topic_manager.join_topic(self.topic)
        with self.assertRaises(PermissionError):
            node.topic_manager.send(self.topic, "Cannot send to a retired ID")
        self.assertNotIn("delete_topic", node.handlers)
        self.assertTrue(any(e["type"] == "topic_deleted" for e in self.events))
        self.assertFalse(any(e["type"] == "message_received" for e in self.events))
        node.delete_topic(self.topic)
        self.assertFalse(node._collect_events())
        self.assertEqual(sum(e["type"] == "topic_deleted" for e in self.events), 2)  # Retry ACK.

    def test_delete_between_tools_is_applied_before_a_stale_send(self):
        node = self.node()
        node.topic_manager.join_topic(self.topic)
        node.delete_topic(self.topic)
        result = node._execute_tool({"id": "stale-send", "function": {"name": "send", "arguments":
            json.dumps({"topic_id": self.topic, "payload": "Must never arrive"})}})
        self.assertIn("deleted by the administrator", json.loads(result["content"])["error"])
        self.assertEqual(self.external.receive(self.topic), [])

    def test_startup_tombstones_apply_before_first_model_tool(self):
        node = self.node(deleted_topic_ids=[self.topic])
        self.assertNotIn(self.topic, {t["topic_id"] for t in node.topic_manager.discover_topics()})
        with self.assertRaises(PermissionError):
            node.topic_manager.join_topic(self.topic)
        self.assertLess(next(i for i, e in enumerate(self.events) if e["type"] == "topic_deleted"),
                        next(i for i, e in enumerate(self.events) if e["type"] == "ready"))

    def test_admin_topic_is_protected_even_from_an_invalid_supervisor_command(self):
        node = self.node()
        node.invite_admin_topic(self.topic, self.external.agent_id, "offline-admin-key")
        node._collect_events()
        node.delete_topic(self.topic)
        self.assertFalse(node._collect_events())
        self.assertIn(self.topic, node.topic_manager.joined_topics)
        self.assertNotIn(self.topic, node.topic_manager.deleted_topics)
        self.assertEqual(self.events[-1]["type"], "topic_delete_failed")
        self.assertEqual(node.topic_manager.admin_topic_id, self.topic)


if __name__ == "__main__":
    unittest.main()
