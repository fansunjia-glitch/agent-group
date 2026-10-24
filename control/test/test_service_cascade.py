"""Offline cascades are durable, scoped, and honest about incomplete cleanup."""

import fcntl
import json
from pathlib import Path
from unittest.mock import Mock, patch

from control.history import event_references_topic
from control.service import ControlError
from control.test.test_control import ControlCase, host_data, profile_data


class ServiceCascadeTests(ControlCase):
    def event(self, service, profile_id, topic_id, text="message"):
        with service.lock:
            service._record_event(profile_id, "run", {"type": "message_sent", "topic_id": topic_id, "payload": text})

    def workspace(self, profile_id):
        path = Path(self.directory.name) / "workspaces" / profile_id
        for relative in [".logs/history.json", ".memory/long-term.md", "tasks.json", "user_id", "result.txt"]:
            file = path / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("managed agent data")
        return path

    def test_offline_profile_after_restart_cascades_all_rotations_and_preserves_shared_data(self):
        service = self.service()
        alice = service.save_profile(profile_data("alice"))["id"]
        bobe = service.save_profile(profile_data("bobe"))["id"]
        alice_workspace, bobe_workspace = self.workspace(alice), self.workspace(bobe)
        self.event(service, alice, "shared", "remove Alice's message")
        self.event(service, bobe, "shared", "keep Bobe's message")
        rotation = service.root / "events.123.old.jsonl"
        rotation.write_text((service.root / "events.jsonl").read_text() + "broken unrelated line\n")
        logs = service.root / "logs"
        logs.mkdir()
        (logs / f"{alice}-old.log").write_text("private run output")
        (logs / f"{bobe}-old.log").write_text("other output")
        service.create_admin_topic({})
        service.create_notice({"content": "hello residents"})
        service.close()

        restored = self.service()
        self.assertFalse(restored.processes)
        result = restored.delete_profile(alice)
        self.assertFalse(result["cleanup_pending"])
        self.assertFalse(alice_workspace.exists())
        self.assertTrue((bobe_workspace / "result.txt").exists())
        self.assertFalse((logs / f"{alice}-old.log").exists())
        self.assertEqual((logs / f"{bobe}-old.log").read_text(), "other output")
        messages = restored.topic_messages("shared")["messages"]
        self.assertEqual([message["profile_id"] for message in messages], [bobe])
        notice = restored.snapshot()["notices"][0]
        self.assertEqual([target["profile_id"] for target in notice["targets"]], [bobe])
        self.assertEqual(set(notice["receipts"]), {bobe})
        for path in restored.root.glob("events*.jsonl"):
            self.assertNotIn(alice, path.read_text())
            self.assertIn(bobe, path.read_text())
        self.assertIn("broken unrelated line", rotation.read_text())
        # Both the live sink and compatibility importer reject delayed records.
        restored._record_event(alice, "late", {"type": "state", "state": "idle"})
        restored.history.import_legacy(restored.root, restored.data["notices"])
        self.assertTrue(all(event.get("profile_id") != alice for event in restored.history.recent_events()))

    def test_offline_topic_cascades_old_notice_records_and_structured_tool_events(self):
        service = self.service()
        profile = service.save_profile(profile_data())["id"]
        workspace = self.workspace(profile)
        old_topic = service.create_admin_topic({})["topic_id"]
        old_notice = service.create_notice({"content": "old notice"})["id"]
        self.event(service, profile, old_topic)
        self.event(service, profile, "shared", "keep this unrelated discussion")
        service._record_event(profile, "run", {"type": "tool_finished", "name": "discover_topics",
                                                "result": json.dumps([{"topic_id": old_topic, "name": "old"}])})
        (service.root / "events.legacy.jsonl").write_text((service.root / "events.jsonl").read_text())
        service.close()
        restored = self.service()
        current_notice = restored.create_notice({"content": "new notice"})["id"]
        result = restored.delete_topic(old_topic)
        self.assertFalse(result["cleanup_pending"])
        self.assertEqual(result["pending_profiles"], [])
        self.assertIsNone(restored.history.topic(old_topic))
        self.assertEqual([n["id"] for n in restored.snapshot()["notices"]], [current_notice])
        self.assertNotIn(old_notice, restored.last_attempt)
        self.assertTrue((workspace / ".logs/history.json").exists())
        self.assertEqual(restored.topic_messages("shared")["total"], 1)
        for path in restored.root.glob("events*.jsonl"):
            self.assertNotIn(old_topic, path.read_text())
        restored.runtime[profile]["run_id"] = "late"
        restored.handle_event(profile, "late", {"type": "topic_joined", "topic_id": old_topic})
        restored.handle_event(profile, "late", {"type": "message_received", "topic_id": old_topic, "payload": "late"})
        self.assertFalse(any(event_references_topic(e, old_topic) for e in restored.snapshot()["events"]))
        restored.close()
        restarted = self.service()
        self.assertIsNone(restarted.history.topic(old_topic))
        self.assertEqual(restarted.topic_messages("shared")["total"], 1)

    def test_failed_file_cleanup_stays_pending_and_retries_on_restart(self):
        service = self.service()
        profile = service.save_profile(profile_data())["id"]
        workspace = self.workspace(profile)
        self.event(service, profile, "room")
        with patch("control.service.scrub_event_logs", side_effect=OSError("disk temporarily unavailable")):
            result = service.delete_profile(profile)
        self.assertTrue(result["cleanup_pending"])
        self.assertIn("disk temporarily unavailable", result["cleanup_error"])
        self.assertTrue(workspace.exists())
        self.assertTrue(service.snapshot()["profiles"][0]["runtime"]["cleanup_pending"])
        with self.assertRaises(ControlError):
            service.start_profile(profile)
        with self.assertRaises(ControlError):
            service.save_profile(profile_data(), profile)
        self.assertEqual(json.loads(service.path.read_text())["cleanup_jobs"][0]["id"], profile)
        service.close()
        restarted = self.service()
        self.assertEqual(restarted.snapshot()["profiles"], [])
        self.assertEqual(restarted.snapshot()["cleanup_jobs"], [])
        self.assertFalse(workspace.exists())
        self.assertFalse(any(e.get("profile_id") == profile for e in restarted.snapshot()["events"]))

    def test_final_configuration_failure_does_not_claim_complete_or_restart_profile(self):
        service = self.service()
        profile = service.save_profile(profile_data())["id"]
        workspace = self.workspace(profile)
        original_save = service._save
        saves = 0

        def flaky_save():
            nonlocal saves
            saves += 1
            if saves == 2:
                raise OSError("final config write failed")
            original_save()

        with patch.object(service, "_save", side_effect=flaky_save):
            result = service.delete_profile(profile)
        self.assertTrue(result["cleanup_pending"])
        self.assertFalse(workspace.exists())
        self.assertEqual(service.snapshot()["profiles"][0]["id"], profile)
        self.assertTrue(json.loads(service.path.read_text())["cleanup_jobs"])
        service.close()
        restarted = self.service()
        self.assertEqual(restarted.snapshot()["profiles"], [])
        self.assertEqual(restarted.snapshot()["cleanup_jobs"], [])

    def test_pending_topic_cleanup_blocks_notice_retries_and_completes_on_same_delete(self):
        service = self.service()
        profile = service.save_profile(profile_data())["id"]
        topic_id = service.create_admin_topic({})["topic_id"]
        notice_id = service.create_notice({"content": "past notice"})["id"]
        service.close()
        restarted = self.service()
        restarted.runtime[profile]["admin_joined"] = True
        with patch("control.service.scrub_event_logs", side_effect=OSError("rotation is unavailable")):
            result = restarted.delete_topic(topic_id)
        self.assertTrue(result["cleanup_pending"])
        self.assertIn(topic_id, restarted.snapshot()["deleted_topic_ids"])
        with patch.object(restarted.comm, "send") as send:
            restarted.publish_notice(notice_id)
        send.assert_not_called()
        self.assertFalse(restarted.delete_topic(topic_id)["cleanup_pending"])
        self.assertEqual(restarted.snapshot()["cleanup_jobs"], [])
        self.assertEqual(restarted.snapshot()["notices"], [])

    def test_failed_initial_intent_leaves_profile_and_topic_data_untouched(self):
        service = self.service()
        profile = service.save_profile(profile_data())["id"]
        workspace = self.workspace(profile)
        self.event(service, profile, "room")
        previous = service.path.read_text()
        with patch.object(service, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                service.delete_profile(profile)
            with self.assertRaises(OSError):
                service.delete_topic("room")
        self.assertEqual(service.path.read_text(), previous)
        self.assertEqual(service.snapshot()["cleanup_jobs"], [])
        self.assertEqual(service.snapshot()["deleted_topic_ids"], [])
        self.assertTrue(workspace.exists())
        self.assertEqual(service.topic_messages("room")["total"], 1)
        self.assertEqual(service.history.revision(), 0)

    def test_orphan_workspace_lock_prevents_false_offline_deletion(self):
        service = self.service()
        profile = service.save_profile(profile_data())["id"]
        workspace = self.workspace(profile)
        service.close()
        with (workspace / ".control.lock").open("a") as worker_lock:
            fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            restarted = self.service()
            previous = restarted.path.read_text()
            with self.assertRaises(ControlError) as error:
                restarted.delete_profile(profile)
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(restarted.path.read_text(), previous)
            self.assertTrue((workspace / "tasks.json").exists())
        self.assertFalse(restarted.delete_profile(profile)["cleanup_pending"])

    def test_workspace_symlinks_are_refused_and_nested_links_do_not_delete_foreign_files(self):
        service = self.service()
        profile = service.save_profile(profile_data())["id"]
        foreign = service.root / "foreign"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("unrelated")
        base = service.root / "workspaces"
        base.mkdir()
        workspace = base / profile
        workspace.symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(ControlError) as error:
            service.delete_profile(profile)
        self.assertEqual(error.exception.status, 409)
        self.assertFalse((foreign / ".control.lock").exists())
        workspace.unlink()
        workspace.mkdir()
        (workspace / "external").symlink_to(foreign, target_is_directory=True)
        self.assertFalse(service.delete_profile(profile)["cleanup_pending"])
        self.assertEqual((foreign / "keep.txt").read_text(), "unrelated")

    def test_ssh_profile_is_not_reported_deleted_without_verified_remote_cleanup(self):
        service = self.service()
        host = service.add_host(host_data())
        profile = service.save_profile(profile_data(host_id=host["id"]))["id"]
        service.close()
        restarted = self.service()
        with self.assertRaises(ControlError) as error:
            restarted.delete_profile(profile)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(restarted.snapshot()["profiles"][0]["id"], profile)
        self.assertEqual(restarted.snapshot()["cleanup_jobs"], [])

    def test_legacy_retirement_is_not_automatically_upgraded_to_destructive_cleanup(self):
        service = self.service()
        profile = service.save_profile(profile_data())["id"]
        self.event(service, profile, "old")
        service.data["deleted_topics"].append({"topic_id": "old", "name": "Old", "deleted_at": "prior version"})
        service._save()
        service.close()
        restarted = self.service()
        self.assertIsNotNone(restarted.history.topic("old"))
        self.assertEqual(restarted.history.revision(), 0)
        self.assertFalse(restarted.delete_topic("old")["cleanup_pending"])
        self.assertIsNone(restarted.history.topic("old"))

    def test_finished_reader_is_drained_before_log_and_workspace_cleanup(self):
        service = self.service()
        profile = service.save_profile(profile_data())["id"]
        self.workspace(profile)
        process, reader = Mock(), Mock()
        process.poll.return_value = 0
        reader.is_alive.return_value = False

        def drain(**_):
            self.assertFalse(service.lock._is_owned())
            self.event(service, profile, "room", "last buffered message")

        reader.join.side_effect = drain
        service.processes[profile] = {"process": process, "reader": reader, "host_kind": "local"}
        self.assertFalse(service.delete_profile(profile)["cleanup_pending"])
        reader.join.assert_called_once()
        self.assertFalse(any(e.get("profile_id") == profile for e in service.snapshot()["events"]))
