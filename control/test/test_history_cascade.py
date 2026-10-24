"""Cascade cleanup must survive late events, imports, restart, and partial failures."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from control.history import TopicHistory, event_references_topic


def event(identifier, profile="alice", topic="shared", kind="message_sent"):
    return {"id": identifier, "type": kind, "topic_id": topic,
            "profile_id": profile, "name": profile or "管理员",
            "topic_name": f"Room {topic}" if topic else None,
            "ts": "2026-10-24T10:00:00+00:00", "payload": f"Body {identifier}"}


def notice(identifier="notice", topic="shared"):
    return {"id": identifier, "topic_id": topic, "content": f"Notice {identifier}",
            "created_at": "2026-10-24T10:01:00+00:00"}


class HistoryCascadeTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="history-cascade-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "history.sqlite3"
        self.history = TopicHistory(self.path)
        self.addCleanup(lambda: self.history.close())

    def restart(self):
        self.history.close()
        self.history = TopicHistory(self.path)

    def test_profile_cleanup_removes_owned_messages_traces_and_members_only(self):
        for item in [event("a"), event("b", "bobe"), event("model", topic=None, kind="model_finished"),
                     event("receive", "bobe", kind="message_received"), event("other", "bobe", "other")]:
            self.history.append_event(item)
        self.history.add_notice(notice())
        before = self.history.message_page("shared")
        surviving_seq = next(m["seq"] for m in before["messages"] if m["profile_id"] == "bobe")
        result = self.history.delete_profile("alice")
        self.assertEqual((result["messages_deleted"], result["events_deleted"], result["topics_deleted"]), (1, 2, 0))
        self.assertEqual(result["history_revision"], 1)
        page = self.history.message_page("shared")
        self.assertEqual([m["key"] for m in page["messages"]], ["sent-b", "notice-notice"])
        self.assertEqual(page["messages"][0]["seq"], surviving_seq)
        self.assertEqual(page["history_revision"], 1)
        topic = next(t for t in self.history.topics() if t["topic_id"] == "shared")
        self.assertEqual(topic["members"], [{"profile_id": "bobe", "name": "bobe"}])
        self.assertEqual(topic["message_count"], 2)
        self.assertEqual({e["id"] for e in self.history.recent_events()}, {"b", "receive", "other"})
        self.assertEqual(self.history.delete_profile("alice")["history_revision"], 1)
        self.history.append_event(event("late-alice"))
        self.history.append_event(event("new-bobe", "bobe"))
        self.assertEqual(self.history.message_page("shared")["total"], 3)
        self.assertEqual(self.history.message_page("shared")["messages"][-1]["key"], "sent-new-bobe")
        self.assertGreater(self.history.message_page("shared")["latest_cursor"], before["latest_cursor"])

    def test_topic_cleanup_purges_metadata_notices_and_events_and_blocks_discovery(self):
        for item in [event("a"), event("b", "bobe"), event("joined", "bobe", kind="topic_joined"),
                     event("other", "alice", "other"), event("model", "alice", None, "model_finished")]:
            self.history.append_event(item)
        self.history.add_notice(notice())
        result = self.history.delete_topic("shared")
        self.assertEqual((result["messages_deleted"], result["events_deleted"], result["topics_deleted"]), (3, 3, 1))
        self.assertIsNone(self.history.topic("shared"))
        self.assertEqual(self.history.message_page("shared")["total"], 0)
        self.assertEqual({e["id"] for e in self.history.recent_events()}, {"other", "model"})
        self.history.remember_topics([{"topic_id": "shared", "name": "Stale discovery"}])
        self.history.append_event(event("late", "bobe"))
        self.history.add_notice(notice("late-notice"))
        self.assertIsNone(self.history.topic("shared"))
        self.assertEqual(self.history.message_page("shared")["messages"], [])
        self.assertEqual(self.history.delete_topic("shared")["history_revision"], 1)
        self.assertIsNotNone(self.history.topic("other"))

    def test_restart_and_changed_legacy_files_cannot_reimport_deleted_records(self):
        initial = [event("a"), event("b", "bobe"), event("gone", "bobe", "gone")]
        (self.root / "events.previous.jsonl").write_text("\n".join(json.dumps(v) for v in initial) + "\n")
        self.history.import_legacy(self.root, [notice("gone-notice", "gone")])
        self.history.delete_profile("alice")
        self.history.delete_topic("gone")
        self.restart()
        self.assertEqual(self.history.revision(), 2)
        late = [event("a2"), event("gone2", "bobe", "gone"), event("b2", "bobe")]
        (self.root / "events.jsonl").write_text("\n".join(json.dumps(v) for v in initial + late) + "\n")
        self.history.import_legacy(self.root, [notice("gone-notice", "gone"), notice("fresh-gone", "gone")])
        self.assertEqual([m["key"] for m in self.history.message_page("shared")["messages"]], ["sent-b", "sent-b2"])
        self.assertEqual({e["id"] for e in self.history.recent_events()}, {"b", "b2"})
        self.assertIsNone(self.history.topic("gone"))
        self.assertEqual(self.history.revision(), 2)

    def test_structured_tool_references_are_purged_and_cannot_be_reimported(self):
        matching = [
            {"id": "created", "type": "tool_finished", "tool_name": "create_topic", "result": "shared"},
            {"id": "discovered", "type": "tool_finished", "tool_name": "discover_topics",
             "result": json.dumps([{"topic_id": "shared"}, {"topic_id": "other"}])},
            {"id": "arguments", "type": "tool_started", "tool_name": "send",
             "arguments": json.dumps({"topic_id": "shared", "payload": "hello"})},
            {"id": "args", "type": "tool_started", "tool_name": "join_topic", "args": {"topic_id": "shared"}},
            {"id": "result", "type": "tool_finished", "result": {"topic_id": "shared"}},
        ]
        retained = [
            {**event("chat", topic="other"), "payload": '{"topic_id":"shared"}'},
            {"id": "text", "type": "model_finished", "result": '{"topic_id":"shared"}'},
            {"id": "tool-text", "type": "tool_finished", "result": "I mentioned shared here"},
            {"id": "unrelated", "type": "tool_finished", "tool_name": "create_topic", "result": "shared-other"},
            {"id": "tool-start", "type": "tool_started", "tool_name": "discover_topics", "call_id": "call"},
        ]
        for item in matching:
            self.assertTrue(event_references_topic(item, "shared"), item)
        for item in retained:
            self.assertFalse(event_references_topic(item, "shared"), item)
        for item in matching + retained:
            self.history.append_event(item)
        self.assertEqual(self.history.delete_topic("shared")["events_deleted"], len(matching))
        self.assertEqual({e["id"] for e in self.history.recent_events()}, {e["id"] for e in retained})
        self.assertEqual(self.history.message_page("other")["total"], 1)
        self.restart()
        for item in matching:
            self.history.append_event({**item, "id": "late-" + item["id"]})
        (self.root / "events.jsonl").write_text("\n".join(json.dumps(v) for v in matching) + "\n")
        self.history.import_legacy(self.root)
        self.assertEqual({e["id"] for e in self.history.recent_events()}, {e["id"] for e in retained})

    def test_old_schema_backfills_event_profile_column_before_cleanup(self):
        legacy_path = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as db, db:
            db.execute("CREATE TABLE events(event_id TEXT PRIMARY KEY,ts TEXT NOT NULL,topic_id TEXT,kind TEXT NOT NULL,raw TEXT NOT NULL)")
            for e in [event("legacy-a", topic=None, kind="state"), event("legacy-b", "bobe", None, "state")]:
                db.execute("INSERT INTO events VALUES(?,?,?,?,?)", (e["id"], e["ts"], e["topic_id"], e["type"], json.dumps(e)))
        archive = TopicHistory(legacy_path)
        try:
            self.assertEqual(archive.delete_profile("alice")["events_deleted"], 1)
            self.assertEqual([e["id"] for e in archive.recent_events()], ["legacy-b"])
            archive.append_event(event("after-migration", "bobe"))
            self.assertEqual(archive.message_page("shared")["total"], 1)
        finally:
            archive.close()

    def test_cascade_failure_rolls_back_data_tombstone_and_revision_together(self):
        self.history.append_event(event("a"))
        with self.history.db:
            self.history.db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'blocked cleanup'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.history.delete_profile("alice")
        self.assertEqual(self.history.revision(), 0)
        self.assertEqual(self.history.message_page("shared")["total"], 1)
        self.history.append_event(event("a2"))
        self.assertEqual(self.history.message_page("shared")["total"], 2, "Failed deletion must not leave a tombstone")
        with self.history.db:
            self.history.db.execute("DROP TRIGGER reject_delete")
        self.assertEqual(self.history.delete_profile("alice")["messages_deleted"], 2)
        self.assertEqual(self.history.revision(), 1)

    def test_tombstones_for_empty_entities_are_durable_idempotent_and_validate_ids(self):
        for method in [self.history.delete_profile, self.history.delete_topic]:
            for invalid in [None, "", " ", 7]:
                with self.subTest(method=method.__name__, invalid=invalid), self.assertRaises(ValueError):
                    method(invalid)
        self.assertEqual(self.history.revision(), 0)
        self.assertEqual(self.history.delete_profile("offline")["history_revision"], 1)
        self.assertEqual(self.history.delete_topic("offline-room")["history_revision"], 2)
        self.assertEqual(self.history.delete_topic("offline-room")["history_revision"], 2)
        self.restart()
        self.history.append_event(event("late-profile", "offline", "other"))
        self.history.append_event(event("late-topic", "bobe", "offline-room"))
        self.assertEqual(self.history.recent_events(), [])
        self.assertEqual(self.history.topics(), [])
        self.assertEqual(self.history.revision(), 2)
