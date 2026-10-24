"""Deleting a center record stops retries without retracting chat evidence."""

from http.client import HTTPConnection
import json
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from control.server import make_server
from control.service import ControlError
from control.test.test_control import ControlCase, profile_data


class NoticeDeletionTests(ControlCase):
    def prepared_notice(self):
        service = self.service()
        profile = service.save_profile(profile_data("resident"))
        topic = service.create_admin_topic({"name": "Town notices"})
        notice = service.create_notice({"content": "A delivered message must remain in chat history."})
        return service, profile, topic, notice

    def test_deleting_record_preserves_topic_history_and_workspace_across_restart(self):
        service, profile, topic, notice = self.prepared_notice()
        history_path = Path(self.directory.name) / "workspaces" / profile["id"] / ".logs" / "history.json"
        history_path.parent.mkdir(parents=True)
        original = json.dumps([{"role": "user", "content": notice["content"]}])
        history_path.write_text(original)
        before = service.topic_messages(topic["topic_id"])["messages"]
        self.assertEqual(before[0]["notice_id"], notice["id"])
        self.assertEqual(service.delete_notice(notice["id"]), {"deleted": notice["id"], "history_retained": True})
        self.assertEqual(service.snapshot()["notices"], [])
        self.assertEqual(service.topic_messages(topic["topic_id"])["messages"], before)
        self.assertEqual(history_path.read_text(), original)
        self.assertNotIn(notice["id"], service.last_attempt)
        service.close()
        restored = self.service()
        self.assertEqual(restored.snapshot()["notices"], [])
        self.assertEqual(restored.topic_messages(topic["topic_id"])["messages"], before)
        self.assertEqual(history_path.read_text(), original)

    def test_inflight_publish_finishes_before_delete_and_late_receipts_do_not_restore_record(self):
        service, profile, topic, notice = self.prepared_notice()
        service.runtime[profile["id"]].update(admin_joined=True, run_id="run")
        sending, release, deleted, deleting = Event(), Event(), Event(), Event()
        errors = []

        def send(*_):
            sending.set()
            if not release.wait(3):
                raise TimeoutError("test did not release publish")

        def publish():
            try:
                service.publish_notice(notice["id"])
            except Exception as exc:
                errors.append(exc)

        def delete():
            deleting.set()
            try:
                service.delete_notice(notice["id"])
            except Exception as exc:
                errors.append(exc)
            finally:
                deleted.set()

        with patch.object(service.comm, "send", side_effect=send) as delivery:
            publisher = Thread(target=publish, daemon=True)
            publisher.start()
            self.assertTrue(sending.wait(2))
            deleter = Thread(target=delete, daemon=True)
            deleter.start()
            try:
                self.assertTrue(deleting.wait(2))
                self.assertFalse(deleted.wait(.05), "Deletion must serialize with the in-flight publish")
            finally:
                release.set()
                publisher.join(3)
                deleter.join(3)
            self.assertFalse(publisher.is_alive())
            self.assertFalse(deleter.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(delivery.call_count, 1)
            with self.assertRaises(ControlError) as failure:
                service.publish_notice(notice["id"])
            self.assertEqual(failure.exception.status, 404)
            self.assertEqual(delivery.call_count, 1)
        service.handle_event(profile["id"], "run", {"type": "notice_applied", "notice_id": notice["id"]})
        self.assertEqual(service.snapshot()["notices"], [])
        self.assertEqual(service.topic_messages(topic["topic_id"])["messages"][0]["content"], notice["content"])

    def test_failed_persistence_preserves_record_and_retry_state(self):
        service, _, _, notice = self.prepared_notice()
        before = service.snapshot()["notices"]
        attempt = service.last_attempt[notice["id"]]
        with patch.object(service, "_save", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                service.delete_notice(notice["id"])
        self.assertEqual(service.snapshot()["notices"], before)
        self.assertEqual(service.last_attempt[notice["id"]], attempt)

    def test_delete_http_requires_same_origin_token_and_unknown_notice_is_404(self):
        service, _, topic, notice = self.prepared_notice()
        server = make_server(service, port=0)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()

        def request(identifier, headers):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.request("DELETE", "/api/notices/" + identifier, body="{}",
                                   headers={"Content-Type": "application/json", **headers})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            token = service.csrf_token
            for headers in ({}, {"X-Control-Token": "wrong"},
                            {"X-Control-Token": token, "Origin": "http://evil.example"}):
                self.assertEqual(request(notice["id"], headers)[0], 403)
                self.assertEqual(len(service.snapshot()["notices"]), 1)
            headers = {"X-Control-Token": token, "Origin": f"http://127.0.0.1:{server.server_port}"}
            self.assertEqual(request(notice["id"], headers),
                             (200, {"deleted": notice["id"], "history_retained": True}))
            self.assertEqual(request(notice["id"], headers)[0], 404)
            self.assertEqual(request("missing", headers)[0], 404)
            self.assertEqual(len(service.topic_messages(topic["topic_id"])["messages"]), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
