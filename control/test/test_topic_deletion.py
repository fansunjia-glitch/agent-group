"""Topic deletion remains scoped to managed workers and survives stale state/restarts."""

from contextlib import contextmanager
from dataclasses import asdict
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from threading import Lock, Thread
from unittest.mock import Mock

from comm import Comm
from control.server import make_server
from control.service import ControlError
from control.test.test_control import ControlCase, profile_data


@contextmanager
def topic_model(topic_id):
    """A loopback-only model attempts joining the topic at every process startup."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if body["messages"][-1]["role"] == "user":
                delta = {"tool_calls": [{"index": 0, "id": "join-business", "type": "function",
                                         "function": {"name": "join_topic", "arguments": json.dumps({"topic_id": topic_id})}}]}
                finish = "tool_calls"
            else:
                delta, finish = {"content": "Ready."}, "stop"
            base = {"id": "offline", "object": "chat.completion.chunk", "created": 0, "model": "offline-test"}
            events = [{**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
            raw = ("".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class TopicDeletionTests(ControlCase):
    def external_topic(self):
        node = Comm(host="127.0.0.1")
        self.addCleanup(node.close)
        return node, node.create_topic("External-owned discussion")

    def test_delete_preserves_history_and_hides_stale_membership_discovery_after_restart(self):
        service = self.service()
        external, topic_id = self.external_topic()
        service.topic_cache = [asdict(t) for t in external.discover_topics()]
        profile = service.save_profile(profile_data())
        identifier = profile["id"]
        service.runtime[identifier].update(run_id="prior-run", joined_topics=[topic_id])
        service.handle_event(identifier, "prior-run", {"type": "message_sent", "topic_id": topic_id,
                                                       "payload": "Historical discussion remains available"})
        prior_event = service.snapshot()["events"][-1]
        result = service.delete_topic(topic_id)
        self.assertEqual(result["deleted"], topic_id)
        self.assertEqual(result["pending_profiles"], [])
        self.assertEqual(result["scope"], "managed_agents")
        self.assertEqual(result["name"], "External-owned discussion")
        self.assertIn(topic_id, {t.topic_id for t in external.discover_topics()})
        # An independent member still publishes this topic; neither discovery
        # nor an old worker event may resurrect it for the managed group.
        service.topic_cache = [asdict(t) for t in external.discover_topics()]
        service.handle_event(identifier, "prior-run", {"type": "topic_joined", "topic_id": topic_id})
        state = service.snapshot()
        self.assertNotIn(topic_id, {t["topic_id"] for t in state["topics"]})
        self.assertNotIn(topic_id, state["profiles"][0]["runtime"]["joined_topics"])
        self.assertIn(prior_event, state["events"])
        self.assertIn(topic_id, state["deleted_topic_ids"])
        self.assertEqual(service.delete_topic(topic_id)["deleted"], topic_id)
        self.assertEqual(len(service.data["deleted_topics"]), 1)
        service.close()
        restored = self.service()
        restored.topic_cache = [asdict(t) for t in external.discover_topics()]
        self.assertNotIn(topic_id, {t["topic_id"] for t in restored.snapshot()["topics"]})
        self.assertIn(prior_event, restored.snapshot()["events"])
        self.assertEqual(restored.snapshot()["deleted_topic_ids"], [topic_id])

    def test_historical_topic_can_be_deleted_but_admin_and_unknown_cannot(self):
        service = self.service()
        profile = service.save_profile(profile_data())
        service.runtime[profile["id"]]["run_id"] = "run"
        service.handle_event(profile["id"], "run", {"type": "topic_joined", "topic_id": "historical-id", "name": "Old discussion"})
        service.runtime[profile["id"]]["joined_topics"] = []
        result = service.delete_topic("historical-id")
        self.assertEqual(result["name"], "Old discussion")
        admin = service.create_admin_topic({"name": "Administrator"})
        before = service.path.read_text()
        with self.assertRaises(ControlError) as protected:
            service.delete_topic(admin["topic_id"])
        self.assertEqual(protected.exception.status, 409)
        self.assertFalse(next(t for t in service.snapshot()["topics"] if t["topic_id"] == admin["topic_id"])["can_delete"])
        with self.assertRaises(ControlError) as unknown:
            service.delete_topic("does-not-exist")
        self.assertEqual(unknown.exception.status, 404)
        self.assertEqual(service.path.read_text(), before)

    def test_pending_requires_ack_and_transient_write_failure_is_retried(self):
        service = self.service()
        _, topic_id = self.external_topic()
        service.topic_cache = [{"topic_id": topic_id, "name": "Retry discussion", "description": ""}]
        profile = service.save_profile(profile_data())
        identifier = profile["id"]
        process = Mock()
        process.poll.return_value = None
        process.stdin.write.side_effect = BrokenPipeError("test disconnect")
        record = {"process": process, "run_id": "run", "write_lock": Lock()}
        service.processes[identifier] = record
        service.runtime[identifier]["run_id"] = "run"
        try:
            result = service.delete_topic(topic_id)
            self.assertEqual(result["pending_profiles"], [identifier])
            self.assertIn(identifier, result["errors"])
            process.stdin = io.StringIO()
            service.last_delete_attempt.clear()
            service._dispatch_topic_deletions()
            command = json.loads(process.stdin.getvalue())
            self.assertEqual(command, {"type": "delete_topic", "topic_id": topic_id})
            self.assertEqual(service.snapshot()["topic_deletions"][0]["pending_profiles"], [identifier])
            service.handle_event(identifier, "run", {"type": "topic_deleted", "topic_id": topic_id})
            self.assertEqual(service.snapshot()["topic_deletions"][0]["pending_profiles"], [])
            service._dispatch_topic_deletions()
            self.assertEqual(len(process.stdin.getvalue().splitlines()), 1)
            # A lost SSH channel is not proof that its remote process exited.
            record.update(host_kind="ssh", worker_exit=False, deleted_topic_ids=set())
            process.poll.return_value = 255
            service.runtime[identifier]["state"] = "failed"
            uncertain = service.snapshot()["topic_deletions"][0]
            self.assertEqual(uncertain["pending_profiles"], [identifier])
            self.assertIn("unconfirmed", uncertain["errors"][identifier])
            record["worker_exit"] = True
            self.assertEqual(service.snapshot()["topic_deletions"][0]["pending_profiles"], [])
        finally:
            service.processes.pop(identifier)

    def test_http_delete_requires_token_and_rejects_cross_origin(self):
        service = self.service()
        external, topic_id = self.external_topic()
        service.topic_cache = [asdict(t) for t in external.discover_topics()]
        server = make_server(service, port=0)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        try:
            for headers, expected in [({}, 403), ({"X-Control-Token": service.csrf_token, "Origin": "http://evil.example"}, 403),
                                      ({"X-Control-Token": service.csrf_token}, 200)]:
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    connection.request("DELETE", f"/api/topics/{topic_id}", body="{}",
                                       headers={"Content-Type": "application/json", **headers})
                    response = connection.getresponse()
                    result = json.loads(response.read())
                    self.assertEqual(response.status, expected, result)
                finally:
                    connection.close()
                if expected == 403:
                    self.assertEqual(service.snapshot()["deleted_topic_ids"], [])
            self.assertEqual(result["deleted"], topic_id)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_real_workers_leave_and_restarted_worker_cannot_rejoin(self):
        external, topic_id = self.external_topic()
        with topic_model(topic_id) as (base_url, requests):
            service = self.service(background=True)
            service.set_credentials({"api_key": "offline-key"})
            profiles = [service.save_profile(profile_data(name, base_url=base_url)) for name in ("first", "second")]
            for profile in profiles:
                service.start_profile(profile["id"])
            self.wait_for(lambda: all(topic_id in p["runtime"]["joined_topics"] and p["runtime"]["state"] == "idle"
                                     for p in service.snapshot()["profiles"]), service=service, label="two live subscriptions")
            result = service.delete_topic(topic_id)
            self.assertEqual(set(result["pending_profiles"]), {p["id"] for p in profiles})
            self.wait_for(lambda: not service.snapshot()["topic_deletions"][0]["pending_profiles"],
                          service=service, label="actual worker deletion acknowledgements")
            self.assertTrue(all(topic_id not in peer["topics"] for peer in external._refresh()
                                if peer["agent_id"] != external.agent_id))
            self.assertEqual(sum(e["type"] == "topic_deleted" for e in service.snapshot()["events"]), 2)
            first = profiles[0]["id"]
            service.stop_profile(first)
            self.wait_for(lambda: service.processes[first]["process"].poll() is not None
                          and not service.processes[first]["reader"].is_alive(), service=service, label="worker stopped")
            restarted = service.start_profile(first)
            self.wait_for(lambda: any(e["run_id"] == restarted["run_id"] and e["type"] == "tool_finished"
                                     and e.get("tool_name") == "join_topic" and e.get("status") == "error"
                                     for e in service.snapshot()["events"]), service=service, label="stale join rejected after restart")
            self.assertNotIn(topic_id, next(p["runtime"]["joined_topics"] for p in service.snapshot()["profiles"] if p["id"] == first))
            self.assertIn(topic_id, {t.topic_id for t in external.discover_topics()})
            self.assertTrue(requests)
            # Shut down before the fixture closes; the real API is never used.
            service.close()
