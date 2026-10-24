"""Offline control-plane checks, including real supervised worker processes.

Model requests go only to a loopback SSE fixture. SSH subprocess calls are
mocked; no SSH credentials, external model API, or external network is needed.
"""

from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock, Thread
import time
import unittest
from unittest.mock import Mock, patch

from agent.admin import sign_notice, verify_notice
from control.server import make_server
from control.service import ControlError, ControlService, ssh_command


def profile_data(name="worker", **extra):
    return {"name": name, "persona": "Carefully follow administrator notices.",
            "initial_prompt": "Say ready, then wait.", "model": "offline-test",
            "base_url": "http://127.0.0.1:1", "enable_memory": False, **extra}


def host_data(**extra):
    return {"name": "test host", "ssh_target": "worker@lab", "port": 2222,
            "repo_path": "/srv/agent-group", "python_path": "/srv/agent-group/.venv/bin/python",
            "workspace_root": "/srv/workspaces", "comm_host": "192.0.2.20", **extra}


@contextmanager
def fake_model(*, error_message=None, content_parts=("Ready.",)):
    requests = []
    request_lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with request_lock:
                requests.append({"path": self.path, "body": payload})
            if self.path != "/chat/completions":
                self.send_error(404)
                return
            if error_message is not None:
                raw = json.dumps({"error": {"message": error_message, "type": "authentication_error"}}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            base = {"id": "chatcmpl-offline", "object": "chat.completion.chunk",
                    "created": 0, "model": "offline-test"}
            events = [{**base, "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}]}
                      for part in content_parts]
            events.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            raw = ("".join("data: " + json.dumps(item) + "\n\n" for item in events)
                   + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        if thread.is_alive():
            raise AssertionError("Fake model server did not stop")


class ControlCase(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="agent-control-test-")
        self.addCleanup(self.directory.cleanup)
        self.services = []
        self.addCleanup(self.close_services)
        # Child processes inherit this; SDK traffic stays on loopback even on
        # developer machines with an HTTP proxy configured.
        environment = patch.dict(os.environ, {"HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
                                               "http_proxy": "", "https_proxy": "", "all_proxy": "",
                                               "NO_PROXY": "127.0.0.1,localhost"})
        environment.start()
        self.addCleanup(environment.stop)

    def service(self, *, background=False):
        service = ControlService(self.directory.name, comm_host="127.0.0.1", background=background)
        self.services.append(service)
        return service

    def close_services(self):
        for service in reversed(self.services):
            service.close()
            for record in service.processes.values():
                process = record["process"]
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                if reader := record.get("reader"):
                    reader.join(timeout=3)
                    self.assertFalse(reader.is_alive(), "Worker event reader leaked")

    def wait_for(self, predicate, *, service=None, timeout=15, label="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.05)
        details = json.dumps(service.snapshot(), ensure_ascii=False) if service else ""
        self.fail(f"Timed out waiting for {label}: {details}")


class PersistenceAndSSHTests(ControlCase):
    def test_delete_profile_preserves_workspace_events_and_frozen_notice_receipts(self):
        service = self.service()
        removed = service.save_profile(profile_data("remove-me"))
        remaining = service.save_profile(profile_data("keep-me"))
        identifier = removed["id"]
        workspace = Path(self.directory.name) / "workspaces" / identifier
        history_path = workspace / ".logs" / "history.json"
        history_path.parent.mkdir(parents=True)
        history_text = json.dumps([{"role": "user", "content": "Keep this prior work."}])
        history_path.write_text(history_text)
        artifact = workspace / "result.txt"
        artifact.write_text("Saved agent artifact.")
        service.create_admin_topic({"name": "Operators"})
        notice = service.create_notice({"content": "Keep the original delivery denominator."})
        service.runtime[identifier]["run_id"] = "prior-run"
        service.handle_event(identifier, "prior-run", {"type": "model_finished", "status": "completed",
                                                      "output": "Prior work finished."})
        prior_events = service.snapshot()["events"]
        self.assertEqual(service.delete_profile(identifier), {"deleted": identifier})
        state = service.snapshot()
        self.assertEqual([p["id"] for p in state["profiles"]], [remaining["id"]])
        self.assertNotIn(identifier, service.runtime)
        self.assertEqual(state["notices"][0], notice)
        self.assertEqual(state["notices"][0]["receipts"][identifier]["status"], "pending")
        self.assertTrue(all(event in state["events"] for event in prior_events))
        self.assertTrue(any(event["type"] == "profile_deleted" and event["profile_id"] == identifier
                            for event in state["events"]))
        self.assertEqual(history_path.read_text(), history_text)
        self.assertEqual(artifact.read_text(), "Saved agent artifact.")
        saved = json.loads(service.path.read_text())
        self.assertEqual([p["id"] for p in saved["profiles"]], [remaining["id"]])
        self.assertEqual(saved["notices"], [notice])
        with self.assertRaises(ControlError) as missing:
            service.delete_profile(identifier)
        self.assertEqual(missing.exception.status, 404)
        service.close()
        restored = self.service()
        self.assertEqual([p["id"] for p in restored.snapshot()["profiles"]], [remaining["id"]])
        self.assertEqual(restored.snapshot()["notices"][0], notice)
        self.assertTrue(all(event in restored.snapshot()["events"] for event in prior_events))
        self.assertEqual(history_path.read_text(), history_text)
        self.assertEqual(artifact.read_text(), "Saved agent artifact.")

    def test_delete_profile_refuses_any_live_process_without_mutating_saved_configuration(self):
        service = self.service()
        profile = service.save_profile(profile_data())
        identifier = profile["id"]
        process = Mock()
        process.poll.return_value = None
        service.processes[identifier] = {"process": process}
        saved = service.path.read_text()
        try:
            # A stale stopped UI label must not bypass the process check.
            for state in ("working", "stopping", "stopped"):
                with self.subTest(state=state):
                    service.runtime[identifier]["state"] = state
                    with self.assertRaises(ControlError) as conflict:
                        service.delete_profile(identifier)
                    self.assertEqual(conflict.exception.status, 409)
                    self.assertIn(identifier, service.runtime)
                    self.assertIn(identifier, service.processes)
                    self.assertEqual(service.path.read_text(), saved)
            process.terminate.assert_not_called()
            process.kill.assert_not_called()
        finally:
            service.processes.pop(identifier, None)

    def test_retry_signed_targets_exclude_applied_profiles_without_changing_snapshot(self):
        service = self.service()
        first = service.save_profile(profile_data("first"))
        second = service.save_profile(profile_data("second"))
        service.create_admin_topic({"name": "Operators"})
        for profile in (first, second):
            service.runtime[profile["id"]].update(run_id=profile["id"], admin_joined=True)
        with patch.object(service.comm, "send") as send:
            notice = service.create_notice({"content": "Apply once per frozen target."})
            original_targets = notice["targets"]
            initial = json.loads(send.call_args.args[1])
            self.assertEqual(set(initial["targets"]), {first["id"], second["id"]})
            self.assertTrue(verify_notice(initial, service.admin_secret))
            service.handle_event(first["id"], first["id"], {"type": "notice_applied", "notice_id": notice["id"]})
            service.handle_event(second["id"], second["id"], {"type": "notice_received", "notice_id": notice["id"]})
            service.save_profile(profile_data("new-after-notice"))
            send.reset_mock()
            retried = service.publish_notice(notice["id"])
            send.assert_called_once()
            retry = json.loads(send.call_args.args[1])
            self.assertEqual(retry["targets"], [second["id"]])
            self.assertEqual(retry["notice_id"], notice["id"])
            self.assertTrue(verify_notice(retry, service.admin_secret))
            self.assertEqual(retried["targets"], original_targets)
            self.assertEqual(service.snapshot()["notices"][0]["targets"], original_targets)
            self.assertEqual(set(retried["receipts"]), {first["id"], second["id"]})
            service.handle_event(second["id"], second["id"], {"type": "notice_applied", "notice_id": notice["id"]})
            send.reset_mock()
            service.publish_notice(notice["id"])
            send.assert_not_called()

    def test_nested_event_secrets_are_redacted_before_json_escaping(self):
        service = self.service()
        key = 'offline-"quoted"\\key\nmultiline'
        service.set_credentials({"api_key": key})
        profile = service.save_profile(profile_data())
        service.runtime[profile["id"]]["run_id"] = "redaction-run"
        service.handle_event(profile["id"], "redaction-run", {
            "type": "error", "error": "credential=" + key,
            "details": {"nested": [key, {"admin": service.admin_secret}]},
        })
        state = service.snapshot()
        self.assertEqual(state["profiles"][0]["runtime"]["error"], "credential=[redacted]")
        self.assertEqual(state["events"][-1]["details"], {"nested": ["[redacted]", {"admin": "[redacted]"}]})
        event = json.loads((Path(self.directory.name) / "events.jsonl").read_text().splitlines()[-1])
        self.assertEqual(event["details"], state["events"][-1]["details"])
        self.assertEqual(event["error"], "credential=[redacted]")

    def test_data_directory_has_one_supervisor_and_is_reusable_after_close(self):
        first = self.service()
        first.save_profile(profile_data())
        original = first.path.read_text()
        with self.assertRaises(ControlError):
            self.service()
        self.assertEqual(first.path.read_text(), original)
        first.close()
        restored = self.service()
        self.assertEqual(restored.snapshot()["profiles"][0]["name"], "worker")

    def test_profiles_hosts_and_notices_survive_restart_without_credentials(self):
        service = self.service()
        key = "offline-credential-do-not-persist"
        self.assertEqual(service.set_credentials({"api_key": key}), {"credentials_configured": True})
        host = service.add_host(host_data())
        first = service.save_profile(profile_data(api_key=key))
        second = service.save_profile(profile_data("remote", host_id=host["id"]))
        service.create_admin_topic({"name": "Operators"})
        notice = service.create_notice({"content": "Remember this notice."})
        later = service.save_profile(profile_data("created-later"))
        self.assertEqual(set(notice["receipts"]), {first["id"], second["id"]})
        self.assertNotIn(later["id"], notice["receipts"])
        self.assertTrue(all(item["status"] == "pending" for item in notice["receipts"].values()))
        snapshot = service.snapshot()
        disk = service.path.read_text()
        self.assertNotIn(key, disk)
        self.assertNotIn("api_key", disk)
        self.assertNotIn(key, json.dumps(snapshot))
        self.assertNotIn(service.admin_secret, json.dumps(snapshot))
        self.assertNotIn(service.admin_secret, disk)
        self.assertEqual(service.path.stat().st_mode & 0o777, 0o600)
        service.close()
        restored = self.service()
        state = restored.snapshot()
        self.assertFalse(state["credentials_configured"])
        self.assertEqual(state["profiles"][0]["id"], first["id"])
        self.assertEqual(state["hosts"][1]["ssh_target"], "worker@lab")
        self.assertEqual(state["notices"][0], notice)
        self.assertEqual(state["admin"]["topic"]["name"], "Operators")
        self.assertTrue(all(p["runtime"]["state"] == "stopped" for p in state["profiles"]))
        with self.assertRaisesRegex(ControlError, "session API key"):
            restored.start_profile(first["id"])

    def test_ssh_remote_paths_are_shell_quoted_and_batchmode_is_required(self):
        service = self.service()
        host = service.add_host(host_data(repo_path="/srv/repo name'; touch /tmp/unwanted; '",
                                          python_path="/srv/python $(touch /tmp/unwanted)"))
        command = ssh_command(host, probe=True)
        self.assertEqual(command[0], "ssh")
        self.assertIn("BatchMode=yes", command)
        self.assertIn("ConnectTimeout=10", command)
        self.assertIn("-T", command)
        self.assertEqual(command[-2], "worker@lab")
        self.assertEqual(shlex.split(command[-1]), ["cd", host["repo_path"], "&&", "exec",
                                                   host["python_path"], "-u", "-m", "control.worker", "--probe"])
        for target in ("-oProxyCommand=sh", "worker@lab;touch /tmp/no", "lab\ncommand", "lab $(id)"):
            with self.subTest(target=target), self.assertRaises(ControlError):
                service.add_host(host_data(ssh_target=target))

    def test_probe_failures_are_reported_and_saved(self):
        service = self.service()
        host = service.add_host(host_data())
        outcomes = [
            subprocess.CompletedProcess([], 255, "", "Permission denied (publickey)."),
            subprocess.CompletedProcess([], 0, '{"protocol": 99}', ""),
            subprocess.CompletedProcess([], 0, "not-json", ""),
            subprocess.CompletedProcess([], 0, "[]", ""),
            subprocess.CompletedProcess([], 0, "null", ""),
            subprocess.TimeoutExpired(["ssh"], 15),
            OSError("ssh executable unavailable"),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=repr(outcome)):
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch("control.service.subprocess.run", **kwargs) as run:
                    result = service.check_host(host["id"])
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])
                self.assertEqual(run.call_args.args[0], ssh_command(host, probe=True))
                self.assertEqual(run.call_args.kwargs["timeout"], 15)
                self.assertEqual(service.snapshot()["hosts"][1]["last_check"], result)

    def test_ssh_launch_sends_credentials_over_stdin_not_command_arguments(self):
        service = self.service()
        key = "stdin-only-key"
        service.set_credentials({"api_key": key})
        host = service.add_host(host_data())
        profile = service.save_profile(profile_data(host_id=host["id"]))

        class Process:
            pid = 987654
            stdin = io.StringIO()
            stdout = io.StringIO()
            returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

        process = Process()
        with patch("control.service.subprocess.Popen", return_value=process) as popen, \
                patch.object(service, "_read_worker"):
            started = service.start_profile(profile["id"])
        self.assertIsNone(started["pid"])
        self.assertEqual(started["control_pid"], process.pid)
        service.handle_event(profile["id"], started["run_id"], {
            "type": "ready", "pid": 12345, "agent_id": "remote-worker", "endpoint": "http://192.0.2.20:9000",
        })
        state = service.snapshot()["profiles"][0]["runtime"]
        self.assertEqual(state["pid"], 12345)
        self.assertEqual(state["control_pid"], process.pid)
        record = service.processes[profile["id"]]
        record["reader"].join(timeout=2)
        command = json.loads(process.stdin.getvalue().splitlines()[0])
        self.assertEqual(command["type"], "start")
        self.assertEqual(command["config"]["api_key"], key)
        self.assertEqual(command["config"]["profile_id"], profile["id"])
        self.assertEqual(command["config"]["workdir"], "/srv/workspaces/" + profile["id"])
        self.assertEqual(command["config"]["host"], host["comm_host"])
        self.assertEqual(popen.call_args.args[0], ssh_command(host))
        self.assertNotIn(key, repr(popen.call_args))
        self.assertNotIn(key, service.path.read_text())
        process.returncode = 0
        record["log"].close()
        process.stdin.close()
        process.stdout.close()

    def test_local_worker_probe_checks_protocol_without_model_configuration(self):
        service = self.service()
        result = service.check_host("local")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["python"])
        self.assertEqual(service.processes, {})


class HTTPProtectionTests(ControlCase):
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
        self.assertFalse(self.thread.is_alive())

    def request(self, method="GET", path="/api/state", data=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        base = {"Content-Type": "application/json"}
        base.update(headers or {})
        try:
            connection.request(method, path, body=json.dumps(data or {}) if method != "GET" else None,
                               headers=base)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_state_is_available_only_with_trusted_host_and_origin(self):
        self.assertEqual(self.http.server_address[0], "127.0.0.1")
        status, state = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(state["csrf_token"], self.control.csrf_token)
        for headers in ({"Host": "evil.example"}, {"Host": "127.0.0.1"},
                        {"Origin": "http://evil.example"}, {"Origin": "null"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)

    def test_delete_requires_control_token_and_trusted_origin_and_reports_conflict_or_missing(self):
        profile = self.control.save_profile(profile_data())
        identifier = profile["id"]
        path = "/api/profiles/" + identifier
        token = self.control.csrf_token
        headers = {"X-Control-Token": token,
                   "Origin": f"http://127.0.0.1:{self.http.server_port}"}
        for rejected in ({}, {"X-Control-Token": "wrong"},
                         {"X-Control-Token": token, "Origin": "http://evil.example"},
                         {"X-Control-Token": token, "Host": "evil.example"}):
            with self.subTest(headers=rejected):
                self.assertEqual(self.request("DELETE", path, {}, rejected)[0], 403)
                self.assertEqual(self.control.snapshot()["profiles"][0]["id"], identifier)
        process = Mock()
        process.poll.return_value = None
        self.control.processes[identifier] = {"process": process}
        try:
            self.assertEqual(self.request("DELETE", path, {}, headers)[0], 409)
            self.assertEqual(self.control.snapshot()["profiles"][0]["id"], identifier)
        finally:
            self.control.processes.pop(identifier, None)
        status, result = self.request("DELETE", path, {}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(result, {"deleted": identifier})
        self.assertEqual(self.request()[1]["profiles"], [])
        self.assertEqual(self.request("DELETE", path, {}, headers)[0], 404)

    def test_mutations_require_token_and_same_origin_without_side_effects(self):
        token = self.control.csrf_token
        origin = f"http://127.0.0.1:{self.http.server_port}"
        attempts = [{}, {"X-Control-Token": "wrong"},
                    {"X-Control-Token": token, "Origin": "http://evil.example"},
                    {"X-Control-Token": token, "Host": "evil.example"}]
        for headers in attempts:
            with self.subTest(headers=headers):
                status, _ = self.request("POST", "/api/credentials", {"api_key": "do-not-accept"}, headers)
                self.assertEqual(status, 403)
                self.assertFalse(self.control.snapshot()["credentials_configured"])
        status, response = self.request("POST", "/api/credentials", {"api_key": "private-key"},
                                        {"X-Control-Token": token, "Origin": origin})
        self.assertEqual(status, 200)
        self.assertEqual(response, {"credentials_configured": True})
        self.assertNotIn("private-key", json.dumps(self.request()[1]))
        self.assertNotIn("private-key", self.control.path.read_text())

    def test_valid_profile_request_persists_only_allowed_configuration(self):
        status, profile = self.request("POST", "/api/profiles", profile_data(api_key="not-a-profile-field"),
                                       {"X-Control-Token": self.control.csrf_token})
        self.assertEqual(status, 200)
        self.assertNotIn("api_key", profile)
        self.assertNotIn("not-a-profile-field", self.control.path.read_text())
        self.assertEqual(self.request()[1]["profiles"][0]["id"], profile["id"])


class ManagedProcessTests(ControlCase):
    def test_model_chunks_containing_key_do_not_leak_to_control_logs_or_events(self):
        key = "offline-model-chunk-secret"
        parts = ("Echoed credential: ", key[:11], key[11:], ".")
        with fake_model(content_parts=parts) as (base_url, requests):
            service = self.service()
            service.set_credentials({"api_key": key})
            profile = service.save_profile(profile_data(base_url=base_url))
            started = service.start_profile(profile["id"])
            record = service.processes[profile["id"]]
            self.wait_for(lambda: any(e["type"] == "model_finished" and e.get("status") == "completed"
                                       for e in service.snapshot()["events"]), service=service,
                          label="streamed credential-echo fixture")
            service.stop_profile(profile["id"])
            self.wait_for(lambda: record["process"].poll() is not None and not record["reader"].is_alive(),
                          service=service, label="credential-echo worker cleanup")
            state = service.snapshot()
            completed = next(e for e in state["events"] if e["type"] == "model_finished")
            self.assertEqual(completed["output"], "Echoed credential: [redacted].")
            self.assertNotIn(key, json.dumps(state))
            self.assertNotIn(key, json.dumps(requests))
            for path in [service.path, Path(self.directory.name) / "events.jsonl",
                         Path(self.directory.name) / "logs" / f"{profile['id']}-{started['run_id']}.log"]:
                self.assertNotIn(key, path.read_text(errors="replace"), str(path))
            # The durable conversation deliberately retains model-authored
            # output. Control-log redaction is not a history sanitizer.
            history_path = Path(self.directory.name) / "workspaces" / profile["id"] / ".logs" / "history.json"
            history = json.loads(history_path.read_text())
            self.assertTrue(any(m.get("role") == "assistant" and m.get("content") == "".join(parts)
                                for m in history))

    def test_model_error_containing_key_does_not_leak_to_saved_logs(self):
        key = "offline-key-echoed-in-model-error"
        with fake_model(error_message=f"Rejected credential: {key}") as (base_url, _):
            service = self.service()
            service.set_credentials({"api_key": key})
            profile = service.save_profile(profile_data(base_url=base_url))
            service.start_profile(profile["id"])
            record = service.processes[profile["id"]]
            self.wait_for(lambda: record["process"].poll() is not None and not record["reader"].is_alive(),
                          service=service, label="model rejection and worker exit")
            state = service.snapshot()
            self.assertEqual(state["profiles"][0]["runtime"]["state"], "failed")
            self.assertNotIn(key, json.dumps(state))
            for path in Path(self.directory.name).rglob("*"):
                if path.is_file():
                    self.assertNotIn(key, path.read_text(errors="replace"), str(path))

    def test_real_worker_admin_broadcast_restart_backfill_and_deduplication(self):
        with fake_model() as (base_url, requests):
            service = self.service(background=True)
            service.set_credentials({"api_key": "offline-real-process-key"})
            profile = service.save_profile(profile_data(base_url=base_url))
            peer = service.save_profile(profile_data("peer", base_url=base_url))
            identifier = profile["id"]

            def runtime():
                return next(p["runtime"] for p in service.snapshot()["profiles"] if p["id"] == identifier)

            def receipt(notice_id):
                notice = next(n for n in service.snapshot()["notices"] if n["id"] == notice_id)
                return notice["receipts"][identifier]

            def stopped():
                record = service.processes[identifier]
                return record["process"].poll() is not None and not record["reader"].is_alive()

            first_run = service.start_profile(identifier)
            peer_run = service.start_profile(peer["id"])
            self.assertNotEqual(first_run["pid"], os.getpid())
            self.assertNotEqual(first_run["pid"], peer_run["pid"])
            self.wait_for(lambda: runtime()["agent_id"] and requests and runtime()["state"] == "idle",
                          service=service, label="real worker startup and first model response")
            ready = [e for e in service.snapshot()["events"]
                     if e["type"] == "ready" and e["run_id"] == first_run["run_id"]]
            self.assertEqual(len(ready), 1)
            self.assertEqual(ready[0]["pid"], service.processes[identifier]["process"].pid)
            self.assertEqual(runtime()["pid"], ready[0]["pid"])
            self.assertEqual(runtime()["control_pid"], ready[0]["pid"])
            first_agent_id = runtime()["agent_id"]
            with self.assertRaisesRegex(ControlError, "already running"):
                service.start_profile(identifier)
            with self.assertRaisesRegex(ControlError, "Stop the agent"):
                service.save_profile(profile_data(base_url=base_url), identifier)
            topic = service.create_admin_topic({"name": "Administrator"})
            self.wait_for(lambda: runtime()["admin_joined"], service=service, label="automatic topic invitation")
            self.wait_for(lambda: next(p["runtime"]["admin_joined"] for p in service.snapshot()["profiles"]
                                       if p["id"] == peer["id"]), service=service,
                          label="automatic invitation for second worker")
            self.assertIn(topic["topic_id"], runtime()["joined_topics"])
            notice = service.create_notice({"content": "First uniquely identifiable notice."})
            self.wait_for(lambda: all(r["status"] == "applied" for n in service.snapshot()["notices"]
                                       if n["id"] == notice["id"] for r in n["receipts"].values()), service=service,
                          label="received/applied receipt")
            self.assertEqual(set(notice["receipts"]), {identifier, peer["id"]})
            self.assertIn("received_at", receipt(notice["id"]))
            self.assertIn("applied_at", receipt(notice["id"]))
            events = service.snapshot()["events"]
            kinds = [e["type"] for e in events if e.get("notice_id") == notice["id"]]
            self.assertIn("notice_received", kinds)
            self.assertIn("notice_applied", kinds)
            self.assertLess(kinds.index("notice_received"), kinds.index("notice_applied"))
            service.stop_profile(identifier)
            self.wait_for(stopped, service=service, label="graceful worker stop")
            self.assertEqual(runtime()["state"], "stopped")
            pending = service.create_notice({"content": "Created while the agent was stopped."})
            self.assertEqual(receipt(pending["id"])["status"], "pending")
            self.wait_for(lambda: next(n["receipts"][peer["id"]]["status"] == "applied"
                                       for n in service.snapshot()["notices"] if n["id"] == pending["id"]),
                          service=service, label="notice delivery to remaining online peer")
            later = service.save_profile(profile_data("later-profile", base_url=base_url))
            self.assertNotIn(later["id"], pending["receipts"])

            second_run = service.start_profile(identifier)
            self.assertNotEqual(second_run["run_id"], first_run["run_id"])
            self.wait_for(lambda: runtime()["admin_joined"], service=service, label="restarted worker invitation")
            self.assertNotEqual(runtime()["agent_id"], first_agent_id)
            self.wait_for(lambda: receipt(pending["id"])["status"] == "applied", service=service,
                          label="automatic backfill of offline notice")

            # Re-deliver a previously applied notice as if its ACK had been
            # lost. This crosses process restart and exercises durable dedup.
            envelope = {"kind": "admin.notice", "notice_id": notice["id"], "content": notice["content"],
                        "targets": [identifier]}
            envelope["signature"] = sign_notice(envelope, service.admin_secret)
            service.comm.send(service.topic["topic_id"], json.dumps(envelope))
            self.wait_for(lambda: any(e.get("notice_id") == notice["id"] and e.get("duplicate")
                                       for e in service.snapshot()["events"]), service=service,
                          label="duplicate receipt after restart")
            service.stop_profile(identifier)
            self.wait_for(stopped, service=service, label="final worker stop")
            service.stop_profile(peer["id"])
            self.wait_for(lambda: service.processes[peer["id"]]["process"].poll() is not None
                          and not service.processes[peer["id"]]["reader"].is_alive(), service=service,
                          label="second worker cleanup")
            history_path = Path(self.directory.name) / "workspaces" / identifier / ".logs" / "history.json"
            history = json.loads(history_path.read_text())
            for sent in (notice, pending):
                marker = f"[Administrator notice={sent['id']}]\n"
                self.assertEqual(sum(m.get("role") == "user" and m.get("content", "").startswith(marker)
                                     for m in history), 1)
            self.assertTrue(all(item["path"] == "/chat/completions" for item in requests))
            self.assertNotIn(service.api_key, json.dumps(requests))
            self.assertNotIn(service.admin_secret, json.dumps(requests))
            for path in Path(self.directory.name).rglob("*"):
                if path.is_file():
                    content = path.read_text(errors="replace")
                    self.assertNotIn(service.api_key, content, str(path))
                    self.assertNotIn(service.admin_secret, content, str(path))


if __name__ == "__main__":
    unittest.main()
