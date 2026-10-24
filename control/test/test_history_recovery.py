from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control.history_recovery import recover_truncated_sends


def send(call_id, payload, *, topic="town", result="null", interrupted=False):
    messages = [{"role": "assistant", "content": None, "_interrupted": interrupted,
                 "tool_calls": [{"id": call_id, "type": "function", "function": {
                     "name": "send", "arguments": json.dumps({"topic_id": topic, "payload": payload})}}]}]
    if result is not None:
        messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
    return messages


class HistoryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def history(self, profile_id, messages):
        path = self.root / profile_id / ".logs" / "history.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(messages), encoding="utf-8")
        return path

    def event(self, **changes):
        return {"id": "event-1", "type": "message_sent", "profile_id": "alice",
                "topic_id": "town", "payload": "正文前半", "truncated": True,
                "ts": "2026-10-24T10:00:00+00:00", **changes}

    def test_recovers_full_body_without_mutating_events_history_or_message_identity(self):
        text = "正文前半" + "完整后文\n" * 500
        path = self.history("alice", send("send-1", text))
        before_file = path.read_bytes()
        events = [self.event(), self.event(id="event-2")]
        before = deepcopy(events)
        recovered, summary = recover_truncated_sends(events, self.root)
        self.assertEqual(events, before)
        self.assertEqual(path.read_bytes(), before_file)
        self.assertEqual(len(recovered), 2, "Equal repeated sends remain two messages")
        for index, event in enumerate(recovered):
            self.assertEqual(event["payload"], text)
            self.assertFalse(event["truncated"])
            self.assertEqual(event["id"], events[index]["id"])
            self.assertEqual(event["ts"], events[index]["ts"])
            self.assertEqual(event["recovery_source"], "agent_history")
        self.assertEqual(summary["recovered"], 2)
        self.assertEqual(summary["files_read"], 1)

    def test_failed_unexecuted_interrupted_and_unrelated_calls_cannot_repair(self):
        text = "正文前半不能凭空恢复"
        messages = (send("failed", text, result='{"error":"delivery failed"}')
                    + send("missing", text, result=None)
                    + send("interrupted", text, interrupted=True)
                    + send("different-topic", text, topic="elsewhere"))
        self.history("alice", messages)
        self.history("bobe", send("other-profile", text))
        events = [self.event()]
        recovered, summary = recover_truncated_sends(events, self.root)
        self.assertEqual(recovered, events)
        self.assertEqual(summary["no_match"], 1)

    def test_ambiguous_prefix_stays_truncated_unless_existing_call_id_disambiguates(self):
        self.history("alice", send("first", "正文前半不同结尾一") + send("second", "正文前半不同结尾二"))
        events = [self.event(), self.event(id="event-2", call_id="second")]
        recovered, summary = recover_truncated_sends(events, self.root)
        self.assertEqual(recovered[0], events[0])
        self.assertEqual(recovered[1]["payload"], "正文前半不同结尾二")
        self.assertEqual(summary["ambiguous"], 1)
        self.assertEqual(summary["recovered"], 1)

    def test_bad_or_missing_snapshots_and_unsafe_paths_do_not_abort_other_recovery(self):
        self.history("alice", send("ok", "正文前半完整"))
        self.history("broken", []).write_text("not JSON")
        events = [self.event(profile_id=p, id=p) for p in ("missing", "broken", "../outside", "alice")]
        recovered, summary = recover_truncated_sends(events, self.root)
        self.assertTrue(all(event["truncated"] for event in recovered[:3]))
        self.assertFalse(recovered[3]["truncated"])
        self.assertEqual(summary["unavailable"], 3)

    def test_duplicate_or_nonadjacent_tool_results_are_not_success_evidence(self):
        messages = send("duplicate", "正文前半重复结果")
        messages.append({"role": "tool", "tool_call_id": "duplicate", "content": "null"})
        messages += send("late", "正文前半晚到结果", result=None)
        messages += [{"role": "user", "content": "next turn"},
                     {"role": "tool", "tool_call_id": "late", "content": "null"}]
        self.history("alice", messages)
        original = self.event()
        recovered, summary = recover_truncated_sends([original], self.root)
        self.assertEqual(recovered, [original])
        self.assertEqual(summary["recovered"], 0)


if __name__ == "__main__":
    unittest.main()
