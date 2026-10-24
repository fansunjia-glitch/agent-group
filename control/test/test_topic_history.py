"""Complete bodies and paginated archives must not depend on the live window."""

from http.client import HTTPConnection
import json
from threading import Thread
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import quote

from control.history import TopicHistory
from control.service import ControlError
from control.server import make_server
from .test_control import ControlCase, profile_data


def event(number, payload=None, topic="town", **extra):
    return {"id": f"event-{number:05}", "type": "message_sent", "topic_id": topic,
            "topic_name": "Town discussion", "ts": f"2026-10-24T00:{number // 60:02}:{number % 60:02}.000+00:00",
            "profile_id": "alice", "name": "Alice", "payload": payload or f"Message {number}", **extra}


class TopicHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="topic-archive-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "history.sqlite3"
        self.history = TopicHistory(self.path)
        self.addCleanup(lambda: self.history.close())

    def test_older_than_live_window_body_and_restart_are_lossless(self):
        body = "长消息🙂\n" * 2500 + "可检索的结尾"
        for number in range(525):
            self.history.append_event(event(number, body if number == 1 else None))
        latest = self.history.message_page("town")
        self.assertEqual(latest["total"], 525)
        self.assertEqual(len(latest["messages"]), 50)
        self.assertTrue(latest["has_more"])
        found = []
        page = latest
        while True:
            found[:0] = page["messages"]
            if not page["has_more"]:
                break
            page = self.history.message_page("town", before=page["next_before"])
        self.assertEqual(len(found), 525)
        self.assertEqual(len({m["seq"] for m in found}), 525)
        self.assertEqual(found[1]["content"], body)
        self.assertFalse(found[1]["truncated"])
        self.assertEqual(self.history.message_page("town", query="可检索的结尾")["messages"][0]["content"], body)
        self.history.close()
        self.history = TopicHistory(self.path)
        self.assertEqual(self.history.message_page("town", query="ALICE")["total"], 525)
        self.assertEqual(self.history.message_page("town", before=3)["messages"][1]["content"], body)
        topic = self.history.topics()[0]
        self.assertEqual(topic["message_count"], 525)
        self.assertEqual(topic["members"], [{"profile_id": "alice", "name": "Alice"}])

    def test_cursors_stay_stable_when_new_messages_arrive_and_after_drains_every_page(self):
        for number in range(55):
            self.history.append_event(event(number, "same text"))
        initial = self.history.message_page("town", limit=20)
        for number in range(55, 130):
            self.history.append_event(event(number, "same text"))
        older = self.history.message_page("town", before=initial["next_before"], limit=200)
        self.assertEqual(len(older["messages"]), 35)
        self.assertFalse(older["has_more"])
        newer = []
        cursor = initial["latest_cursor"]
        while True:
            page = self.history.message_page("town", after=cursor, limit=20)
            newer += page["messages"]
            cursor = page["latest_cursor"]
            if not page["has_more"]:
                break
        self.assertEqual(len(newer), 75)
        self.assertEqual(len({m["key"] for m in older["messages"] + initial["messages"] + newer}), 130)
        self.assertEqual(self.history.message_page("town", after=cursor)["latest_cursor"], cursor)

    def test_imports_previous_rotation_notices_once_and_keeps_topic_ids_separate(self):
        root = Path(self.directory.name)
        old, new = event(1), event(3)
        (root / "events.previous.jsonl").write_text(json.dumps(old) + '\n' + '{bad json}\n')
        (root / "events.jsonl").write_text(json.dumps(old) + '\n' + json.dumps(new) + '\n')
        notices = [{"id": "n", "topic_id": "town", "content": "notice text", "created_at": event(2)["ts"]},
                   {"id": "other", "topic_id": "another-admin", "content": "other channel", "created_at": event(2)["ts"]}]
        self.history.import_legacy(root, notices)
        self.history.import_legacy(root, notices)
        page = self.history.message_page("town")
        self.assertEqual([m["key"] for m in page["messages"]], ["sent-event-00001", "notice-n", "sent-event-00003"])
        self.assertEqual(page["total"], 3)
        self.assertEqual(self.history.message_page("another-admin")["total"], 1)
        self.history.append_event({**event(4), "type": "message_received"})
        self.assertEqual(self.history.message_page("town")["total"], 3)
        for path in root.glob("events*.jsonl"):
            path.unlink()
        self.history.close()
        self.history = TopicHistory(self.path)
        self.assertEqual(self.history.message_page("town")["total"], 3)

    def test_recovery_upgrades_body_without_changing_id_or_cursor(self):
        clipped = event(1, "prefix", truncated=True)
        self.history.append_event(clipped)
        before = self.history.message_page("town")["messages"][0]
        full = {**clipped, "payload": "prefix plus the full body", "truncated": False, "recovered": True}
        self.history.append_event(full)
        after = self.history.message_page("town")["messages"][0]
        self.assertEqual((before["key"], before["seq"]), (after["key"], after["seq"]))
        self.assertEqual(after["content"], full["payload"])
        self.assertFalse(after["truncated"])
        self.history.append_event(clipped)
        self.assertEqual(self.history.message_page("town")["messages"][0], after)
        self.assertEqual(self.history.truncated_sent_events(), [])


class ControlHistoryTests(ControlCase):
    def test_state_has_older_topic_but_chat_does_not_use_bounded_event_preview(self):
        service = self.service()
        profile = service.save_profile(profile_data("Alice"))
        body = "完整正文🙂" * 1000 + "全文末尾"
        with service.lock:
            service._record_event(profile["id"], "run", {"type": "message_sent", "topic_id": "old-room", "name": "Old room", "payload": body})
            original = service.snapshot()["events"][-1]
            self.assertTrue(original["preview_truncated"])
            for _ in range(410):
                service._record_event(profile["id"], "run", {"type": "state", "state": "idle"})
        self.assertEqual(len(service.snapshot()["events"]), 400)
        archived = next(t for t in service.snapshot()["topics"] if t["topic_id"] == "old-room")
        self.assertEqual(archived["message_count"], 1)
        self.assertTrue(archived["is_historical"])
        self.assertLessEqual(len(archived["last_message"]["content"]), 200)
        self.assertEqual(service.topic_messages("old-room")["messages"][0]["content"], body)
        service.close()
        restored = self.service()
        self.assertEqual(restored.topic_messages("old-room")["messages"][0]["content"], body)
        restored.delete_topic("old-room")
        with self.assertRaises(ControlError) as error:
            restored.topic_messages("old-room")
        self.assertEqual(error.exception.status, 404)


class HistoryHTTPTests(ControlCase):
    def setUp(self):
        super().setUp()
        self.control = self.service()
        self.http = make_server(self.control, port=0)
        self.thread = Thread(target=self.http.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_http)

    def close_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(2)

    def request(self, path, headers=None):
        connection = HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_history_api_query_validation_full_body_and_origin(self):
        self.control.history.append_event(event(1, "x" * 2500 + "尾部", topic="with space"))
        path = "/api/topics/" + quote("with space") + "/messages"
        status, page = self.request(path=path + "?limit=1&q=" + quote("尾部"))
        self.assertEqual(status, 200)
        self.assertEqual(len(page["messages"][0]["content"]), 2502)
        self.assertFalse(page["messages"][0]["truncated"])
        for query in ["limit=0", "limit=201", "limit=x", "before=-1", "after=1&before=2", "after=99999999999999999999", "q=a&q=b", "wat=1"]:
            with self.subTest(query=query):
                self.assertEqual(self.request(path=path + "?" + query)[0], 400)
        self.assertEqual(self.request(path=path, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request(path="/api/topics/missing/messages")[0], 404)
